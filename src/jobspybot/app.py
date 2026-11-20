"""Сборка приложения: площадки + БД + Telegram + пайплайн в одном процессе."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import signal
from html import escape
from typing import Any

from jobspybot import __version__
from jobspybot.admin_web import AdminWeb, Runtime
from jobspybot.ai.client import LLMClient
from jobspybot.ai.service import AIService
from jobspybot.backup import Backups, backup_loop
from jobspybot.billing import PRO, Billing
from jobspybot.config import ConfigError, Settings
from jobspybot.db import Database, User
from jobspybot.guard import AdminAlerts
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
    bot.on_followup = ai.followup
    ai.token_limit = bot.billing.ai_token_limit
    # при нехватке общего бюджета ИИ остаток достаётся подписчикам
    ai.is_priority = lambda user: bot.billing.plan(user) is PRO

    async def on_apply(user: User, cb: dict[str, Any], value: str) -> None:
        """Лучшее резюме под вакансию (файлом) + сопроводительное, написанное под него."""
        vacancy = (
            bot.db.delivery_vacancy(user.id, int(value))
            if value.isascii() and value.isdigit()
            else None
        )
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
        async with bot.thinking(user, "✍️ Пишу сопроводительное письмо"):
            letter = await ai.cover_letter(user, vacancy, resume)
        await bot.api.send_message(
            user.id, f"✉️ {vacancy.title}\n{vacancy.url}\n\n{letter}", parse_mode=None
        )

    bot.extra_buttons["cl"] = ("📄 Отклик", on_apply)


async def plan_notices(bot: Bot, stop: asyncio.Event, every: float = 1800) -> None:
    """Напоминания: окончание пробного периода и подписки, отклики без ответа."""
    while not stop.is_set():
        try:
            await bot.send_plan_notices()
        except Exception:
            log.exception("plan notices failed")
        try:
            await bot.send_application_reminders()
        except Exception:
            log.exception("application reminders failed")
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(stop.wait(), timeout=every)


async def maintenance(bot: Bot, stop: asyncio.Event, every: float = 6 * 3600) -> None:
    """Удаление данных неактивных пользователей (политика конфиденциальности, #23)."""
    days = bot.settings.guard.inactive_days
    while days and not stop.is_set():
        try:
            purged = bot.db.purge_inactive(days)
            if purged:
                log.info("purged data of %d inactive users", len(purged))
                await bot.notify_admins(
                    f"🧹 Удалены данные {len(purged)} неактивных пользователей (>{days} дн.)"
                )
        except Exception:
            log.exception("maintenance failed")
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(stop.wait(), timeout=every)


async def run(settings: Settings, runtime: Runtime | None = None) -> bool:
    """Работает до остановки. True — просили перезапуск (из веб-админки или API)."""
    runtime = runtime or Runtime()
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

    # ошибки и важные события — админу в Telegram (#21)
    alerts = AdminAlerts(cooldown_s=settings.guard.alert_cooldown_min * 60)
    logging.getLogger("jobspybot").addHandler(alerts)
    pipeline.on_alert = lambda key, text, cooldown: alerts.alert(key, text, cooldown_s=cooldown)

    llm: LLMClient | None = None
    if settings.ai.enabled:
        llm = LLMClient(settings.ai.base_url, settings.ai.api_key, settings.ai.model)
        ai = AIService(db, llm, settings.ai)
        ai.describe = pipeline.describe
        ai.on_alert = alerts.alert
        pipeline.evaluator = ai
        wire_ai(bot, ai)
        log.info("AI enabled: model %s", settings.ai.model)
    else:
        log.info("AI disabled: set AI_BASE_URL, AI_API_KEY and AI_MODEL to enable")

    stop = runtime.stop = asyncio.Event()
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
            asyncio.create_task(pipeline.flush_forever(stop), name="digest"),
            asyncio.create_task(plan_notices(bot, stop), name="billing"),
            asyncio.create_task(alerts.run(bot.notify_admins, stop), name="alerts"),
            asyncio.create_task(maintenance(bot, stop), name="maintenance"),
        ]
        backups = (
            Backups(db, settings.backup_dir, settings.backup.keep) if settings.backup.keep else None
        )
        if backups:
            send = bot.send_backup if settings.backup.to_telegram else None
            tasks.append(
                asyncio.create_task(
                    backup_loop(backups, send, settings.backup.hour_utc, stop), name="backup"
                )
            )
        if settings.admin_web.enabled:
            web = AdminWeb(
                settings,
                db,
                billing,
                bot=bot,
                pipeline=pipeline,
                alerts=alerts,
                runtime=runtime,
                backups=backups,
            )
            tasks.append(asyncio.create_task(web.serve(stop), name="admin-web"))
        await bot.notify_admins(f"🔄 Бот запущен (v{__version__})")
        await stop.wait()
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
    finally:
        logging.getLogger("jobspybot").removeHandler(alerts)
        if llm:
            await llm.aclose()
        await api.aclose()
        await http.aclose()
        db.close()
        log.info("restarting" if runtime.restart_requested else "stopped")
    return runtime.restart_requested
