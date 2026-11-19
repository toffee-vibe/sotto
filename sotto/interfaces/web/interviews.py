"""Раздел «Собеседования»: план, исходы, разбор отказов."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from typing import TYPE_CHECKING

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from ...application.interviews import parse_when
from ...domain.interviews import STATUSES

if TYPE_CHECKING:
    from ...bootstrap import Sotto



def _to(iid: int | None) -> RedirectResponse:
    return RedirectResponse(f"/interviews/{iid}" if iid else "/interviews", status_code=303)


def register(app: FastAPI, s: Sotto, page: Callable[..., HTMLResponse]) -> None:
    svc = s.interviews

    @app.get("/interviews", response_class=HTMLResponse)
    async def interviews_page(request: Request, invite: str = ""):
        import time
        return page(request, "interviews.html", statuses=STATUSES, draft=svc.draft if invite else {},
                    now=time.time(), **s.queries.interviews_page())

    @app.post("/interviews/parse")
    async def interviews_parse(text: str = Form(...)):
        """Вставили приглашение — модель заполнит форму, ты проверишь и сохранишь."""
        await svc.parse_invite(text)
        return RedirectResponse("/interviews?invite=1#new", status_code=303)

    @app.post("/interviews/new")
    async def interviews_new(company: str = Form(...), role: str = Form(""), stage: str = Form(""),
                             when: str = Form(""), duration_min: int = Form(60), link: str = Form(""),
                             vacancy: str = Form(""), notes: str = Form(""), vacancy_id: str = Form("")):
        iid = svc.schedule(company, role=role, stage=stage, scheduled_at=parse_when(when), duration_min=duration_min,
                           link=link, vacancy=vacancy, notes=notes)
        if vacancy_id.isdigit():
            s.jobs.mark_replied(int(vacancy_id))
        return _to(iid)

    @app.get("/interviews/{iid}", response_class=HTMLResponse)
    async def interview_page(request: Request, iid: int):
        data = s.queries.interview_page(iid)
        if data is None:
            return _to(None)
        ts = data["iv"].scheduled_at
        when = datetime.fromtimestamp(ts).strftime("%Y-%m-%dT%H:%M") if ts else ""  # для поля datetime-local
        return page(request, "interview.html", when=when, statuses=STATUSES, **data)

    @app.post("/interviews/{iid}")
    async def interview_edit(iid: int, company: str = Form(...), role: str = Form(""), stage: str = Form(""),
                             when: str = Form(""), duration_min: int = Form(60), link: str = Form(""),
                             vacancy: str = Form(""), brief: str = Form(""), notes: str = Form("")):
        svc.edit(iid, company=company, role=role, stage=stage, scheduled_at=parse_when(when),
                 duration_min=duration_min, link=link, vacancy=vacancy, brief=brief, notes=notes)
        return _to(iid)

    @app.post("/interviews/{iid}/status")
    async def interview_status(iid: int, status: str = Form(...), outcome: str = Form("")):
        svc.change_status(iid, status, outcome)
        return _to(iid)

    @app.post("/interviews/{iid}/next")
    async def interview_next(iid: int):
        """Позвали дальше: этот этап — «позвали дальше», следующий — черновик у той же компании."""
        return _to(svc.next_stage(iid))

    @app.post("/interviews/{iid}/analyze")
    async def interview_analyze(iid: int):
        s.background.spawn(svc.analyze_rejection(iid))
        return _to(iid)

    @app.post("/interviews/{iid}/brief")
    async def interview_brief(iid: int):
        s.background.spawn(svc.make_brief(iid))
        return _to(iid)

    @app.post("/interviews/{iid}/train")
    async def interview_train(iid: int, lang: str = Form("en")):
        """Текстовая тренировка с интервьюером этой компании."""
        iv = s.queries.interview(iid)
        if iv is None or not s.cfg.api_key:
            s.bus.publish("notice", {"text": "Нужен ключ OpenRouter: интервьюер — языковая модель"})
            return _to(iid)
        vacancy = iv.vacancy or f"{iv.company}: {iv.role or ''}\n{iv.brief or ''}"
        chat = s.chats.start("interviewer", lang, iv.role or "", vacancy)
        return RedirectResponse(f"/chat/{chat.id}", status_code=303)

    @app.post("/interviews/{iid}/delete")
    async def interview_delete(iid: int):
        svc.delete(iid)
        return _to(None)

    @app.post("/interviews-summary")
    async def interviews_summary():
        s.background.spawn(svc.analyze_all())
        return RedirectResponse("/interviews#summary", status_code=303)

    @app.post("/calls/{call_id}/interview")
    async def call_link(call_id: int, interview_id: str = Form(""), company: str = Form("")):
        """Привязать звонок к собеседованию (или просто указать компанию) — легенда переедет туда же."""
        svc.link_call(call_id, int(interview_id) if interview_id.isdigit() else None, company)
        return RedirectResponse(f"/calls/{call_id}", status_code=303)
