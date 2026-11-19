"""Windows 11: WASAPI (звук), Win32 (окна, хоткеи), ffmpeg+ddagrab (экран), mss (скриншоты),
ConsentStore (кто держит микрофон), PowerShell balloon (уведомления).

Невидимость оверлея — SetWindowDisplayAffinity(WDA_EXCLUDEFROMCAPTURE): окна нет ни в демонстрации
экрана (Zoom, Meet, Teams, Telegram, браузер), ни в скриншотах. Windows 10 2004+ / 11.

Чего нет (и не планируется без драйверов): «сцена» (виртуальный монитор — нужен IddCx) и
переводчик (виртуальные устройства PipeWire). В звонке показывай окно, а не экран.

Модули winsys/audio — только для Windows, поэтому импортируются лениво внутри методов:
сам этот файл читается на любой ОС (тесты, подсветка кода).
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import logging
import shlex
import subprocess
from pathlib import Path
from typing import Any

import numpy as np

from ... import process as proc
from ..null import NullPlatform, NullWM

log = logging.getLogger(__name__)

_OVERLAY_TITLE = "Sotto"   # заголовок окна оверлея (interfaces/overlay.py)


class WindowsWM(NullWM):
    """Окна: невидимость оверлея, его геометрия, глобальные хоткеи. «Сцены» на Windows нет."""

    def __init__(self) -> None:
        self._ws_mod: Any = None
        self._hotkeys: Any = None

    @property
    def _ws(self) -> Any:
        """winsys — только Windows; импортируем при первом обращении, а не при создании объекта."""
        if self._ws_mod is None:
            from . import winsys
            self._ws_mod = winsys
        return self._ws_mod

    def available(self) -> bool:
        ok = self._ws.supports_capture_exclusion()
        if not ok:
            log.error("Windows старше 10 2004 не умеет исключать окно из захвата — оверлей показывать нельзя")
        return ok

    def _overlay_hwnd(self) -> int | None:
        marked = self._ws.windows_marked_as_overlay()
        if marked:
            return marked[0]
        # Fallback: окно ещё не пометило себя (старый оверлей) — ищем по заголовку.
        found: list[int] = []
        user32 = self._ws.user32

        @ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)
        def each(hwnd: int, _: int) -> bool:
            if user32.IsWindowVisible(hwnd) and user32.GetWindowTextLengthW(hwnd):
                buf = ctypes.create_unicode_buffer(user32.GetWindowTextLengthW(hwnd) + 1)
                user32.GetWindowTextW(hwnd, buf, len(buf))
                if buf.value == _OVERLAY_TITLE:
                    found.append(hwnd)
            return True

        user32.EnumWindows(each, 0)
        return found[0] if found else None

    def overlay_window(self) -> str | None:
        hwnd = self._overlay_hwnd()
        return str(hwnd) if hwnd else None

    def protect(self, window: str) -> None:
        hwnd = int(window)
        self._ws.make_no_activate(hwnd)
        self._ws.hide_overlay(hwnd)

    def is_protected(self, window: str) -> bool:
        return self._ws.overlay_hidden(int(window))

    def protect_overlay(self, x: int, y: int, w: int, h: int) -> bool:
        hwnd = self._overlay_hwnd()
        if not hwnd:
            return False
        self.protect(str(hwnd))
        return self.is_protected(str(hwnd))

    def overlay_geometry(self) -> tuple[int, int, int, int] | None:
        hwnd = self._overlay_hwnd()
        return self._ws.overlay_geometry(hwnd) if hwnd else None

    def default_overlay_position(self, width: int) -> tuple[int, int]:
        screen_w, _ = self._ws.primary_monitor_size()
        return max(0, (screen_w - width) // 2), 24

    def focus(self, window: str) -> None:
        self._ws.user32.SetForegroundWindow(int(window))

    def bind_hotkeys(self, binds: dict[str, str], command: str) -> list[str]:
        """Регистрирует хоткеи; возвращает сочетания, занятые чем-то другим (их не трогает)."""
        from .winsys import Hotkeys

        def on_action(action: str) -> None:
            argv, env = self._ctl_argv(command, action)
            try:
                subprocess.Popen(argv, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                 **proc.CHILD)
            except OSError:
                log.exception("Не удалось выполнить действие хоткея %s", action)

        if self._hotkeys is None:
            self._hotkeys = Hotkeys()
        accepted = set(self._hotkeys.bind(binds, on_action))
        return [keys for keys in binds if keys and keys not in accepted]

    def unbind_hotkeys(self, keys: list[str]) -> None:
        if self._hotkeys is not None:
            self._hotkeys.close()
            self._hotkeys = None

    @staticmethod
    def _ctl_argv(command: str, action: str) -> tuple[list[str], dict[str, str] | None]:
        """Команда `sotto ctl <action>`: установленный sotto.exe или модуль из исходников."""
        if command.startswith("env PYTHONPATH="):
            return [*proc.sotto_argv(), "ctl", action], proc.child_env()
        return [*shlex.split(command), "ctl", action], None


class WindowsPlatform(NullPlatform):
    name = "windows"

    def __init__(self, *_: Any, **__: Any):
        self.wm = WindowsWM()

    def audio_input(self, mic: str, system: str, on_chunk: Any) -> Any:
        from .audio import AudioCapture
        return AudioCapture(mic, system, on_chunk)

    def audio_devices(self) -> dict[str, list[dict[str, str]]]:
        """Устройства для панели настроек (микрофоны и выходы)."""
        from . import audio
        return audio.devices()

    def microphone_users(self, ignore: list[str]) -> list[str]:
        from . import winsys
        return winsys.microphone_users(ignore)

    def play(self, samples: np.ndarray, sample_rate: int, target: str | None = None) -> Any:
        from .audio import Playback
        return Playback(samples, sample_rate, target)

    def screen_locked(self) -> bool:
        from . import winsys
        return winsys.screen_locked()

    def screenshot(self, dest: Path, output: str | None = None, region: bool = False) -> Path | None:
        from . import winsys
        if region:
            # Выделение области — отдельным процессом: Qt-цикл не должен жить внутри демона.
            cmd = [*proc.sotto_argv(), "shot", "--region", str(dest)]
            res = subprocess.run(cmd, timeout=300, env=proc.child_env(),
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, **proc.CHILD)
            return dest if res.returncode == 0 and Path(dest).exists() else None
        try:
            return winsys.screenshot(dest, output)
        except ImportError:
            log.warning("Для скриншотов нужны пакеты mss и pillow: pip install sotto[windows]")
            return None

    def focused_output(self) -> str:
        return "1"  # основной монитор — номером, который понимает ddagrab и mss

    def active_window(self) -> dict[str, Any]:
        from . import winsys
        return winsys.active_window()

    def notify(self, title: str, text: str) -> None:
        from . import winsys
        winsys.notify(title, text)

    def open_media(self, path: Path, offset_s: float = 0) -> None:
        from . import winsys
        winsys.open_path(path, offset_s)

    def clipboard(self) -> str:
        from . import winsys
        return winsys.paste_text()

    def copy_text(self, text: str) -> None:
        from . import winsys
        winsys.copy_text(text)

    def video_available(self) -> bool:
        from . import screen
        return screen.video_available()

    def record_screen(self, path: Path, fps: int, output: str, settings: Any) -> Any:
        from .screen import DdaGrabRecorder
        return DdaGrabRecorder(path, fps, output, settings)
