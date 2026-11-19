"""Linux: PipeWire (звук), Hyprland (окна, хоткеи, «сцена»), wf-recorder (экран), grim/slurp (скриншоты),
notify-send (уведомления), wl-paste (буфер обмена)."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from typing import Any

import numpy as np

from ...application.ports import Platform
from . import hyprland, pipewire, screen


class LinuxPlatform(Platform):
    name = "linux-hyprland"

    def __init__(self, stage_name: str = "SOTTO-STAGE", mirror_on: str = ""):
        self.wm = hyprland.HyprlandWM(stage_name, mirror_on)

    def audio_input(self, mic: str, system: str, on_chunk: Any) -> pipewire.AudioCapture:
        return pipewire.AudioCapture(mic, system, on_chunk)

    def microphone_users(self, ignore: list[str]) -> list[str]:
        return pipewire.microphone_users(ignore)

    def play(self, samples: np.ndarray, sample_rate: int, target: str | None = None) -> subprocess.Popen:
        return pipewire.play(samples, sample_rate, target)

    def screen_locked(self) -> bool:
        return screen.screen_locked()

    def screenshot(self, dest: Path, output: str | None = None, region: bool = False) -> Path | None:
        return hyprland.screenshot(dest, output, region)

    def focused_output(self) -> str:
        return hyprland.focused_monitor()

    def active_window(self) -> dict[str, Any]:
        return hyprland.active_window()

    def notify(self, title: str, text: str) -> None:
        if shutil.which("notify-send"):
            subprocess.Popen(["notify-send", "-a", "Sotto", title, text],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def open_media(self, path: Path, offset_s: float = 0) -> None:
        for player in (["mpv", f"--start={offset_s:.0f}"], ["vlc", f"--start-time={offset_s:.0f}"], ["xdg-open"]):
            try:
                subprocess.Popen([*player, str(path)], start_new_session=True,
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                return
            except FileNotFoundError:
                continue

    def clipboard(self) -> str:
        try:
            return subprocess.run(["wl-paste", "--no-newline"], capture_output=True, text=True, timeout=3).stdout
        except (OSError, subprocess.SubprocessError):
            return ""

    def copy_text(self, text: str) -> None:
        try:
            subprocess.run(["wl-copy"], input=text.encode(), check=False, timeout=3)
        except (OSError, subprocess.SubprocessError):
            pass

    def video_available(self) -> bool:
        return screen.video_available()

    def record_screen(self, path: Path, fps: int, output: str, settings: Any) -> screen.WfRecorder:
        return screen.WfRecorder(path, fps, output, settings)
