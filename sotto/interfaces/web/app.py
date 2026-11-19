"""Локальный сайт и API (только 127.0.0.1) — адаптер к сценариям.

Маршруты не ходят в базу сами: чтение — через `app.queries` (CQRS, сторона чтения), изменения — через
сценарии (`app.interviews`, `app.legend`, `app.voice`…). Страницы — обычный HTML с формами (Jinja2), без сборки
фронтенда. Живые обновления — Server-Sent Events (/api/events), один поток на браузер (SharedWorker).
"""

from __future__ import annotations

import asyncio
import json
import time
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

import markdown as md
from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from ... import config as config_mod
from ... import paths
from ...application.ports import AgentError
from ...domain.errors import DomainError
from ...infrastructure.prompts import CATALOG, KINDS

if TYPE_CHECKING:
    from ...bootstrap import Sotto

HERE = Path(__file__).parent
templates = Jinja2Templates(directory=str(HERE / "templates"))
templates.env.filters["dt"] = lambda ts: datetime.fromtimestamp(ts).strftime("%d.%m.%Y %H:%M") if ts else "—"
templates.env.filters["time"] = lambda ts: datetime.fromtimestamp(ts).strftime("%H:%M:%S") if ts else ""
templates.env.filters["md"] = lambda text: md.markdown(text or "", extensions=["fenced_code", "nl2br"])
templates.env.filters["duration"] = lambda s: f"{int(s // 60)} мин" if s else "—"
templates.env.filters["size"] = lambda b: f"{(b or 0) / 1e6:.0f} МБ"
MONTHS = ["янв", "фев", "мар", "апр", "май", "июн", "июл", "авг", "сен", "окт", "ноя", "дек"]
templates.env.filters["day"] = lambda ts: datetime.fromtimestamp(ts).strftime("%d") if ts else ""
templates.env.filters["mon"] = lambda ts: MONTHS[datetime.fromtimestamp(ts).month - 1] if ts else ""
templates.env.filters["hm"] = lambda ts: datetime.fromtimestamp(ts).strftime("%H:%M") if ts else ""


def relative(ts: float | None) -> str:
    """«через 40 мин», «завтра в 15:00», «20 ноя в 15:00», «5 мин назад», «вчера в 10:00»."""
    if not ts:
        return ""
    now = datetime.now()
    when = datetime.fromtimestamp(ts)
    diff = (when - now).total_seconds()
    if 0 <= diff < 3600:
        return f"через {max(1, round(diff / 60))} мин"
    if 0 <= diff < 6 * 3600:
        return f"через {round(diff / 3600)} ч"
    if -3600 < diff < 0:
        return f"{max(1, round(-diff / 60))} мин назад"
    if -6 * 3600 < diff < 0:
        return f"{round(-diff / 3600)} ч назад"
    days = (when.date() - now.date()).days
    at = when.strftime("%H:%M")
    if days == 0:
        return f"сегодня в {at}"
    if days == 1:
        return f"завтра в {at}"
    if days == -1:
        return f"вчера в {at}"
    return f"{when.day} {MONTHS[when.month - 1]} в {at}"


def interview_time(ts: float | None) -> str:
    """Время собеседования: как relative, но в первые полтора часа после начала — «идёт сейчас»."""
    if ts and -5400 <= ts - datetime.now().timestamp() < 0:
        return "идёт сейчас"
    return relative(ts)


templates.env.filters["soon"] = interview_time
templates.env.filters["rel"] = relative

# Разделы конфига, которые можно менять формой настроек (остальное — только в config.toml).
EDITABLE_SECTIONS = {"openrouter", "providers", "models", "answer", "recording", "calls", "agents"}


def back(request: Request, default: str = "/") -> RedirectResponse:
    return RedirectResponse(request.headers.get("referer") or default, status_code=303)


def sse(request: Request, bus: Any, stopping: Any, first: list[dict[str, Any]] | None = None) -> StreamingResponse:
    """Поток событий шины для браузера (или оверлея)."""
    q = bus.subscribe()

    async def stream():
        try:
            yield "retry: 1000\n\n"
            for event in first or []:
                yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
            while not await request.is_disconnected() and not stopping.is_set():
                try:
                    event = await asyncio.wait_for(q.get(), timeout=15)
                    yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
                except TimeoutError:
                    yield ": ping\n\n"
        finally:
            bus.unsubscribe(q)

    return StreamingResponse(stream(), media_type="text/event-stream")


def build_app(s: Sotto) -> FastAPI:
    app = FastAPI(title="Sotto", docs_url=None, redoc_url=None)
    allowed_hosts = {f"{h}:{s.cfg.web.port}" for h in ("127.0.0.1", "localhost", s.cfg.web.host)}
    q = s.queries

    @app.middleware("http")
    async def local_only(request: Request, call_next):
        """Защита от чужих сайтов в браузере: DNS rebinding (Host) и CSRF (Origin у изменяющих запросов).
        `sotto ctl` и хоткеи шлют запросы без Origin — их пропускаем."""
        if request.headers.get("host") not in allowed_hosts:
            return JSONResponse({"error": "forbidden host"}, status_code=403)
        origin = request.headers.get("origin")
        if request.method not in ("GET", "HEAD", "OPTIONS") and origin is not None:
            if origin.removeprefix("http://") not in allowed_hosts:
                return JSONResponse({"error": "forbidden origin"}, status_code=403)
        return await call_next(request)

    @app.exception_handler(DomainError)
    async def domain_error(request: Request, exc: DomainError):
        s.bus.publish("notice", {"text": str(exc)})
        return back(request)

    app.mount("/static", StaticFiles(directory=str(HERE / "static")), name="static")

    def page(request: Request, name: str, **ctx: Any) -> HTMLResponse:
        return templates.TemplateResponse(request, name, {"status": s.status(), "cfg": s.cfg,
                                                          "path": request.url.path, **ctx})

    # ---------------- API ----------------

    @app.get("/api/status")
    async def api_status() -> dict[str, Any]:
        return s.status()

    @app.post("/api/ctl/{action}")
    async def api_ctl(action: str) -> JSONResponse:
        return JSONResponse(await s.voice.action(action))

    @app.get("/api/events")
    async def api_events(request: Request) -> StreamingResponse:
        return sse(request, s.bus, s.stopping)

    @app.post("/api/reload")
    async def api_reload() -> dict[str, bool]:
        s.reload_config()
        return {"ok": True}

    # ---------------- главная ----------------

    @app.get("/", response_class=HTMLResponse)
    async def index(request: Request):
        credits = await s.llm.credits() if s.cfg.api_key else None
        return page(request, "index.html", calls=q.recent_calls(8), credits=credits, spent=q.month_spend(),
                    agents=s.agents.status(), profile_filled=bool(s.prompts.profile()),
                    brief=q.next_call_brief(), upcoming=q.upcoming(3), now=time.time())

    @app.post("/brief")
    async def set_brief(request: Request, brief: str = Form("")):
        s.calls.set_next_brief(brief)
        return back(request)

    @app.post("/brief/generate")
    async def gen_brief(request: Request, vacancy: str = Form(...)):
        await s.interviews.brief_for_vacancy(vacancy)
        return back(request)

    # ---------------- звонки и поиск ----------------

    @app.get("/calls", response_class=HTMLResponse)
    async def calls_page(request: Request):
        text = request.query_params.get("q", "")
        results = await asyncio.to_thread(s.search.search_calls, text) if text else None
        return page(request, "calls.html", calls=[] if text else q.calls(), results=results, q=text)

    @app.get("/calls/{call_id}", response_class=HTMLResponse)
    async def call_page(request: Request, call_id: int):
        data = q.call_page(call_id)
        if data is None:
            return RedirectResponse("/calls", status_code=303)
        return page(request, "call.html", **data)

    @app.post("/calls/{call_id}/edit")
    async def call_edit(request: Request, call_id: int, title: str = Form(""), summary: str = Form("")):
        await s.calls.edit(call_id, title, summary)
        return back(request)

    @app.post("/calls/{call_id}/process")
    async def call_process(request: Request, call_id: int):
        s.background.spawn(s.postcall.process(call_id))
        return back(request)

    @app.post("/calls/{call_id}/delete")
    async def call_delete(call_id: int):
        s.calls.delete(call_id)
        return RedirectResponse("/calls", status_code=303)

    @app.post("/speakers/{speaker_id}")
    async def speaker_edit(request: Request, speaker_id: int, name: str = Form(""), role: str = Form("")):
        s.calls.rename_speaker(speaker_id, name, role)
        return back(request)

    # ---------------- записи ----------------

    @app.get("/recordings", response_class=HTMLResponse)
    async def recordings_page(request: Request):
        recs, total = q.recordings()
        return page(request, "recordings.html", recordings=recs, total=total, current=s.voice.recorder.current())

    @app.post("/recordings/{rec_id}/open")
    async def recording_open(request: Request, rec_id: int, at: float = Form(0)):
        s.calls.open_recording(rec_id, at)
        return back(request)

    @app.post("/recordings/{rec_id}/delete")
    async def recording_delete(request: Request, rec_id: int):
        s.calls.delete_recording(rec_id)  # ту, что пишется сейчас, удалить нельзя
        return back(request)

    # ---------------- легенда ----------------

    @app.get("/legend", response_class=HTMLResponse)
    async def legend_page(request: Request):
        return page(request, "legend.html", groups=q.legend_groups())

    @app.post("/legend/add")
    async def legend_add(request: Request, claim: str = Form(...), note: str = Form(""), company: str = Form("")):
        s.legend.add(claim, note, company)
        return back(request)

    @app.post("/legend/company/delete")
    async def legend_company_delete(request: Request, company: str = Form(...)):
        s.legend.forget_company(company)
        return back(request)

    @app.post("/legend/{item_id}")
    async def legend_edit(request: Request, item_id: int, claim: str = Form(...), note: str = Form(""),
                          active: str = Form("")):
        s.legend.edit(item_id, claim, note, bool(active))
        return back(request)

    @app.post("/legend/{item_id}/delete")
    async def legend_delete(request: Request, item_id: int):
        s.legend.delete(item_id)
        return back(request)

    # ---------------- профиль и промпты ----------------

    @app.get("/profile", response_class=HTMLResponse)
    async def profile_page(request: Request):
        return page(request, "profile.html", profile=s.prompts.profile())

    @app.post("/profile")
    async def profile_save(request: Request, text: str = Form("")):
        s.prompts.save_profile(text)
        return back(request)

    @app.post("/profile/structure")
    async def profile_structure(request: Request, notes: str = Form(...)):
        prompt = ("Из заметок кандидата ниже составь профиль для ИИ-суфлёра собеседований в Markdown: "
                  "целевая роль и уровень; стек (по уровню владения); опыт по местам работы (компания, годы, "
                  "роль, проекты, масштаб, конкретные достижения с цифрами); образование; языки (уровень "
                  "английского); что важно в работе; ограничения (зарплата, формат, релокация). Ничего не "
                  "выдумывай: чего нет в заметках — оставь пункт с пометкой «не указано».\n\nЗаметки:\n" + notes)
        try:
            text, _ = await s.agents.run(prompt, task="профиль кандидата")
            existing = s.prompts.profile()
            s.prompts.save_profile((existing + "\n\n" if existing else "") + text.strip())
        except AgentError as e:
            s.bus.publish("notice", {"text": str(e)[:200]})
        return back(request)

    @app.get("/prompts", response_class=HTMLResponse)
    async def prompts_page(request: Request):
        return page(request, "prompts.html", groups=s.prompts.catalog(), prompts_dir=str(paths.PROMPTS_DIR))

    # ---------------- анализ резюме ----------------

    @app.get("/resume/analysis", response_class=HTMLResponse)
    async def resume_analysis_page(request: Request):
        return page(request, "resume_analysis.html", resume=s.prompts.profile(), specialization="", result="", error="")

    @app.post("/resume/analysis", response_class=HTMLResponse)
    async def resume_analysis_run(request: Request, resume: str = Form(...), specialization: str = Form(...)):
        prompt = s.prompts.load("resume_analysis").format(resume=resume.strip()[:12000],
                                                          specialization=specialization.strip() or "разработчик")
        result, error = "", ""
        try:
            result, _ = await s.agents.run(prompt, task="анализ резюме")
        except AgentError as e:
            error = str(e)[:300]
        return page(request, "resume_analysis.html", resume=resume, specialization=specialization,
                    result=result.strip(), error=error)

    @app.post("/prompts/{kind}")
    async def prompt_save(request: Request, kind: str, text: str = Form(...)):
        if kind in CATALOG:
            s.prompts.save(kind, text)
        return back(request)

    @app.post("/prompts/{kind}/reset")
    async def prompt_reset(request: Request, kind: str):
        if kind in CATALOG:
            s.prompts.reset(kind)
        return back(request)

    @app.post("/prompts/{kind}/rewrite")
    async def prompt_rewrite(request: Request, kind: str, wishes: str = Form("")):
        if kind not in CATALOG:
            return back(request)
        from ...application.prompting import render
        prompt = render(s.prompts.builtin("prompt_author"), kind=KINDS.get(kind, kind), current=s.prompts.load(kind),
                        profile=s.prompts.profile() or "(не заполнен)", wishes=wishes or "(нет)")
        try:
            text, agent = await s.agents.run(prompt, task=f"промпт {kind}")
            s.prompts.save(kind, text.strip().strip("`"))
            s.bus.publish("notice", {"text": f"Промпт «{kind}» переписан ({agent})"})
        except AgentError as e:
            s.bus.publish("notice", {"text": str(e)[:200]})
        return back(request)

    # ---------------- настройки ----------------

    @app.get("/settings", response_class=HTMLResponse)
    async def settings_page(request: Request):
        return page(request, "settings.html", c=s.cfg, agents=s.agents.status(), platform=s.platform.name,
                    deepseek_set=bool(s.cfg.deepseek_api_key()))

    @app.post("/settings")
    async def settings_save(request: Request):
        form = await request.form()
        for key, value in form.items():
            if key.startswith("_") or key.split(".")[0] not in EDITABLE_SECTIONS or key == "openrouter.base_url":
                continue
            if (key.endswith("api_key") or key.endswith("password")) and not value:
                continue  # пустое поле секрета = «не менять»
            try:
                config_mod.set_value(s.cfg, key, value)
            except (AttributeError, ValueError):
                pass
        for key in str(form.get("_checkboxes", "")).split(","):  # чекбоксы: не пришли — значит выключены
            if key and key not in form:
                config_mod.set_value(s.cfg, key, False)
        config_mod.save(s.cfg)
        s.reload_config()
        return back(request)

    # ---------------- разделы ----------------
    from ...jobs.interfaces.web import register as register_jobs
    from .chat import register as register_chat
    from .interviews import register as register_interviews
    from .trainer import register as register_trainer
    register_trainer(app, s, page)
    register_interviews(app, s, page)
    register_chat(app, s, page)
    register_jobs(app, s, page)

    return app
