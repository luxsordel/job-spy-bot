"""Защита и наблюдение: ограничение частоты запросов и оповещения админу об ошибках."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime

log = logging.getLogger(__name__)


class RateLimiter:
    """Скользящее окно: не больше `per_minute` событий за 60 секунд на пользователя."""

    def __init__(self, per_minute: int, window: float = 60.0) -> None:
        self.per_minute = per_minute
        self.window = window
        self._hits: dict[int, deque[float]] = {}
        self._warned: dict[int, float] = {}

    def allow(self, user_id: int, now: float | None = None) -> bool:
        now = time.monotonic() if now is None else now
        hits = self._hits.setdefault(user_id, deque())
        while hits and hits[0] <= now - self.window:
            hits.popleft()
        if len(hits) >= self.per_minute:
            return False
        hits.append(now)
        if len(self._hits) > 10_000:  # не копим память на тысячах разовых гостей
            self._gc(now)
        return True

    def should_warn(self, user_id: int, now: float | None = None) -> bool:
        """Предупреждать о лимите один раз за окно, а не на каждое лишнее сообщение."""
        now = time.monotonic() if now is None else now
        if now - self._warned.get(user_id, -1e9) < self.window:
            return False
        self._warned[user_id] = now
        return True

    def _gc(self, now: float) -> None:
        for uid in [u for u, h in self._hits.items() if not h or h[-1] <= now - self.window]:
            del self._hits[uid]
        self._warned = {u: t for u, t in self._warned.items() if now - t < self.window}


@dataclass(slots=True)
class AlertRecord:
    at: datetime
    key: str
    text: str
    count: int = 1


Notify = Callable[[str], Awaitable[None]]


class AdminAlerts(logging.Handler):
    """Ошибки и важные события — админу в Telegram, с дедупликацией.

    Подключается как обработчик логов уровня ERROR; события вроде «площадка молчит»
    отправляются напрямую через `alert`. Последние события хранятся для веб-админки.
    """

    def __init__(self, cooldown_s: float = 1800, keep: int = 50) -> None:
        super().__init__(level=logging.ERROR)
        self.cooldown_s = cooldown_s
        self.recent: deque[AlertRecord] = deque(maxlen=keep)
        self._last_sent: dict[str, float] = {}
        self._queue: asyncio.Queue[str] = asyncio.Queue(maxsize=100)
        self._loop: asyncio.AbstractEventLoop | None = None

    # --- логирование ------------------------------------------------------------

    def emit(self, record: logging.LogRecord) -> None:
        try:
            message = record.getMessage()
            text = f"❗️ {record.levelname} в {record.name}\n{message[:500]}"
            if record.exc_info and record.exc_info[1] is not None:
                exc = record.exc_info[1]
                text += f"\n{type(exc).__name__}: {str(exc)[:300]}"
            self.alert(f"log:{record.name}:{message[:80]}", text)
        except Exception:  # обработчик логов не должен ронять программу
            self.handleError(record)

    # --- прямые оповещения ------------------------------------------------------

    def alert(self, key: str, text: str, *, cooldown_s: float | None = None) -> bool:
        """Поставить сообщение админу в очередь. False — такое уже было недавно."""
        now = time.monotonic()
        last = self.recent[-1] if self.recent else None
        if last and last.key == key:
            last.count += 1
        else:
            self.recent.append(AlertRecord(datetime.now(UTC), key, text))
        cooldown = self.cooldown_s if cooldown_s is None else cooldown_s
        if now - self._last_sent.get(key, -1e18) < cooldown:
            return False
        self._last_sent[key] = now
        self._put(text)
        return True

    def _put(self, text: str) -> None:
        loop = self._loop
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if loop is not None and running is not loop:
            # запись из другого потока — передаём в цикл событий потокобезопасно
            loop.call_soon_threadsafe(self._put_nowait, text)
        else:
            self._put_nowait(text)

    def _put_nowait(self, text: str) -> None:
        with contextlib.suppress(asyncio.QueueFull):
            self._queue.put_nowait(text)

    async def run(self, notify: Notify, stop: asyncio.Event) -> None:
        """Отправка накопленных оповещений, пока не остановят."""
        self._loop = asyncio.get_running_loop()
        while not stop.is_set():
            getter = asyncio.ensure_future(self._queue.get())
            stopper = asyncio.ensure_future(stop.wait())
            done, _ = await asyncio.wait({getter, stopper}, return_when=asyncio.FIRST_COMPLETED)
            if getter not in done:
                getter.cancel()
                stopper.cancel()
                break
            stopper.cancel()
            try:
                await notify(getter.result())
            except Exception as exc:  # без log.error — иначе сами себе пришлём алерт
                log.warning("admin alert failed: %r", exc)


@dataclass(slots=True)
class LogLine:
    at: datetime
    level: str
    name: str
    text: str


class LogBuffer(logging.Handler):
    """Последние строки лога в памяти — чтобы читать их из веб-админки и API без SSH."""

    def __init__(self, keep: int = 3000, level: int = logging.INFO) -> None:
        super().__init__(level=level)
        self.lines: deque[LogLine] = deque(maxlen=keep)

    def emit(self, record: logging.LogRecord) -> None:
        try:
            text = record.getMessage()
            if record.exc_info and record.exc_info[1] is not None:
                text += "\n" + logging.Formatter().formatException(record.exc_info)
            self.lines.append(
                LogLine(
                    datetime.fromtimestamp(record.created, UTC),
                    record.levelname,
                    record.name,
                    text[:4000],
                )
            )
        except Exception:
            self.handleError(record)

    def tail(self, limit: int = 200, *, level: str = "", query: str = "") -> list[LogLine]:
        """Последние `limit` строк не ниже `level`, содержащие `query`."""
        min_level = logging.getLevelName(level.upper()) if level else 0
        if not isinstance(min_level, int):
            min_level = 0
        q = query.lower()
        found = [
            line
            for line in self.lines
            if logging.getLevelName(line.level) >= min_level
            and (not q or q in line.text.lower() or q in line.name.lower())
        ]
        return found[-limit:]
