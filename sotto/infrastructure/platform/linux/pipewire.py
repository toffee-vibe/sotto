"""PipeWire: захват звука, кто держит микрофон (признак звонка), воспроизведение.

Два независимых потока:
* "me"   — микрофон;
* "them" — монитор системного выхода, т.е. всё, что ты слышишь (голос собеседника).

Раздельные каналы дают бесплатную и точную разметку «я / не я».
Используем `pw-record` как подпроцесс: никаких нативных биндингов, легко отлаживать.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import threading
import time
from collections.abc import Callable

import numpy as np

from ... import process as proc
from ...application.ports import AudioInput
from ...audio import CHUNK, SAMPLE_RATE

log = logging.getLogger(__name__)

# callback(канал, отсчёты float32, время начала куска по time.time())
ChunkCallback = Callable[[str, np.ndarray, float], None]


class Stream:
    """Один pw-record, читаемый в отдельном потоке. Перезапускается при падении."""

    def __init__(self, channel: str, target: str, monitor: bool, on_chunk: ChunkCallback):
        self.channel = channel
        self.target = target
        self.monitor = monitor
        self.on_chunk = on_chunk
        self._proc: subprocess.Popen | None = None
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name=f"audio-{channel}", daemon=True)

    def command(self) -> list[str]:
        cmd = ["pw-record", "--raw", "--rate", str(SAMPLE_RATE), "--channels", "1", "--format", "s16"]
        if self.target:  # пусто — устройство по умолчанию (для монитора — выход по умолчанию)
            cmd += ["--target", self.target]
        cmd += ["-P", "{ node.name = sotto-" + self.channel + ", media.role = Production"
               + (", stream.capture.sink = true" if self.monitor else "") + " }",
               "-"]
        return cmd

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._proc and self._proc.poll() is None:
            self._proc.terminate()

    def _run(self) -> None:
        bytes_per_chunk = CHUNK * 2
        while not self._stop.is_set():
            try:
                # Своя сессия: Ctrl+C в терминале не должен убивать захват мимо демона.
                self._proc = subprocess.Popen(self.command(), stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                              **proc.CHILD)
            except FileNotFoundError:
                log.error("pw-record не найден — нужен PipeWire")
                return
            assert self._proc.stdout is not None
            # Время по количеству прочитанных отсчётов: точнее, чем time.time() на каждый кусок.
            t0 = time.time()
            n = 0
            while not self._stop.is_set():
                raw = self._proc.stdout.read(bytes_per_chunk)
                if len(raw) < bytes_per_chunk:
                    break
                samples = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
                ts = t0 + n / SAMPLE_RATE
                n += CHUNK
                # Если накопился дрейф (например, система спала) — пересинхронизируемся.
                if abs(time.time() - (ts + CHUNK / SAMPLE_RATE)) > 2.0:
                    t0, n = time.time() - CHUNK / SAMPLE_RATE, CHUNK
                    ts = t0
                try:
                    self.on_chunk(self.channel, samples, ts)
                except Exception:
                    log.exception("Ошибка обработки аудио (%s)", self.channel)
            if self._proc.poll() is None:
                self._proc.terminate()
            try:
                self._proc.wait(timeout=3)  # иначе остаётся процесс-зомби
            except subprocess.TimeoutExpired:
                self._proc.kill()
            if not self._stop.is_set():
                log.warning("pw-record (%s) завершился, перезапуск через 1 с", self.channel)
                time.sleep(1)


class AudioCapture(AudioInput):
    def __init__(self, mic: str, system: str, on_chunk: ChunkCallback):
        self.streams = [
            Stream("me", mic, monitor=False, on_chunk=on_chunk),
            Stream("them", system, monitor=True, on_chunk=on_chunk),
        ]

    def start(self) -> None:
        for s in self.streams:
            s.start()

    def stop(self) -> None:
        for s in self.streams:
            s.stop()


def microphone_users(ignore: list[str]) -> list[str]:
    """Имена приложений, которые прямо сейчас пишут звук с микрофона (кроме нас и игнор-листа).
    Опрашиваем pw-dump и ищем чужие потоки Stream/Input/Audio в состоянии running."""
    try:
        out = subprocess.run(["pw-dump"], capture_output=True, text=True, timeout=5).stdout
        objects = json.loads(out or "[]")
    except (OSError, subprocess.TimeoutExpired, json.JSONDecodeError):
        return []
    ignore_l = [i.lower() for i in ignore]
    users = []
    for o in objects:
        info = o.get("info") or {}
        props = info.get("props") or {}
        if props.get("media.class") != "Stream/Input/Audio" or info.get("state") != "running":
            continue
        if props.get("stream.capture.sink"):
            continue  # запись системного звука (не микрофона) звонком не считаем
        node = str(props.get("node.name") or "")
        app = str(props.get("application.process.binary") or props.get("application.name") or node)
        if node.startswith("sotto-") or any(i in app.lower() or i in node.lower() for i in ignore_l):
            continue
        users.append(app)
    return users


def play(samples: np.ndarray, sample_rate: int, target: str | None = None) -> subprocess.Popen:
    """Запускает воспроизведение через pw-play и сразу возвращает процесс (terminate() — оборвать)."""
    pcm = (np.clip(samples, -1, 1) * 32767).astype(np.int16).tobytes()
    cmd = ["pw-play", "--raw", "--rate", str(sample_rate), "--channels", "1", "--format", "s16"]
    target = os.environ.get("SOTTO_TTS_TARGET") or target  # тесты: весь синтез — в беззвучный выход
    if target:
        cmd += ["--target", target]
    p = subprocess.Popen([*cmd, "-"], stdin=subprocess.PIPE, **proc.CHILD)

    def feed() -> None:  # pw-play читает со скоростью воспроизведения — пишем в отдельном потоке
        assert p.stdin is not None
        try:
            p.stdin.write(pcm)
            p.stdin.close()
        except (BrokenPipeError, ValueError):
            pass

    threading.Thread(target=feed, daemon=True).start()
    return p
