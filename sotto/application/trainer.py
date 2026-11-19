"""Голосовая тренировка: тренер говорит (локальный синтез), ты отвечаешь голосом (локальное
распознавание), ход разговора и разбор ведёт LLM по API (быстро) или CLI-агент (медленно).
Текст — только как обратная связь.

Два тренера:
  knowledge — собеседование по знаниям (mode=interview) или разбор темы (mode=lesson);
  english   — разговорная практика / собеседование на английском с разбором ошибок.

События: trainer.state {active, kind, topic, phase}, trainer.turn {role, text, feedback}.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

import numpy as np

from ..config import Config
from ..domain import training
from ..infrastructure.audio import local_stt, tts
from ..infrastructure.audio.vad import ChannelVAD
from .ports import LLM, AgentError, Agents, Events, Prompts, TrainerRepository
from .prompting import parse_json_object, render

log = logging.getLogger(__name__)


class TrainerSession:
    def __init__(self, cfg: Config, store: TrainerRepository, agents: Agents, prompts: Prompts, events: Events,
                 loop: asyncio.AbstractEventLoop, kind: str, topic: str, mode: str = "interview",
                 llm: LLM | None = None):
        self.cfg = cfg
        self.store = store
        self.agents = agents
        self.llm = llm
        self.prompts = prompts
        self.events = events
        self.loop = loop
        self.kind = kind
        self.mode = mode
        self.topic = topic
        self.session_id: int = 0  # появится в run()
        self.history: list[dict[str, str]] = []
        self.listening = False
        self.speaking = False
        self.stopped = False
        self._segments: asyncio.Queue = asyncio.Queue()
        self._vad = ChannelVAD("me", min_silence=cfg.trainer.end_of_turn_silence, max_speech=90)
        self.phase = "starting"
        self._speech: Any = None  # воспроизведение, пока тренер говорит — чтобы stop() мог его оборвать

    # --- звук из демона (поток захвата) ---

    def on_mic(self, samples: np.ndarray, ts: float) -> None:
        if not self.listening or self.speaking:
            return
        for seg in self._vad.feed(samples, ts):
            self.loop.call_soon_threadsafe(self._segments.put_nowait, seg)

    # --- жизненный цикл ---

    def state(self) -> dict[str, Any]:
        return {"active": not self.stopped, "kind": self.kind, "mode": self.mode, "topic": self.topic,
                "phase": self.phase, "session_id": self.session_id}

    def _publish_state(self, phase: str) -> None:
        self.phase = phase
        self.events.publish("trainer.state", self.state())

    def stop(self) -> None:
        """Можно вызывать откуда угодно в потоке event loop: прерывает ожидание ответа и озвучку."""
        self.stopped = True
        self.listening = False
        self._segments.put_nowait(None)
        if self._speech is not None:
            self._speech.terminate()

    async def run(self) -> None:
        self.session_id = self.store.start_session(self.kind, self.topic, time.time())
        user_text: str | None = None
        try:
            while not self.stopped:
                self._publish_state("thinking")
                reply = await self._coach_turn(user_text)
                if reply is None or self.stopped:  # пока агент думал, тренировку могли остановить (звонок!)
                    break
                await self._speak(reply.get("say") or "")
                if reply.get("done") or self.stopped:
                    break
                self._publish_state("listening")
                user_text = await self._listen()
                if user_text is None or self.stopped:
                    break
                if training.wants_to_stop(user_text):
                    self.history.append({"role": "user", "text": user_text})
                    user_text = "(пользователь просит закончить)"
        finally:
            await self._finish()

    # --- шаги ---

    async def _coach_turn(self, user_text: str | None) -> dict[str, Any] | None:
        persona = render(self.prompts.load(f"trainer_{self.kind}_{self.mode}"), topic=self.topic)
        dialog = "\n".join(f"{'Тренер' if t['role'] == 'coach' else 'Пользователь'}: {t['text']}" for t in self.history)
        prompt = (f"{persona}\n\nПрофиль пользователя:\n{self.prompts.profile() or '(не заполнен)'}\n\n"
                  f"Разговор до сих пор:\n{dialog or '(ещё не начинали — начни: поприветствуй коротко и задай первый вопрос)'}\n"
                  f"\n{self.prompts.load('trainer_turn')}")
        reply = await self._ask_coach(prompt)
        if reply is None:
            return None
        if user_text is not None and self.history:
            # Разбор относится к последнему ответу пользователя.
            last = self.history[-1]
            last["feedback"] = reply.get("feedback") or ""
            self.store.set_last_feedback(self.session_id, last["feedback"])
            self.events.publish("trainer.feedback", {"session_id": self.session_id, "feedback": last["feedback"],
                                                    "mastery": reply.get("mastery")})
            self._update_items(reply)
        say = (reply.get("say") or "").strip()
        if say:
            self.history.append({"role": "coach", "text": say})
            self.store.add_turn(self.session_id, "coach", say, time.time())
            self.events.publish("trainer.turn", {"session_id": self.session_id, "role": "coach", "text": say})
        return reply

    async def _ask_coach(self, prompt: str) -> dict[str, Any] | None:
        """Ход тренера. С настроенным LLM — быстрый запрос по API (диалог живой: секунда-две на
        реплику); без него — CLI-агент, как раньше (холодный старт claude/codex — 8–20 с тишины)."""
        if self.llm is not None and self.llm.configured:
            try:
                res = await self.llm.chat(self.cfg.models.answer, [{"role": "user", "content": prompt}],
                                          max_tokens=700, json_mode=True)
                reply = parse_json_object(res.text)
                if isinstance(reply, dict):
                    return reply
                log.warning("Тренер: LLM вернул не JSON, пробую CLI-агента")
            except Exception as e:  # noqa: BLE001 — ниже запасной путь через агента
                log.warning("Тренер: LLM не ответил (%s), пробую CLI-агента", str(e)[:120])
        try:
            reply, _agent = await self.agents.run_json(prompt, task=f"тренер {self.kind}")
        except AgentError as e:
            self.events.publish("notice", {"text": f"Тренер: {e}"[:200]})
            return None
        return reply if isinstance(reply, dict) else None

    async def _speak(self, text: str) -> None:
        if not text:
            return
        self._publish_state("speaking")
        self.speaking = True
        voice = self.cfg.trainer.voice_en if self.kind == "english" else tts.pick_voice(
            text, self.cfg.trainer.voice_ru, self.cfg.trainer.voice_en)
        try:
            samples, sr = await asyncio.to_thread(tts.synthesize, text, voice)
            if self.stopped:
                return
            self._speech = tts.start_playback(samples, sr)
            await asyncio.to_thread(self._speech.wait)
        finally:
            self._speech = None
            await asyncio.sleep(0.3)  # хвост эха из колонок
            self._vad.request_reset()
            self.speaking = False

    async def _listen(self) -> str | None:
        # Старые куски (эхо тренера и т.п.) выбрасываем, но сигнал остановки (None) сохраняем.
        while not self._segments.empty():
            if self._segments.get_nowait() is None:
                return None
        if self.stopped:
            return None
        self.listening = True
        parts: list[str] = []
        try:
            while True:
                seg = await self._segments.get()
                if seg is None:
                    return None
                self._publish_state("recognizing")
                lang = "en" if self.kind == "english" else None
                text, _ = await asyncio.to_thread(local_stt.transcribe, seg.samples, self.cfg.trainer.stt_model, lang)
                if text.strip():
                    parts.append(text.strip())
                # Длинный ответ мог разбиться паузой: если ты продолжил говорить — ждём ещё кусок.
                await asyncio.sleep(0.2)
                if self._segments.empty() and not self._vad.speaking:
                    break
                self._publish_state("listening")
        finally:
            self.listening = False
        text = " ".join(parts).strip()
        if not text:
            return None if self.stopped else await self._listen()
        self.history.append({"role": "user", "text": text})
        self.store.add_turn(self.session_id, "user", text, time.time())
        self.events.publish("trainer.turn", {"session_id": self.session_id, "role": "user", "text": text})
        return text

    def _update_items(self, reply: dict[str, Any]) -> None:
        now = time.time()
        mastery = reply.get("mastery")
        if isinstance(mastery, (int, float)):
            self.store.add_items(self.kind, [self.topic], "session", 0)
            row = self.store.item(self.kind, self.topic)
            if row:
                score = training.blend_score(row["score"], row["seen"], float(mastery))
                self.store.update_item(row["id"], score=score, seen=row["seen"] + 1,
                                       due_at=training.next_due(score, now))
        mistakes = [str(m).strip()[:200] for m in reply.get("mistakes") or [] if str(m).strip()]
        self.store.add_items(self.kind, mistakes, "session", now)

    async def _finish(self) -> None:
        self.stopped = True
        self.listening = False
        if self.session_id:
            notes = "\n\n".join(f"**{t['text']}**\n{t.get('feedback', '')}" for t in self.history
                                if t["role"] == "user" and t.get("feedback"))
            self.store.finish_session(self.session_id, time.time(), notes or None)
        self._publish_state("finished")
