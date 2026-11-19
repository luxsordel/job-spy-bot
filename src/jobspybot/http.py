"""Общий HTTP-клиент: один пул соединений, ретраи, вежливая пауза между запросами к хосту."""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any
from urllib.parse import urlsplit

import httpx

log = logging.getLogger(__name__)

RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})


class HttpError(Exception):
    def __init__(self, status: int, url: str, body: str = "") -> None:
        super().__init__(f"HTTP {status} for {url}: {body[:200]}")
        self.status = status
        self.url = url
        self.body = body


class Http:
    def __init__(
        self,
        *,
        user_agent: str,
        timeout: float = 20.0,
        retries: int = 3,
        min_interval: float = 1.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._client = httpx.AsyncClient(
            headers={"User-Agent": user_agent, "Accept-Language": "ru,en;q=0.8"},
            timeout=timeout,
            follow_redirects=True,
            limits=httpx.Limits(max_connections=8, max_keepalive_connections=4),
            transport=transport,
        )
        self._retries = retries
        self._min_interval = min_interval
        self._last_hit: dict[str, float] = {}
        self._host_locks: dict[str, asyncio.Lock] = {}

    @property
    def client(self) -> httpx.AsyncClient:
        return self._client

    async def _polite(self, url: str) -> None:
        host = urlsplit(url).netloc
        lock = self._host_locks.setdefault(host, asyncio.Lock())
        async with lock:
            wait = self._last_hit.get(host, 0.0) + self._min_interval - time.monotonic()
            if wait > 0:
                await asyncio.sleep(wait)
            self._last_hit[host] = time.monotonic()

    async def request(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        delay = 2.0
        for attempt in range(1, self._retries + 1):
            await self._polite(url)
            try:
                resp = await self._client.request(method, url, **kwargs)
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                if attempt == self._retries:
                    raise
                log.info("%s %s: %r, повтор %d", method, url[:120], exc, attempt)
            else:
                if resp.status_code < 400:
                    return resp
                if resp.status_code not in RETRY_STATUSES or attempt == self._retries:
                    raise HttpError(resp.status_code, str(resp.url), resp.text)
                log.info("%s %s: HTTP %d, повтор %d", method, url[:120], resp.status_code, attempt)
                retry_after = resp.headers.get("Retry-After", "")
                if retry_after.isdigit():
                    delay = max(delay, min(float(retry_after), 60.0))
            await asyncio.sleep(delay)
            delay *= 2
        raise AssertionError("unreachable")

    async def get_json(self, url: str, **kwargs: Any) -> Any:
        return (await self.request("GET", url, **kwargs)).json()

    async def post_json(self, url: str, **kwargs: Any) -> Any:
        return (await self.request("POST", url, **kwargs)).json()

    async def get_text(self, url: str, **kwargs: Any) -> str:
        return (await self.request("GET", url, **kwargs)).text

    async def aclose(self) -> None:
        await self._client.aclose()
