"""Клиент OpenAI-совместимого Chat Completions API (OpenAI, DeepSeek, OpenRouter, Ollama…).

Официальный SDK не используется — он тяжелее, а нужен один эндпоинт.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from dataclasses import dataclass
from typing import Any

import httpx

log = logging.getLogger(__name__)


class LLMError(Exception):
    pass


@dataclass(slots=True)
class LLMReply:
    text: str
    tokens: int


class LLMClient:
    def __init__(
        self,
        base_url: str,
        api_key: str,
        model: str,
        *,
        timeout: float = 60.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.model = model
        self._url = f"{base_url.rstrip('/')}/chat/completions"
        self._client = httpx.AsyncClient(
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=timeout,
            transport=transport,
        )
        self._json_mode_supported = True
        # не больше двух одновременных запросов к модели
        self._sem = asyncio.Semaphore(2)

    async def chat(
        self,
        system: str,
        user: str,
        *,
        max_tokens: int,
        json_mode: bool = False,
        temperature: float = 0.2,
    ) -> LLMReply:
        payload: dict[str, Any] = {
            "model": self.model,
            # системная часть идёт первой и не меняется между вакансиями одного
            # пользователя — провайдеры с префиксным кэшем считают её дешевле
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "max_tokens": max_tokens,
            "temperature": temperature,
        }
        if json_mode and self._json_mode_supported:
            payload["response_format"] = {"type": "json_object"}

        async with self._sem:
            data = await self._post(payload)
            if data is None:  # провайдер не поддерживает response_format
                self._json_mode_supported = False
                payload.pop("response_format", None)
                data = await self._post(payload)
        if data is None:
            raise LLMError("bad request")
        try:
            text = data["choices"][0]["message"]["content"] or ""
        except (KeyError, IndexError, TypeError) as exc:
            raise LLMError(f"unexpected response: {str(data)[:200]}") from exc
        usage = data.get("usage") or {}
        tokens = usage.get("total_tokens") or (
            (usage.get("prompt_tokens") or 0) + (usage.get("completion_tokens") or 0)
        )
        return LLMReply(text=text.strip(), tokens=int(tokens))

    async def _post(self, payload: dict[str, Any]) -> dict[str, Any] | None:
        delay = 2.0
        for attempt in range(3):
            try:
                resp = await self._client.post(self._url, json=payload)
            except httpx.HTTPError as exc:
                if attempt == 2:
                    raise LLMError(f"network: {exc}") from exc
            else:
                if resp.status_code == 200:
                    return resp.json()
                if resp.status_code == 400 and "response_format" in payload:
                    return None
                if resp.status_code not in (429, 500, 502, 503, 504) or attempt == 2:
                    raise LLMError(f"HTTP {resp.status_code}: {resp.text[:300]}")
            await asyncio.sleep(delay)
            delay *= 2
        raise LLMError("retries exhausted")

    async def aclose(self) -> None:
        await self._client.aclose()


_FENCE = re.compile(r"^```(?:json)?\s*|\s*```$", re.MULTILINE)


def parse_json(text: str) -> dict[str, Any]:
    """Достаёт JSON-объект из ответа модели (даже если он обёрнут в ```json)."""
    text = _FENCE.sub("", text.strip())
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end <= start:
        raise LLMError(f"no JSON in reply: {text[:200]}")
    try:
        data = json.loads(text[start : end + 1])
    except json.JSONDecodeError as exc:
        raise LLMError(f"bad JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise LLMError("JSON is not an object")
    return data
