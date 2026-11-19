"""`sotto jobs …` — вход в Telegram/LinkedIn и сбор вакансий из терминала."""

from __future__ import annotations

from ... import config as config_mod
from ... import paths
from ..infrastructure import linkedin, telegram


async def run_jobs(action: str) -> int:
    paths.ensure_dirs()
    cfg = config_mod.load()
    if action == "login-telegram":
        await telegram.login(cfg.jobs.telegram)
        return 0
    if action == "login-linkedin":
        await linkedin.login()
        cfg.jobs.linkedin.enabled = True
        config_mod.save(cfg)
        return 0
    from ...bootstrap import Sotto
    app = Sotto(cfg, with_overlay=False, with_desktop=False)
    if action == "fetch":
        print(await app.jobs.fetch_all())
    elif action == "cluster":
        from ..application.cluster import update_clusters
        print(await update_clusters(app.jobs_repo, app.search, app.agents))
    return 0
