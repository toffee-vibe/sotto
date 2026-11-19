"""Шина событий демона.

Модули публикуют события (`bus.publish("answer.delta", {...})`), а оверлей и веб-панель
получают их через SSE (/api/events). Публиковать можно из любого потока.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

from ..application.ports import Events


class EventBus(Events):
    def __init__(self) -> None:
        self._subscribers: set[asyncio.Queue] = set()
        self._loop: asyncio.AbstractEventLoop | None = None
        self.last: dict[str, dict[str, Any]] = {}  # последнее событие каждого типа — для новых подписчиков

    def bind(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop

    def publish(self, kind: str, data: dict[str, Any] | None = None) -> None:
        event = {"type": kind, "ts": time.time(), **(data or {})}
        if kind.endswith(".state") or kind == "status":
            self.last[kind] = event
        loop = self._loop
        if loop is None:
            return
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if running is loop:
            self._fanout(event)
        else:
            loop.call_soon_threadsafe(self._fanout, event)

    def _fanout(self, event: dict[str, Any]) -> None:
        for q in list(self._subscribers):
            if q.qsize() > 1000:  # медленный подписчик — не копим бесконечно
                continue
            q.put_nowait(event)

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue()
        for event in self.last.values():
            q.put_nowait(event)
        self._subscribers.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        self._subscribers.discard(q)
