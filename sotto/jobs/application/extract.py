"""Из сырых сообщений/постов — в вакансии.

1) Дешёвый локальный фильтр: длинный текст со словами найма.
2) Контакты достаём регулярками (@username, t.me, email, linkedin) — агенту не доверяем адреса.
3) Агент пачками решает «это вакансия?» и раскладывает поля.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

from ...application.ports import AgentError, Agents, Prompts
from ...application.prompting import render
from ..domain import RawPost, contacts, looks_like_job, text_hash

EXTRACT_PROMPT = """Ниже пронумерованные сообщения из Telegram-каналов и LinkedIn. Для КАЖДОГО реши, является ли оно
вакансией или постом о найме (компания или рекрутер ищет сотрудника). Резюме соискателей, курсы, реклама,
новости, мемы — не вакансии.

Для вакансий заполни поля по тексту (не выдумывай; нет данных — null):
title (должность), company, location, remote ("remote" | "hybrid" | "office" | null), salary (как в тексте),
language (язык текста: "ru" | "en"), summary (2 строки: стек, уровень, ключевые требования), fit (0..1 —
насколько подходит кандидату с профилем ниже).

Профиль кандидата:
{profile}

Верни ТОЛЬКО JSON-массив: [{"n": номер, "is_vacancy": true/false, "title": ..., "company": ..., "location": ...,
"remote": ..., "salary": ..., "language": ..., "summary": ..., "fit": ...}]

Сообщения:
{posts}
"""


async def ingest(posts: list[RawPost], store: Any, agents: Agents, prompts: Prompts, search: Any = None,
                 batch: int = 15) -> tuple[int, int]:
    """Фильтрует, разбирает агентом и сохраняет новые вакансии.
    Возвращает (добавлено, пачек не разобрано из-за агентов)."""
    fresh: list[RawPost] = []
    for p in posts:
        if not looks_like_job(p.text):
            continue
        if store.known(p.source, p.source_ref):
            continue
        h = text_hash(p.text)
        if store.seen_text(h) or any(text_hash(f.text) == h for f in fresh):
            continue  # тот же текст уже видели в другом канале (репост)
        fresh.append(p)

    added = failed = 0
    for i in range(0, len(fresh), batch):
        chunk = fresh[i:i + batch]
        listing = "\n\n".join(f"### {n}\n{p.text[:2500]}" for n, p in enumerate(chunk))
        prompt = render(EXTRACT_PROMPT, profile=prompts.profile() or "(не заполнен)", posts=listing)
        try:
            parsed, _agent = await agents.run_json(prompt, task="разбор вакансий")
        except AgentError:
            failed += 1
            continue  # пачку не помечаем просмотренной — разберём при следующем сборе
        if not isinstance(parsed, list):
            failed += 1
            continue
        for p in chunk:  # разобрано — больше не показываем агенту (в том числе «не вакансии»)
            store.mark_seen(text_hash(p.text), p.source_ref)
        used: set[int] = set()
        for item in parsed:
            if not isinstance(item, dict) or not item.get("is_vacancy"):
                continue
            n = item.get("n")
            if not isinstance(n, int) or not 0 <= n < len(chunk) or n in used:
                continue
            used.add(n)
            p = chunk[n]
            vid = store.add_vacancy(
                source=p.source, source_ref=p.source_ref, url=p.url,
                title=item.get("title"), company=item.get("company"), location=item.get("location"),
                salary=item.get("salary"), remote=item.get("remote"), language=item.get("language"),
                text=p.text, summary=item.get("summary"),
                fit=item.get("fit") if isinstance(item.get("fit"), (int, float)) else None,
                contacts=json.dumps(contacts(p.text, p.extra.get("author")), ensure_ascii=False),
                posted_at=p.posted_at)
            if search is not None:
                await asyncio.to_thread(search.index, "vacancy", vid, f"{item.get('title') or ''}. {p.text[:1500]}")
            added += 1
    return added, failed
