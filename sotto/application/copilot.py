"""Движок подсказок: ручной и автоматический режимы, скриншоты, вопросы собеседнику, легенда.

События для оверлея:
  answer.start  {id, kind, slot, model}
  answer.delta  {id, slot, text}            — кусок текста
  answer.done   {id, slot, text, first_token_ms, total_ms, cost, legend}
  answer.skip   {id, slot}                   — модель решила, что отвечать не нужно (авто-режим)
  answer.error  {id, slot, error}
"""

from __future__ import annotations

import asyncio
import base64
import logging
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from .. import paths
from ..config import Config
from ..domain import hints
from ..domain.hints import SKIP, is_backchannel, is_interjection, same_text
from ..domain.legend import strip_marks
from ..domain.transcript import last_question, transcript_message
from .legend import LegendService
from .ports import LLM, CallRepository, Events, Platform, Prompts
from .prompting import system_prompt

log = logging.getLogger(__name__)

AUTO_ASK = "Подскажи ответ на последний вопрос/реплику собеседника."


def since(minutes: int) -> float:
    return time.time() - minutes * 60


class _Stream:
    sent: int = 0
    text: str = ""
    display: str = ""  # что реально показано в оверлее (после обрезки неродного языка)


class _Speculation:
    def __init__(self, text: str, rows: list[dict[str, Any]]):
        self.text = text
        self.rows = rows
        self.task: asyncio.Task | None = None
        self.visible = False
        self.failed = False
        self.revealed = asyncio.Event()
        self.on_reveal: Callable[[], None] | None = None

    def reveal(self) -> None:
        self.visible = True
        self.revealed.set()
        if self.on_reveal:
            self.on_reveal()


class Copilot:
    def __init__(self, cfg: Config, calls: CallRepository, legend: LegendService, prompts: Prompts, bus: Events,
                 llm: LLM, them_speaking: Callable[[], bool], stage_name: Callable[[], str | None],
                 me_speaking: Callable[[], bool] = lambda: False, platform: Platform | None = None):
        self.cfg = cfg
        self.calls = calls
        self.legend = legend
        self.prompts = prompts
        self.platform = platform
        self.bus = bus
        self.llm = llm
        self.them_speaking = them_speaking
        self.me_speaking = me_speaking
        self.stage_name = stage_name
        self.call_id: int | None = None
        self.record_only = False
        self._tasks: dict[str, asyncio.Task] = {}   # slot → текущая генерация
        self._auto_timer: asyncio.Task | None = None
        self._spec: _Speculation | None = None
        self.last_hint = ""  # последняя показанная подсказка — по ней распознаётся речь кандидата

    # --- жизненный цикл звонка ---

    def set_call(self, call_id: int | None) -> None:
        self.call_id = call_id
        self.last_hint = ""
        self.cancel_all()

    @property
    def mode(self) -> str:
        return self.cfg.answer.mode

    def toggle_mode(self) -> str:
        self.cfg.answer.mode = "auto" if self.cfg.answer.mode == "manual" else "manual"
        return self.cfg.answer.mode

    def cancel_all(self) -> None:
        for t in self._tasks.values():
            t.cancel()
        self._tasks.clear()
        self.drop_speculation()
        if self._auto_timer:
            self._auto_timer.cancel()

    # --- триггеры ---

    def speculate(self, text: str) -> None:
        """Собеседник замолчал на 0.2 с. Начинаем подсказку сразу, но не показываем: если через 0.3 с
        реплика закончится тем же текстом — показываем то, что уже пишется (экономит ~1 с), если
        собеседник продолжил — выбрасываем."""
        if (self.mode != "auto" or self.record_only or self.call_id is None or self.cfg.answer.style == "dual"
                or not hints.worth_speculating(text, self.me_speaking())):
            self.drop_speculation()
            return
        if self._spec and same_text(self._spec.text, text):
            return  # уже пишется по тому же тексту
        self.drop_speculation()
        rows = self._recent()
        last = next((r for r in reversed(rows) if r["channel"] == "them"), {})
        rows = rows + [{"channel": "them", "text": text, "started_at": time.time(),
                        "speaker": last.get("speaker"), "speaker_role": last.get("speaker_role")}]
        messages = [{"role": "system", "content": self._system("answer")},
                    {"role": "user", "content": transcript_message(rows, AUTO_ASK)}]
        log.debug("Упреждение: «%s»", text[-60:])
        spec = _Speculation(text, rows)
        spec.task = asyncio.create_task(self._run("main", "auto", self.cfg.models.answer, messages,
                                                  self.cfg.answer.max_tokens, rows, spec=spec))
        self._spec = spec

    def drop_speculation(self) -> None:
        spec, self._spec = self._spec, None
        if spec and not spec.visible and spec.task and not spec.task.done():
            spec.task.cancel()

    def _promote(self, final_text: str) -> bool:
        """Окончательная реплика совпала с предварительной — показываем уже начатую подсказку."""
        spec, self._spec = self._spec, None
        if spec is None or spec.task is None:
            return False
        if spec.task.done() and spec.failed or not same_text(spec.text, final_text):
            log.debug("Упреждение не подошло: «%s» ≠ «%s»", spec.text[-50:], final_text[-50:])
            if not spec.task.done():
                spec.task.cancel()
            return False
        old = self._tasks.get("main")
        if old and not old.done() and old is not spec.task:
            old.cancel()
        self._tasks["main"] = spec.task
        spec.reveal()
        log.debug("Упреждение подошло, подсказка уже пишется")
        return True

    def on_utterance(self, channel: str) -> None:
        """Вызывается после сохранения новой реплики. В авто-режиме ждём паузу и отвечаем."""
        if channel != "them" or self.mode != "auto" or self.record_only or self.call_id is None:
            return
        if self._auto_timer and not self._auto_timer.done():
            self._auto_timer.cancel()
        self._auto_timer = asyncio.create_task(self._auto_after_pause())

    async def _auto_after_pause(self) -> None:
        pause = self.cfg.answer.auto_pause_ms / 1000
        if pause > 0:
            await asyncio.sleep(pause)
        # Собеседник продолжил говорить — дождёмся следующей реплики (она снова запустит таймер).
        if self.them_speaking():
            return
        rows = self._recent()
        last = next((r for r in reversed(rows) if r["channel"] == "them"), None)
        if last and is_backchannel(last["text"]):
            self.drop_speculation()
            return  # «угу», «понятно, спасибо» — отвечать не на что, модель не спрашиваем
        if last and self.me_speaking() and is_interjection(last["text"]):
            self.drop_speculation()
            log.debug("Реплика посреди ответа кандидата, подсказку не трогаю: «%s»", last["text"])
            return
        if last and self._promote(last["text"]):
            return
        await self.answer(kind="auto")

    async def answer(self, kind: str = "manual") -> None:
        if self.record_only:
            self.bus.publish("notice", {"text": "Режим «только запись»: подсказки выключены"})
            return
        rows = self._recent()
        if self.cfg.answer.style == "dual":
            jobs = [("short", "answer_short", self.cfg.models.answer_short, 250),
                    ("detailed", "answer_detailed", self.cfg.models.answer_detailed, self.cfg.answer.max_tokens * 2)]
        elif self.cfg.answer.style == "short":
            # Тезисы вместо готового текста: кандидат говорит своими словами, чтение не палится.
            jobs = [("main", "answer_short", self.cfg.models.answer_short, 250)]
        else:
            jobs = [("main", "answer", self.cfg.models.answer, self.cfg.answer.max_tokens)]
        for slot, prompt_kind, model, max_tokens in jobs:
            ask = AUTO_ASK
            if kind != "auto":  # нажали хоткей — человеку нужна подсказка, «∅» здесь недопустим
                ask += (" Ответ обязателен: если последняя реплика — не вопрос, подскажи, что сказать "
                        "или на какой вопрос из разговора ответить. Символ ∅ не используй.")
            messages = [
                {"role": "system", "content": self._system(prompt_kind)},
                {"role": "user", "content": transcript_message(rows, ask)},
            ]
            self._launch(slot, kind, model, messages, max_tokens, rows)

    async def screen(self, region: bool = False) -> None:
        if self.platform is None:
            return
        stage = self.stage_name()
        output = stage or await asyncio.to_thread(self.platform.focused_output)
        dest = paths.SCREENSHOTS_DIR / f"{time.strftime('%Y%m%d-%H%M%S')}.jpg"
        shot = await asyncio.to_thread(self.platform.screenshot, dest, output, region)
        if shot is None:
            return
        win = await asyncio.to_thread(self.platform.active_window)
        window_info = f"Активное окно: {win.get('class', '?')} — «{win.get('title', '?')}»" if win else ""
        rows = self._recent()
        image = base64.b64encode(Path(shot).read_bytes()).decode()
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": self._system("screen")},
            {"role": "user", "content": [
                {"type": "text", "text": transcript_message(
                    rows, f"{window_info}\nСкриншот экрана кандидата ниже. Помоги с тем, что на нём.")},
                {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{image}"}},
            ]},
        ]
        self._launch("main", "screen", self.cfg.models.vision, messages, 1500, rows)

    async def questions(self) -> None:
        rows = self._recent()
        messages = [
            {"role": "system", "content": self._system("questions")},
            {"role": "user", "content": transcript_message(rows, "Какие вопросы задать собеседнику?")},
        ]
        self._launch("main", "questions", self.cfg.models.answer, messages, 300, rows)

    # --- внутреннее ---

    def _recent(self) -> list[dict[str, Any]]:
        if self.call_id is None:
            return []
        return self.calls.transcript(self.call_id, since=since(self.cfg.answer.context_minutes))

    def _system(self, kind: str) -> str:
        call = self.calls.get(self.call_id) if self.call_id is not None else None
        return system_prompt(self.prompts.load(kind), allow_invention=self.cfg.answer.allow_invention,
                             profile=self.prompts.profile(), legend=self.legend.for_call(self.call_id),
                             brief=call.brief if call else None)

    def reading_hint(self) -> str:
        return strip_marks(self.last_hint) if self.call_id is not None else ""

    def _launch(self, slot: str, kind: str, model: str, messages: list[dict], max_tokens: int,
                rows: list[dict[str, Any]]) -> None:
        old = self._tasks.get(slot)
        if old and not old.done():
            old.cancel()
        self._tasks[slot] = asyncio.create_task(self._run(slot, kind, model, messages, max_tokens, rows))

    async def _run(self, slot: str, kind: str, model: str, messages: list[dict], max_tokens: int,
                   rows: list[dict[str, Any]], spec: _Speculation | None = None) -> None:
        """spec — упреждающая генерация: пока её не раскрыли (spec.reveal), ничего не публикуем,
        текст копится; при раскрытии публикуется всё накопленное и дальше — как обычно."""
        answer_id = f"{int(time.time() * 1000)}-{slot}"
        question = last_question(rows)
        start = {"id": answer_id, "kind": kind, "slot": slot, "model": model,
                 "question": question if kind in ("auto", "manual") else ""}
        state = _Stream()  # сколько символов уже показали; накопленный текст

        def show() -> None:
            text = state.text
            # Пока непонятно, не «∅» ли это, ничего не показываем: иначе затрём прошлый ответ.
            if text.startswith(SKIP) or len(text) < 2 or (spec and not spec.visible):
                return
            # Обрезка неродного языка идёт по ходу стрима, а не в конце: кандидат читает вслух,
            # и текст не должен исчезать с экрана на полуслове. Поэтому показанное только растёт:
            # если модель вышла за лимит, обрезка замораживается на последнем показанном куске.
            trimmed = hints.trim_nonnative(text, self.prompts.profile(), self.cfg.answer.nonnative_word_limit)
            if len(trimmed) >= len(state.display):
                state.display = trimmed
            if len(state.display) > state.sent:
                self.bus.publish("answer.delta", {"id": answer_id, "slot": slot, "text": state.display[state.sent:]})
                state.sent = len(state.display)
                if slot == "main":
                    self.last_hint = state.display

        finished: list[Any] = []

        async def finish(result: Any) -> None:
            if spec and not spec.visible:
                finished.append(result)  # дописали раньше, чем реплика подтвердилась
                await spec.revealed.wait()
            show()
            await self._finish(answer_id, slot, kind, result, rows, shown=state.display.strip())

        if spec:
            def on_reveal() -> None:
                self.bus.publish("answer.start", start)
                show()
            spec.on_reveal = on_reveal
        else:
            self.bus.publish("answer.start", start)
        session = f"sotto-call-{self.call_id}" if self.call_id else None
        fallback = self.cfg.models.answer_fallback
        models = [model] + ([fallback] if fallback and fallback != model and slot == "main" else [])
        for attempt, current in enumerate(models):
            stream = self.llm.stream_chat(current, messages, max_tokens=max_tokens, session_id=session)
            try:
                async for delta, result in stream:
                    if delta:
                        state.text = result.text.lstrip()
                        show()
                    else:
                        await finish(result)
                return
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001 — показываем любую ошибку в оверлее
                if state.sent == 0 and not state.text and attempt + 1 < len(models):
                    log.warning("Модель %s не ответила (%s) — подсказку пишет %s", current, str(e)[:120],
                                models[attempt + 1])
                    continue
                if spec and not spec.visible:
                    spec.failed = True  # окончательная реплика запустит обычную подсказку
                    return
                log.exception("Ошибка генерации")
                self.bus.publish("answer.error", {"id": answer_id, "slot": slot, "error": str(e)[:300]})
                return
            finally:
                await stream.aclose()  # type: ignore[attr-defined]  # оборванная генерация закрывает HTTP-поток сразу

    async def _finish(self, answer_id: str, slot: str, kind: str, result: Any,
                      rows: list[dict[str, Any]], shown: str = "") -> None:
        # shown — текст, который кандидат уже видел и, возможно, читает вслух: финальный текст
        # обязан совпадать с ним, иначе подсказка исчезнет с экрана на полуслове.
        text = shown or hints.trim_nonnative(result.text.strip(), self.prompts.profile(),
                                             self.cfg.answer.nonnative_word_limit)
        if hints.is_skip(text):
            self.bus.publish("answer.skip", {"id": answer_id, "slot": slot})
            return
        row_id = self.calls.add_answer(
            call_id=self.call_id, kind=kind if slot == "main" else slot, model=result.model,
            prompt_tail=last_question(rows), text=text, created_at=time.time(),
            first_token_ms=result.first_token_ms, total_ms=result.total_ms, cost=result.cost)
        legend = self.legend.remember(self.call_id, row_id, text) if kind != "questions" else []
        self.bus.publish("answer.done", {
            "id": answer_id, "slot": slot, "text": text, "first_token_ms": result.first_token_ms,
            "total_ms": result.total_ms, "cost": result.cost, "legend": legend})
