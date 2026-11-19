"""Движок подсказок с поддельной моделью: авто-режим, пропуск «∅», легенда, отмена старого ответа."""

import asyncio
import time

from sotto import config as config_mod
from sotto.application.copilot import Copilot
from sotto.application.legend import LegendService
from sotto.infrastructure.events import EventBus
from sotto.infrastructure.llm.openrouter import Completion
from sotto.infrastructure.prompts import prompts
from sotto.infrastructure.sqlite.db import DB
from sotto.infrastructure.sqlite.repositories import Repositories


class FakeLLM:
    configured = True

    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = []

    async def stream_chat(self, model, messages, **_):
        self.calls.append(messages)
        text = self.replies.pop(0) if self.replies else "ok"
        res = Completion(model=model)
        for ch in [text[i:i + 5] for i in range(0, len(text), 5)]:
            await asyncio.sleep(0.001)
            res.text += ch
            res.first_token_ms = res.first_token_ms or 10
            yield ch, res
        res.total_ms = 20
        yield "", res


async def collect(bus, seconds=0.3):
    q = bus.subscribe()
    await asyncio.sleep(seconds)
    out = []
    while not q.empty():
        out.append(q.get_nowait())
    return out


async def make(replies, mode="auto"):
    cfg = config_mod.Config()
    cfg.answer.mode = mode
    cfg.answer.auto_pause_ms = 10
    db = DB()
    bus = EventBus()
    bus.bind(asyncio.get_running_loop())
    llm = FakeLLM(replies)
    repos = Repositories(db)
    legend = LegendService(repos.calls, repos.legend, repos.interviews)
    cp = Copilot(cfg, repos.calls, legend, prompts, bus, llm, them_speaking=lambda: False, stage_name=lambda: None)
    db.legend = legend  # тестам удобно: правила легенды — через сервис
    call = db.insert("calls", started_at=time.time())
    db.insert("utterances", call_id=call, channel="them", started_at=time.time(), ended_at=time.time(),
              text="Как обеспечить идемпотентность?")
    cp.set_call(call)
    return cp, db, bus, llm


async def test_auto_answer_saves_legend(isolated_dirs):
    cp, db, bus, llm = await make(["Через ключ идемпотентности. [[В 2023 делал это в платежах]]"])
    q = bus.subscribe()
    cp.on_utterance("them")
    await asyncio.sleep(0.3)
    events = [q.get_nowait() for _ in range(q.qsize())]
    done = [e for e in events if e["type"] == "answer.done"]
    assert done and done[0]["legend"] == ["В 2023 делал это в платежах"]
    call = db.one("SELECT id FROM calls")["id"]
    assert db.legend.for_call(call)[0].claim == "В 2023 делал это в платежах"
    assert db.legend.for_call(None) == []          # в звонок без компании чужая легенда не идёт
    assert db.one("SELECT kind FROM answers")["kind"] == "auto"


async def test_legend_scoped_to_company(isolated_dirs):
    cp, db, bus, llm = await make(["Ответ [[вёл миграцию в Ozon]]"])
    call = db.one("SELECT id FROM calls")["id"]
    db.legend.assign_company(call, "Acme")
    cp.on_utterance("them")
    await asyncio.sleep(0.3)
    same = db.insert("calls", started_at=time.time(), company="acme ")
    other = db.insert("calls", started_at=time.time(), company="Globex")
    training = db.insert("calls", started_at=time.time(), app="training", company="Acme")
    assert [x.claim for x in db.legend.for_call(same)] == ["вёл миграцию в Ozon"]
    assert db.legend.for_call(other) == [] and db.legend.for_call(training) == []
    db.insert("interviews", company="Acme", status="rejected", created_at=time.time())
    assert db.legend.cleanup() == 1 and db.legend.for_call(same) == []


async def test_auto_skip(isolated_dirs):
    cp, db, bus, llm = await make(["∅"])
    q = bus.subscribe()
    cp.on_utterance("them")
    await asyncio.sleep(0.3)
    types = [q.get_nowait()["type"] for _ in range(q.qsize())]
    assert "answer.skip" in types and "answer.delta" not in types
    assert db.one("SELECT COUNT(*) AS n FROM answers")["n"] == 0


async def test_manual_mode_ignores_utterances(isolated_dirs):
    cp, db, bus, llm = await make(["x"], mode="manual")
    cp.on_utterance("them")
    await asyncio.sleep(0.1)
    assert not llm.calls
    await cp.answer("manual")
    await asyncio.sleep(0.2)
    assert len(llm.calls) == 1
    system = llm.calls[0][0]["content"]
    assert "Профиль кандидата" in system


async def test_dual_style(isolated_dirs):
    cp, db, bus, llm = await make(["коротко", "подробно"], mode="manual")
    cp.cfg.answer.style = "dual"
    q = bus.subscribe()
    await cp.answer("manual")
    await asyncio.sleep(0.3)
    slots = {e["slot"] for e in [q.get_nowait() for _ in range(q.qsize())] if e["type"] == "answer.done"}
    assert slots == {"short", "detailed"}


async def test_skip_prefix_never_shown(isolated_dirs):
    cp, db, bus, llm = await make(["∅ это не вопрос"])
    q = bus.subscribe()
    cp.on_utterance("them")
    await asyncio.sleep(0.3)
    types = [q.get_nowait()["type"] for _ in range(q.qsize())]
    assert "answer.delta" not in types and "answer.skip" in types


async def test_manual_answer_forbids_skip(isolated_dirs):
    cp, db, bus, llm = await make(["Ответ"], mode="manual")
    await cp.answer("manual")
    await asyncio.sleep(0.2)
    assert "Ответ обязателен" in llm.calls[0][1]["content"]


def test_backchannel_and_whisper_tail():
    from sotto.domain.hints import is_backchannel
    from sotto.domain.speech import strip_hallucinations
    assert is_backchannel("Угу, понятно.") and is_backchannel("Okay, got it.")
    assert not is_backchannel("Хорошо. Как бы вы обеспечили идемпотентность?")
    assert strip_hallucinations("Что такое MVCC? Продолжение следует...") == "Что такое MVCC?"


def test_unfilled_profile_template_is_empty():
    from sotto.domain.profile import TEMPLATE as PROFILE_TEMPLATE
    from sotto.domain.profile import is_filled
    assert not is_filled(PROFILE_TEMPLATE) and is_filled("Go, 5 лет, платежи")


async def test_speculation_is_hidden_until_confirmed(isolated_dirs):
    """Упреждающая подсказка пишется скрыто и показывается, когда окончательная реплика совпала."""
    cp, db, bus, llm = await make(["Через ключ идемпотентности в заголовке."])
    q = bus.subscribe()
    cp.speculate("А как бы вы сделали повторы платежей безопасными?")
    await asyncio.sleep(0.2)
    seen = []
    while not q.empty():
        seen.append(q.get_nowait()["type"])
    assert not any(t.startswith("answer.") for t in seen), seen  # пока ничего не видно
    db.insert("utterances", call_id=cp.call_id, channel="them", started_at=time.time(), ended_at=time.time(),
              text="А как бы вы сделали повторы платежей безопасными?")
    cp.on_utterance("them")
    events = await collect(bus)
    types = [e["type"] for e in events]
    assert "answer.start" in types and "answer.done" in types
    assert len(llm.calls) == 1  # вторую генерацию не запускали
    assert db.one("SELECT text FROM answers")["text"] == "Через ключ идемпотентности в заголовке."


async def test_speculation_dropped_when_question_changes(isolated_dirs):
    cp, db, bus, llm = await make(["черновик", "настоящий ответ"])
    cp.speculate("Расскажите про ваш опыт с Kafka в продакшене")
    await asyncio.sleep(0.05)
    db.insert("utterances", call_id=cp.call_id, channel="them", started_at=time.time(), ended_at=time.time(),
              text="Расскажите про ваш опыт с Kafka в продакшене и почему не RabbitMQ")
    cp.on_utterance("them")
    await collect(bus)
    assert len(llm.calls) == 2
    assert db.one("SELECT text FROM answers ORDER BY id DESC")["text"] == "настоящий ответ"


async def test_speculation_skips_unfinished_phrase(isolated_dirs):
    cp, db, bus, llm = await make(["x"])
    cp.speculate("Расскажите, как вы")
    cp.speculate("Tell me about the")
    await asyncio.sleep(0.05)
    assert llm.calls == []


async def test_cough_while_candidate_speaks_keeps_hint(isolated_dirs):
    """Собеседник кашлянул посреди ответа кандидата — подсказку, которую он читает, не трогаем."""
    cp, db, bus, llm = await make(["Новая подсказка"])
    cp.me_speaking = lambda: True
    db.insert("utterances", call_id=cp.call_id, channel="them", started_at=time.time() + 1,
              ended_at=time.time() + 1, text="Кхм.")
    cp.on_utterance("them")
    await asyncio.sleep(0.3)
    assert db.one("SELECT COUNT(*) AS n FROM answers")["n"] == 0
    # настоящий вопрос посреди ответа — отвечаем
    db.insert("utterances", call_id=cp.call_id, channel="them", started_at=time.time() + 2,
              ended_at=time.time() + 2, text="А в чём была проблема?")
    cp.on_utterance("them")
    await asyncio.sleep(0.3)
    assert db.one("SELECT COUNT(*) AS n FROM answers")["n"] == 1


class _ProfilePrompts:
    """prompts с подменённым профилем (родной язык для обрезки неродного)."""

    def __init__(self, base, profile):
        self._base, self._profile = base, profile

    def load(self, kind):
        return self._base.load(kind)

    def profile(self):
        return self._profile


async def test_short_style(isolated_dirs):
    """Режим тезисов: один слот main с промптом answer_short."""
    cp, db, bus, llm = await make(["ключ идемпотентности\nповтор безопасен"], mode="manual")
    cp.cfg.answer.style = "short"
    q = bus.subscribe()
    await cp.answer("manual")
    await asyncio.sleep(0.3)
    events = [q.get_nowait() for _ in range(q.qsize())]
    dones = [e for e in events if e["type"] == "answer.done"]
    assert [e["slot"] for e in dones] == ["main"]
    assert "ТЕЗИСАМИ" in llm.calls[0][0]["content"]


async def test_nonnative_trim_never_shrinks_on_screen(isolated_dirs):
    """Обрезка неродного языка идёт по ходу стрима: финальный текст совпадает с показанным,
    показанное никогда не откатывается назад (кандидат читает вслух)."""
    reply = ("I kept a record of every removed permission so the sync could replay safely. "
             "Then the worker retried the failed rows one by one. After that we alerted on lag.")
    cp, db, bus, llm = await make([reply], mode="manual")
    cp.prompts = _ProfilePrompts(prompts, "Русский — родной\nGo, 5 лет")
    cp.cfg.answer.nonnative_word_limit = 20
    q = bus.subscribe()
    await cp.answer("manual")
    await asyncio.sleep(0.4)
    events = [q.get_nowait() for _ in range(q.qsize())]
    deltas = [e["text"] for e in events if e["type"] == "answer.delta"]
    shown = "".join(deltas)
    done = [e for e in events if e["type"] == "answer.done"]
    assert done and done[0]["text"] == shown  # финал не откатил то, что уже читали с экрана
    assert "alerted on lag" not in shown  # хвост за лимитом на экран не попал


async def test_trainer_prefers_fast_llm(isolated_dirs):
    """Тренер с настроенным LLM не зовёт CLI-агента; при сбое LLM — откат на агента."""
    from sotto.application.trainer import TrainerSession

    class AgentsFail:
        async def run_json(self, prompt, **kw):
            raise AssertionError("CLI-агент не нужен, когда LLM ответил")

    class ChatLLM:
        configured = True

        def __init__(self, text=None, fail=False):
            self.text, self.fail = text, fail

        async def chat(self, model, messages, **kw):
            if self.fail:
                raise RuntimeError("api down")
            from sotto.infrastructure.llm.chatapi import Completion
            return Completion(model=model, text=self.text)

    class AgentsOK:
        async def run_json(self, prompt, **kw):
            return {"say": "from agent"}, "claude"

    cfg = config_mod.Config()
    db = DB()
    repos = Repositories(db)
    bus = EventBus()
    loop = asyncio.get_running_loop()
    bus.bind(loop)
    ts = TrainerSession(cfg, repos.trainer, AgentsFail(), prompts, bus, loop, "knowledge", "postgres",
                        llm=ChatLLM('{"say": "Hi", "done": true}'))
    assert await ts._ask_coach("prompt") == {"say": "Hi", "done": True}
    ts = TrainerSession(cfg, repos.trainer, AgentsOK(), prompts, bus, loop, "knowledge", "postgres",
                        llm=ChatLLM(fail=True))
    assert await ts._ask_coach("prompt") == {"say": "from agent"}
