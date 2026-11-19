"""Платформа без рабочего стола: тесты, сервер, ещё не поддержанная ОС.

Сайт, текстовый режим, собеседования, легенда, тренер по тексту работают; захвата звука, записи экрана,
оверлея и хоткеев нет. Новая ОС начинается с копии этого файла: реализуй методы по одному.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any

import numpy as np

from ...application.ports import AudioInput, Platform, WindowManager


class _Silent(AudioInput):
    def start(self) -> None: ...
    def stop(self) -> None: ...


class _Done:
    def poll(self) -> int | None:
        return 0

    def wait(self, timeout: float | None = None) -> int:
        return 0

    def terminate(self) -> None: ...


class NullWM(WindowManager):
    stage_name = "SOTTO-STAGE"

    def available(self) -> bool:
        return False

    def protect_overlay(self, x: int, y: int, w: int, h: int) -> bool:
        return False  # не можем скрыть оверлей из демонстрации — значит, не показываем его

    def overlay_window(self) -> str | None:
        return None

    def is_protected(self, window: str) -> bool:
        return False

    def protect(self, window: str) -> None: ...

    def overlay_geometry(self) -> tuple[int, int, int, int] | None:
        return None

    def default_overlay_position(self, width: int) -> tuple[int, int]:
        return 24, 24

    def focus(self, window: str) -> None: ...
    def bind_hotkeys(self, binds: dict[str, str], command: str) -> list[str]:
        return []
    def unbind_hotkeys(self, keys: list[str]) -> None: ...

    def listen(self, on_focus: Callable[[str], None], on_reload: Callable[[], None], stop: threading.Event) -> None: ...

    def stage_active(self) -> bool:
        return False

    def stage_on(self) -> str:
        return "Сцена не поддерживается на этой системе"

    def stage_off(self) -> str:
        return "Сцена не поддерживается на этой системе"


class NullPlatform(Platform):
    name = "null"

    def __init__(self, *_: Any, **__: Any):
        self.wm = NullWM()

    def audio_input(self, mic: str, system: str, on_chunk: Any) -> _Silent:
        return _Silent()

    def microphone_users(self, ignore: list[str]) -> list[str]:
        return []

    def play(self, samples: np.ndarray, sample_rate: int, target: str | None = None) -> _Done:
        return _Done()

    def screen_locked(self) -> bool:
        return False

    def screenshot(self, dest: Path, output: str | None = None, region: bool = False) -> Path | None:
        return None

    def focused_output(self) -> str:
        return ""

    def active_window(self) -> dict[str, Any]:
        return {}

    def notify(self, title: str, text: str) -> None: ...
    def open_media(self, path: Path, offset_s: float = 0) -> None: ...

    def clipboard(self) -> str:
        return ""

    def copy_text(self, text: str) -> None: ...

    def video_available(self) -> bool:
        return False

    def record_screen(self, path: Path, fps: int, output: str, settings: Any) -> Any:
        raise RuntimeError("запись экрана не поддерживается на этой системе")
