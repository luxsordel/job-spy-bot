"""Сборка приложения: площадки + БД + Telegram + пайплайн в одном процессе."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import signal

from jobspybot.config import ConfigError, Settings
from jobspybot.db import Database
from jobspybot.http import Http
from jobspybot.pipeline import Pipeline
from jobspybot.sources import REGISTRY, build_sources
from jobspybot.telegram.api import TelegramAPI
from jobspybot.telegram.bot import Bot

log = logging.getLogger(__name__)


async def run(settings: Settings) -> None:
    if not settings.telegram_token:
        raise ConfigError("не задан TELEGRAM_BOT_TOKEN")
    if not settings.admin_ids:
        log.warning("ADMIN_IDS пуст — некому выдавать доступ к боту")

    http = Http(user_agent=settings.user_agent, timeout=settings.http_timeout)
    db = Database(settings.db_path)
    api = TelegramAPI(settings.telegram_token)
    sources = build_sources(http, settings)
    log.info(
        "sources: %s (skipped: %s)",
        ", ".join(sources) or "none",
        ", ".join(sorted(set(REGISTRY) - set(sources))) or "none",
    )

    bot = Bot(api, db, settings, list(sources))
    pipeline = Pipeline(db, sources, bot, settings)

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError):  # Windows
            loop.add_signal_handler(sig, stop.set)

    try:
        await bot.setup()
        log.info("bot @%s started", bot.username)
        tasks = [
            asyncio.create_task(bot.run(stop), name="telegram"),
            asyncio.create_task(pipeline.run_forever(stop), name="pipeline"),
        ]
        await stop.wait()
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
    finally:
        await api.aclose()
        await http.aclose()
        db.close()
        log.info("stopped")
