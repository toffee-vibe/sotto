"""Вакансии: сбор (источники → разбор → кластеры → новые запросы) и отклики (только по кнопке)."""

from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path
from typing import Any, Protocol

from ...application.ports import AgentError, Agents, Events, Prompts
from ...config import Config
from .. import domain as rules
from ..domain import RawPost
from .cluster import update_clusters
from .extract import ingest
from .resume import ResumeService

log = logging.getLogger(__name__)


class Sources(Protocol):
    """Откуда берутся вакансии и куда уходят отклики (Telegram, LinkedIn, почта, PDF)."""
    def telegram_ready(self) -> bool: ...
    def linkedin_ready(self) -> bool: ...
    async def telegram(self, queries: list[str]) -> tuple[list[RawPost], dict[str, str]]: ...
    async def linkedin(self, queries: list[str]) -> list[RawPost]: ...
    async def send_telegram(self, target: str, text: str, attachment: Path | None) -> None: ...
    def send_email(self, to: str, subject: str, body: str, attachment: Path | None) -> None: ...
    def open_in_browser(self, url: str, text: str) -> None: ...
    def render_pdf(self, markdown_text: str, name: str) -> Path: ...


class JobsService:
    def __init__(self, cfg: Config, store: Any, sources: Sources, resume: ResumeService, agents: Agents,
                 prompts: Prompts, search: Any, events: Events):
        self.cfg = cfg
        self.store = store
        self.sources = sources
        self.resume = resume
        self.agents = agents
        self.prompts = prompts
        self.search = search
        self.events = events

    def _note(self, text: str) -> None:
        log.info(text)
        self.events.publish("notice", {"text": text[:300]})

    # --- поисковые запросы ---

    def queries(self) -> list[str]:
        self.store.add_queries(self.cfg.jobs.queries, "manual")  # запросы из конфига — как ручные
        return self.store.active_queries()

    async def suggest_queries(self) -> int:
        """Агент пополняет список запросов: синонимы ролей, RU/EN, хэштеги, по найденным вакансиям."""
        existing = self.store.all_queries()
        prompt = ("Предложи до 8 НОВЫХ поисковых запросов для поиска вакансий этого кандидата в Telegram-каналах и "
                  "постах LinkedIn (там люди пишут «ищем», «hiring», хэштеги вроде #вакансия #hiring). Смешай русские и "
                  "английские формулировки, синонимы названия роли, ключевой стек. Не повторяй существующие.\n\n"
                  f"Профиль:\n{self.prompts.profile() or '(не заполнен)'}\n\nУже есть:\n" + "\n".join(existing) +
                  "\n\nПодходящие найденные вакансии:\n" + "\n".join(self.store.good_titles()) +
                  "\n\nВерни ТОЛЬКО JSON-массив строк.")
        try:
            items, _ = await self.agents.run_json(prompt, task="новые поисковые запросы")
        except AgentError:
            return 0
        have = {e.lower() for e in existing}
        return self.store.add_queries([str(q) for q in (items if isinstance(items, list) else [])
                                       if str(q).strip().lower() not in have], "agent")

    # --- сбор ---

    async def fetch_all(self) -> dict[str, Any]:
        queries = self.queries()
        if not queries and self.prompts.profile():
            await self.suggest_queries()
            queries = self.queries()
        report: dict[str, Any] = {"telegram": 0, "linkedin": 0, "errors": []}
        raw: list[RawPost] = []
        cursors: dict[str, str] = {}
        if self.sources.telegram_ready():
            try:
                tg, cursors = await self.sources.telegram(queries)
                raw += tg
                report["telegram"] = len(tg)
            except Exception as e:  # noqa: BLE001 — источник упал, остальные продолжают
                report["errors"].append(f"Telegram: {e}")
        if self.sources.linkedin_ready():
            try:
                li = await self.sources.linkedin(queries)
                raw += li
                report["linkedin"] = len(li)
            except Exception as e:  # noqa: BLE001
                report["errors"].append(f"LinkedIn: {e}")
        self._note(f"Собрано сообщений: Telegram {report['telegram']}, LinkedIn {report['linkedin']}. Разбираю…")
        report["added"], failed = await ingest(raw, self.store, self.agents, self.prompts, self.search)
        if failed:
            report["errors"].append(f"агенты не разобрали {failed} пачек — повторю при следующем сборе")
        else:  # курсоры каналов двигаем, только если всё разобрано — иначе сообщения потерялись бы
            for key, value in cursors.items():
                self.store.set_meta(key, value)
        report["clusters"] = await update_clusters(self.store, self.search, self.agents)
        for q in queries:  # какие запросы приносят вакансии — те поднимаются выше
            if q.split():
                self.store.set_hits(q, self.store.mentions(q.split()[0]))
        report["new_queries"] = await self.suggest_queries()
        self._note(f"Новых вакансий: {report['added']}; кластеры: {report['clusters']}; "
                   f"новых запросов: {report['new_queries']}"
                   + (f"; ошибки: {'; '.join(report['errors'])}" if report["errors"] else ""))
        self.events.publish("job.done", {"text": f"Вакансии обновлены: +{report['added']}"})
        return report

    def add_query(self, query: str) -> None:
        self.store.add_queries([query], "manual")

    def toggle_query(self, qid: int) -> None:
        self.store.toggle_query(qid)

    def set_status(self, vid: int, status: str) -> None:
        if status in rules.STATUSES:
            self.store.set_status(vid, status)

    def assign_cluster(self, vid: int, cluster_id: int | None) -> None:
        self.store.assign(vid, cluster_id)

    def save_cluster(self, cid: int, name: str, resume_md: str, letter_md: str) -> None:
        values: dict[str, Any] = {"resume_md": resume_md.strip(), "letter_md": letter_md.strip(), "updated_at": time.time()}
        if name.strip():
            values["name"] = name.strip()
        self.store.update_cluster(cid, **values)

    def mark_replied(self, vid: int) -> None:
        """По вакансии назначили собеседование."""
        self.store.set_status(vid, "replied")

    # --- письмо и резюме под вакансию ---

    def materials(self, v: dict[str, Any]) -> tuple[dict[str, Any] | None, str, str]:
        cluster = self.store.cluster(v.get("cluster_id"))
        return cluster, rules.letter_for(v, cluster), rules.resume_for(v, cluster, self.resume.base("ru"))

    def save_overrides(self, vid: int, letter: str, resume_md: str) -> None:
        v = self.store.vacancy(vid)
        if v is None:
            return
        cluster = self.store.cluster(v.get("cluster_id"))
        base_letter = rules.letter_for({**v, "letter_override": None}, cluster)
        base_resume = rules.resume_for({**v, "resume_override": None}, cluster, self.resume.base("ru"))
        self.store.update_vacancy(vid, letter_override=rules.override(letter, base_letter),
                                  resume_override=rules.override(resume_md, base_resume))

    async def pdf(self, markdown_text: str, name: str) -> Path:
        return await asyncio.to_thread(self.sources.render_pdf, markdown_text, name)

    # --- отклик ---

    def _check(self, channel: str, target: str) -> None:
        j = self.cfg.jobs
        daily, interval = ((j.telegram.daily_send_limit, j.telegram.min_send_interval_s) if channel == "telegram"
                           else (j.email.daily_send_limit, 0))
        rules.check_send(channel, target, sent_today=self.store.sent_since(channel, time.time() - 86400),
                         daily_limit=daily, last_sent_at=self.store.last_sent(channel), min_interval_s=interval,
                         already_sent=self.store.already_sent(channel, target), now=time.time())

    async def send(self, vid: int, channel: str, target: str) -> None:
        """Единственная точка отправки. Вызывается только кнопкой пользователя."""
        v = self.store.vacancy(vid)
        if v is None:
            return
        cluster, letter, resume_md = self.materials(v)
        if not letter or not resume_md:
            self._note("Нет письма или резюме: сначала сгенерируй их для кластера (раздел «Резюме»)")
            return
        name = (self.resume.candidate_name() or "resume").replace(" ", "_")
        status = "sent"
        try:
            if channel in ("telegram", "email"):
                self._check(channel, target)
            pdf = await self.pdf(resume_md, f"{name}_{(cluster or {}).get('name', 'CV')}")
            if channel == "telegram":
                await self.sources.send_telegram(target, letter, pdf)
            elif channel == "email":
                title = v.get("title")
                subject = ((f"Application: {title}" if v.get("language") == "en" else f"Отклик: {title}")
                           if title else "Отклик на вакансию")
                await asyncio.to_thread(self.sources.send_email, target, subject, letter, pdf)
            elif channel in ("linkedin", "url"):
                self.sources.open_in_browser(target if target.startswith("http") else (v.get("url") or target), letter)
                status = "opened_in_browser"
                self._note("Открыл страницу, текст письма в буфере обмена. PDF: " + str(pdf))
            else:
                raise RuntimeError(f"неизвестный канал {channel}")
        except rules.SendLimit as e:
            self._note(str(e))
            return
        except Exception as e:  # noqa: BLE001
            self.store.log_outreach(vid, channel, target, letter, "failed")
            self._note(f"Не отправлено: {e}")
            return
        self.store.log_outreach(vid, channel, target, letter, status)
        if status == "sent":
            self.store.set_status(vid, "sent")
            self._note(f"Отправлено: {target}")
