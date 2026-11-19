"""Настройки Sotto.

Все параметры описаны здесь как dataclass'ы со значениями по умолчанию.
Пользовательский ~/.config/sotto/config.toml перекрывает только то, что в нём указано.
"""

from __future__ import annotations

import os
import sys
import tomllib
from dataclasses import asdict, dataclass, field, fields, is_dataclass
from pathlib import Path
from typing import Any

from . import paths


@dataclass
class OpenRouter:
    # Ключ можно не хранить в файле, а задать переменной OPENROUTER_API_KEY.
    api_key: str = ""
    base_url: str = "https://openrouter.ai/api/v1"
    # Сортировка провайдеров внутри модели: "latency" | "throughput" | "price".
    provider_sort: str = "latency"


@dataclass
class DeepSeek:
    # platform.deepseek.com → API Keys; или переменная DEEPSEEK_API_KEY.
    api_key: str = ""
    base_url: str = "https://api.deepseek.com/v1"


@dataclass
class Providers:
    """Прямые провайдеры моделей. Модель вида «deepseek/deepseek-chat» при заданном ключе идёт напрямую
    (дешевле и без прослойки), без ключа — через OpenRouter, где тот же идентификатор тоже работает."""
    deepseek: DeepSeek = field(default_factory=DeepSeek)


@dataclass
class Models:
    """Модели OpenRouter по ролям. `sotto bench` подбирает их по реальным замерам."""
    # Подсказка. Выбрана слепым тестом на реальном собеседовании (оценивал живой интервьюер):
    # GPT-6.1 terra — больше всего «лучших» и меньше всего «не принял бы»; ~0.9 с до первых слов, ~$0.003.
    answer: str = "openai/gpt-6.1-terra"
    answer_fallback: str = "deepseek/deepseek-v4.1-flash"  # если основная упёрлась в лимит или упала
    answer_short: str = "anthropic/claude-haiku-5.5"    # режим dual: короткая подсказка
    answer_detailed: str = "anthropic/claude-sonnet-5.5"  # режим dual: подробный ответ
    vision: str = "anthropic/claude-sonnet-5.5"         # ответы по скриншоту — нужна сильная модель
    utility: str = "anthropic/claude-haiku-5.5"         # мелочи: имена из представлений, брифы
    stt: str = "openai/whisper-large-v3-turbo"          # распознавание реплик; "local:auto" — бесплатно на своей видеокарте


@dataclass
class Answer:
    mode: str = "auto"            # "auto" — сам отвечает на вопросы, "manual" — только по хоткею
    # "single" — один готовый ответ; "short" — только тезисы-опоры (кандидат говорит своими словами,
    # живее и не палится чтением); "dual" — короткий и подробный рядом.
    style: str = "single"
    allow_invention: bool = True  # разрешить модели придумывать опыт (легенда)
    context_minutes: int = 20     # сколько минут разговора отдавать модели
    # Доп. пауза после реплики собеседника перед авто-ответом (VAD уже ждёт 0.5 с тишины). Без неё
    # пауза «набрать воздух» посреди вопроса запускала ответ на обрывок фразы, а продолжение вопроса
    # стирало его на глазах у читающего. С упреждением (local STT) видимой задержки пауза не добавляет.
    auto_pause_ms: int = 800
    early: bool = True  # упреждение: подсказка начинает писаться через 0.2 с паузы собеседника (скрыто), ~1 с быстрее
    max_tokens: int = 350
    # Подсказка на неродном языке (родной берётся из профиля) обрезается до стольких слов
    # в коде, даже если модель не уложилась: кандидат читает её вслух. 0 — не обрезать.
    nonnative_word_limit: int = 25


@dataclass
class Audio:
    mic: str = ""      # node.name источника PipeWire; пусто — источник по умолчанию
    system: str = ""   # node.name выхода, чей монитор пишем (голос собеседника); пусто — выход по умолчанию
    min_silence: float = 0.5               # секунд тишины = конец реплики
    max_utterance: float = 25.0            # длинные монологи режем на куски
    speaker_threshold: float = 0.6         # косинусная близость голоса «это тот же человек»
    # Распознавание звонка: "auto" — локальный Whisper, если есть видеокарта NVIDIA (≈0.3 с на фразу,
    # бесплатно), иначе OpenRouter (≈2–4 с); "local" — всегда локально; "cloud" — всегда OpenRouter.
    stt: str = "auto"


@dataclass
class Recording:
    enabled: bool = True
    fps_idle: int = 2            # вне звонка: экран почти статичен, 2 кадра/с хватает
    fps_call: int = 5
    codec: str = "auto"          # auto | hevc_vaapi | libx265
    vaapi_device: str = "/dev/dri/renderD128"
    output: str = ""             # монитор; пусто — основной
    segment_minutes: int = 30    # файлы режутся на куски, чтобы сбой не терял всё
    pause_when_locked: bool = True


@dataclass
class Calls:
    auto_detect: bool = True
    # Программы, которые держат микрофон постоянно и звонком не являются.
    ignore_apps: list[str] = field(default_factory=lambda: [
        "easyeffects", "noisetorch", "pw-record", "pw-cat", "sotto", "python3", "obs",
        "wireplumber", "pipewire",
    ])
    start_after_s: int = 4
    end_after_s: int = 20


@dataclass
class Hotkeys:
    """Хоткеи регистрируются при запуске демона: в Hyprland — на лету (hyprctl), в Windows —
    глобальные (RegisterHotKey). Пусто — не назначен (действие остаётся в панели и в `sotto ctl`).

    В Windows по умолчанию Ctrl+Alt: SUPER+A, SUPER+X и SUPER+H заняты самой Windows 11
    (панель быстрых настроек, меню Win+X, голосовой ввод) — RegisterHotKey их не отдаёт."""
    ask: str = "CTRL + ALT + A" if sys.platform == "win32" else "SUPER + A"  # ответить сейчас
    screen: str = "CTRL + ALT + X" if sys.platform == "win32" else "SUPER + X"  # по скриншоту экрана
    screen_region: str = ("CTRL + ALT + SHIFT + X" if sys.platform == "win32"
                          else "SUPER + SHIFT + X")  # … по выделенной области
    questions: str = "CTRL + ALT + Q" if sys.platform == "win32" else "SUPER + SHIFT + A"
    toggle_overlay: str = "CTRL + ALT + H" if sys.platform == "win32" else "SUPER + H"
    overlay_edit: str = ("CTRL + ALT + O" if sys.platform == "win32"
                         else "SUPER + SHIFT + O")  # передвинуть/растянуть оверлей (иначе он прозрачен для мыши)
    toggle_mode: str = ""
    toggle_call: str = ""
    scroll_up: str = ""
    scroll_down: str = ""
    clear: str = ""


@dataclass
class Overlay:
    """Положение и размер запоминаются, когда окно двигают мышью. x = y = -1 — сверху по центру
    (под камерой ноутбука: читая подсказку, смотришь почти в камеру)."""
    width: int = 860
    height: int = 420
    x: int = -1
    y: int = -1
    opacity: float = 0.94
    font_size: int = 15
    transcript_lines: int = 3


@dataclass
class Stage:
    """Виртуальный монитор «сцена», который транслируется в звонок."""
    name: str = "SOTTO-STAGE"
    mirror_on: str = ""  # монитор, где показывать зеркало сцены; пусто — основной


@dataclass
class Agents:
    # Порядок перебора CLI-агентов для фоновых задач. Недоступные пропускаются.
    order: list[str] = field(default_factory=lambda: ["claude", "codex", "opencode", "kimi", "grok", "agy"])
    timeout_s: int = 600
    cooldown_minutes: int = 60   # сколько не трогать агента, упёршегося в лимит


@dataclass
class Trainer:
    stt_model: str = "auto"  # faster-whisper локально: auto = large-v3-turbo на NVIDIA (CUDA-сборка), иначе small
    voice_ru: str = "ru_RU-dmitri-medium"
    voice_en: str = "en_US-lessac-medium"
    end_of_turn_silence: float = 1.4


@dataclass
class Telegram:
    api_id: int = 0
    api_hash: str = ""
    channels: list[str] = field(default_factory=list)  # @каналы/чаты с вакансиями
    daily_send_limit: int = 8
    min_send_interval_s: int = 240


@dataclass
class LinkedIn:
    enabled: bool = False
    daily_page_limit: int = 25   # сколько страниц выдачи открывать в день
    page_delay_s: int = 25


@dataclass
class Email:
    smtp_host: str = ""
    smtp_port: int = 465
    username: str = ""
    password: str = ""   # пароль приложения
    from_name: str = ""
    daily_send_limit: int = 15


@dataclass
class Jobs:
    queries: list[str] = field(default_factory=list)  # поисковые запросы; агенты предлагают новые
    telegram: Telegram = field(default_factory=Telegram)
    linkedin: LinkedIn = field(default_factory=LinkedIn)
    email: Email = field(default_factory=Email)


@dataclass
class Translator:
    """ЭКСПЕРИМЕНТ: голосовой переводчик звонка (sotto translate)."""
    their_language: str = "en"            # на каком языке говорят собеседники
    my_language: str = "ru"               # на каком языке говоришь ты
    translate_model: str = "anthropic/claude-haiku-5.5"
    clone_model: str = "fish-audio/s2.1-pro"   # синтез твоим голосом (OpenRouter, input_references)
    stt: str = "auto"                     # "auto": локальный Whisper на видеокарте, иначе OpenRouter (models.stt)
    original_volume: float = 0.15         # громкость оригинала собеседников под переводом (0 — выключить)
    min_silence_in: float = 0.4           # пауза = конец фразы собеседника
    min_silence_out: float = 0.55         # у себя чуть длиннее: ты подбираешь слова
    max_segment: float = 6.0              # длинную речь режем, чтобы не копить задержку
    voices_male: list[str] = field(default_factory=lambda: ["ru_RU-dmitri-medium", "ru_RU-denis-medium", "ru_RU-ruslan-medium"])
    voices_female: list[str] = field(default_factory=lambda: ["ru_RU-irina-medium"])
    fallback_my_voice: str = "en_US-ryan-medium"  # если нет образца голоса или ключа


@dataclass
class Web:
    host: str = "127.0.0.1"
    port: int = 8765


@dataclass
class Config:
    openrouter: OpenRouter = field(default_factory=OpenRouter)
    providers: Providers = field(default_factory=Providers)
    models: Models = field(default_factory=Models)
    answer: Answer = field(default_factory=Answer)
    audio: Audio = field(default_factory=Audio)
    recording: Recording = field(default_factory=Recording)
    calls: Calls = field(default_factory=Calls)
    hotkeys: Hotkeys = field(default_factory=Hotkeys)
    overlay: Overlay = field(default_factory=Overlay)
    stage: Stage = field(default_factory=Stage)
    agents: Agents = field(default_factory=Agents)
    trainer: Trainer = field(default_factory=Trainer)
    jobs: Jobs = field(default_factory=Jobs)
    translator: Translator = field(default_factory=Translator)
    web: Web = field(default_factory=Web)

    @property
    def api_key(self) -> str:
        return os.environ.get("OPENROUTER_API_KEY") or self.openrouter.api_key

    def deepseek_api_key(self) -> str:
        return os.environ.get("DEEPSEEK_API_KEY") or self.providers.deepseek.api_key

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _merge(obj: Any, data: dict[str, Any]) -> None:
    """Перекладывает значения из TOML в dataclass, игнорируя неизвестные ключи."""
    known = {f.name: f for f in fields(obj)}
    for key, value in data.items():
        if key not in known:
            continue
        current = getattr(obj, key)
        if is_dataclass(current) and isinstance(value, dict):
            _merge(current, value)
        else:
            setattr(obj, key, value)


def load(path: Path | None = None) -> Config:
    path = path or paths.CONFIG_FILE
    cfg = Config()
    if path.exists():
        _merge(cfg, tomllib.loads(path.read_text(encoding="utf-8")))
    return cfg


def save(cfg: Config, path: Path | None = None) -> None:
    """Атомарно и с правами 600: в файле может лежать ключ."""
    path = path or paths.CONFIG_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(dumps(cfg.to_dict()))
    os.replace(tmp, path)


def update_in_place(dst: Any, src: Any) -> None:
    """Копирует значения src в dst, сохраняя вложенные объекты: компоненты держат ссылки на них."""
    for f in fields(dst):
        new = getattr(src, f.name)
        if is_dataclass(new):
            update_in_place(getattr(dst, f.name), new)
        else:
            setattr(dst, f.name, new)


def set_value(cfg: Config, dotted: str, value: Any) -> None:
    """set_value(cfg, "answer.mode", "auto")."""
    *parents, last = dotted.split(".")
    obj: Any = cfg
    for p in parents:
        obj = getattr(obj, p)
    current = getattr(obj, last)
    if isinstance(current, bool):
        value = value if isinstance(value, bool) else str(value).lower() in ("1", "true", "yes", "on")
    elif isinstance(current, int):
        value = int(value)
    elif isinstance(current, float):
        value = float(value)
    elif isinstance(current, list) and isinstance(value, str):
        value = [v.strip() for v in value.splitlines() if v.strip()]
    setattr(obj, last, value)


# --- Минимальный TOML-писатель: нам хватает таблиц, строк, чисел, bool и списков. ---

def _toml_value(v: Any) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return repr(v)
    if isinstance(v, str):
        escaped = v.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")
        return f'"{escaped}"'
    if isinstance(v, list):
        return "[" + ", ".join(_toml_value(x) for x in v) + "]"
    raise TypeError(f"unsupported TOML value: {v!r}")


def dumps(data: dict[str, Any], prefix: str = "") -> str:
    scalars = [(k, v) for k, v in data.items() if not isinstance(v, dict)]
    tables = [(k, v) for k, v in data.items() if isinstance(v, dict)]
    out: list[str] = []
    if prefix and scalars:
        out.append(f"[{prefix}]")
    out += [f"{k} = {_toml_value(v)}" for k, v in scalars]
    if scalars:
        out.append("")
    for k, v in tables:
        out.append(dumps(v, f"{prefix}.{k}" if prefix else k))
    return "\n".join(out)
