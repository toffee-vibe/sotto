"""ИИ-интервьюер для тренировок: кто он, что знает о вакансии и резюме, план вопросов.

Общее для текстовой тренировки на сайте (application/textchat.py) и голосовой в терминале (sotto sim).
"""

from __future__ import annotations

from typing import Any

from .ports import LLM, Prompts
from .prompting import parse_json_object, render

END_MARK = "[КОНЕЦ]"

LANGS = {
    "ru": {"name": "Анна", "voice": "ru_RU-irina-medium", "default_role": "бэкенд-разработчик",
           "joined": "(кандидат подключился)", "silence": "(молчание)"},
    "en": {"name": "Emily", "voice": "en_US-lessac-medium", "default_role": "backend developer",
           "joined": "(the candidate joined)", "silence": "(silence)"},
}


def system_prompt(prompts: Prompts, lang: str, **values: str) -> str:
    """Инструкция интервьюера — промпт interviewer (правится на странице «Промпты»)."""
    return render(prompts.load("interviewer"), lang_rule=LANG_RULE[lang]["speak"],
                  silence=LANGS[lang]["silence"], **values)


def plan_prompt(prompts: Prompts, lang: str, **values: str) -> str:
    return render(prompts.load("interviewer_plan"), lang_rule=LANG_RULE[lang]["plan"], **values)


LANG_RULE = {
    "ru": {"speak": "Говори только по-русски, даже если вакансия или резюме на английском.",
           "plan": "Пиши компактно, без вступлений."},
    "en": {"speak": "Speak ONLY English, every line, even if the resume and vacancy are in Russian. "
                    "If the candidate answers in Russian, ask them in English to switch to English.",
           "plan": "Be compact, no preamble. Write it in English."},
}


CONTEXT = {
    "ru": {
        "vacancy": "\nВакансия, на которую собеседуешь (ты из этой компании; вопросы — по её задачам и требованиям):\n{text}\n",
        "resume": "\nРезюме кандидата (ты его прочитала заранее; спрашивай и по нему — «вижу, вы делали…», "
                  "проверяй, что за строчками стоит реальный опыт):\n{text}\n",
    },
    "en": {
        "vacancy": "\nThe vacancy you are hiring for (you work at this company; ask about its tasks and requirements):\n{text}\n",
        "resume": "\nThe candidate's resume (you read it beforehand; ask about it too — \"I see you did…\" — "
                  "and check there is real experience behind the lines):\n{text}\n",
    },
}

PLAN_RULES = {
    "ru": """
Твой план (кандидат его не видит):
{plan}
Иди по плану по порядку. По каждой теме — не больше двух уточнений, потом переходи к следующей: собеседование
длится ограниченное время, важнее покрыть все темы, чем докопаться до дна одной. Какие темы пройдены — смотри по разговору.
""",
    "en": """
Your plan (the candidate can't see it):
{plan}
Follow the plan in order. At most two follow-ups per topic, then move to the next one: time is limited and covering
all topics matters more than digging to the bottom of one. Track which topics are done from the conversation.
""",
}


async def digest_vacancy(llm: LLM, model: str, prompts: Prompts, raw: str) -> dict[str, str] | None:
    """Сжимает вакансию: роль, компания и бриф. Сырой текст со страницы — десятки килобайт мусора."""
    try:
        res = await llm.chat(model, [{"role": "user", "content": render(prompts.load("vacancy"), text=raw[:20000])}],
                             max_tokens=700, json_mode=True)
        data: dict[str, Any] = parse_json_object(res.text)
    except Exception:  # noqa: BLE001 — без вакансии тренажёр всё равно работает
        return None
    if not str(data.get("role", "")).strip():
        return None
    return {k: str(data.get(k, "")).strip() for k in ("role", "company", "brief")}
