"""Промпты-файлы: встроенные (sotto/prompts/*.md) и свои (~/.config/sotto/prompts/*.md — перекрывают встроенные).

Свой промпт = файл с тем же именем. Удалишь файл — вернётся встроенный. Плейсхолдеры в фигурных скобках
({profile}, {topic}…) подставляет код; остальные фигурные скобки (например, JSON-примеры) остаются как есть.
Профиль кандидата — ~/.config/sotto/profile.md.
"""

from __future__ import annotations

from pathlib import Path

from .. import paths
from ..application.ports import Prompts
from ..domain.profile import is_filled

# Все промпты: имя файла → (раздел, что делает).
CATALOG: dict[str, tuple[str, str]] = {
    "answer": ("Суфлёр", "основной ответ на вопрос собеседника"),
    "answer_short": ("Суфлёр", "короткая подсказка-тезисы (режим dual)"),
    "answer_detailed": ("Суфлёр", "подробный ответ (режим dual)"),
    "screen": ("Суфлёр", "ответ по скриншоту экрана (задача, код, тест)"),
    "questions": ("Суфлёр", "вопросы, которые кандидату стоит задать собеседнику"),
    "names": ("Суфлёр", "извлечение имени и роли из самопредставления"),
    "brief": ("Собеседования", "бриф к собеседованию по тексту вакансии"),
    "invite": ("Собеседования", "разбор приглашения на собеседование в поля плана"),
    "postcall": ("Собеседования", "итоги звонка: название, описание, компания, вопросы и темы"),
    "rejection": ("Собеседования", "разбор одного отказа по расшифровкам и подсказкам"),
    "rejections_summary": ("Собеседования", "общий разбор всех отказов"),
    "interviewer": ("Тренировочный интервьюер", "интервьюер (язык подставляет код)"),
    "interviewer_plan": ("Тренировочный интервьюер", "скрытый план вопросов (язык подставляет код)"),
    "vacancy": ("Тренировочный интервьюер", "выжимка вакансии: роль, компания, бриф"),
    "trainer_knowledge_interview": ("Голосовой тренер", "знания: пробное собеседование по теме"),
    "trainer_knowledge_lesson": ("Голосовой тренер", "знания: разбор темы с наставником"),
    "trainer_english_interview": ("Голосовой тренер", "английский: собеседование с разбором ошибок"),
    "trainer_english_lesson": ("Голосовой тренер", "английский: разговорная практика"),
    "trainer_turn": ("Голосовой тренер", "формат хода тренера (JSON)"),
    "trainer_topics": ("Голосовой тренер", "подбор тем для тренировок"),
    "prompt_author": ("Служебные", "как агент переписывает промпт под тебя"),
    "resume_analysis": ("Поиск работы", "разбор резюме глазами рекрутера под специализацию"),
}
KINDS = {k: v[1] for k, v in CATALOG.items()}


class FilePrompts(Prompts):
    """Реализация порта Prompts. Каталоги берутся из paths при каждом обращении (тесты их подменяют)."""

    @property
    def user_dir(self) -> Path:
        return paths.PROMPTS_DIR

    @property
    def builtin_dir(self) -> Path:
        return paths.BUILTIN_PROMPTS_DIR

    def load(self, kind: str) -> str:
        user = self.user_dir / f"{kind}.md"
        if user.exists() and user.read_text(encoding="utf-8").strip():
            return user.read_text(encoding="utf-8")
        return self.builtin(kind)

    def builtin(self, kind: str) -> str:
        return (self.builtin_dir / f"{kind}.md").read_text(encoding="utf-8")

    def is_customized(self, kind: str) -> bool:
        return (self.user_dir / f"{kind}.md").exists()

    def save(self, kind: str, text: str) -> None:
        """Совпадает со встроенным — своя копия не нужна."""
        if " ".join(text.split()) == " ".join(self.builtin(kind).split()):
            self.reset(kind)
            return
        self.user_dir.mkdir(parents=True, exist_ok=True)
        (self.user_dir / f"{kind}.md").write_text(text.strip() + "\n", encoding="utf-8")

    def reset(self, kind: str) -> None:
        (self.user_dir / f"{kind}.md").unlink(missing_ok=True)

    def catalog(self) -> dict[str, list[dict[str, object]]]:
        groups: dict[str, list[dict[str, object]]] = {}
        for k, (group, title) in CATALOG.items():
            groups.setdefault(group, []).append({"kind": k, "title": title, "text": self.load(k),
                                                 "custom": self.is_customized(k)})
        return groups

    # --- профиль ---

    def profile(self) -> str:
        """Пустая строка, если профиль не заполнен (в том числе если это нетронутый шаблон)."""
        if not paths.PROFILE_FILE.exists():
            return ""
        text = paths.PROFILE_FILE.read_text(encoding="utf-8").strip()
        return text if is_filled(text) else ""

    def save_profile(self, text: str) -> None:
        paths.PROFILE_FILE.parent.mkdir(parents=True, exist_ok=True)
        paths.PROFILE_FILE.write_text(text.strip() + "\n", encoding="utf-8")


prompts = FilePrompts()   # один на процесс: состояния нет, всё в файлах
