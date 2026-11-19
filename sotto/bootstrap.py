"""Корень композиции: здесь и только здесь собираются реализации (infrastructure) и сценарии (application).

`sotto` запускает сайт (127.0.0.1:8765); голосовой суфлёр запускается на нём или сразу (`sotto run`).
Хоткеи вызывают `sotto ctl <действие>`, оверлей и сайт слушают /api/events.
"""

from __future__ import annotations

import asyncio
import logging
import shutil
import subprocess
import sys
import threading
import time
from typing import Any

import uvicorn

from . import config as config_mod
from . import paths
from .application.calls import CallService
from .application.copilot import Copilot
from .application.interviews import InterviewService
from .application.legend import LegendService
from .application.listener import Listener
from .application.postcall import PostCall
from .application.tasks import Background
from .application.textchat import ChatDeps, TextSessions
from .application.topics import TopicService
from .application.trainer import TrainerSession
from .application.voice import VoiceRuntime
from .config import Config
from .infrastructure import platform as platform_mod
from .infrastructure.events import EventBus
from .infrastructure.llm.agents import Agents
from .infrastructure.llm.router import RoutedLLM
from .infrastructure.media import FfmpegRecording, remove_parts
from .infrastructure.platform.null import NullWM
from .infrastructure.prompts import prompts
from .infrastructure.search import Search
from .infrastructure.sqlite.db import DB
from .infrastructure.sqlite.queries import Queries
from .infrastructure.sqlite.repositories import Repositories
from .jobs.application.pipeline import JobsService
from .jobs.application.resume import ResumeService
from .jobs.infrastructure import JobSources
from .jobs.infrastructure.store import JobsStore

log = logging.getLogger(__name__)


def sotto_command() -> str:
    exe = shutil.which("sotto")
    if exe:
        return exe
    # Запуск из исходников (nix develop): хоткеи оконного менеджера не видят окружение оболочки.
    return f"env PYTHONPATH={paths.PACKAGE_DIR.parent} {sys.executable} -m sotto"


def _save_overlay_geometry(geo: tuple[int, int, int, int]) -> None:
    """Положение оверлея, которое выбрали мышью, — в config.toml (перечитываем файл, чтобы не затереть правки)."""
    fresh = config_mod.load()
    fresh.overlay.x, fresh.overlay.y, fresh.overlay.width, fresh.overlay.height = geo
    config_mod.save(fresh)


def _spawn(cmd: list[str]) -> subprocess.Popen:
    from .infrastructure import process as proc
    # Оверлей со своим окном: на Windows — без start_new_session (там его нет) и с PYTHONPATH
    # для запуска из исходников; на POSIX — своя сессия, как раньше.
    return subprocess.Popen(cmd, env=proc.child_env() if proc.WINDOWS else None, **proc.DETACHED)


class Sotto:
    """Собранное приложение. Сайт и CLI обращаются к сценариям (атрибуты ниже), а не к базе."""

    def __init__(self, cfg: Config, with_overlay: bool = True, with_desktop: bool = True, platform: Any = None,
                 llm: Any = None):
        paths.ensure_dirs()
        self.cfg = cfg
        self.with_overlay = with_overlay
        self.platform = platform or platform_mod.detect(cfg.stage.name, cfg.stage.mirror_on)
        if not with_desktop:
            self.platform.wm = NullWM()  # ни хоткеев, ни правил окон
        platform_mod.use(self.platform)
        self.db = DB()
        self.repos = Repositories(self.db)
        self.queries = Queries(self.db)
        self.bus = EventBus()
        self.background = Background()
        self.stopping = threading.Event()
        self.prompts = prompts
        self.llm = llm or RoutedLLM(cfg)
        self.agents = Agents(self.db, cfg.agents)
        self.search = Search(self.db)

        r = self.repos
        self.legend = LegendService(r.calls, r.legend, r.interviews)
        self.topics = TopicService(r.trainer, r.interviews, self.queries, r, self.agents, prompts, self.bus,
                                   self.background)
        self.interviews = InterviewService(
            r.interviews, r.calls, r, self.legend, self.topics, self.agents, self.llm, prompts, self.bus,
            self.background, invite_model=lambda: cfg.models.answer,
            notify=lambda title, text: self.platform.notify(title, text) if self.with_overlay else None)
        self.postcall = PostCall(r.calls, self.interviews, self.legend, self.topics, self.agents, prompts,
                                 self.search, self.bus)

        self.listener = Listener(cfg, r.calls, prompts, self.bus, self.llm, on_utterance=self._on_utterance)
        self.copilot = Copilot(cfg, r.calls, self.legend, prompts, self.bus, self.llm,
                               them_speaking=self.listener.them_speaking, me_speaking=self.listener.me_speaking,
                               stage_name=lambda: self.platform.wm.stage_name if self.voice.stage_active() else None,
                               platform=self.platform)
        self.listener.on_early = self.copilot.speculate
        self.listener.on_resume = self.copilot.drop_speculation
        self.listener.reading_hint = self.copilot.reading_hint
        self.voice = VoiceRuntime(cfg, self.platform, r.calls, r, self.bus, self.listener, self.copilot,
                                  self.interviews, self.postcall, self.topics, self.agents, r.recordings,
                                  FfmpegRecording(self.platform), self.background, sotto_command, with_overlay,
                                  save_overlay_geometry=_save_overlay_geometry, spawn=_spawn)
        self.voice.reload_config = self.reload_config
        self.calls = CallService(r.calls, r.recordings, r, self.search, self.bus, self.platform,
                                 current_call=lambda: self.voice.call_id, recording_now=lambda: self.voice.recorder.current(),
                                 remove_files=remove_parts)
        self.jobs_repo = JobsStore(self.db)
        self.resume = ResumeService(self.jobs_repo, self.agents, prompts, legend=lambda: self.legend.for_call(None))
        self.jobs = JobsService(cfg, self.jobs_repo, JobSources(cfg, self.jobs_repo), self.resume, self.agents,
                                prompts, self.search, self.bus)
        self.chats = TextSessions(ChatDeps(cfg, r.calls, self.legend, prompts, self.llm, self.background,
                                           process_call=self.postcall.process, new_bus=self._new_bus))

    def _on_utterance(self, channel: str) -> None:
        self.copilot.on_utterance(channel)

    def _new_bus(self) -> EventBus:
        bus = EventBus()
        bus.bind(asyncio.get_running_loop())
        return bus

    # --- то, что нужно сайту ---

    def status(self) -> dict[str, Any]:
        return self.voice.status()

    def trainer_session(self, kind: str, topic: str, mode: str) -> TrainerSession:
        assert self.voice.loop is not None
        return TrainerSession(self.cfg, self.repos.trainer, self.agents, prompts, self.bus, self.voice.loop,
                              kind, topic.strip() or self.topics.pick(kind), mode, llm=self.llm)

    def reload_config(self) -> None:
        """Перечитать config.toml без перезапуска. Объекты настроек обновляются на месте, поэтому
        запись, определение звонков, агенты и подсказки сразу видят новые значения."""
        config_mod.update_in_place(self.cfg, config_mod.load())
        self.llm.reconfigure(self.cfg)
        self.voice.reload()

    @property
    def url(self) -> str:
        return f"http://{self.cfg.web.host}:{self.cfg.web.port}"

    # --- запуск ---

    async def reminders(self) -> None:
        while not self.stopping.is_set():
            self.interviews.remind(time.time())
            await asyncio.sleep(60)

    async def serve(self, voice: bool = True) -> None:
        """Сайт работает всегда; голосовой суфлёр — если voice или когда его запустят с сайта."""
        from .interfaces.web.app import build_app

        loop = asyncio.get_running_loop()
        self.bus.bind(loop)
        self.listener.bind(loop)
        self.voice.bind(loop)
        self.voice.finish_interrupted_calls()
        self.legend.cleanup()
        self.background.spawn(self.reminders())
        self.topics.refresh_soon()
        if voice:
            await self.voice.start()
        self.voice.publish_status()
        app = build_app(self)
        # Оверлей держит SSE-подключение вечно — без таймаута остановка ждала бы его бесконечно.
        server = uvicorn.Server(uvicorn.Config(app, host=self.cfg.web.host, port=self.cfg.web.port,
                                               log_level="warning", access_log=False, timeout_graceful_shutdown=2))
        log.info("Sotto: %s%s. Ctrl+C — остановить всё", self.url,
                 "" if voice else " (голосовой суфлёр запускается на сайте)")
        try:
            await server.serve()
        finally:
            await self.shutdown()

    async def startup_for_tests(self) -> None:
        """То же, что serve(), но без HTTP-сервера и голоса: тестам нужен только собранный сайт."""
        loop = asyncio.get_running_loop()
        self.bus.bind(loop)
        self.listener.bind(loop)
        self.voice.bind(loop)

    async def shutdown(self) -> None:
        self.stopping.set()
        await self.chats.close_all()
        await self.voice.stop()
        await self.llm.close()


def run(with_overlay: bool = True, with_desktop: bool = True, voice: bool = True, open_browser: bool = False) -> None:
    import webbrowser

    cfg = config_mod.load()
    url = f"http://{cfg.web.host}:{cfg.web.port}"
    if _already_running(url):
        print(f"Sotto уже запущен: {url}")
        if open_browser:
            webbrowser.open(url)
        return
    app = Sotto(cfg, with_overlay=with_overlay, with_desktop=with_desktop)
    if open_browser:
        threading.Timer(1.5, webbrowser.open, args=(url,)).start()
    try:
        asyncio.run(app.serve(voice=voice))
    except KeyboardInterrupt:
        pass  # остановка по Ctrl+C — штатная, без трассировки


def _already_running(url: str) -> bool:
    """Демон уже запущен? Проверяем, что отвечает именно Sotto: системный прокси (VPN) отвечает
    на любой localhost-запрос, и по одному факту ответа мы бы ложно решили, что демон жив."""
    import httpx

    try:
        resp = httpx.get(f"{url}/api/status", timeout=2, trust_env=False)
        return resp.status_code == 200 and "voice" in resp.json()
    except (httpx.HTTPError, ValueError):
        return False
