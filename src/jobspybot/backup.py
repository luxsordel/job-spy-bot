"""Ежедневные бэкапы базы (#20).

Копия делается через SQLite backup API — она согласованна даже при работающем боте
и WAL-журнале, в отличие от простого копирования файла. Копия сжимается gzip (база
в основном текст — сжимается в разы), хранится N последних, свежая уходит админам
в Telegram: так копия есть и вне сервера.

Восстановление — см. docs/backup.md.
"""

from __future__ import annotations

import asyncio
import contextlib
import gzip
import logging
import shutil
import sqlite3
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from jobspybot.db import Database

log = logging.getLogger(__name__)

PREFIX = "jobspy-"
SUFFIX = ".sqlite3.gz"
#: Bot API принимает до 50 МБ, но файл читается в память — держим запас под MemoryMax=160M
TELEGRAM_MAX_BYTES = 20 * 1024 * 1024

SendFile = Callable[[str, bytes, str], Awaitable[None]]


@dataclass(slots=True)
class BackupInfo:
    path: Path
    size: int
    at: datetime


class Backups:
    def __init__(self, db: Database, directory: Path, keep: int = 7) -> None:
        self.db = db
        self.directory = directory
        self.keep = max(1, keep)
        self.last_error = ""

    def copies(self) -> list[BackupInfo]:
        """Копии от новых к старым."""
        if not self.directory.is_dir():
            return []
        items = [
            BackupInfo(p, p.stat().st_size, datetime.fromtimestamp(p.stat().st_mtime, UTC))
            for p in self.directory.glob(f"{PREFIX}*{SUFFIX}")
        ]
        return sorted(items, key=lambda b: b.path.name, reverse=True)

    async def make(self, now: datetime | None = None) -> BackupInfo:
        now = now or datetime.now(UTC)
        self.directory.mkdir(parents=True, exist_ok=True)
        name = f"{PREFIX}{now:%Y-%m-%d_%H%M%S}{SUFFIX}"
        raw = self.directory / (name.removesuffix(".gz") + ".tmp")
        target = self.directory / name
        try:
            # согласованная копия в отдельный файл; база маленькая — блокирует ненадолго
            dest = sqlite3.connect(raw)
            try:
                self.db.conn.backup(dest)
            finally:
                dest.close()
            await asyncio.to_thread(_gzip, raw, target)
        except Exception as exc:
            self.last_error = f"{now:%d.%m %H:%M}: {exc!r}"
            target.unlink(missing_ok=True)
            raise
        finally:
            raw.unlink(missing_ok=True)
        self.last_error = ""
        self.rotate()
        size = target.stat().st_size
        log.info("backup %s (%d KB)", target.name, size // 1024)
        return BackupInfo(target, size, now)

    def rotate(self) -> list[Path]:
        removed = [b.path for b in self.copies()[self.keep :]]
        for path in removed:
            path.unlink(missing_ok=True)
        return removed


def _gzip(src: Path, dst: Path) -> None:
    tmp = dst.with_suffix(".part")
    with src.open("rb") as fin, gzip.open(tmp, "wb", compresslevel=6) as fout:
        shutil.copyfileobj(fin, fout, 1024 * 1024)
    tmp.replace(dst)


def seconds_until(hour_utc: int, now: datetime | None = None) -> float:
    """Сколько ждать до ближайших hour_utc:00 по UTC."""
    now = now or datetime.now(UTC)
    target = now.replace(hour=hour_utc % 24, minute=0, second=0, microsecond=0)
    if target <= now:
        target += timedelta(days=1)
    return (target - now).total_seconds()


async def run_and_send(backups: Backups, send: SendFile | None) -> BackupInfo:
    info = await backups.make()
    if send is not None:
        if info.size > TELEGRAM_MAX_BYTES:
            log.warning("backup %s too big for Telegram: %d bytes", info.path.name, info.size)
        else:
            caption = f"💾 Бэкап базы {info.at:%d.%m.%Y %H:%M} UTC, {info.size // 1024} КБ"
            await send(info.path.name, info.path.read_bytes(), caption)
    return info


async def backup_loop(
    backups: Backups, send: SendFile | None, hour_utc: int, stop: asyncio.Event
) -> None:
    """Раз в сутки в hour_utc:00. Если свежей копии нет (первый запуск) — сразу."""
    latest = backups.copies()
    if not latest or datetime.now(UTC) - latest[0].at > timedelta(hours=26):
        await _safe(backups, send)
    while not stop.is_set():
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(stop.wait(), timeout=seconds_until(hour_utc))
            return
        await _safe(backups, send)


async def _safe(backups: Backups, send: SendFile | None) -> None:
    try:
        await run_and_send(backups, send)
    except Exception:
        # ERROR уходит админу через AdminAlerts
        log.exception("database backup failed")
