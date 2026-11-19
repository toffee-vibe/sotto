"""Экран под Wayland: запись (wf-recorder, аппаратный HEVC через VAAPI), блокировка (hyprlock)."""

from __future__ import annotations

import logging
import shutil
import subprocess
from pathlib import Path
from typing import Any

from ... import process as proc
from ...application.ports import VideoSegment

log = logging.getLogger(__name__)
_codecs: dict[tuple[str, str], list[str]] = {}  # (codec, устройство) → аргументы; проба — раз на настройку


def detect_codec(cfg: Any) -> list[str]:
    """Аргументы кодека для wf-recorder."""
    if cfg.codec in ("auto", "hevc_vaapi") and Path(cfg.vaapi_device).exists():
        probe = subprocess.run(
            ["ffmpeg", "-hide_banner", "-loglevel", "error", "-vaapi_device", cfg.vaapi_device,
             "-f", "lavfi", "-i", "testsrc=size=640x360:rate=2", "-t", "0.5",
             "-vf", "format=nv12,hwupload", "-c:v", "hevc_vaapi", "-f", "null", "-"],
            capture_output=True)
        if probe.returncode == 0:
            return ["-c", "hevc_vaapi", "-d", cfg.vaapi_device, "-p", "qp=34"]
    return ["-c", "libx265", "-p", "crf=30", "-p", "preset=ultrafast"]


def screen_locked() -> bool:
    return subprocess.run(["pgrep", "-x", "hyprlock"], capture_output=True).returncode == 0



def video_available() -> bool:
    return shutil.which("wf-recorder") is not None


class WfRecorder(VideoSegment):
    """Видео экрана: 2 кадра/с вне звонка и 5 во время звонка — экран в основном статичен, этого хватает.
    ~100–150 МБ в час. Если VAAPI нет — libx265 ultrafast на CPU."""

    def __init__(self, path: Path, fps: int, output: str, settings: Any):
        key = (settings.codec, settings.vaapi_device)
        if key not in _codecs:
            _codecs[key] = detect_codec(settings)
            log.info("Запись экрана: %s", " ".join(_codecs[key]))
        cmd = ["wf-recorder", "-r", str(fps), "-D", *_codecs[key], "-f", str(path), "-y"]
        if output:
            cmd += ["-o", output]
        self.proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                     stdin=subprocess.DEVNULL, **proc.CHILD)

    def alive(self) -> bool:
        return self.proc.poll() is None

    def close(self) -> None:
        if self.proc.poll() is None:
            self.proc.send_signal(2)  # SIGINT — wf-recorder корректно закрывает файл
            try:
                self.proc.wait(timeout=15)
            except subprocess.TimeoutExpired:
                self.proc.kill()
