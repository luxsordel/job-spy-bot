"""Клиент OpenAI-совместимого Chat Completions API (OpenAI, DeepSeek, OpenRouter, Groq…)
и цепочка провайдеров с переключением при сбое.

Официальный SDK не используется — он тяжелее, а нужен один эндпоинт.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

import httpx

log = logging.getLogger(__name__)


class LLMError(Exception):
    def __init__(self, message: str, *, status: int | None = None, retry_after: float = 0) -> None:
        super().__init__(message)
        #: HTTP-код ответа провайдера (None — сеть или разбор ответа)
        self.status = status
        #: сколько секунд провайдер просит подождать (заголовок Retry-After)
        self.retry_after = retry_after


@dataclass(slots=True)
class LLMReply:
    text: str
    tokens: int
    #: какой провайдер ответил (заполняет LLMRouter)
    provider: str = ""


class LLMClient:
    def __init__(
        self,
        base_url: str,
        api_key: str,
        model: str,
        *,
        timeout: float = 60.0,
        attempts: int = 3,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.model = model
        self._attempts = attempts
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
        model: str | None = None,
    ) -> LLMReply:
        payload: dict[str, Any] = {
            "model": model or self.model,
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
        last = self._attempts - 1
        for attempt in range(self._attempts):
            try:
                resp = await self._client.post(self._url, json=payload)
            except httpx.HTTPError as exc:
                if attempt == last:
                    # у таймаутов str(exc) пустой — без имени класса в логе «network: »
                    raise LLMError(f"network: {type(exc).__name__} {exc}".rstrip()) from exc
            else:
                if resp.status_code == 200:
                    try:
                        return resp.json()
                    except ValueError as exc:
                        raise LLMError(f"not JSON: {resp.text[:200]}", status=200) from exc
                if resp.status_code == 400 and "response_format" in payload:
                    return None
                if resp.status_code not in (429, 500, 502, 503, 504) or attempt == last:
                    raise LLMError(
                        f"HTTP {resp.status_code}: {resp.text[:300]}",
                        status=resp.status_code,
                        retry_after=_retry_after(resp),
                    )
            await asyncio.sleep(delay)
            delay *= 2
        raise LLMError("retries exhausted")

    async def aclose(self) -> None:
        await self._client.aclose()


def _retry_after(resp: httpx.Response) -> float:
    try:
        return max(0.0, float(resp.headers.get("retry-after", "0")))
    except ValueError:
        return 0.0


#: признаки того, что кончился дневной лимит или деньги, а не минутный лимит
_LONG_LIMIT = ("per day", "daily", "quota", "exhausted", "insufficient", "credits", "balance")


def cooldown_for(exc: LLMError) -> float:
    """На сколько секунд отложить провайдера после ошибки."""
    text = str(exc).lower()
    if exc.status == 429:
        if any(w in text for w in _LONG_LIMIT):
            return max(exc.retry_after, 30 * 60)
        return min(max(exc.retry_after, 60), 30 * 60)
    if exc.status in (401, 402, 403, 404):
        return 60 * 60  # ключ, деньги, доступ из страны, нет модели — само не пройдёт
    return 60  # сеть, 5xx, мусор в ответе


@dataclass(slots=True)
class Route:
    """Провайдер в цепочке и его состояние с момента запуска."""

    name: str
    client: LLMClient
    model: str
    ok: int = 0
    failed: int = 0
    tokens: int = 0
    last_error: str = ""
    resting_until: float = 0.0


class LLMRouter:
    """Цепочка провайдеров: бесплатные по очереди, платный — последним.

    Провайдер, упёршийся в лимит или ошибку, откладывается (`cooldown_for`), и следующие
    запросы сразу идут к следующему. Последний в цепочке не откладывается никогда — это
    страховка: к нему обращаются всегда, когда остальные не смогли.
    """

    def __init__(
        self, routes: Sequence[Route], *, clock: Callable[[], float] = time.monotonic
    ) -> None:
        if not routes:
            raise ValueError("empty provider chain")
        self.routes = list(routes)
        self._clock = clock

    @property
    def model(self) -> str:
        return " → ".join(f"{r.name}:{r.model}" for r in self.routes)

    async def chat(self, system: str, user: str, **kw: Any) -> LLMReply:
        kw.pop("model", None)
        last_exc: LLMError | None = None
        for i, route in enumerate(self.routes):
            is_last = i == len(self.routes) - 1
            if route.resting_until > self._clock() and not is_last:
                continue
            try:
                reply = await route.client.chat(system, user, model=route.model, **kw)
            except LLMError as exc:
                route.failed += 1
                route.last_error = str(exc)[:300]
                last_exc = exc
                if not is_last:
                    pause = cooldown_for(exc)
                    route.resting_until = self._clock() + pause
                    log.warning(
                        "ИИ %s не ответил (%s) — отложен на %d мин",
                        route.name,
                        str(exc)[:160],
                        max(1, int(pause // 60)),
                    )
                continue
            route.ok += 1
            route.tokens += reply.tokens
            reply.provider = route.name
            return reply
        assert last_exc is not None
        raise last_exc

    def status(self) -> list[dict[str, Any]]:
        now = self._clock()
        return [
            {
                "name": r.name,
                "model": r.model,
                "ok": r.ok,
                "failed": r.failed,
                "tokens": r.tokens,
                "resting_s": max(0, int(r.resting_until - now)),
                "last_error": r.last_error,
            }
            for r in self.routes
        ]

    async def aclose(self) -> None:
        closed: set[int] = set()
        for r in self.routes:
            if id(r.client) not in closed:
                closed.add(id(r.client))
                await r.client.aclose()


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
