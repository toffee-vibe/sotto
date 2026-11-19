"""OpenAI-совместимый клиент /chat/completions (поток и обычный) — основа провайдеров моделей.

Провайдер наследует ChatAPI и при необходимости переопределяет:
  _body  — поля запроса (у OpenRouter свои: provider.sort, reasoning, session_id)
  _cost  — стоимость из usage (у OpenRouter её считает сервер, у DeepSeek — прайс-таблица)
  transcribe / credits — если провайдер их вообще умеет
"""

from __future__ import annotations

import io
import json
import time
import wave
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

import httpx
import numpy as np

from ...application.ports import LLM, LLMError


class ChatAPIError(LLMError):
    pass


@dataclass
class Completion:
    text: str = ""
    first_token_ms: int | None = None
    total_ms: int = 0
    cost: float | None = None
    model: str = ""
    usage: dict[str, Any] = field(default_factory=dict)


def wav_bytes(samples: np.ndarray, sample_rate: int = 16000) -> bytes:
    """float32 [-1, 1] → WAV (16 bit mono)."""
    pcm = (np.clip(samples, -1.0, 1.0) * 32767).astype(np.int16)
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sample_rate)
        w.writeframes(pcm.tobytes())
    return buf.getvalue()


class ChatAPI(LLM):
    name = "api"

    def __init__(self, api_key: str, base_url: str, extra_headers: dict[str, str] | None = None):
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.http = httpx.AsyncClient(
            timeout=httpx.Timeout(60.0, connect=10.0),
            headers={"Authorization": f"Bearer {api_key}", **(extra_headers or {})},
            http2=False,
        )

    @property
    def configured(self) -> bool:
        return bool(self.api_key)

    def reconfigure(self, api_key: str, base_url: str) -> None:
        """Новые настройки без перезапуска (ключ поменяли на сайте)."""
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.http.headers["Authorization"] = f"Bearer {api_key}"

    async def close(self) -> None:
        await self.http.aclose()

    def _body(self, model: str, messages: list[dict], *, max_tokens: int, temperature: float,
              session_id: str | None, reasoning: bool, stream: bool,
              json_mode: bool = False) -> dict[str, Any]:
        body: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "stream": stream,
        }
        if stream:
            body["stream_options"] = {"include_usage": True}
        if json_mode:
            body["response_format"] = {"type": "json_object"}
        return body

    def _cost(self, model: str, usage: dict[str, Any]) -> float | None:
        return None

    async def stream_chat(self, model: str, messages: list[dict], *, max_tokens: int = 700,
                          temperature: float = 0.4, session_id: str | None = None,
                          reasoning: bool = False) -> AsyncIterator[tuple[str, Completion]]:
        """Отдаёт (кусок_текста, накопленный Completion). Последний кусок — пустая строка с итогом."""
        if not self.configured:
            raise ChatAPIError(f"Не задан ключ {self.name}")
        body = self._body(model, messages, max_tokens=max_tokens, temperature=temperature,
                          session_id=session_id, reasoning=reasoning, stream=True)
        result = Completion(model=model)
        start = time.perf_counter()
        try:
            async for item in self._stream(body, result, start):
                yield item
        except httpx.HTTPError as e:
            raise ChatAPIError(f"сеть: {e!r}") from e

    async def _stream(self, body: dict[str, Any], result: Completion,
                      start: float) -> AsyncIterator[tuple[str, Completion]]:
        async with self.http.stream("POST", f"{self.base_url}/chat/completions", json=body) as resp:
            if resp.status_code != 200:
                raise ChatAPIError(f"{resp.status_code}: {(await resp.aread()).decode(errors='ignore')[:500]}")
            async for line in resp.aiter_lines():
                if not line.startswith("data:"):
                    continue  # комментарии-пинги и пустые строки
                payload = line[5:].strip()
                if payload == "[DONE]":
                    break
                chunk = json.loads(payload)
                if "error" in chunk:
                    raise ChatAPIError(str(chunk["error"]))
                if chunk.get("usage"):
                    result.usage = chunk["usage"]
                    result.cost = self._cost(result.model, result.usage)
                for choice in chunk.get("choices", []):
                    delta = (choice.get("delta") or {}).get("content") or ""
                    if delta:
                        if result.first_token_ms is None:
                            result.first_token_ms = int((time.perf_counter() - start) * 1000)
                        result.text += delta
                        yield delta, result
        result.total_ms = int((time.perf_counter() - start) * 1000)
        yield "", result

    async def chat(self, model: str, messages: list[dict], *, max_tokens: int = 700,
                   temperature: float = 0.2, json_mode: bool = False,
                   reasoning: bool = False) -> Completion:
        """Обычный (не потоковый) запрос — для служебных задач."""
        if not self.configured:
            raise ChatAPIError(f"Не задан ключ {self.name}")
        body = self._body(model, messages, max_tokens=max_tokens, temperature=temperature,
                          session_id=None, reasoning=reasoning, stream=False, json_mode=json_mode)
        start = time.perf_counter()
        try:
            resp = await self.http.post(f"{self.base_url}/chat/completions", json=body)
        except httpx.HTTPError as e:
            raise ChatAPIError(f"сеть: {e!r}") from e
        if resp.status_code != 200:
            raise ChatAPIError(f"{resp.status_code}: {resp.text[:500]}")
        data = resp.json()
        text = data["choices"][0]["message"].get("content") or ""
        usage = data.get("usage") or {}
        return Completion(text=text, total_ms=int((time.perf_counter() - start) * 1000),
                          cost=self._cost(model, usage), model=model, usage=usage)

    async def transcribe(self, model: str, samples: np.ndarray, sample_rate: int = 16000,
                         language: str | None = None) -> tuple[str, str | None, float | None]:
        raise ChatAPIError(f"{self.name} не умеет распознавать речь — нужен OpenRouter или локальный Whisper "
                           "([models].stt / [audio].stt)")

    async def credits(self, max_age: float = 300) -> dict[str, Any] | None:
        return None
