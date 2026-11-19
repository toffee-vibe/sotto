"""Резюме: базовое (по профилю) и вариант под кластер похожих вакансий вместе с шаблоном письма."""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import Any

from ...application.ports import Agents, Prompts
from ...domain.legend import LegendItem

RESUME_RULES = """Правила резюме:
- Markdown: # Имя, строка контактов, ## Разделы (О себе — 2–3 строки; Навыки; Опыт; Образование; Языки).
- Опыт: компания, роль, годы; 3–5 пунктов-достижений на место: глагол + что сделал + результат в цифрах.
- Ключевые слова из требований кластера — естественно, без переспама (ATS это ценит).
- 1–2 страницы. Без фото, без «коммуникабельный, стрессоустойчивый».
- Опирайся только на профиль и легенду ниже. Не выдумывай новых мест работы, дат и цифр сверх них."""



class ResumeService:
    def __init__(self, store: Any, agents: Agents, prompts: Prompts, legend: Callable[[], list[LegendItem]]):
        self.store = store
        self.agents = agents
        self.prompts = prompts
        self.legend = legend   # общая легенда (без компании) — то, что можно писать в резюме

    def _legend_text(self) -> str:
        return "\n".join(f"- {i.claim}" + (f" ({i.note})" if i.note else "") for i in self.legend()) or "(нет)"

    def base(self, lang: str = "ru") -> str:
        return self.store.get_meta(f"resume_base_{lang}") or ""

    def save_base(self, lang: str, text: str) -> None:
        self.store.set_meta(f"resume_base_{lang}", text.strip())

    @property
    def use_legend(self) -> bool:
        return self.store.get_meta("resume_use_legend") == "1"

    def set_use_legend(self, value: bool) -> None:
        self.store.set_meta("resume_use_legend", "1" if value else "0")

    async def generate_base(self, use_legend: bool, language: str = "ru") -> str:
        prompt = (f"Составь резюме кандидата на языке: {'русский' if language == 'ru' else 'английский'}.\n{RESUME_RULES}\n\n"
                  f"Профиль:\n{self.prompts.profile()}\n\nЛегенда (рассказанный на собеседованиях опыт):\n"
                  f"{self._legend_text() if use_legend else '(не использовать)'}\n\nВерни ТОЛЬКО Markdown резюме.")
        text, _ = await self.agents.run(prompt, task="базовое резюме")
        text = _strip_fence(text)
        self.save_base(language, text)
        return text

    async def generate_for_cluster(self, cluster_id: int, use_legend: bool) -> dict[str, Any]:
        cluster = self.store.cluster(cluster_id)
        vacancies = self.store.cluster_vacancies(cluster_id, 8)
        langs = [v["language"] for v in vacancies if v["language"]]
        lang = "en" if langs and langs.count("en") > len(langs) / 2 else "ru"
        base = self.base(lang) or self.base("ru") or "(базового резюме нет)"
        sample = "\n\n".join(f"- {v['title']} @ {v['company']}: {v['text'][:700]}" for v in vacancies)
        prompt = (
            f"Адаптируй резюме под группу похожих вакансий «{cluster['name']}» и напиши шаблон сопроводительного письма.\n"
            f"Язык обоих текстов: {'английский' if lang == 'en' else 'русский'}.\n{RESUME_RULES}\n\n"
            "Письмо: 5–8 предложений, живым языком, без канцелярита; почему подходишь — 2–3 конкретных факта из опыта, "
            "релевантных этой группе; в конце — предложение созвониться. Используй подстановки ровно в таком виде: "
            "{greeting} (приветствие), {position} (должность), {company} (компания или пусто). Не ставь подпись с "
            "контактами — они в резюме.\n\n"
            f"Описание группы: {cluster['description'] or ''}\n\nПримеры вакансий:\n{sample}\n\n"
            f"Базовое резюме:\n{base}\n\nПрофиль:\n{self.prompts.profile()}\n\n"
            f"Легенда:\n{self._legend_text() if use_legend else '(не использовать)'}\n\n"
            'Верни ТОЛЬКО JSON {"resume_md": "...", "letter_md": "..."}')
        data, _ = await self.agents.run_json(prompt, task=f"резюме для кластера {cluster['name']}")
        resume_md, letter_md = _strip_fence(str(data.get("resume_md") or "")), str(data.get("letter_md") or "")
        self.store.update_cluster(cluster_id, resume_md=resume_md, letter_md=letter_md)
        return {"resume_md": resume_md, "letter_md": letter_md}

    def candidate_name(self) -> str:
        """Имя для файла резюме — из первой строки «# Имя» профиля."""
        for line in self.prompts.profile().splitlines():
            if line.startswith("# ") and "профиль" not in line.lower():
                return line[2:].strip()
        return ""


def _strip_fence(text: str) -> str:
    text = text.strip()
    m = re.match(r"^```(?:markdown|md)?\s*\n(.*)\n```$", text, re.S)
    return m.group(1).strip() if m else text
