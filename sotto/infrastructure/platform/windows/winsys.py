"""Windows 11: окна, глобальные хоткеи, скриншоты, буфер обмена, определение звонка.

Ядро — Win32 через ctypes, без сторонних библиотек: приложение должно ставиться одним
`pip install` и не требовать Visual Studio, pywin32 и прав администратора.

Модуль только для Windows (ctypes.windll на верхнем уровне): его импортируют
WindowsPlatform и `sotto shot`, которые на других ОС не выбираются. Чистая логика,
которую нужно тестировать везде, вынесена в hotkeys.py и select.py.
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import logging
import os
import subprocess
import sys
import threading
import time
import winreg
from collections.abc import Callable
from pathlib import Path
from typing import Any

from ... import process as _proc
from .hotkeys import MOD_NOREPEAT, parse_hotkey  # noqa: F401  (re-export для вызывающих)

log = logging.getLogger(__name__)

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
shell32 = ctypes.WinDLL("shell32", use_last_error=True)

# --- константы ---
WDA_NONE = 0x0
WDA_MONITOR = 0x1
WDA_EXCLUDEFROMCAPTURE = 0x11

WM_HOTKEY = 0x0312
WM_QUIT = 0x0012
WM_REREGISTER = 0x8001  # WM_APP+1: своё сообщение потоку хоткеев

GWL_EXSTYLE = -20
WS_EX_NOACTIVATE = 0x08000000
WS_EX_TOOLWINDOW = 0x00000080
WS_EX_APPWINDOW = 0x00040000

DESKTOP_SWITCHDESKTOP = 0x0100
CF_UNICODETEXT = 13

MIC_CONSENT = r"SOFTWARE\Microsoft\Windows\CurrentVersion\CapabilityAccessManager\ConsentStore\microphone"


def _winver() -> tuple[int, int, int]:
    try:
        version = tuple(int(x) for x in sys.getwindowsversion()[:3])  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001
        return (0, 0, 0)
    return (version + (0, 0, 0))[:3]  # type: ignore[return-value]


def supports_capture_exclusion() -> bool:
    """WDA_EXCLUDEFROMCAPTURE появился в Windows 10 2004 (сборка 19041). На Windows 11 — всегда."""
    major, _minor, build = _winver()
    return major >= 10 and build >= 19041


def set_dpi_awareness() -> None:
    """Чёткий текст и координаты окон в физических пикселях: без этого Qt рисует мыльно."""
    try:
        user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))  # PER_MONITOR_AWARE_V2
    except Exception:  # noqa: BLE001 — уже задано или старая Windows
        pass


def set_app_user_model_id(app_id: str = "Sotto.Overlay") -> None:
    """Своё имя в панели задач и в микшере громкости вместо «python.exe»."""
    try:
        shell32.SetCurrentProcessExplicitAppUserModelID(app_id)
    except Exception:  # noqa: BLE001
        pass


# --- окна ---

def _window_pid(hwnd: int) -> int:
    pid = wt.DWORD()
    user32.GetWindowThreadProcessId(wt.HWND(hwnd), ctypes.byref(pid))
    return int(pid.value)


class _ProcessEntry32W(ctypes.Structure):
    _fields_ = [("dwSize", wt.DWORD), ("cntUsage", wt.DWORD), ("th32ProcessID", wt.DWORD),
                ("th32DefaultHeapID", ctypes.c_size_t), ("th32ModuleID", wt.DWORD),
                ("cntThreads", wt.DWORD), ("th32ParentProcessID", wt.DWORD),
                ("pcPriClassBase", ctypes.c_long), ("dwFlags", wt.DWORD),
                ("szExeFile", ctypes.c_wchar * 260)]


kernel32.CreateToolhelp32Snapshot.argtypes = [wt.DWORD, wt.DWORD]
kernel32.CreateToolhelp32Snapshot.restype = wt.HANDLE
kernel32.Process32FirstW.argtypes = [wt.HANDLE, ctypes.POINTER(_ProcessEntry32W)]
kernel32.Process32FirstW.restype = wt.BOOL
kernel32.Process32NextW.argtypes = [wt.HANDLE, ctypes.POINTER(_ProcessEntry32W)]
kernel32.Process32NextW.restype = wt.BOOL

TH32CS_SNAPPROCESS = 0x00000002
INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value


def process_tree(pid: int) -> set[int]:
    """Процесс и все его потомки.

    `python -m sotto overlay` может оказаться запускающим процессом: venv-питон
    (и pythonw.exe в автозапуске) перезапускает настоящий интерпретатор отдельным
    процессом, и окно принадлежит уже внуку. Без этого окно оверлея не находилось
    и не скрывалось из демонстрации."""
    children: dict[int, list[int]] = {}
    snapshot = kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if snapshot and snapshot != INVALID_HANDLE_VALUE:
        try:
            entry = _ProcessEntry32W()
            entry.dwSize = ctypes.sizeof(_ProcessEntry32W)
            if kernel32.Process32FirstW(snapshot, ctypes.byref(entry)):
                while True:
                    children.setdefault(int(entry.th32ParentProcessID), []).append(int(entry.th32ProcessID))
                    if not kernel32.Process32NextW(snapshot, ctypes.byref(entry)):
                        break
        finally:
            kernel32.CloseHandle(snapshot)
    tree, queue = {pid}, [pid]
    while queue:
        for child in children.get(queue.pop(), []):
            if child not in tree:
                tree.add(child)
                queue.append(child)
    return tree


def windows_of_process(pid: int, visible_only: bool = True) -> list[int]:
    """Видимые окна верхнего уровня процесса и его потомков (для оверлея — ровно одно)."""
    tree = process_tree(pid)
    found: list[int] = []

    @ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)
    def _cb(hwnd, _lparam):  # noqa: ANN001
        if _window_pid(hwnd) in tree and (not visible_only or user32.IsWindowVisible(hwnd)):
            found.append(int(hwnd))
        return True

    user32.EnumWindows(_cb, 0)
    return found


OVERLAY_PROP = "SottoOverlayWindow"
user32.GetPropW.argtypes = [wt.HWND, wt.LPCWSTR]
user32.GetPropW.restype = wt.HANDLE
user32.SetPropW.argtypes = [wt.HWND, wt.LPCWSTR, wt.HANDLE]
user32.SetPropW.restype = wt.BOOL


def mark_overlay(hwnd: int) -> None:
    """Пометить окно оверлея свойством: демон находит его даже через процесс-посредник."""
    if hwnd:
        user32.SetPropW(wt.HWND(int(hwnd)), OVERLAY_PROP, 1)


def windows_marked_as_overlay() -> list[int]:
    found: list[int] = []

    @ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)
    def _cb(hwnd, _lparam):  # noqa: ANN001
        if user32.GetPropW(hwnd, OVERLAY_PROP):
            found.append(int(hwnd))
        return True

    user32.EnumWindows(_cb, 0)
    return found


def overlay_pids() -> list[int]:
    """pid процессов, которым принадлежат окна оверлея (чтобы погасить осиротевшие)."""
    return sorted({_window_pid(hwnd) for hwnd in windows_marked_as_overlay()})


WM_CLOSE = 0x0010
user32.PostMessageW.argtypes = [wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM]
user32.PostMessageW.restype = wt.BOOL


def close_overlay_windows() -> int:
    """Попросить окна оверлея закрыться (WM_CLOSE). Работает и без прав на завершение процесса."""
    closed = 0
    for hwnd in windows_marked_as_overlay():
        if user32.PostMessageW(wt.HWND(hwnd), WM_CLOSE, 0, 0):
            closed += 1
    return closed


def _window_title(hwnd: int) -> str:
    length = user32.GetWindowTextLengthW(wt.HWND(hwnd))
    buf = ctypes.create_unicode_buffer(length + 1)
    user32.GetWindowTextW(wt.HWND(hwnd), buf, length + 1)
    return buf.value


def process_name(pid: int) -> str:
    """Имя exe по pid — «zoom.exe», «chrome.exe»."""
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    h = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not h:
        return ""
    try:
        buf = ctypes.create_unicode_buffer(1024)
        size = wt.DWORD(1024)
        if kernel32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
            return os.path.basename(buf.value)
    finally:
        kernel32.CloseHandle(h)
    return ""


def active_window() -> dict[str, Any]:
    """Активное окно в том же виде, что и в Hyprland: {"class": exe, "title": заголовок}."""
    hwnd = user32.GetForegroundWindow()
    if not hwnd:
        return {}
    pid = _window_pid(hwnd)
    return {"class": process_name(pid) or "?", "title": _window_title(hwnd) or "?", "pid": pid, "hwnd": int(hwnd)}


def overlay_hidden(hwnd: int) -> bool:
    """Скрыто ли окно из захвата экрана. WDA_MONITOR тоже годится: в трансляции чёрный прямоугольник,
    а не подсказка. Поэтому «скрыто» = аффинити не WDA_NONE."""
    if not hwnd:
        return False
    affinity = wt.DWORD(0)
    if not user32.GetWindowDisplayAffinity(wt.HWND(int(hwnd)), ctypes.byref(affinity)):
        return False
    return affinity.value in (WDA_EXCLUDEFROMCAPTURE, WDA_MONITOR)


def hide_overlay(hwnd: int) -> bool:
    """Исключить окно из захвата. Возвращает True, если Windows приняла правило."""
    if not hwnd:
        return False
    for flag in (WDA_EXCLUDEFROMCAPTURE, WDA_MONITOR):
        if user32.SetWindowDisplayAffinity(wt.HWND(int(hwnd)), flag):
            return True
    log.error("SetWindowDisplayAffinity: ошибка %s", ctypes.get_last_error())
    return False


def overlay_geometry(hwnd: int) -> tuple[int, int, int, int] | None:
    """(x, y, ширина, высота) окна — для запоминания положения."""
    if not hwnd:
        return None
    rect = wt.RECT()
    if not user32.GetWindowRect(wt.HWND(int(hwnd)), ctypes.byref(rect)):
        return None
    return rect.left, rect.top, rect.right - rect.left, rect.bottom - rect.top


def primary_monitor_size() -> tuple[int, int]:
    return int(user32.GetSystemMetrics(0)), int(user32.GetSystemMetrics(1))


def make_no_activate(hwnd: int) -> None:
    """Окно не должно брать фокус у окна звонка и не должно попадать в Alt+Tab и на панель задач."""
    if not hwnd:
        return
    ex = user32.GetWindowLongW(wt.HWND(int(hwnd)), GWL_EXSTYLE)
    user32.SetWindowLongW(wt.HWND(int(hwnd)), GWL_EXSTYLE,
                          ex | WS_EX_NOACTIVATE | WS_EX_TOOLWINDOW & ~WS_EX_APPWINDOW)


# --- экран ---

def screen_locked() -> bool:
    """Экран заблокирован (Win+L): писать нечего, микрофона звонка тоже нет."""
    h = user32.OpenInputDesktop(0, False, DESKTOP_SWITCHDESKTOP)
    if not h:
        return True
    user32.CloseDesktop(h)
    return False


def _grab(bbox: dict[str, int] | None):
    """Снимок экрана через mss (без нативных зависимостей). bbox=None — монитор по умолчанию."""
    import mss
    from PIL import Image

    with mss.mss() as sct:
        area = bbox or sct.monitors[1]
        shot = sct.grab(area)
        return Image.frombytes("RGB", shot.size, shot.bgra, "raw", "BGRX")


def grab_rect(left: int, top: int, width: int, height: int):
    """Снимок прямоугольника экрана (Pillow Image)."""
    return _grab({"left": int(left), "top": int(top), "width": int(width), "height": int(height)})


def screenshot(dest: Path, output: str | None = None, scale: float = 0.75) -> Path | None:
    """Снимок монитора в JPEG (output — номер монитора, 1 — основной)."""
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    image = _grab(_monitor_bbox(output))
    if scale and scale < 1.0:
        w, h = image.size
        image = image.resize((max(1, int(w * scale)), max(1, int(h * scale))))
    image.convert("RGB").save(dest, "JPEG", quality=85)
    return dest


def _monitor_bbox(output: str | None) -> dict[str, int] | None:
    """output — номер монитора (1 — основной). Имя монитора на Windows нам не нужно."""
    if not output:
        return None
    try:
        index = int(str(output).strip())
    except ValueError:
        return None
    import mss

    with mss.mss() as sct:
        if 1 <= index < len(sct.monitors):
            return sct.monitors[index]
    return None


# --- глобальные хоткеи ---

class Hotkeys:
    """Глобальные сочетания клавиш Windows. Один поток с циклом сообщений; сочетания
    перерегистрируются целиком при смене настроек."""

    def __init__(self) -> None:
        self._thread: threading.Thread | None = None
        self._thread_id: int | None = None
        self._binds: dict[int, tuple[int, int, str]] = {}   # id → (mods, vk, action)
        self._lock = threading.Lock()
        self._ready = threading.Event()
        self._actions: dict[str, str] = {}
        self._accepted_keys: list[str] = []
        self._on_action: Callable[[str], None] | None = None
        self._stop = threading.Event()

    def bind(self, binds: dict[str, str], on_action: Callable[[str], None]) -> list[str]:
        """binds: {"SUPER + A": "ask"}. Возвращает список принятых сочетаний."""
        self._on_action = on_action
        self._actions = {keys: action for keys, action in binds.items() if keys}
        if self._thread is None:
            self._stop.clear()
            self._thread = threading.Thread(target=self._run, name="win-hotkeys", daemon=True)
            self._thread.start()
            self._ready.wait(5)
        else:
            self._reregister()
            time.sleep(0.2)  # поток успевает перерегистрировать
        return list(self._accepted_keys)

    def _reregister(self) -> None:
        with self._lock:
            self._binds = {}
            # Перерегистрация — в потоке хоткеев: RegisterHotKey привязан к очереди сообщений потока.
            user32.PostThreadMessageW(self._thread_id or 0, WM_REREGISTER, 0, 0)

    def _run(self) -> None:
        self._thread_id = kernel32.GetCurrentThreadId()
        msg = wt.MSG()
        user32.PeekMessageW(ctypes.byref(msg), None, 0, 0, 1)  # создать очередь сообщений потока
        self._accepted_keys = []
        self._register_all()
        self._ready.set()
        while not self._stop.is_set():
            got = user32.GetMessageW(ctypes.byref(msg), None, 0, 0)
            if got in (0, -1):
                break
            if msg.message == WM_REREGISTER:  # внутренний сигнал «перерегистрировать»
                self._register_all()
                continue
            if msg.message == WM_HOTKEY:
                entry = self._binds.get(int(msg.wParam))
                if entry and self._on_action:
                    try:
                        self._on_action(entry[2])
                    except Exception:  # noqa: BLE001
                        log.exception("Ошибка обработчика хоткея %s", entry[2])

    def _register_all(self) -> None:
        for hotkey_id in list(self._binds):
            user32.UnregisterHotKey(None, hotkey_id)
        self._binds = {}
        accepted = []
        for index, (keys, action) in enumerate(self._actions.items()):
            parsed = parse_hotkey(keys)
            if not parsed:
                continue
            mods, vk = parsed
            hotkey_id = 0x5000 + index
            if user32.RegisterHotKey(None, hotkey_id, mods, vk):
                self._binds[hotkey_id] = (mods, vk, action)
                accepted.append(keys)
            else:
                log.warning("Хоткей «%s» занят другой программой", keys)
        self._accepted_keys = accepted
        log.info("Хоткеи Windows: %s", ", ".join(accepted) or "нет")

    def close(self) -> None:
        """Снять все сочетания и остановить поток с циклом сообщений.

        UnregisterHotKey действует в потоке, который регистрировал сочетания, поэтому
        очищаем список и просим поток перерегистрировать пустой набор."""
        self._stop.set()
        self._actions = {}
        if self._thread_id:
            user32.PostThreadMessageW(self._thread_id, WM_REREGISTER, 0, 0)
            user32.PostThreadMessageW(self._thread_id, WM_QUIT, 0, 0)
        if self._thread:
            self._thread.join(timeout=2)
        self._thread = None
        self._binds = {}
        self._accepted_keys = []


# --- микрофон: кто его держит ---

def _decode_reg_name(name: str) -> str:
    """Ключ реестра хранит путь через «#»: C:#Program Files#Zoom#bin#Zoom.exe."""
    if "#" in name:
        return name.replace("#", "\\")
    return name


def microphone_users(ignore: list[str]) -> list[str]:
    """Приложения, которые прямо сейчас пишут с микрофона.

    Windows ведёт ConsentStore: у каждого приложения LastUsedTimeStart/LastUsedTimeStop.
    Start > 0 и Stop == 0 — приложение держит микрофон. Обновляется с задержкой в пару секунд,
    поэтому «звонок начался» определяется через start_after_s, как и раньше.
    """
    ignore_l = [i.lower() for i in ignore]
    users: list[str] = []
    for sub in ("NonPackaged", ""):
        path = MIC_CONSENT + ("\\" + sub if sub else "")
        try:
            key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, path)
        except FileNotFoundError:
            continue
        try:
            count = winreg.QueryInfoKey(key)[0]
            for index in range(count):
                try:
                    name = winreg.EnumKey(key, index)
                except OSError:
                    continue
                try:
                    with winreg.OpenKey(key, name) as app:
                        start = int(winreg.QueryValueEx(app, "LastUsedTimeStart")[0])
                        stop = int(winreg.QueryValueEx(app, "LastUsedTimeStop")[0])
                except (FileNotFoundError, OSError, ValueError):
                    continue
                if start <= 0 or stop != 0:
                    continue
                label = os.path.basename(_decode_reg_name(name)) or name
                haystack = (label + " " + name).lower()
                if any(i in haystack for i in ignore_l):
                    continue
                users.append(label)
        finally:
            winreg.CloseKey(key)
    return users


# --- буфер обмена, запуск программ ---

GMEM_MOVEABLE = 0x0002

# Прототипы обязательны: без них ctypes режет 64-битные дескрипторы (OverflowError).
user32.OpenClipboard.argtypes = [wt.HWND]
user32.OpenClipboard.restype = wt.BOOL
user32.EmptyClipboard.restype = wt.BOOL
user32.CloseClipboard.restype = wt.BOOL
user32.SetClipboardData.argtypes = [wt.UINT, wt.HANDLE]
user32.SetClipboardData.restype = wt.HANDLE
user32.GetClipboardData.argtypes = [wt.UINT]
user32.GetClipboardData.restype = wt.HANDLE
kernel32.GlobalAlloc.argtypes = [wt.UINT, ctypes.c_size_t]
kernel32.GlobalAlloc.restype = wt.HANDLE
kernel32.GlobalLock.argtypes = [wt.HANDLE]
kernel32.GlobalLock.restype = ctypes.c_void_p
kernel32.GlobalUnlock.argtypes = [wt.HANDLE]
kernel32.GlobalUnlock.restype = wt.BOOL


def copy_text(text: str) -> None:
    """Положить текст в буфер обмена (аналог wl-copy)."""
    for _ in range(10):
        if user32.OpenClipboard(None):
            break
        time.sleep(0.05)
    else:
        raise OSError("буфер обмена занят другой программой")
    try:
        user32.EmptyClipboard()
        data = text.encode("utf-16-le") + b"\x00\x00"
        handle = kernel32.GlobalAlloc(GMEM_MOVEABLE, len(data))
        if not handle:
            raise OSError("GlobalAlloc")
        pointer = kernel32.GlobalLock(handle)
        if not pointer:
            raise OSError("GlobalLock")
        ctypes.memmove(pointer, data, len(data))
        kernel32.GlobalUnlock(handle)
        if not user32.SetClipboardData(CF_UNICODETEXT, handle):
            raise OSError("SetClipboardData")
    finally:
        user32.CloseClipboard()


def paste_text() -> str:
    """Прочитать буфер обмена (аналог wl-paste)."""
    for _ in range(10):
        if user32.OpenClipboard(None):
            break
        time.sleep(0.05)
    else:
        return ""
    try:
        handle = user32.GetClipboardData(CF_UNICODETEXT)
        if not handle:
            return ""
        pointer = kernel32.GlobalLock(handle)
        if not pointer:
            return ""
        try:
            return ctypes.wstring_at(pointer)
        finally:
            kernel32.GlobalUnlock(handle)
    finally:
        user32.CloseClipboard()


def open_path(path: str | Path, at: float = 0) -> None:
    """Открыть файл программой по умолчанию. at — секунда, с которой начать (если умеет)."""
    path = str(path)
    if at and path.lower().endswith((".mkv", ".mp4", ".webm", ".mov", ".avi")):
        for player in (["mpv", f"--start={at:.0f}"], ["vlc", f"--start-time={at:.0f}"],
                       ["ffplay", "-ss", f"{at:.0f}"]):
            try:
                subprocess.Popen([*player, path], stdout=subprocess.DEVNULL,
                                 stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL,
                                 **_proc.DETACHED)
                return
            except FileNotFoundError:
                continue
    os.startfile(path)  # noqa: S606 — Windows сам выберет программу


def notify(title: str, body: str) -> None:
    """Всплывающее уведомление Windows. Центр уведомлений требует COM, поэтому — balloon
    через PowerShell без внешних модулей; молча пропускаем, если не вышло."""
    script = (
        "[reflection.assembly]::LoadWithPartialName('System.Windows.Forms') | Out-Null;"
        "[reflection.assembly]::LoadWithPartialName('System.Drawing') | Out-Null;"
        "$n = New-Object System.Windows.Forms.NotifyIcon;"
        "$n.Icon = [System.Drawing.SystemIcons]::Information;"
        "$n.Visible = $true;"
        f"$n.ShowBalloonTip(8000, {_ps_str(title)}, {_ps_str(body)}, "
        "[System.Windows.Forms.ToolTipIcon]::None);"
        "Start-Sleep -Seconds 9; $n.Dispose()"
    )
    try:
        subprocess.Popen(["powershell", "-NoProfile", "-WindowStyle", "Hidden", "-Command", script],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         **_proc.CHILD)
    except OSError:
        log.debug("Не удалось показать уведомление")


def _ps_str(text: str) -> str:
    return "'" + text.replace("'", "''")[:300] + "'"


# --- автозапуск ---

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
RUN_VALUE = "Sotto"


def autostart_command() -> list[str]:
    """Команда автозапуска: pythonw без консоли (иначе при входе в систему висит чёрное окно).
    Запускаем модуль с явным путём к пакету — это работает и без `pip install`, и из исходников."""
    python = Path(sys.executable)
    pythonw = python.with_name("pythonw.exe")
    exe = pythonw if pythonw.exists() else python
    root = Path(__file__).resolve().parent.parent.parent.parent
    code = (f"import sys; sys.path.insert(0, r\"{root}\"); "
            "from sotto.interfaces.cli.main import main; sys.exit(main(['run']))")
    return [str(exe), "-c", code]


def autostart_enabled() -> bool:
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
            winreg.QueryValueEx(key, RUN_VALUE)
        return True
    except OSError:  # ключа нет, нет доступа или реестр закрыт политикой
        return False


def set_autostart(enabled: bool) -> bool:
    """Включить/выключить запуск при входе в систему (HKCU\\…\\Run, без прав администратора).
    True — записано/удалено; ошибки реестра вылетают исключением."""
    command = " ".join(f'"{part}"' if " " in part else part for part in autostart_command())
    key = winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE)
    try:
        if enabled:
            winreg.SetValueEx(key, RUN_VALUE, 0, winreg.REG_SZ, command)
        else:
            try:
                winreg.DeleteValue(key, RUN_VALUE)
            except FileNotFoundError:
                pass
    finally:
        winreg.CloseKey(key)
    return True
