"""Мост к CLI-агентам пользователя: claude, codex, opencode, kimi, grok, agy.

Фоновые задачи (итоги звонка, промпты, тренеры, резюме, разбор вакансий) идут через подписки
пользователя, а не через API-ключ. Агенты перебираются по порядку из конфига; если агент упёрся
в лимит или упал — он уходит «остыть», задача уходит следующему.

Добавить нового агента: допиши его в AGENTS (как запустить с промптом и как достать ответ).
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import shutil
import tempfile
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ...application.ports import AgentError
from ...application.ports import Agents as AgentsPort
from ...config import Agents as AgentsCfg
from .. import process as proc_mod
from ..sqlite.db import DB

log = logging.getLogger(__name__)


@dataclass
class AgentSpec:
    name: str
    binary: str
    # (prompt, рабочая папка) → команда. prompt=None — агент читает задание из stdin.
    build: Callable[[str | None, Path], list[str]]
    parse: Callable[[str, Path], str] = lambda out, _wd: out  # stdout → текст ответа
    stdin: bool = False  # умеет читать промпт из stdin (нет лимита длины аргумента)


def _codex_parse(out: str, wd: Path) -> str:
    f = wd / "last.txt"
    return f.read_text(encoding="utf-8") if f.exists() else out


def _json_result(out: str, _wd: Path) -> str:
    """claude/agy с --output-format json возвращают объект с полем result."""
    try:
        data = json.loads(out)
        if isinstance(data, dict):
            return str(data.get("result") or data.get("text") or data.get("response") or out)
    except json.JSONDecodeError:
        pass
    return out


def _arg(p: str | None) -> list[str]:
    return [p] if p is not None else []


AGENTS: dict[str, AgentSpec] = {
    "claude": AgentSpec("claude", "claude", lambda p, wd: ["claude", "-p", *_arg(p), "--output-format", "json"],
                        _json_result, stdin=True),
    "codex": AgentSpec("codex", "codex",
                       lambda p, wd: ["codex", "exec", "--skip-git-repo-check", "--sandbox", "read-only",
                                      "-o", str(wd / "last.txt"), p if p is not None else "-"],
                       _codex_parse, stdin=True),
    "opencode": AgentSpec("opencode", "opencode", lambda p, wd: ["opencode", "run", p or ""]),
    "kimi": AgentSpec("kimi", "kimi", lambda p, wd: ["kimi", "-p", p or ""]),
    "grok": AgentSpec("grok", "grok", lambda p, wd: ["grok", "-p", p or ""]),
    "agy": AgentSpec("agy", "agy", lambda p, wd: ["agy", "-p", p or "", "--output-format", "json"], _json_result),
}

# Linux не пускает один аргумент длиннее 128 КБ. Длинные задания — через stdin или файл.
MAX_ARG_BYTES = 100_000

LIMIT_PATTERNS = re.compile(
    r"(rate.?limit|usage.?limit|quota|too many requests|429|limit reached|exceeded|out of credits|"
    r"try again later|лимит)", re.I)

PREAMBLE = (
    "Ты работаешь как вызываемая из программы функция. Не используй инструменты, не читай и не меняй "
    "файлы, не задавай вопросов — сразу выдай результат строго в запрошенном формате.\n\n"
)
FILE_PREAMBLE = (
    "Ты работаешь как вызываемая из программы функция. Задание слишком длинное для командной строки и лежит "
    "в файле task.md в текущей папке. Прочитай его (это единственное, что можно делать с файлами), не меняй "
    "никаких файлов, не задавай вопросов и выдай результат строго в формате, который там запрошен."
)


class Agents(AgentsPort):
    def __init__(self, db: DB, cfg: AgentsCfg):
        self.db = db
        self.cfg = cfg  # живая ссылка на настройки — изменения из панели применяются сразу

    @property
    def order(self) -> list[str]:
        return self.cfg.order

    def installed(self) -> list[str]:
        return [n for n in self.order if n in AGENTS and shutil.which(AGENTS[n].binary)]

    def status(self) -> list[dict[str, Any]]:
        out = []
        for name in self.order:
            st = self.db.one("SELECT * FROM agent_state WHERE name = ?", [name]) or {}
            out.append({
                "name": name,
                "installed": name in AGENTS and bool(shutil.which(AGENTS[name].binary)),
                "cooling_until": st.get("cooldown_until") if (st.get("cooldown_until") or 0) > time.time() else None,
                "last_error": st.get("last_error"),
                "calls": st.get("calls", 0),
            })
        return out

    def _available(self, prefer: str | None) -> list[str]:
        names = self.installed()
        if prefer in names:
            names.remove(prefer)
            names.insert(0, prefer)
        now = time.time()
        ready = []
        for n in names:
            st = self.db.one("SELECT cooldown_until FROM agent_state WHERE name = ?", [n])
            if not st or (st["cooldown_until"] or 0) <= now:
                ready.append(n)
        return ready

    def _record(self, name: str, error: str | None, cooldown: bool) -> None:
        self.db.execute(
            "INSERT INTO agent_state(name, cooldown_until, last_error, calls) VALUES(?, ?, ?, 1) "
            "ON CONFLICT(name) DO UPDATE SET calls = calls + 1, last_error = excluded.last_error, "
            "cooldown_until = CASE WHEN excluded.cooldown_until > 0 THEN excluded.cooldown_until ELSE cooldown_until END",
            [name, time.time() + self.cfg.cooldown_minutes * 60 if cooldown else 0, error])

    async def _exec(self, spec: AgentSpec, prompt: str, wd: Path) -> tuple[int, str, str]:
        full = PREAMBLE + prompt
        stdin_data: bytes | None = None
        if spec.stdin:
            cmd, stdin_data = spec.build(None, wd), full.encode()
        elif len(full.encode()) > MAX_ARG_BYTES:
            (wd / "task.md").write_text(prompt, encoding="utf-8")
            cmd = spec.build(FILE_PREAMBLE, wd)
        else:
            cmd = spec.build(full, wd)
        proc = await asyncio.create_subprocess_exec(
            *cmd, cwd=wd, stdin=asyncio.subprocess.PIPE if stdin_data is not None else asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            **proc_mod.CHILD)  # Ctrl+C в терминале не должен убивать агента посреди работы
        try:
            out, err = await asyncio.wait_for(proc.communicate(stdin_data), timeout=self.cfg.timeout_s)
        finally:
            if proc.returncode is None:  # таймаут или отмена задачи — не оставляем агента жить
                proc.kill()
                await proc.wait()
        return proc.returncode or 0, out.decode(errors="ignore").strip(), err.decode(errors="ignore").strip()

    async def run(self, prompt: str, *, prefer: str | None = None, task: str = "") -> tuple[str, str]:
        """Выполняет задачу первым доступным агентом. Возвращает (ответ, имя агента)."""
        candidates = self._available(prefer)
        if not candidates:
            raise AgentError("Нет доступных агентов: все не установлены или упёрлись в лимиты")
        errors = []
        for name in candidates:
            spec = AGENTS[name]
            log.info("Агент %s: %s", name, task or prompt[:60])
            with tempfile.TemporaryDirectory(prefix="sotto-agent-") as tmp:
                wd = Path(tmp)
                try:
                    code, stdout, stderr = await self._exec(spec, prompt, wd)
                except TimeoutError:
                    self._record(name, "timeout", cooldown=False)
                    errors.append(f"{name}: таймаут")
                    continue
                except OSError as e:
                    self._record(name, str(e), cooldown=False)
                    errors.append(f"{name}: {e}")
                    continue
                if code != 0 or not stdout:
                    limited = bool(LIMIT_PATTERNS.search(stderr + stdout))
                    self._record(name, (stderr or stdout)[-300:], cooldown=limited or code != 0)
                    errors.append(f"{name}: {'лимит' if limited else 'ошибка'} ({(stderr or stdout)[-120:]})")
                    continue
                text = spec.parse(stdout, wd).strip()
                self._record(name, None, cooldown=False)
                return text, name
        raise AgentError("Все агенты не справились: " + "; ".join(errors))

    async def run_json(self, prompt: str, *, prefer: str | None = None, task: str = "") -> tuple[Any, str]:
        """То же, но ответ — JSON. Если агент вернул мусор, пробуем следующего."""
        last_err = ""
        tried: set[str] = set()
        for _ in range(3):
            available = [n for n in self._available(prefer) if n not in tried]
            if not available:
                break
            text, name = await self.run(prompt, prefer=available[0], task=task)
            tried.add(name)
            try:
                return extract_json(text), name
            except ValueError as e:
                last_err = f"{name}: {e}"
        raise AgentError(f"Не удалось получить JSON: {last_err}")


def extract_json(text: str) -> Any:
    """Достаёт первый JSON-объект/массив из текста (агенты любят обрамлять ответ ```json)."""
    fenced = re.search(r"```(?:json)?\s*(.+?)```", text, re.S)
    if fenced:
        text = fenced.group(1)
    starts = [i for i in (text.find("{"), text.find("[")) if i >= 0]
    if not starts:
        raise ValueError("в ответе нет JSON")
    start = min(starts)
    decoder = json.JSONDecoder()
    obj, _ = decoder.raw_decode(text[start:])
    return obj
