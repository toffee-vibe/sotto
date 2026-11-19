"""Разделы «Вакансии» и «Резюме» в панели. Любая отправка — только по нажатию кнопки."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import TYPE_CHECKING

from fastapi import FastAPI, Form, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse

from ... import config as config_mod
from ...application.ports import AgentError
from ..infrastructure import linkedin, telegram

if TYPE_CHECKING:
    from ...bootstrap import Sotto


def register(app: FastAPI, d: Sotto, page: Callable) -> None:
    store = d.jobs_repo  # только чтение для страниц; изменения — через jobs
    jobs = d.jobs
    resume = d.resume

    def back(request: Request, default: str = "/jobs") -> RedirectResponse:
        return RedirectResponse(request.headers.get("referer") or default, status_code=303)

    def notify(text: str) -> None:
        d.bus.publish("notice", {"text": text[:300]})

    # ---------------- список ----------------

    @app.get("/jobs", response_class=HTMLResponse)
    async def jobs_page(request: Request, cluster: int | None = None, status: str = "active", q: str = ""):
        ids = None
        if q:
            hits = await asyncio.to_thread(d.search.query, q, ("vacancy",), 60)
            ids = [h["ref_id"] for h in hits]
        vacancies = store.page(cluster, status, ids)
        clusters = store.clusters_with_counts()
        queries = store.queries_list()
        return page(request, "jobs.html", vacancies=vacancies, clusters=clusters, queries=queries,
                    cluster=cluster, status_filter=status, q=q, tg_logged=telegram.logged_in(),
                    li_logged=linkedin.logged_in(), j=d.cfg.jobs)

    @app.post("/jobs/fetch")
    async def jobs_fetch(request: Request):
        d.background.spawn(jobs.fetch_all())
        notify("Сбор вакансий запущен — это займёт несколько минут")
        return back(request)

    @app.post("/jobs/queries/add")
    async def query_add(request: Request, query: str = Form(...)):
        if not query.strip():
            return back(request)
        jobs.add_query(query)
        return back(request)

    @app.post("/jobs/queries/suggest")
    async def query_suggest(request: Request):
        n = await jobs.suggest_queries()
        notify(f"Добавлено запросов: {n}")
        return back(request)

    @app.post("/jobs/queries/{qid}/toggle")
    async def query_toggle(request: Request, qid: int):
        jobs.toggle_query(qid)
        return back(request)

    @app.post("/jobs/settings")
    async def jobs_settings(request: Request):
        form = await request.form()
        for key, value in form.items():
            if not key.startswith("jobs.") or (key.endswith("api_hash") or key.endswith("password")) and not value:
                continue
            try:
                config_mod.set_value(d.cfg, key, value)
            except (AttributeError, ValueError):
                pass
        if "jobs.linkedin.enabled" not in form:
            d.cfg.jobs.linkedin.enabled = False
        config_mod.save(d.cfg)
        return back(request)

    # ---------------- вакансия ----------------

    @app.get("/jobs/{vid}", response_class=HTMLResponse)
    async def vacancy_page(request: Request, vid: int):
        v = store.vacancy(vid)
        if not v:
            return RedirectResponse("/jobs", status_code=303)
        cluster, letter, resume_md = jobs.materials(v)
        return page(request, "vacancy.html", v=v, cluster=cluster, clusters=store.cluster_names(),
                    sent=store.outreach(vid), letter=letter, resume_md=resume_md)

    @app.post("/jobs/{vid}/status")
    async def vacancy_status(request: Request, vid: int, status: str = Form(...)):
        jobs.set_status(vid, status)
        return back(request)

    @app.post("/jobs/{vid}/cluster")
    async def vacancy_cluster(request: Request, vid: int, cluster_id: str = Form("")):
        jobs.assign_cluster(vid, int(cluster_id) if cluster_id else None)
        return back(request)

    @app.post("/jobs/{vid}/override")
    async def vacancy_override(request: Request, vid: int, letter: str = Form(""), resume_md: str = Form("")):
        jobs.save_overrides(vid, letter, resume_md)  # хранится только то, что отличается от шаблона
        return back(request)

    @app.get("/jobs/{vid}/resume.pdf")
    async def vacancy_pdf(vid: int):
        v = store.vacancy(vid)
        cluster, _, resume_md = jobs.materials(v or {})
        pdf = await jobs.pdf(resume_md, f"resume_{cluster['name'] if cluster else 'base'}_{vid}")
        return FileResponse(pdf, media_type="application/pdf", filename=pdf.name)

    @app.post("/jobs/{vid}/send")
    async def vacancy_send(request: Request, vid: int, channel: str = Form(...), target: str = Form(...)):
        """Единственная точка отправки. Вызывается только кнопкой пользователя."""
        await jobs.send(vid, channel, target)
        return back(request)

    # ---------------- резюме ----------------

    @app.get("/resume", response_class=HTMLResponse)
    async def resume_page(request: Request):
        return page(request, "resume.html", base_ru=resume.base("ru"), base_en=resume.base("en"),
                    clusters=store.clusters_with_counts(), use_legend=resume.use_legend)

    @app.post("/resume/base/{lang}")
    async def resume_base_save(request: Request, lang: str, text: str = Form("")):
        resume.save_base(lang, text)
        return back(request, "/resume")

    @app.post("/resume/base/{lang}/generate")
    async def resume_base_generate(request: Request, lang: str, use_legend: str = Form("")):
        resume.set_use_legend(bool(use_legend))
        try:
            await resume.generate_base(bool(use_legend), lang)
        except AgentError as e:
            notify(str(e))
        return back(request, "/resume")

    @app.get("/resume/base/{lang}.pdf")
    async def resume_base_pdf(lang: str):
        pdf = await jobs.pdf(resume.base(lang), f"{resume.candidate_name() or 'resume'}_{lang}")
        return FileResponse(pdf, media_type="application/pdf", filename=pdf.name)

    @app.post("/resume/cluster/{cid}")
    async def resume_cluster_save(request: Request, cid: int, resume_md: str = Form(""), letter_md: str = Form(""),
                                  name: str = Form("")):
        jobs.save_cluster(cid, name, resume_md, letter_md)
        return back(request, "/resume")

    @app.post("/resume/cluster/{cid}/generate")
    async def resume_cluster_generate(request: Request, cid: int):
        try:
            await resume.generate_for_cluster(cid, resume.use_legend)
        except AgentError as e:
            notify(str(e))
        return back(request, "/resume")

    @app.get("/resume/cluster/{cid}.pdf")
    async def resume_cluster_pdf(cid: int):
        c = store.cluster(cid) or {"resume_md": "", "name": cid}
        pdf = await jobs.pdf(c["resume_md"] or "", f"resume_{c['name']}")
        return FileResponse(pdf, media_type="application/pdf", filename=pdf.name)
