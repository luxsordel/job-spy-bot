"""Сборка приложения: площадки + БД + Telegram + пайплайн в одном процессе."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import signal
from html import escape
from typing import Any

from jobspybot.ai.client import LLMClient
from jobspybot.ai.service import AIService
from jobspybot.billing import Billing
from jobspybot.config import ConfigError, Settings
from jobspybot.db import Database, User
from jobspybot.http import Http
from jobspybot.pipeline import Pipeline
from jobspybot.sources import REGISTRY, build_sources
from jobspybot.telegram.api import TelegramAPI
from jobspybot.telegram.bot import Bot

log = logging.getLogger(__name__)


def wire_ai(bot: Bot, ai: AIService) -> None:
    """Подключает ИИ к боту: профиль из резюме и кнопка «Отклик» под вакансиями."""
    bot.on_resume = ai.build_profile
    bot.on_profile_edit = ai.parse_profile_edit
    ai.token_limit = bot.billing.ai_token_limit

    async def on_apply(user: User, cb: dict[str, Any], value: str) -> None:
        """Лучшее резюме под вакансию (файлом) + сопроводительное, написанное под него."""
        vacancy = bot.db.delivery_vacancy(user.id, int(value)) if value.isdigit() else None
        if vacancy is None:
            await bot.api.answer_callback(cb["id"], "Вакансия уже удалена из базы")
            return
        if not bot.billing.plan(user).apply_button:
            await bot.api.answer_callback(cb["id"], "Отклик с письмом — в 💎 Pro")
            await bot.show_plan(user)
            return
        resume = await ai.best_resume(user, vacancy)
        if resume is None:
            await bot.api.answer_callback(cb["id"], "Сначала пришлите резюме (PDF)")
            return
        await bot.api.answer_callback(cb["id"], f"✍️ Готовлю отклик: «{resume.title}»")
        if resume.file_id:
            await bot.api.send_document(
                user.id, resume.file_id, f"📄 Резюме для «{escape(vacancy.title)}»"
            )
        letter = await ai.cover_letter(user, vacancy, resume)
        await bot.api.send_message(
            user.id, f"✉️ {vacancy.title}\n{vacancy.url}\n\n{letter}", parse_mode=None
        )

    bot.extra_buttons["cl"] = ("📄 Отклик", on_apply)


async def plan_notices(bot: Bot, stop: asyncio.Event, every: float = 1800) -> None:
    """Напоминания об окончании пробного периода и подписки."""
    while not stop.is_set():
        try:
            await bot.send_plan_notices()
        except Exception:
            log.exception("plan notices failed")
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(stop.wait(), timeout=every)


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
        "access: %s, trial: %d days, Pro: %d stars",
        settings.billing.access_mode,
        settings.billing.trial_days,
        settings.billing.pro_price_stars,
    )
    log.info(
        "sources: %s (skipped: %s)",
        ", ".join(sources) or "none",
        ", ".join(sorted(set(REGISTRY) - set(sources))) or "none",
    )

    billing = Billing(db, settings)
    bot = Bot(api, db, settings, list(sources), billing)
    pipeline = Pipeline(db, sources, bot, settings, billing=billing)
    llm: LLMClient | None = None
    if settings.ai.enabled:
        llm = LLMClient(settings.ai.base_url, settings.ai.api_key, settings.ai.model)
        ai = AIService(db, llm, settings.ai)
        ai.describe = pipeline.describe
        pipeline.evaluator = ai
        wire_ai(bot, ai)
        log.info("AI enabled: model %s", settings.ai.model)
    else:
        log.info("AI disabled: set AI_BASE_URL, AI_API_KEY and AI_MODEL to enable")

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
            asyncio.create_task(plan_notices(bot, stop), name="billing"),
        ]
        await stop.wait()
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
    finally:
        if llm:
            await llm.aclose()
        await api.aclose()
        await http.aclose()
        db.close()
        log.info("stopped")
