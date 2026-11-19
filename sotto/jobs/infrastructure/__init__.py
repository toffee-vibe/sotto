"""Источники вакансий и каналы откликов: Telegram (Telethon), LinkedIn (Playwright), почта (SMTP), PDF."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ...config import Config
from ..domain import RawPost


class JobSources:
    """Реализация порта Sources для сценария вакансий."""

    def __init__(self, cfg: Config, store: Any):
        self.cfg = cfg
        self.store = store

    def telegram_ready(self) -> bool:
        from . import telegram
        return bool(self.cfg.jobs.telegram.api_id) and telegram.logged_in()

    def linkedin_ready(self) -> bool:
        from . import linkedin
        return self.cfg.jobs.linkedin.enabled and linkedin.logged_in()

    async def telegram(self, queries: list[str]) -> tuple[list[RawPost], dict[str, str]]:
        from . import telegram
        return await telegram.fetch(self.cfg.jobs.telegram, self.store.get_meta, queries)

    async def linkedin(self, queries: list[str]) -> list[RawPost]:
        from . import linkedin
        return await linkedin.fetch(self.cfg.jobs.linkedin, self.store, queries)

    async def send_telegram(self, target: str, text: str, attachment: Path | None) -> None:
        from . import telegram
        await telegram.send(self.cfg.jobs.telegram, target, text, attachment)

    def send_email(self, to: str, subject: str, body: str, attachment: Path | None) -> None:
        from . import emailer
        emailer.send(self.cfg.jobs.email, to, subject, body, attachment)

    def open_in_browser(self, url: str, text: str) -> None:
        from . import linkedin
        linkedin.open_for_reply(url, text)

    def render_pdf(self, markdown_text: str, name: str) -> Path:
        from .pdf import render_pdf
        return render_pdf(markdown_text, name)
