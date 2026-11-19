"""Локальный синтез речи (Piper через sherpa-onnx). Воспроизведение — через платформу (своя на каждой ОС)."""

from __future__ import annotations

import re
from functools import lru_cache

import numpy as np
import sherpa_onnx

from . import models

CYRILLIC = re.compile(r"[а-яё]", re.I)


@lru_cache(maxsize=4)
def _engine(voice: str) -> sherpa_onnx.OfflineTts:
    d = models.piper_voice(voice)
    cfg = sherpa_onnx.OfflineTtsConfig(
        model=sherpa_onnx.OfflineTtsModelConfig(
            vits=sherpa_onnx.OfflineTtsVitsModelConfig(
                model=str(d / f"{voice}.onnx"), tokens=str(d / "tokens.txt"),
                data_dir=str(d / "espeak-ng-data")),
            num_threads=4),
        max_num_sentences=2)
    return sherpa_onnx.OfflineTts(cfg)


def pick_voice(text: str, voice_ru: str, voice_en: str) -> str:
    return voice_ru if CYRILLIC.search(text) else voice_en


def synthesize(text: str, voice: str, speed: float = 1.0) -> tuple[np.ndarray, int]:
    audio = _engine(voice).generate(text, sid=0, speed=speed)
    samples = np.asarray(audio.samples, dtype=np.float32)
    if samples.size == 0:
        # sherpa-onnx 2.x убрал espeak-фронтенд: Piper-голоса без готовых фонем молчат.
        raise RuntimeError(
            "Озвучка не получилась: установленная версия sherpa-onnx не умеет голоса Piper. "
            "Поставь sherpa-onnx<2:  pip install 'sherpa-onnx<2'")
    return samples, int(audio.sample_rate)


def start_playback(samples: np.ndarray, sample_rate: int, target: str | None = None):
    """Запускает воспроизведение и сразу возвращает процесс (его можно оборвать terminate())."""
    from ..platform import current
    return current().play(samples, sample_rate, target)


def play(samples: np.ndarray, sample_rate: int, target: str | None = None) -> None:
    """Проигрывает звук и ждёт окончания. target — имя выхода PipeWire (по умолчанию системный)."""
    start_playback(samples, sample_rate, target).wait()


def say(text: str, voice: str, target: str | None = None, speed: float = 1.0) -> float:
    """Синтезирует и проигрывает; возвращает длительность в секундах."""
    samples, sr = synthesize(text, voice, speed)
    play(samples, sr, target)
    return len(samples) / sr
