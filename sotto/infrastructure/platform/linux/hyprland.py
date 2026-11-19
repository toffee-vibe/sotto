"""Интеграция с Hyprland (0.55+, Lua-конфиг).

Всё регистрируется на лету через `hyprctl eval`, поэтому руками в конфиг ничего прописывать
не надо (и он может быть read-only из home-manager). После `hyprctl reload` правила
пропадают — демон слушает событие `configreloaded` и применяет их снова.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import socket
import subprocess
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from ...application.ports import WindowManager

log = logging.getLogger(__name__)

OVERLAY_CLASS = "sotto-overlay"
MIRROR_CLASS = "at.yrlf.wl_mirror"


def available() -> bool:
    return bool(os.environ.get("HYPRLAND_INSTANCE_SIGNATURE")) and shutil.which("hyprctl") is not None


def hyprctl(*args: str, json_out: bool = False) -> Any:
    cmd = ["hyprctl", *(["-j"] if json_out else []), *args]
    res = subprocess.run(cmd, capture_output=True, text=True, timeout=5)
    out = res.stdout.strip()
    if json_out:
        return json.loads(out or "null")
    return out


def lua(code: str) -> str:
    out = hyprctl("eval", code)
    if out and out != "ok":
        log.warning("hyprctl eval: %s → %s", code[:120], out)
    return out


def lua_str(s: str) -> str:
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


# --- окно оверлея ---

def apply_overlay_rules(x: int, y: int, w: int, h: int, opacity: float) -> bool:
    """Оверлей: плавает, на всех рабочих столах, НЕ попадает в демонстрацию экрана.
    Фокус не берёт при появлении и при наведении мыши (no_follow_mouse). Правило no_focus ставить
    нельзя: Hyprland тогда вообще не видит окно под курсором — ни перетащить, ни растянуть.
    Возвращает True, только если Hyprland принял правило — иначе оверлей показывать нельзя."""
    ok = lua(
        "hl.window_rule({ name = \"sotto-overlay\", match = { class = \"^" + OVERLAY_CLASS + "$\" }, "
        "float = true, pin = true, no_focus = false, no_follow_mouse = true, no_initial_focus = true, no_screen_share = true, "
        "no_shadow = true, no_blur = false, no_anim = true, border_size = 0, decorate = false, "
        f"size = {{{w}, {h}}}, move = {{{x}, {y}}}, opacity = \"{opacity} override\" }})"
    ) == "ok"
    # Зеркало «сцены» тоже прячем от захвата и не даём ему фокус.
    lua(
        "hl.window_rule({ name = \"sotto-mirror\", match = { class = \"^" + MIRROR_CLASS.replace(".", "\\\\.") + "$\" }, "
        "no_screen_share = true, no_focus = true, no_anim = true })"
    )
    return ok


def overlay_address() -> str | None:
    for c in hyprctl("clients", json_out=True) or []:
        if c.get("class") == OVERLAY_CLASS:
            return c["address"]
    return None


def overlay_geometry() -> tuple[int, int, int, int] | None:
    """(x, y, ширина, высота) окна оверлея относительно его монитора."""
    clients = hyprctl("clients", json_out=True) or []
    win = next((c for c in clients if c.get("class") == OVERLAY_CLASS), None)
    if not win:
        return None
    mon = next((m for m in monitors() if m.get("id") == win.get("monitor")), {"x": 0, "y": 0})
    (x, y), (w, h) = win["at"], win["size"]
    return x - mon["x"], y - mon["y"], w, h


def default_overlay_position(width: int, stage_name: str) -> tuple[int, int]:
    """Сверху по центру основного монитора — там камера ноутбука."""
    m = primary_monitor(stage_name)
    if not m:
        return 24, 24
    logical_w = int(m["width"] / (m.get("scale") or 1))
    return max(0, (logical_w - width) // 2), 44


def focus_window(address: str) -> None:
    hyprctl("dispatch", f"hl.dsp.focus({{window = {lua_str('address:' + address)}}})")


def overlay_hidden(address: str) -> bool:
    """Скрыто ли уже открытое окно оверлея из демонстрации экрана."""
    return hyprctl("getprop", f"address:{address}", "no_screen_share").strip().lower() in ("true", "1")


def hide_window(address: str) -> None:
    hyprctl("setprop", f"address:{address}", "no_screen_share", "1")


MODS = {"SUPER": 64, "WIN": 64, "SHIFT": 1, "CTRL": 4, "CONTROL": 4, "ALT": 8}


def _combo(keys: str) -> tuple[int, str]:
    """«SUPER + SHIFT + H» → (маска модификаторов, клавиша) — как их показывает `hyprctl binds -j`."""
    parts = [p.strip() for p in keys.split("+") if p.strip()]
    mods = sum(MODS.get(p.upper(), 0) for p in parts[:-1])
    return mods, parts[-1].upper() if parts else ""


def _taken() -> set[tuple[int, str]]:
    try:
        return {(int(b.get("modmask", 0)), str(b.get("key", "")).upper()) for b in hyprctl("binds", json_out=True) or []}
    except Exception:  # noqa: BLE001 — не узнали, что занято: ведём себя как раньше (ставим все)
        return set()


def _ours_file() -> Path:
    from .... import paths
    return paths.STATE_DIR / "hyprland-binds.json"


def _ours() -> set[str]:
    try:
        return set(json.loads(_ours_file().read_text()))
    except (OSError, ValueError):
        return set()


def _save_ours(keys: set[str]) -> None:
    _ours_file().parent.mkdir(parents=True, exist_ok=True)
    _ours_file().write_text(json.dumps(sorted(keys)))


def apply_binds(binds: dict[str, str], sotto_cmd: str) -> list[str]:
    """binds: {"SUPER + A": "ask", ...} → вызывает `sotto ctl <action>`.
    Сочетание, которое уже занято чем-то своим у пользователя (а не нами), не трогаем: возвращаем его в списке
    конфликтов. Какие сочетания поставили мы, помним в файле — после сбоя наши старые привязки не считаются чужими."""
    taken, ours = _taken(), _ours()
    conflicts = []
    for keys, action in binds.items():
        if not keys:
            continue
        if _combo(keys) in taken and keys not in ours:
            conflicts.append(keys)
            continue
        lua(f"hl.unbind({lua_str(keys)})")
        lua(f"hl.bind({lua_str(keys)}, hl.dsp.exec_cmd({lua_str(sotto_cmd + ' ctl ' + action)}))")
        ours.add(keys)
    _save_ours(ours)
    return conflicts


def remove_binds(keys: list[str]) -> None:
    """Снимает только наши привязки."""
    ours = _ours()
    for k in keys:
        if k and k in ours:
            lua(f"hl.unbind({lua_str(k)})")
            ours.discard(k)
    _save_ours(ours)


# --- информация об окнах и мониторах ---

def active_window() -> dict[str, Any]:
    try:
        return hyprctl("activewindow", json_out=True) or {}
    except Exception:
        return {}


def monitors() -> list[dict[str, Any]]:
    try:
        return hyprctl("monitors", json_out=True) or []
    except Exception:
        return []


def focused_monitor() -> str:
    for m in monitors():
        if m.get("focused"):
            return m["name"]
    mons = monitors()
    return mons[0]["name"] if mons else ""


def primary_monitor(stage_name: str) -> dict[str, Any] | None:
    real = [m for m in monitors() if m["name"] != stage_name]
    return next((m for m in real if m.get("focused")), real[0] if real else None)


# --- скриншоты ---

def screenshot(dest: Path, output: str | None = None, region: bool = False, scale: float = 0.75) -> Path | None:
    """Снимок монитора (или выделенной области) в JPEG. None — если пользователь отменил выделение."""
    cmd = ["grim", "-t", "jpeg", "-q", "85", "-s", str(scale)]
    if region:
        sel = subprocess.run(["slurp"], capture_output=True, text=True)
        if sel.returncode != 0 or not sel.stdout.strip():
            return None
        cmd += ["-g", sel.stdout.strip()]
    elif output:
        cmd += ["-o", output]
    cmd.append(str(dest))
    subprocess.run(cmd, check=True, timeout=10)
    return dest


# --- «сцена»: виртуальный монитор для демонстрации ---

class Stage:
    """Виртуальный (headless) монитор, который ты показываешь в звонке.

    Сцена ставится справа от основного монитора, а на основном мониторе на весь экран
    открывается её зеркало (wl-mirror). Ты видишь сцену и оверлей поверх, собеседник —
    только сцену. Окна, которые надо показать, перекладываются на рабочий стол сцены.
    """

    WORKSPACE = "name:stage"

    def __init__(self, name: str = "SOTTO-STAGE", mirror_on: str = ""):
        self.name = name
        self.mirror_on = mirror_on  # где показывать зеркало сцены; пусто — основной монитор
        self._mirror: subprocess.Popen | None = None

    def active(self) -> bool:
        return any(m["name"] == self.name for m in monitors())

    def on(self) -> str:
        main = next((m for m in monitors() if m["name"] == self.mirror_on), None) or primary_monitor(self.name)
        if main is None:
            raise RuntimeError("Не нашёл ни одного монитора")
        if not self.active():
            hyprctl("output", "create", "headless", self.name)
            time.sleep(0.5)
        w, h = main["width"], main["height"]
        if main.get("transform", 0) in (1, 3, 5, 7):  # повёрнутый монитор
            w, h = h, w
        # Координаты в Hyprland логические: ширина в пикселях режима делится на масштаб.
        right_edge = main["x"] + round(w / (main.get("scale") or 1))
        lua("hl.monitor({ output = " + lua_str(self.name) + f", mode = \"{w}x{h}@30\", "
            f"position = \"{right_edge}x{main['y']}\", scale = {main['scale']} }})")
        lua("hl.workspace_rule({ workspace = " + lua_str(self.WORKSPACE) + ", monitor = " + lua_str(self.name) + ", persistent = true })")
        hyprctl("dispatch", f"hl.dsp.focus({{workspace = {lua_str(self.WORKSPACE)}}})")
        if shutil.which("wl-mirror"):
            self._mirror = subprocess.Popen(
                ["wl-mirror", "--fullscreen-output", main["name"], "--title", "sotto-stage-mirror", self.name],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
        return (f"Сцена {self.name} включена справа от {main['name']}. "
                f"В звонке показывай монитор «{self.name}». Окна для показа переноси на рабочий стол «stage».")

    def off(self) -> str:
        subprocess.run(["pkill", "-f", f"wl-mirror.*{self.name}"], check=False)
        if self.active():
            # Окна со сцены возвращаем на основной монитор, иначе Hyprland раскидает их сам.
            main = primary_monitor(self.name)
            for c in hyprctl("clients", json_out=True) or []:
                if c.get("workspace", {}).get("name") == "stage" and main:
                    hyprctl("dispatch", f"hl.dsp.window.move({{workspace = {main['activeWorkspace']['id']}, "
                                        f"window = \"address:{c['address']}\", follow = false}})")
            hyprctl("output", "remove", self.name)
        return "Сцена выключена"


# --- события Hyprland (socket2) ---

def listen_events(on_event: Callable[[str, str], None], stop: threading.Event) -> threading.Thread:
    """Читает события Hyprland ("configreloaded", "activewindow>>..."), вызывает on_event(имя, данные)."""
    def run() -> None:
        sig = os.environ.get("HYPRLAND_INSTANCE_SIGNATURE", "")
        runtime = os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")
        path = Path(runtime) / "hypr" / sig / ".socket2.sock"
        while not stop.is_set():
            try:
                with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
                    s.connect(str(path))
                    buf = b""
                    while not stop.is_set():
                        data = s.recv(4096)
                        if not data:
                            break
                        buf += data
                        while b"\n" in buf:
                            line, buf = buf.split(b"\n", 1)
                            name, _, payload = line.decode(errors="ignore").partition(">>")
                            try:
                                on_event(name, payload)
                            except Exception:  # noqa: BLE001
                                log.exception("Обработчик события Hyprland %s", name)
            except OSError:
                time.sleep(2)
    t = threading.Thread(target=run, name="hypr-events", daemon=True)
    t.start()
    return t


class HyprlandWM(WindowManager):
    """Реализация порта WindowManager для Hyprland."""

    def __init__(self, stage_name: str = "SOTTO-STAGE", mirror_on: str = ""):
        self.stage_name = stage_name
        self._stage = Stage(stage_name, mirror_on)

    def available(self) -> bool:
        return available()

    def protect_overlay(self, x: int, y: int, w: int, h: int) -> bool:
        # Прозрачность — только у фона (её рисует сам оверлей), текст остаётся чётким.
        return apply_overlay_rules(x, y, w, h, 1.0)

    def overlay_window(self) -> str | None:
        return overlay_address()

    def is_protected(self, window: str) -> bool:
        return overlay_hidden(window)

    def protect(self, window: str) -> None:
        hide_window(window)

    def overlay_geometry(self) -> tuple[int, int, int, int] | None:
        return overlay_geometry()

    def default_overlay_position(self, width: int) -> tuple[int, int]:
        return default_overlay_position(width, self.stage_name)

    def focus(self, window: str) -> None:
        focus_window(window)

    def bind_hotkeys(self, binds: dict[str, str], command: str) -> list[str]:
        return apply_binds(binds, command)

    def unbind_hotkeys(self, keys: list[str]) -> None:
        remove_binds(keys)

    def listen(self, on_focus: Callable[[str], None], on_reload: Callable[[], None],
               stop: threading.Event) -> None:
        def on_event(name: str, payload: str) -> None:
            if name == "activewindowv2" and payload and payload != ",":
                on_focus(payload if payload.startswith("0x") else "0x" + payload)
            elif name == "configreloaded":
                on_reload()
        listen_events(on_event, stop)

    def stage_active(self) -> bool:
        return available() and self._stage.active()

    def stage_on(self) -> str:
        return self._stage.on()

    def stage_off(self) -> str:
        return self._stage.off()
