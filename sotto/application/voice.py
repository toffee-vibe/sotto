"""Голосовой суфлёр: звук → звонки → реплики → подсказки, запись, оверлей и хоткеи.

Запускается и останавливается с сайта; сайт работает всегда. Всё, что зависит от ОС (захват звука,
кто держит микрофон, окно оверлея, скрытое из демонстрации, хоткеи, запись экрана), — через порт Platform.
"""

from __future__ import annotations

import asyncio
import logging
import sys
import threading
import time
from collections.abc import Callable
from typing import Any

import numpy as np

from ..config import Config
from ..infrastructure.process import sotto_argv, terminate_tree
from .call_detection import CallDetector
from .copilot import Copilot
from .interviews import InterviewService
from .listener import Listener
from .ports import Agents, CallRepository, Events, Meta, Platform
from .postcall import PostCall
from .recording import RecordingBackend, ScreenRecorder
from .tasks import Background
from .topics import TopicService

log = logging.getLogger(__name__)


class VoiceRuntime:
    def __init__(self, cfg: Config, platform: Platform, calls: CallRepository, meta: Meta, bus: Events,
                 listener: Listener, copilot: Copilot, interviews: InterviewService, postcall: PostCall,
                 topics: TopicService, agents: Agents, recordings: Any, recording_backend: RecordingBackend,
                 background: Background, sotto_command: Callable[[], str], with_overlay: bool = True,
                 save_overlay_geometry: Callable[[tuple[int, int, int, int]], None] = lambda geo: None,
                 spawn: Callable[[list[str]], Any] = lambda cmd: None):
        self.cfg = cfg
        self.platform = platform
        self.wm = platform.wm
        self.calls = calls
        self.meta = meta
        self.bus = bus
        self.listener = listener
        self.copilot = copilot
        self.interviews = interviews
        self.postcall = postcall
        self.topics = topics
        self.agents = agents
        self.background = background
        self.sotto_command = sotto_command
        self.with_overlay = with_overlay
        self.save_overlay_geometry = save_overlay_geometry
        self.spawn = spawn                 # запуск процесса оверлея (инфраструктура)
        self.reload_config: Callable[[], None] = self.reload  # корень композиции подставит полный reload
        self._recordings = recordings
        self._recording_backend = recording_backend
        self.loop: asyncio.AbstractEventLoop | None = None
        self.call_id: int | None = None
        self.trainer: Any = None          # TrainerSession, пока идёт голосовая тренировка
        self.active = False               # звук, звонки, запись, оверлей, хоткеи запущены
        self.overlay_visible = True
        self.overlay_allowed = False      # оконный менеджер принял правило «не показывать в демонстрации»
        self._make_capture()
        self._used = False
        self._stop = threading.Event()
        self._overlay_proc: Any = None
        self._overlay_auto_pos: tuple[int, int] | None = None
        self._overlay_window: str | None = None
        self._prev_focus: str | None = None
        self._bound_keys: list[str] = []
        self._stage_cache: tuple[float, bool] = (0.0, False)

    def _make_capture(self) -> None:
        """Потоки захвата одноразовые — после остановки создаются заново."""
        self.recorder = ScreenRecorder(self.cfg.recording, self._recordings, self._recording_backend, self.platform)
        self.capture = self.platform.audio_input(self.cfg.audio.mic, self.cfg.audio.system, self._on_audio)
        self.detector = CallDetector(self.cfg.calls, self.platform.microphone_users,
                                     on_start=self._threadsafe(self.call_started), on_end=self._threadsafe(self.call_ended))

    def _threadsafe(self, fn: Callable[..., None]) -> Callable[..., None]:
        def call(*args: Any) -> None:
            assert self.loop is not None
            self.loop.call_soon_threadsafe(fn, *args)
        return call

    def bind(self, loop: asyncio.AbstractEventLoop) -> None:
        self.loop = loop

    # --- состояние для оверлея и сайта ---

    def status(self) -> dict[str, Any]:
        return {
            "mode": self.cfg.answer.mode,
            "style": self.cfg.answer.style,
            "in_call": self.call_id is not None,
            "call_id": self.call_id,
            "record_only": self.copilot.record_only,
            "recording": self.recorder.segment is not None,
            "stage": self.stage_active(),
            "overlay_visible": self.overlay_visible,
            "api_key": bool(self.cfg.api_key),
            "voice": self.active,
            "panel_only": not self.active,
            "platform": self.platform.name,
        }

    def publish_status(self) -> None:
        self.bus.publish("status", self.status())

    def stage_active(self) -> bool:
        """Сцену включают из другого процесса (sotto stage on) — спрашиваем, но не чаще раза в 5 с."""
        checked_at, active = self._stage_cache
        if time.time() - checked_at > 5:
            active = self.wm.stage_active()
            self._stage_cache = (time.time(), active)
        return active

    # --- звук ---

    def _on_audio(self, channel: str, samples: np.ndarray, ts: float) -> None:
        self.recorder.write_audio(channel, samples)
        self.listener.on_chunk(channel, samples, ts)
        trainer = self.trainer
        if trainer is not None and channel == "me" and not trainer.stopped:
            trainer.on_mic(samples, ts)

    # --- звонки ---

    def call_started(self, app: str) -> None:
        if self.call_id is not None:
            return
        if self.trainer is not None and not self.trainer.stopped:
            self.trainer.stop()  # начался настоящий звонок — тренировку прекращаем
        self.call_id = self.calls.start(app, record_only=self.copilot.record_only)
        pending = self.meta.get_meta("next_call_brief")
        if pending:
            self.calls.update(self.call_id, brief=pending)
            self.meta.set_meta("next_call_brief", "")
        self.interviews.attach_starting_call(self.call_id, time.time())
        self.listener.start_call(self.call_id)
        self.copilot.set_call(self.call_id)
        self.recorder.set_call(True)
        log.info("Звонок #%s (%s)", self.call_id, app)
        self.publish_status()

    def call_ended(self) -> None:
        if self.call_id is None:
            return
        call_id, self.call_id = self.call_id, None
        self.calls.update(call_id, ended_at=time.time())
        self.listener.end_call()
        self.copilot.set_call(None)
        self.recorder.set_call(False)
        self.publish_status()
        self.background.spawn(self.postcall.process(call_id))

    def finish_interrupted_calls(self) -> None:
        """Итоги звонков, которые не успели посчитаться (демон остановили сразу после звонка)."""
        for call_id in self.calls.unfinished_since(time.time() - 7 * 86400, 4):
            log.info("Досчитываю итоги звонка #%s", call_id)
            self.background.spawn(self.postcall.process(call_id))

    # --- действия (хоткеи, сайт, sotto ctl) ---

    async def action(self, name: str) -> dict[str, Any]:
        if name == "ask":
            await self.copilot.answer("manual")
        elif name in ("screen", "screen_region"):
            self.background.spawn(self.copilot.screen(region=name == "screen_region"))
        elif name == "questions":
            await self.copilot.questions()
        elif name == "toggle_mode":
            mode = self.copilot.toggle_mode()
            self.bus.publish("notice", {"text": "Авто-ответы" if mode == "auto" else "Ответы по хоткею"})
        elif name == "toggle_style":
            order = ["single", "short", "dual"]
            styles = {"single": "один ответ", "short": "тезисы", "dual": "короткий + подробный"}
            self.cfg.answer.style = order[(order.index(self.cfg.answer.style) + 1) % len(order)] \
                if self.cfg.answer.style in order else "single"
            self.bus.publish("notice", {"text": f"Стиль ответа: {styles[self.cfg.answer.style]}"})
        elif name == "toggle_record_only":
            self.copilot.record_only = not self.copilot.record_only
            self.bus.publish("notice", {"text": "Только запись" if self.copilot.record_only else "Подсказки включены"})
        elif name == "toggle_call":
            await asyncio.to_thread(self.detector.toggle_manual)
        elif name == "voice_start":
            await self.start()
        elif name == "voice_stop":
            await self.stop()
        elif name == "toggle_training_call":  # тренажёр: звонок, чья легенда не идёт в настоящие
            await asyncio.to_thread(self.detector.toggle_manual, "training")
        elif name == "reload":  # перечитать config.toml (например, после `sotto bench --apply`)
            self.reload_config()
        elif name == "toggle_overlay":
            self.overlay_visible = not self.overlay_visible
        elif name == "overlay_edit":  # оверлей прозрачен для мыши; этот режим даёт его передвинуть
            self.bus.publish("overlay.edit", {})
        elif name in ("scroll_up", "scroll_down", "clear"):
            self.bus.publish("overlay." + name, {})
        else:
            return {"ok": False, "error": f"неизвестное действие {name}"}
        self.publish_status()
        return {"ok": True}

    # --- окна и хоткеи ---

    def setup_desktop(self) -> None:
        """Правило окна оверлея и хоткеи. Без подтверждённой невидимости в демонстрации оверлей не показываем."""
        if not self.wm.available():
            self.overlay_allowed = False
            log.warning("Оконный менеджер не поддержан (%s) — оверлей и хоткеи выключены", self.platform.name)
            self.bus.publish("notice", {"text": "Оверлей выключен: не могу скрыть его из демонстрации на этой системе"})
            return
        o = self.cfg.overlay
        x, y = (o.x, o.y) if o.x >= 0 and o.y >= 0 else self.wm.default_overlay_position(o.width)
        self.overlay_allowed = self.wm.protect_overlay(x, y, o.width, o.height)
        if not self.overlay_allowed:
            self.bus.publish("notice", {"text": "Оконный менеджер не принял правило невидимости — оверлей выключен"})
        window = self.wm.overlay_window()
        if window:  # окно уже открыто (например, после перезагрузки конфига) — скрываем его сразу
            self.wm.protect(window)
        self.wm.unbind_hotkeys(self._bound_keys)  # старые сочетания после смены [hotkeys] не должны работать
        hk = self.cfg.hotkeys
        binds = {getattr(hk, f): f for f in vars(hk)}
        conflicts = self.wm.bind_hotkeys(binds, self.sotto_command())
        self._bound_keys = [k for k in binds if k and k not in conflicts]
        if conflicts:
            names = ", ".join(f"{k} ({binds[k]})" for k in conflicts)
            log.warning("Хоткеи уже заняты в конфиге оконного менеджера, не трогаю: %s", names)
            self.bus.publish("notice", {"text": f"Хоткей занят другим действием, не перехватываю: {names}. "
                                                "Поменяй в config.toml, раздел [hotkeys]"})

    def teardown_desktop(self) -> None:
        if self.wm.available():
            self.wm.unbind_hotkeys(self._bound_keys)

    def _on_focus(self, window: str) -> None:
        # Оверлей взял фокус, потому что его тащили мышью, — возвращаем фокус окну звонка или редактора.
        if window == self._overlay_window:
            prev = self._prev_focus
            if prev:
                threading.Timer(0.8, self.wm.focus, args=(prev,)).start()
        else:
            self._prev_focus = window

    def _on_wm_reload(self) -> None:
        log.info("Оконный менеджер перечитал конфиг — заново регистрирую правила и хоткеи")
        self.setup_desktop()

    def _overlay_argv(self) -> list[str]:
        """Как запустить оверлей. На Windows — модулем текущего интерпретатора: консольный
        sotto.exe открыл бы чёрное окно, а строка «env PYTHONPATH=…» (запуск из nix-shell) там не работает."""
        if sys.platform == "win32":
            return sotto_argv() + ["overlay"]
        return self.sotto_command().split() + ["overlay"]

    def _overlay_watchdog(self) -> None:
        """Держит оверлей запущенным и проверяет, что его окно действительно скрыто из демонстрации."""
        stop = self._stop
        while not stop.wait(3.0):
            running = self._overlay_proc is not None and self._overlay_proc.poll() is None
            if not self.overlay_allowed:
                if running and self._overlay_proc:
                    terminate_tree(self._overlay_proc)
                continue
            if not running:
                cmd = self._overlay_argv()
                self._overlay_proc = self.spawn(cmd)
                continue
            window = self.wm.overlay_window()
            self._overlay_window = window
            if window:
                self._remember_overlay_geometry()
            if window and not self.wm.is_protected(window):
                self.wm.protect(window)
                if not self.wm.is_protected(window):
                    log.error("Окно оверлея не скрыто из демонстрации — закрываю его")
                    self.bus.publish("notice", {"text": "Не удалось скрыть оверлей из демонстрации — он закрыт"})
                    self.overlay_allowed = False
                    if self._overlay_proc:
                        terminate_tree(self._overlay_proc)

    def _remember_overlay_geometry(self) -> None:
        """Окно передвинули или растянули мышью — запоминаем, чтобы в следующий раз оно открылось там же."""
        geo = self.wm.overlay_geometry()
        o = self.cfg.overlay
        if not geo or geo == (o.x, o.y, o.width, o.height):
            return
        if o.x < 0 and geo[2:] == (o.width, o.height) and self._overlay_auto_pos == geo[:2]:
            return  # стоит на позиции по умолчанию — не записываем её как выбранную
        if o.x < 0 and self._overlay_auto_pos is None:
            self._overlay_auto_pos = geo[:2]  # первое появление окна на позиции по умолчанию
            return
        o.x, o.y, o.width, o.height = geo
        try:
            self.save_overlay_geometry(geo)
        except Exception:  # noqa: BLE001 — не критично
            log.exception("Не удалось сохранить положение оверлея")

    def _warm_up_stt(self) -> None:
        """Загрузить Whisper заранее, чтобы первая реплика звонка не ждала ~5 с загрузки модели."""
        from ..infrastructure.audio import local_stt
        try:
            local_stt.warm_up(self.cfg.trainer.stt_model)
            log.info("Распознавание: локальный Whisper (%s)", "GPU" if local_stt.has_cuda() else "CPU")
        except Exception:  # noqa: BLE001
            log.exception("Локальный Whisper не загрузился")

    # --- запуск и остановка ---

    async def start(self) -> None:
        if self.active:
            return
        if self._used:
            self._make_capture()
        self._used = True
        self._stop = threading.Event()
        self.active = True
        self.capture.start()
        self.recorder.start()
        self.detector.start()
        await asyncio.to_thread(self.setup_desktop)
        self.wm.listen(self._on_focus, self._on_wm_reload, self._stop)
        if self.with_overlay:
            threading.Thread(target=self._overlay_watchdog, name="overlay", daemon=True).start()
        if self.listener.use_local_stt():
            threading.Thread(target=self._warm_up_stt, name="stt-warmup", daemon=True).start()
        log.info("Голосовой суфлёр запущен (%s)", self.platform.name)
        self.publish_status()

    async def stop(self) -> None:
        if not self.active:
            return
        self.active = False
        self._stop.set()
        self.capture.stop()
        self.detector.stop()
        self.recorder.stop()  # до конца звонка: иначе граница звонка откроет новый пустой кусок
        if self.call_id is not None:
            self.call_ended()
        if self.trainer is not None and not self.trainer.stopped:
            self.trainer.stop()
        await asyncio.to_thread(self.teardown_desktop)
        if self._overlay_proc and self._overlay_proc.poll() is None:
            terminate_tree(self._overlay_proc)
        log.info("Голосовой суфлёр остановлен")
        self.publish_status()

    def reload(self) -> None:
        if self.active:
            self.setup_desktop()
        self.publish_status()

    # --- голосовой тренер ---

    def start_trainer(self, session: Any) -> dict[str, Any]:
        if self.call_id is not None:
            return {"error": "Идёт звонок — тренер не запускается"}
        if self.trainer is not None and not self.trainer.stopped:
            return {"error": "Тренировка уже идёт"}
        self.trainer = session
        self.background.spawn(session.run())
        return {"ok": True, "topic": session.topic}

    def stop_trainer(self) -> None:
        if self.trainer is not None:
            self.trainer.stop()
