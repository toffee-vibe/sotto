"""Запись экрана на Windows: ffmpeg + ddagrab (DXGI Desktop Duplication).

Аппаратный HEVC, если есть видеокарта (NVENC / QSV / AMF), иначе libx265 ultrafast на CPU.
Как и на Linux: 2 кадра/с вне звонка и 5 во время, ~100–150 МБ в час.
Закрытие файла — буквой «q» в stdin ffmpeg: так индекс дописывается корректно.

Модуль импортируется на любой ОС (win32 API не используется), выбирается только на Windows.
"""

from __future__ import annotations

import logging
import shutil
import subprocess
from pathlib import Path
from typing import Any

from ... import process as proc

log = logging.getLogger(__name__)
_codecs: dict[str, list[str]] = {}  # codec → аргументы; проба — раз на настройку

# Кандидаты по кодекам: сначала видеокарта, потом процессор.
CODECS = {
    "hevc_nvenc": ["-c:v", "hevc_nvenc", "-preset", "p1", "-tune", "ll", "-rc", "vbr", "-cq", "32", "-b:v", "0"],
    "hevc_qsv": ["-c:v", "hevc_qsv", "-preset", "veryfast", "-global_quality", "30"],
    "hevc_amf": ["-c:v", "hevc_amf", "-quality", "speed", "-rc", "cqp", "-qp_i", "30", "-qp_p", "30"],
    "libx265": ["-c:v", "libx265", "-preset", "ultrafast", "-crf", "30"],
}
ORDER = ["hevc_nvenc", "hevc_qsv", "hevc_amf", "libx265"]


def _test_encoder(args: list[str]) -> bool:
    probe = subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-f", "lavfi",
         "-i", "testsrc=size=640x360:rate=2", "-t", "0.4", *args, "-f", "null", "-"],
        capture_output=True, **proc.CHILD)
    return probe.returncode == 0


def detect_codec(cfg: Any) -> list[str]:
    """Аргументы кодека для записи экрана."""
    if cfg.codec in CODECS and cfg.codec != "auto":  # выбрано вручную — проверяем, что работает
        return CODECS[cfg.codec] if _test_encoder(CODECS[cfg.codec]) else CODECS["libx265"]
    for name in ORDER:
        if _test_encoder(CODECS[name]):
            log.info("Кодек записи: %s", name)
            return CODECS[name]
    return CODECS["libx265"]


def video_available() -> bool:
    return shutil.which("ffmpeg") is not None


class DdaGrabRecorder:
    """Видео экрана через ddagrab. output — номер монитора (1 — основной), пусто — основной."""

    def __init__(self, path: Path, fps: int, output: str, settings: Any):
        if settings.codec not in _codecs:
            _codecs[settings.codec] = detect_codec(settings)
            log.info("Запись экрана: %s", " ".join(_codecs[settings.codec]))
        source = f"ddagrab=framerate={fps}"
        if output and str(output).strip().isdigit():  # номер монитора
            source += f":output_idx={int(str(output).strip()) - 1}"
        # ddagrab отдаёт кадры в видеопамяти: hwdownload перекладывает их в обычную память.
        cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i", source,
               "-vf", "hwdownload,format=bgra", *_codecs[settings.codec], "-pix_fmt", "yuv420p",
               "-y", str(path)]
        self.proc = proc.attach(subprocess.Popen(cmd, stdin=subprocess.PIPE,
                                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                                 **proc.CHILD))

    def alive(self) -> bool:
        return self.proc.poll() is None

    def close(self) -> None:
        if self.proc.poll() is None:
            proc.graceful_stop(self.proc, b"q")  # ffmpeg дописывает файл по «q»
            try:
                self.proc.wait(timeout=15)
            except subprocess.TimeoutExpired:
                self.proc.kill()
