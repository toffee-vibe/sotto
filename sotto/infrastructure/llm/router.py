"""Единая точка входа к моделям: по префиксу «провайдер/модель» — напрямую к провайдеру, если у него
задан ключ ([providers.<имя>] в config.toml или переменная окружения), иначе — через OpenRouter.

Так «deepseek/deepseek-chat» с ключом DeepSeek идёт на api.deepseek.com (дешевле, свой кэш контекста),
а без ключа — на OpenRouter, где этот же идентификатор тоже работает. Меняется только запись модели
в [models], остальной код про провайдеров не знает.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import numpy as np

from ...application.ports import LLM
from ...config import Config
from .chatapi import ChatAPI, Completion
from .deepseek import DeepSeek
from .openrouter import OpenRouter


class RoutedLLM(LLM):
    def __init__(self, cfg: Config):
        self.openrouter = OpenRouter(cfg.api_key, cfg.openrouter.base_url, cfg.openrouter.provider_sort)
        self.providers: dict[str, ChatAPI] = {}
        self._configure_providers(cfg)

    def _configure_providers(self, cfg: Config) -> None:
        key = cfg.deepseek_api_key()
        if key:
            self.providers["deepseek"] = DeepSeek(key, cfg.providers.deepseek.base_url)
        else:
            self.providers.pop("deepseek", None)

    @property
    def configured(self) -> bool:
        return self.openrouter.configured or any(p.configured for p in self.providers.values())

    def _route(self, model: str) -> tuple[ChatAPI, str]:
        vendor, sep, name = model.partition("/")
        client = self.providers.get(vendor) if sep else None
        if client and client.configured:
            return client, name
        return self.openrouter, model

    async def chat(self, model: str, messages: list[dict], **kw: Any) -> Completion:
        client, name = self._route(model)
        return await client.chat(name, messages, **kw)

    async def stream_chat(self, model: str, messages: list[dict],
                          **kw: Any) -> AsyncIterator[tuple[str, Completion]]:
        client, name = self._route(model)
        async for item in client.stream_chat(name, messages, **kw):
            yield item

    async def transcribe(self, model: str, samples: np.ndarray,
                         **kw: Any) -> tuple[str, str | None, float | None]:
        client, name = self._route(model)
        return await client.transcribe(name, samples, **kw)

    async def credits(self, max_age: float = 300) -> dict[str, Any] | None:
        return await self.openrouter.credits(max_age)

    def reconfigure(self, cfg: Config) -> None:
        """Новые настройки без перезапуска (ключи поменяли на сайте)."""
        self.openrouter.reconfigure(cfg.api_key, cfg.openrouter.base_url, cfg.openrouter.provider_sort)
        old = self.providers.get("deepseek")
        key = cfg.deepseek_api_key()
        if old and key:
            old.reconfigure(key, cfg.providers.deepseek.base_url)
        else:
            self._configure_providers(cfg)

    async def close(self) -> None:
        await self.openrouter.close()
        for client in self.providers.values():
            await client.close()
