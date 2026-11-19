"""Вакансии и отклики: что считать вакансией, контакты, письмо и резюме под вакансию, лимиты отправки.

Отправка — только по нажатию кнопки и в пределах лимитов (суточный, интервал, не писать дважды одному
адресату): это главный предохранитель от бана в Telegram и почте.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from typing import Any

import numpy as np

from sotto.domain.errors import DomainError

STATUSES = ("new", "hidden", "ready", "sent", "replied")


class SendLimit(DomainError):
    """Отправка сейчас запрещена лимитами — объяснение показывается человеку."""


HIRING = re.compile(
    r"(ваканси|ищем|ищу|требуется|в поиске|открыта позиция|нанимаем|присоединяйся|зарплат|вилка|оклад|"
    r"hiring|we'?re looking|looking for|join (our|us)|open (position|role)|vacancy|job opening|salary|"
    r"#job|#ваканси|#hiring|remote|удал[её]нк)", re.I)

TG_USER = re.compile(r"(?<![\w.])@([A-Za-z][A-Za-z0-9_]{4,31})\b")
TG_LINK = re.compile(r"(?:https?://)?t\.me/([A-Za-z][A-Za-z0-9_]{4,31})(?![/\w])")
EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
LINKEDIN = re.compile(r"https?://(?:[a-z]{2,3}\.)?linkedin\.com/(?:in|company|jobs/view)/[^\s)\]]+", re.I)
URL = re.compile(r"https?://[^\s)\]]+")

# Ботов и служебные аккаунты контактами не считаем.
NOT_CONTACTS = {"gmail", "mail", "yandex", "telegram", "username"}


@dataclass
class RawPost:
    source: str        # telegram | linkedin_jobs | linkedin_posts
    source_ref: str    # уникальный id внутри источника
    text: str
    url: str | None = None
    posted_at: float | None = None
    extra: dict[str, Any] = field(default_factory=dict)  # автор поста и т.п.


def looks_like_job(text: str) -> bool:
    return len(text) >= 150 and bool(HIRING.search(text))


def contacts(text: str, author: str | None = None) -> list[dict[str, str]]:
    out: list[dict[str, str]] = []
    seen = set()

    def add(kind: str, value: str) -> None:
        key = (kind, value.lower())
        if key not in seen:
            seen.add(key)
            out.append({"type": kind, "value": value})

    for m in EMAIL.finditer(text):
        add("email", m.group(0).rstrip("."))
    emails_users = {e["value"].split("@")[0].lower() for e in out}
    for rx in (TG_USER, TG_LINK):
        for m in rx.finditer(text):
            name = m.group(1)
            if name.lower() not in NOT_CONTACTS and name.lower() not in emails_users and not name.lower().endswith("bot"):
                add("telegram", "@" + name)
    for m in LINKEDIN.finditer(text):
        add("linkedin", m.group(0).rstrip(".,"))
    if author:
        add("linkedin" if "linkedin.com" in author else "telegram", author)
    for m in URL.finditer(text):
        u = m.group(0).rstrip(".,")
        if "t.me/" not in u and "linkedin.com" not in u:
            add("url", u)
    return out


def text_hash(text: str) -> str:
    norm = re.sub(r"\W+", " ", text.lower()).strip()[:400]
    return hashlib.sha1(norm.encode()).hexdigest()


def letter_for(vacancy: dict[str, Any], cluster: dict[str, Any] | None) -> str:
    """Письмо под вакансию: ручная правка, иначе шаблон кластера с подстановками."""
    if vacancy.get("letter_override"):
        return vacancy["letter_override"]
    template = (cluster or {}).get("letter_md") or ""
    lang_en = vacancy.get("language") == "en"
    greeting = "Hi" if lang_en else "Здравствуйте"
    return (template.replace("{greeting}", greeting)
            .replace("{position}", vacancy.get("title") or ("the position" if lang_en else "вакансию"))
            .replace("{company}", vacancy.get("company") or "")
            .replace(" ,", ",").replace("  ", " ").strip())


def resume_for(vacancy: dict[str, Any], cluster: dict[str, Any] | None, base: str) -> str:
    """Резюме под вакансию: ручная правка, иначе вариант кластера, иначе базовое."""
    if vacancy.get("resume_override"):
        return vacancy["resume_override"]
    if cluster and cluster.get("resume_md"):
        return cluster["resume_md"]
    return base


def override(text: str, base: str) -> str | None:
    """Ручную правку храним, только если она отличается от шаблона."""
    text = text.strip()
    return text if text and text != base.strip() else None


def check_send(channel: str, target: str, *, sent_today: int, daily_limit: int, last_sent_at: float | None,
               min_interval_s: float, already_sent: bool, now: float) -> None:
    if sent_today >= daily_limit:
        raise SendLimit(f"Лимит на сегодня: {daily_limit} ({channel})")
    if last_sent_at and min_interval_s and now - last_sent_at < min_interval_s:
        raise SendLimit(f"Слишком часто: следующее сообщение через {int(min_interval_s - (now - last_sent_at))} с")
    if already_sent:
        raise SendLimit(f"{target} уже писали — повторно не отправляю")


# --- группировка похожих вакансий ---

ASSIGN = 0.62    # близость к центру кластера, чтобы попасть в него
GROUP = 0.66     # близость, чтобы две новые вакансии оказались в одной группе
MIN_SIZE = 2


def centroid(vecs: np.ndarray) -> np.ndarray:
    c = vecs.mean(axis=0)
    return c / (np.linalg.norm(c) + 1e-9)


def nearest(vec: np.ndarray, clusters: list[tuple[int, np.ndarray | None]]) -> int | None:
    """Кластер, к которому вакансия ближе всего, если достаточно близко."""
    best_id, best_sim = None, -1.0
    for cid, cen in clusters:
        if cen is not None:
            sim = float(cen @ vec)
            if sim > best_sim:
                best_id, best_sim = cid, sim
    return best_id if best_sim >= ASSIGN else None


def group(vecs: np.ndarray, idx: list[int]) -> list[list[int]]:
    """Жадная группировка: вакансия идёт в первую группу, к центру которой достаточно близка."""
    groups: list[list[int]] = []
    for i in idx:
        for g in groups:
            if float(centroid(vecs[g]) @ vecs[i]) >= GROUP:
                g.append(i)
                break
        else:
            groups.append([i])
    return [g for g in groups if len(g) >= MIN_SIZE]
