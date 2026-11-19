"""Telegram-интерфейс: меню, онбординг, фильтры, резюме, подписка, админка."""

from __future__ import annotations

import asyncio
import json
import logging
import re
from collections.abc import Awaitable, Callable
from html import escape
from typing import Any

from jobspybot.billing import PRO, SUBSCRIPTION_PERIOD_SECONDS, Billing
from jobspybot.config import Settings
from jobspybot.db import Database, Resume, User, utcnow
from jobspybot.filters import COUNTRIES, UserFilters
from jobspybot.guard import RateLimiter
from jobspybot.limits import (
    MAX_CITY_LEN,
    MAX_GRANT_DAYS,
    MAX_PHRASE_LEN,
    MAX_WISHES_LEN,
    InputError,
    clean_list,
    clean_text,
    parse_int,
    parse_salary,
)
from jobspybot.models import Level, Vacancy
from jobspybot.profile import (
    PATCH_KEYS,
    RELOCATION_COUNTRIES,
    SALARY_PRESETS,
    WORK_FORMATS,
    apply_patch,
    apply_to_filters,
    preview_patch,
    remember_override,
)
from jobspybot.resume_files import (
    MAX_FILE_BYTES,
    ResumeFileError,
    extract_text,
    title_from_filename,
)
from jobspybot.sources import REGISTRY, SOURCE_ORDER
from jobspybot.telegram import render as r
from jobspybot.telegram.api import (
    TelegramAPI,
    TelegramError,
    button,
    inline_keyboard,
    reply_keyboard,
)

log = logging.getLogger(__name__)

COMMANDS = [
    ("start", "Главное меню"),
    ("search", "Мой поиск"),
    ("profile", "Профиль"),
    ("filters", "Фильтры"),
    ("resumes", "Мои резюме"),
    ("plan", "Подписка"),
    ("stats", "Статистика"),
    ("help", "Помощь"),
    ("privacy", "Мои данные и их удаление"),
    ("reset", "Настроить поиск заново"),
]

BOT_DESCRIPTION = (
    "🔎 Ищу вакансии на 10 площадках Беларуси, России, Казахстана, Грузии и Польши.\n\n"
    "🤖 ИИ читает каждую вакансию и оценивает шанс на оффер под ваше резюме.\n"
    "🇧🇾 Предупреждаю, если вакансия недоступна гражданам РБ.\n"
    "📄 Выбираю лучшее из ваших резюме и пишу сопроводительное письмо.\n\n"
    "Нажмите «Старт» — настройка займёт минуту."
)
BOT_SHORT_DESCRIPTION = "Вакансии BY · RU · KZ · GE · PL с ИИ-оценкой под ваше резюме"

_SPLIT = re.compile(r"[;,\n]+")
MAX_RESUME_CHARS = 8000
#: резюме текстом — не меньше стольких букв (а не «0000…» или набор цифр)
MIN_RESUME_LETTERS = 40
#: Telegram режет текст длиннее 4096 символов на несколько сообщений подряд
TG_MAX_TEXT = 4096
#: сообщение такой длины — вероятно, кусок длинного; следующие куски приходят в ту же секунду
SPLIT_MIN_LEN = 3500
SPLIT_GAP_SEC = 2

#: вопросы анкеты в винительном падеже — «потом спрошу …»
QUESTION_NAMES = {
    "directions": "направления поиска",
    "level": "уровень вакансий",
    "work_formats": "формат работы",
    "relocation": "релокацию",
    "salary": "зарплату",
}

ResumeHook = Callable[[User, Resume], Awaitable[str]]
EditHook = Callable[[User, str], Awaitable[dict[str, Any] | None]]
CallbackHook = Callable[[User, dict[str, Any], str], Awaitable[None]]


def parse_list(raw: str) -> list[str]:
    return clean_list(_SPLIT.split(raw))


def looks_like_resume(text: str) -> bool:
    return sum(ch.isalpha() for ch in text) >= MIN_RESUME_LETTERS


def merge_split_messages(updates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Склеивает куски одного длинного сообщения, которые Telegram прислал подряд.

    Иначе каждый кусок обрабатывается как отдельный ответ: кусок «зарплаты» из тысяч
    нулей становился резюме, следующий — перезапускал онбординг, и так по кругу.
    """
    merged: list[dict[str, Any]] = []
    for update in updates:
        msg = update.get("message") or {}
        prev = (merged[-1].get("message") or {}) if merged else {}
        text = msg.get("text")
        if (
            text
            and prev.get("text")
            and len(prev["text"]) >= SPLIT_MIN_LEN
            and not text.startswith("/")
            and (prev.get("from") or {}).get("id") == (msg.get("from") or {}).get("id")
            and (prev.get("chat") or {}).get("id") == (msg.get("chat") or {}).get("id")
            and abs((msg.get("date") or 0) - (prev.get("date") or 0)) <= SPLIT_GAP_SEC
        ):
            prev["text"] = prev["text"] + "\n" + text
            merged[-1]["update_id"] = update["update_id"]
            continue
        merged.append(update)
    return merged


def main_menu() -> dict[str, Any]:
    return reply_keyboard(*r.MENU_ROWS, placeholder="Пришлите PDF резюме или выберите раздел")


class Bot:
    def __init__(
        self,
        api: TelegramAPI,
        db: Database,
        settings: Settings,
        available_sources: list[str],
        billing: Billing | None = None,
    ) -> None:
        self.api = api
        self.db = db
        self.settings = settings
        self.billing = billing or Billing(db, settings)
        self.available_sources = [s for s in SOURCE_ORDER if s in available_sources]
        self.username = ""
        #: ожидаемый текстовый ввод: user_id → поле (resume, queries, exclude, salary, skills)
        self._awaiting: dict[int, str] = {}
        self._cap_notified: dict[int, str] = {}
        #: режим анкеты: ob — онбординг, fill — дозаполнение, edit — правка одного поля
        self._profile_mode: dict[int, str] = {}
        #: обработчик нового резюме (подключается модулем ИИ); возвращает ответ пользователю
        self.on_resume: ResumeHook | None = None
        #: дополнительные кнопки под вакансией: префикс → (текст, обработчик)
        self.extra_buttons: dict[str, tuple[str, CallbackHook]] = {}
        #: разбор фразы «хочу только гибрид…» в правку профиля (подключается модулем ИИ)
        self.on_profile_edit: EditHook | None = None
        self._pending_patch: dict[int, dict[str, Any]] = {}
        #: время (date из Telegram) последнего очень длинного сообщения пользователя
        self._long_text_at: dict[int, int] = {}
        self.limiter = RateLimiter(settings.guard.rate_limit_per_min)
        self._menu_actions: dict[str, Callable[[User], Awaitable[None]]] = {
            r.BTN_SEARCH: self.show_search,
            r.BTN_RESUMES: self.show_resumes,
            r.BTN_FILTERS: self.show_filters,
            r.BTN_PLAN: self.show_plan,
            r.BTN_STATS: self.show_stats,
            r.BTN_PROFILE: self.show_profile,
            r.BTN_HELP: self.show_help,
        }

    # --- жизненный цикл ------------------------------------------------------

    async def setup(self) -> None:
        me = await self.api.get_me()
        self.username = me.get("username", "")
        await self.api.set_commands(COMMANDS)
        try:
            await self.api.set_profile_texts(BOT_DESCRIPTION, BOT_SHORT_DESCRIPTION)
        except TelegramError as exc:  # не критично
            log.warning("cannot set bot description: %s", exc)

    async def run(self, stop: asyncio.Event) -> None:
        offset: int | None = None
        while not stop.is_set():
            try:
                updates = await self.api.get_updates(offset)
            except Exception as exc:  # сеть, 5xx — подождать и продолжить
                log.warning("getUpdates failed: %s", exc)
                await asyncio.sleep(5)
                continue
            for update in merge_split_messages(updates):
                offset = update["update_id"] + 1
                try:
                    await self.handle_update(update)
                except Exception:
                    log.exception("update %s failed", update.get("update_id"))

    # --- рассылка ------------------------------------------------------------

    async def send_vacancy(
        self, user: User, vacancy: Vacancy, delivery_id: int, extra: str = ""
    ) -> None:
        row = [button("🔗 Открыть", url=vacancy.url)]
        row += [
            button(text, data=f"{p}:{delivery_id}") for p, (text, _) in self.extra_buttons.items()
        ]
        row.append(button("👎", data=f"nope:{delivery_id}"))
        await self.api.send_message(
            user.id, r.vacancy_card(vacancy, extra), reply_markup=inline_keyboard(row)
        )

    async def daily_limit_reached(self, user: User) -> None:
        """Один раз в день сообщает Free-пользователю, что лимит вакансий исчерпан."""
        today = utcnow().date().isoformat()
        if self._cap_notified.get(user.id) == today:
            return
        self._cap_notified[user.id] = today
        await self.reply(
            user,
            "📭 На сегодня лимит бесплатного тарифа исчерпан — новые вакансии придут завтра.\n"
            "В Pro — без ограничений и с проверкой каждые 30 минут.",
            inline_keyboard([button("💎 Подробнее о Pro", data="plan:show")]),
        )

    async def send_plan_notices(self) -> None:
        for user in self.db.all_users():
            if not user.allowed or user.banned:
                continue
            notice = self.billing.notice(user)
            if notice is None:
                continue
            key, text = notice
            try:
                await self.reply(
                    user, text, inline_keyboard([button("💎 Подписка", data="plan:show")])
                )
            except TelegramError as exc:
                log.info("notice to %s failed: %s", user.id, exc)
            self.db.set_notified(user.id, key)

    async def notify_admins(self, text: str, markup: dict[str, Any] | None = None) -> None:
        for admin in self.settings.admin_ids:
            try:
                await self.api.send_message(admin, text, reply_markup=markup)
            except TelegramError as exc:
                log.warning("cannot notify admin %s: %s", admin, exc)

    async def send_backup(self, filename: str, data: bytes, caption: str) -> None:
        """Свежий бэкап базы — файлом каждому админу (копия вне сервера)."""
        for admin in self.settings.admin_ids:
            try:
                await self.api.upload_document(admin, filename, data, caption)
            except Exception as exc:
                log.warning("cannot send backup to admin %s: %r", admin, exc)

    # --- входящие ------------------------------------------------------------

    async def handle_update(self, update: dict[str, Any]) -> None:
        if await self._throttled(update):
            return
        if "callback_query" in update:
            await self.handle_callback(update["callback_query"])
        elif "pre_checkout_query" in update:
            await self.handle_pre_checkout(update["pre_checkout_query"])
        elif "message" in update:
            await self.handle_message(update["message"])
        elif "my_chat_member" in update:
            await self.handle_chat_member(update["my_chat_member"])

    async def handle_chat_member(self, change: dict[str, Any]) -> None:
        """Человек остановил или заблокировал бота — рассылку ставим на паузу.

        Telegram не сообщает об удалении переписки без остановки бота, поэтому при
        возвращении через /start спрашиваем, продолжить ли с прежними настройками.
        """
        if (change.get("chat") or {}).get("type") != "private":
            return
        uid = (change.get("from") or {}).get("id")
        status = (change.get("new_chat_member") or {}).get("status")
        if not isinstance(uid, int) or self.db.get_user(uid) is None:
            return
        if status == "kicked":
            self.db.set_stopped(uid, True)
            self._forget(uid)
            log.info("user %s stopped the bot", uid)

    async def _throttled(self, update: dict[str, Any]) -> bool:
        """Слишком частые сообщения и нажатия отбрасываются (оплаты — никогда)."""
        cb = update.get("callback_query")
        msg = update.get("message")
        sender = (cb or msg or {}).get("from") or {}
        uid = sender.get("id")
        if not isinstance(uid, int) or uid in self.settings.admin_ids:
            return False
        if msg and msg.get("successful_payment"):
            return False
        if self.limiter.allow(uid):
            return False
        if cb:
            await self.api.answer_callback(cb["id"], "Слишком часто — подождите минуту")
        elif self.limiter.should_warn(uid):
            await self.api.send_message(
                uid, "⏳ Слишком много сообщений подряд. Подождите минуту — и продолжим."
            )
        return True

    def _user_from(self, sender: dict[str, Any]) -> User:
        uid = sender["id"]
        name = sender.get("username") or sender.get("first_name") or ""
        return self.db.ensure_user(uid, name, allowed=uid in self.settings.admin_ids)

    def _is_admin(self, user: User) -> bool:
        return user.id in self.settings.admin_ids

    async def reply(self, user: User, text: str, markup: dict[str, Any] | None = None) -> None:
        await self.api.send_message(user.id, text, reply_markup=markup)

    async def edit(self, msg: dict[str, Any], text: str, markup: dict[str, Any] | None) -> None:
        try:
            await self.api.call(
                "editMessageText",
                chat_id=msg["chat"]["id"],
                message_id=msg["message_id"],
                text=text,
                parse_mode="HTML",
                reply_markup=markup,
                link_preview_options={"is_disabled": True},
            )
        except TelegramError as exc:
            if "not modified" not in exc.description:
                raise

    async def handle_message(self, msg: dict[str, Any]) -> None:
        chat = msg.get("chat") or {}
        if chat.get("type") != "private" or "from" not in msg:
            return
        user = self._user_from(msg["from"])
        first_name = msg["from"].get("first_name", "")

        if msg.get("successful_payment"):
            await self._on_successful_payment(user, msg["successful_payment"])
            return

        if user.banned and not self._is_admin(user):
            return

        if self._is_split_tail(user, msg):
            log.info("dropped tail of a split message from %s", user.id)
            return

        text = (msg.get("text") or "").strip()
        if text.startswith("/"):
            cmd, _, args = text[1:].partition(" ")
            cmd = cmd.split("@", 1)[0].lower()
            args = args.strip()
        else:
            cmd, args = "", text

        if not user.allowed and not await self._admit(user, cmd, args, first_name):
            return

        if msg.get("document"):
            await self._receive_document(user, msg["document"])
            return

        if text in self._menu_actions:
            self._awaiting.pop(user.id, None)
            await self._menu_actions[text](user)
            return

        if not cmd and user.id in self._awaiting:
            await self._receive_input(user, self._awaiting.pop(user.id), text)
            return

        if not cmd and not user.onboarded and self._has_setup(user):
            # посреди настройки: не перезапускать онбординг каждым случайным сообщением
            await self.reply(
                user,
                "Ответьте на вопрос выше кнопками 👆 или пришлите резюме файлом.\n"
                "Начать настройку заново — /start.",
            )
            return

        if cmd == "start" or (not user.onboarded and not cmd):
            await self.cmd_start(user, args, first_name=first_name)
            return

        handler = getattr(self, f"cmd_{cmd}", None) if cmd else None
        if handler is None:
            if not cmd and self.on_profile_edit and len(text) >= 6:
                # обычная фраза — это просьба что-то поменять в поиске
                await self._nl_edit(user, text)
                return
            await self.reply(
                user,
                "Не понял 🤔 Выберите раздел в меню ниже или пришлите PDF резюме.",
                main_menu(),
            )
            return
        await handler(user, args)

    async def _admit(self, user: User, cmd: str, args: str, first_name: str) -> bool:
        """Новый пользователь: инвайт, открытая регистрация или запрос админу."""
        invited = cmd == "start" and bool(args) and self.db.use_invite(args, user.id)
        if invited or self.settings.billing.access_mode == "open":
            self.db.set_allowed(user.id, True)
            user.allowed = True
            await self.notify_admins(
                f"👤 Новый пользователь: {escape(user.username or first_name)} "
                f"(<code>{user.id}</code>){' по приглашению' if invited else ''}"
            )
            await self.cmd_start(user, "", first_name=first_name)
            return False  # сообщение уже обработано онбордингом
        await self.reply(
            user, "🔒 Бот сейчас работает по приглашениям. Попросите ссылку у администратора."
        )
        if cmd == "start":
            await self.notify_admins(
                f"Запрос доступа: {escape(user.username)} (<code>{user.id}</code>)",
                inline_keyboard(
                    [
                        button("✅ Пустить", data=f"allow:{user.id}"),
                        button("🚫 Нет", data=f"deny:{user.id}"),
                    ]
                ),
            )
        return False

    # --- старт и онбординг ---------------------------------------------------

    def _is_split_tail(self, user: User, msg: dict[str, Any]) -> bool:
        """Кусок длинного сообщения, не склеенный с началом (пришёл другой пачкой)."""
        text = msg.get("text") or ""
        date = msg.get("date")
        if not text or not isinstance(date, int):
            return False
        prev = self._long_text_at.get(user.id)
        if len(text) >= SPLIT_MIN_LEN:
            self._long_text_at[user.id] = date
        return prev is not None and not text.startswith("/") and 0 <= date - prev <= SPLIT_GAP_SEC

    def _has_setup(self, user: User) -> bool:
        """Есть ли что сбрасывать: резюме, профиль или направления поиска."""
        return bool(
            self._resume_count(user)
            or user.filters.queries
            or not self.db.get_profile(user.id).is_empty
        )

    def _forget(self, user_id: int) -> None:
        """Забыть незаконченный ввод и режим анкеты."""
        for state in (self._awaiting, self._profile_mode, self._pending_patch):
            state.pop(user_id, None)

    async def reset_setup(self, user: User) -> None:
        """«Начать заново»: стирает резюме, профиль и фильтры; доступ и подписка остаются."""
        self.db.reset_setup(user.id)
        self._forget(user.id)
        fresh = self.db.get_user(user.id)
        assert fresh is not None
        user.filters, user.onboarded, user.paused, user.stopped_at = (
            fresh.filters,
            False,
            False,
            None,
        )

    async def cmd_start(self, user: User, args: str, *, first_name: str = "") -> None:
        if user.onboarded and user.stopped_at:
            # вернулся после остановки бота (часто — вместе с удалением переписки)
            await self.reply(
                user,
                "👋 С возвращением! Продолжить поиск с прежними настройками "
                "или настроить всё заново?",
                inline_keyboard(
                    [button("▶️ Продолжить", data="back:keep")],
                    [button("🔄 Начать заново", data="back:reset")],
                ),
            )
            return
        if user.onboarded:
            await self.reply(user, "Главное меню 👇", main_menu())
            await self.show_search(user)
            return
        if self._has_setup(user):
            # не закончил настройку и нажал «Старт» — начинаем с чистого листа
            await self.reset_setup(user)
            await self.reply(user, "🔄 Начинаем заново — прежние ответы и резюме стёрты.")
        elif user.stopped_at:
            self.db.set_stopped(user.id, False)
            self.db.set_paused(user.id, False)
            user.stopped_at, user.paused = None, False
        trial = self.billing.start_trial(user) and self.settings.billing.trial_days
        await self.reply(user, r.welcome(first_name, trial or 0), main_menu())
        self._awaiting[user.id] = "resume"
        self._profile_mode[user.id] = "ob"
        await self.reply(
            user,
            "<b>📄 Пришлите резюме — PDF или .txt</b>\n\n"
            "Я сам достану из него навыки, опыт и пожелания, заполню профиль и настрою поиск. "
            "Спрошу только то, чего в резюме нет.",
            inline_keyboard([button("✍️ Нет резюме — отвечу на вопросы", data="ob:manual")]),
        )

    async def _finish_onboarding(self, user: User) -> None:
        self._awaiting.pop(user.id, None)
        self.db.set_onboarded(user.id)
        user.onboarded = True
        plan = self.billing.plan(user)
        summary = r.search_summary(
            user.filters,
            self.available_sources,
            paused=user.paused,
            resumes=self._resume_count(user),
            plan=plan,
            check_every_min=plan.check_every_min,
        )
        await self.reply(
            user,
            "🎉 <b>Готово! Поиск настроен и запущен.</b>\n\n"
            f"{summary}\n\n"
            f"Первая подборка придёт в течение {min(plan.check_every_min, 30)} минут. "
            "Поправить что угодно — 👤 Профиль или ⚙️ Фильтры.",
            main_menu(),
        )

    # --- профиль и анкета -----------------------------------------------------

    def _apply_profile(self, user: User) -> None:
        """Анкета поиска из профиля."""
        profile = self.db.get_profile(user.id)
        apply_to_filters(profile, user.filters, max_queries=self.billing.plan(user).max_queries)
        self.db.save_profile(user.id, profile)
        self.db.save_filters(user.id, user.filters)

    def _save_filter_edit(self, user: User, *fields: str) -> None:
        """Ручная правка в ⚙️ Фильтрах: сохраняем и запоминаем, чтобы профиль её не перетёр."""
        profile = self.db.get_profile(user.id)
        if "levels" in fields:
            profile.set_levels(user.filters.levels)
        for name in fields:
            remember_override(profile, user.filters, name)
        self.db.save_profile(user.id, profile)
        self.db.save_filters(user.id, user.filters)

    async def _ask_next(self, user: User, msg: dict[str, Any] | None = None) -> None:
        """Следующий вопрос анкеты или завершение, если профиль заполнен."""
        missing = self.db.get_profile(user.id).missing(user.filters)
        if not missing:
            await self._complete_profile(user, msg)
            return
        await self._show_question(user, missing[0], msg)

    async def _complete_profile(self, user: User, msg: dict[str, Any] | None) -> None:
        self._apply_profile(user)
        self._profile_mode.pop(user.id, None)
        if not user.onboarded:
            if msg:
                await self.edit(msg, "✅ Профиль заполнен", None)
            await self._finish_onboarding(user)
        else:
            await self.show_profile(user, msg)

    async def _after_answer(self, user: User, msg: dict[str, Any] | None) -> None:
        if self._profile_mode.get(user.id) == "edit":
            self._profile_mode.pop(user.id, None)
            self._apply_profile(user)
            await self.show_profile(user, msg)
        else:
            await self._ask_next(user, msg)

    def _question_markup(self, user: User, key: str) -> dict[str, Any]:
        profile = self.db.get_profile(user.id)
        back = (
            [button("⬅️ К профилю", data="pe:show")]
            if self._profile_mode.get(user.id) == "edit"
            else []
        )
        if key == "directions":
            return self._directions_markup(
                user, ctx="pq", done=button("Готово ➡️", data="pq:next:directions")
            )
        if key == "level":
            return self._toggle_markup(
                "pq",
                "level",
                r.LEVEL_BUTTONS,
                profile.target_levels(),
                nav=[*back, button("Далее ➡️", data="pq:next:level")],
            )
        if key == "work_formats":
            return self._toggle_markup(
                "pq",
                "fmt",
                WORK_FORMATS,
                profile.work_formats,
                nav=[*back, button("Далее ➡️", data="pq:next:work_formats")],
                per_row=3,
            )
        if key == "relocation":
            return inline_keyboard(
                [button("✈️ Да, готов", data="pq:reloc:yes"), button("Нет", data="pq:reloc:no")],
                back,
            )
        if key == "relocation_countries":
            return self._toggle_markup(
                "pq",
                "rc",
                RELOCATION_COUNTRIES,
                profile.relocation_countries,
                nav=[*back, button("Далее ➡️", data="pq:next:relocation_countries")],
            )
        # salary
        presets = [button(f"от {v}$", data=f"pq:sal:{v}") for v in SALARY_PRESETS]
        return inline_keyboard(
            presets[:3],
            presets[3:],
            [button("Не важно", data="pq:sal:0"), button("✍️ Своя сумма", data="pq:salc")],
            back,
        )

    async def _show_question(self, user: User, key: str, msg: dict[str, Any] | None) -> None:
        text = r.question(key)
        markup = self._question_markup(user, key)
        if msg:
            await self.edit(msg, text, markup)
        else:
            await self.reply(user, text, markup)

    async def show_profile(
        self,
        user: User,
        msg: dict[str, Any] | None = None,
        *,
        title: str = "👤 Профиль",
        summary: str = "",
    ) -> None:
        profile = self.db.get_profile(user.id)
        missing = profile.missing(user.filters)
        text = r.profile_card(profile, user.filters.queries, title=title, summary=summary)
        if profile.is_empty:
            text += "\n\n<i>Пришлите резюме PDF — заполню профиль автоматически.</i>"
        elif not user.onboarded:
            then = ", ".join(QUESTION_NAMES[q] for q in missing)
            text += (
                "\n\n<i>ИИ мог ошибиться — поправьте кнопками ниже то, что не так, "
                "и нажмите «Всё верно»." + (f" Потом спрошу {then}." if then else "") + "</i>"
            )
        rows = [
            [
                button("🔍 Направления", data="pe:directions"),
                button("🎓 Уровень", data="pe:level"),
            ],
            [
                button("🏢 Формат и город", data="pe:fmtcity"),
                button("✈️ Релокация", data="pe:relocation"),
            ],
            [button("💰 Зарплата", data="pe:salary"), button("🧩 Навыки", data="pe:skills")],
            [button("📝 Пожелания", data="pe:wishes")],
        ]
        if self.on_profile_edit:
            rows.append([button("✍️ Изменить словами", data="pe:nl")])
        if not user.onboarded:
            # онбординг: после разбора резюме человек проверяет профиль и идёт дальше
            rows.insert(0, [button("✅ Всё верно — дальше", data="ob:next")])
        elif missing:
            rows.insert(0, [button(f"✍️ Дозаполнить ({len(missing)})", data="pe:fill_missing")])
        if user.onboarded and self.on_resume and self.db.get_resumes(user.id):
            rows.append([button("📄 Обновить из резюме", data="pe:fill")])
        markup = inline_keyboard(*rows)
        if msg:
            await self.edit(msg, text, markup)
        else:
            await self.reply(user, text, markup)

    async def cmd_profile(self, user: User, args: str) -> None:
        await self.show_profile(user)

    # --- формат и город -------------------------------------------------------

    def _fmtcity_view(self, user: User) -> tuple[str, dict[str, Any]]:
        p = self.db.get_profile(user.id)
        city = p.city or "не указан"
        rows = [
            [
                button(f"{'✅ ' if k in p.work_formats else ''}{label}", data=f"pf:fmt:{k}")
                for k, label in WORK_FORMATS.items()
            ],
            [
                button(
                    f"{'✅' if p.only_home_city else '▫️'} Офис и гибрид только в г. {city}"[:60],
                    data="pf:city",
                )
            ],
            [button("🏙 Сменить город", data="pf:setcity")],
            [
                button(
                    f"{'✅' if p.strict_format else '▫️'} Скрывать вакансии без формата",
                    data="pf:strict",
                )
            ],
            [button("⬅️ К профилю", data="pe:show")],
        ]
        text = (
            "<b>🏢 Формат и город</b>\n\n"
            "Отметьте подходящие форматы. «Только мой город» убирает офис и гибрид в других "
            "городах вашей страны — удалёнка и релокация остаются.\n\n"
            "Район или метро напишите в 📝 Пожеланиях — ИИ сверит их с адресом вакансии."
        )
        return text, inline_keyboard(*rows)

    async def show_fmtcity(self, user: User, msg: dict[str, Any] | None = None) -> None:
        text, markup = self._fmtcity_view(user)
        if msg:
            await self.edit(msg, text, markup)
        else:
            await self.reply(user, text, markup)

    async def _cb_fmtcity(self, user: User, cb: dict, value: str, msg: dict) -> None:
        action, _, arg = value.partition(":")
        p = self.db.get_profile(user.id)
        note = None
        if action == "fmt" and arg in WORK_FORMATS:
            formats = (
                [x for x in p.work_formats if x != arg]
                if arg in p.work_formats
                else [
                    *p.work_formats,
                    arg,
                ]
            )
            if not formats:
                note = "Нужен хотя бы один формат"
            else:
                p.work_formats = [k for k in WORK_FORMATS if k in formats]
                p.mark("work_formats")
        elif action == "city":
            if not p.city:
                self._awaiting[user.id] = "city_pq"
                await self.api.answer_callback(cb["id"])
                await self.reply(user, "🏙 В каком городе вы живёте?")
                return
            p.only_home_city = not p.only_home_city
        elif action == "setcity":
            self._awaiting[user.id] = "city_pq"
            await self.api.answer_callback(cb["id"])
            await self.reply(user, "🏙 Напишите город, например <b>Минск</b>.")
            return
        elif action == "strict":
            p.strict_format = not p.strict_format
        self.db.save_profile(user.id, p)
        self._apply_profile(user)
        await self.api.answer_callback(cb["id"], note)
        await self.show_fmtcity(user, msg or None)

    # --- правка словами -------------------------------------------------------

    async def _nl_edit(self, user: User, text: str) -> None:
        if not self.on_profile_edit:
            await self.reply(user, "Не понял 🤔 Выберите раздел в меню ниже.", main_menu())
            return
        if len(text) > MAX_PHRASE_LEN:
            await self.reply(
                user,
                f"Слишком длинно — уложитесь в {MAX_PHRASE_LEN} символов. Резюме присылайте "
                "файлом PDF.",
            )
            return
        patch = await self.on_profile_edit(user, text)
        if patch is None:
            await self.reply(
                user, "ИИ сейчас недоступен — поменяйте настройки кнопками в 👤 Профиль."
            )
            return
        patch = {k: v for k, v in patch.items() if k in PATCH_KEYS or k == "unclear"}
        profile = self.db.get_profile(user.id)
        lines = preview_patch(
            profile, user.filters, patch, max_queries=self.billing.plan(user).max_queries
        )
        if not lines:
            unclear = str(patch.get("unclear") or "").strip()
            await self.reply(
                user,
                (
                    f"Не совсем понял: {escape(unclear)}\n\n"
                    if unclear
                    else "Не понял, что поменять 🤔\n\n"
                )
                + "Напишите, например: <i>только гибрид в Минске, во Фрунзенском районе, "
                "от 1000$</i> — или поменяйте кнопками в 👤 Профиль.",
            )
            return
        self._pending_patch[user.id] = patch
        await self.reply(
            user,
            "<b>Понял так:</b>\n" + "\n".join(f"• {escape(x)}" for x in lines) + "\n\nПрименить?",
            inline_keyboard(
                [button("✅ Применить", data="nl:apply"), button("✖️ Отмена", data="nl:cancel")]
            ),
        )

    async def _cb_nl(self, user: User, cb: dict, value: str, msg: dict) -> None:
        patch = self._pending_patch.pop(user.id, None)
        if value != "apply" or patch is None:
            await self.api.answer_callback(cb["id"], "Отменено" if value == "cancel" else None)
            if msg:
                await self.api.edit_reply_markup(msg["chat"]["id"], msg["message_id"], None)
            return
        profile = self.db.get_profile(user.id)
        lines = apply_patch(
            profile, user.filters, patch, max_queries=self.billing.plan(user).max_queries
        )
        self.db.save_profile(user.id, profile)
        self.db.save_filters(user.id, user.filters)
        await self.api.answer_callback(cb["id"], "Готово")
        if msg:
            await self.edit(
                msg, "✅ <b>Применено:</b>\n" + "\n".join(f"• {escape(x)}" for x in lines), None
            )
        await self.show_profile(user)

    # --- выборки (общие для анкеты и фильтров) -------------------------------

    def _directions_markup(
        self, user: User, *, ctx: str, done: dict[str, str] | None = None
    ) -> dict[str, Any]:
        selected = {q.lower() for q in user.filters.queries}
        keys = list(r.DIRECTIONS)
        rows = []
        for i in range(0, len(keys), 2):
            rows.append(
                [
                    button(
                        f"{'✅ ' if r.DIRECTIONS[k][1] in selected else ''}{r.DIRECTIONS[k][0]}",
                        data=f"{ctx}:dir:{k}",
                    )
                    for k in keys[i : i + 2]
                ]
            )
        custom = [q for q in user.filters.queries if q.lower() not in _PRESET_QUERIES]
        for q in custom:
            rows.append(
                [button(f"✅ {q[:40]}", data=f"{ctx}:dirx:{user.filters.queries.index(q)}")]
            )
        nav = [button("✍️ Свой вариант", data=f"{ctx}:custom")]
        nav.append(done or button("⬅️ Назад", data="f:menu"))
        rows.append(nav)
        return inline_keyboard(*rows)

    @staticmethod
    def _toggle_markup(
        ctx: str,
        field: str,
        options: dict[str, str],
        selected: list[str],
        *,
        nav: list[dict[str, str]],
        per_row: int = 2,
    ) -> dict[str, Any]:
        keys = list(options)
        rows = [
            [
                button(f"{'✅ ' if k in selected else ''}{options[k]}", data=f"{ctx}:{field}:{k}")
                for k in keys[i : i + per_row]
            ]
            for i in range(0, len(keys), per_row)
        ]
        rows.append(nav)
        return inline_keyboard(*rows)

    def _toggle_query(self, user: User, query: str, *, add_only: bool = False) -> str | None:
        """Добавить/убрать направление. Возвращает текст ошибки, если упёрлись в тариф."""
        f = user.filters
        lowered = [q.lower() for q in f.queries]
        if query.lower() in lowered:
            if add_only:
                return None
            f.queries = [q for q in f.queries if q.lower() != query.lower()]
        else:
            limit = self.billing.plan(user).max_queries
            if len(f.queries) >= limit:
                return f"На Free — {limit} направление. В 💎 Pro — до {PRO.max_queries}."
            f.queries = [*f.queries, query]
        self.db.save_filters(user.id, f)
        return None

    # --- разделы меню --------------------------------------------------------

    async def show_search(self, user: User) -> None:
        plan = self.billing.plan(user)
        text = r.search_summary(
            user.filters,
            self.available_sources,
            paused=user.paused,
            resumes=self._resume_count(user),
            plan=plan,
            check_every_min=plan.check_every_min,
        )
        pause = (
            button("▶️ Возобновить", data="f:unpause")
            if user.paused
            else button("⏸ Пауза", data="f:pause")
        )
        await self.reply(
            user,
            text,
            inline_keyboard(
                [button("⚙️ Изменить", data="f:menu"), pause],
                [button("📊 Статистика", data="st:show"), button("👤 Профиль", data="pe:show")],
            ),
        )

    def _filters_menu(self, user: User) -> tuple[str, dict[str, Any]]:
        f = user.filters
        rows = [
            [
                button(f"🔍 Направления ({len(f.queries)})", data="f:dir"),
                button("🎓 Уровень", data="f:lvl"),
            ],
            [
                button("🌍 Страны", data="f:cty"),
                button("🏢 Формат и город", data="pe:fmtcity"),
            ],
            [
                button(
                    f"💰 Зарплата: {f'от {f.min_salary_usd}$' if f.min_salary_usd else '—'}",
                    data="f:salary",
                ),
                button(f"🚫 Стоп-слова ({len(f.exclude)})", data="f:exclude"),
            ],
            [
                button("🔎 Площадки", data="f:src"),
                button(f"🤖 Порог ИИ: {f.ai_threshold}", data="f:thr"),
            ],
        ]
        return "<b>⚙️ Фильтры</b>\nЧто настроить?", inline_keyboard(*rows)

    async def show_filters(self, user: User) -> None:
        text, markup = self._filters_menu(user)
        await self.reply(user, text, markup)

    async def show_plan(self, user: User, msg: dict[str, Any] | None = None) -> None:
        plan = self.billing.plan(user)
        cfg = self.settings.billing
        subscribed = bool(user.sub_charge_id)
        text = r.plan_screen(
            plan,
            until=user.plan_until if plan is PRO and not self._is_admin(user) else None,
            trial=self.billing.is_trial(user),
            subscribed=subscribed,
            price_stars=cfg.pro_price_stars if cfg.payments_enabled else 0,
            support=cfg.support_contact,
        )
        rows = []
        if cfg.payments_enabled and not subscribed and not self._is_admin(user):
            try:
                link = await self.api.create_invoice_link(
                    title="Job Spy Pro — 30 дней",
                    description="Все направления и резюме, вакансии без лимита, ИИ-оценка, "
                    "отклик с письмом, проверка каждые 30 минут.",
                    payload=f"pro:{user.id}",
                    amount=cfg.pro_price_stars,
                    subscription_period=SUBSCRIPTION_PERIOD_SECONDS,
                )
                label = "💎 Продлить Pro" if plan is PRO else "💎 Подключить Pro"
                rows.append([button(f"{label} — {cfg.pro_price_stars} ⭐/мес", url=link)])
            except TelegramError as exc:
                log.warning("invoice link failed: %s", exc)
        if subscribed:
            rows.append([button("Отменить автопродление", data="sub:cancel")])
        markup = inline_keyboard(*rows) if rows else None
        if msg:
            await self.edit(msg, text, markup)
        else:
            await self.reply(user, text, markup)

    async def show_stats(self, user: User) -> None:
        s = self.db.stats(user.id)
        plan = self.billing.plan(user)
        today = self.db.sent_today(user.id)
        limit = plan.daily_vacancies
        tokens = self.db.tokens_today(user.id)
        await self.reply(
            user,
            "<b>📊 Статистика за 7 дней</b>\n\n"
            f"📨 Прислал вакансий: <b>{s.get('sent', 0) + s.get('disliked', 0)}</b>\n"
            f"🧹 Отсеял фильтрами: {s.get('filtered', 0)}\n"
            f"🤖 Отсеял ИИ как неподходящие: {s.get('ai_rejected', 0)}\n"
            f"👎 Вы скрыли: {s.get('disliked', 0)}\n\n"
            f"<b>Сегодня</b>: {today}" + (f" из {limit}" if limit else "") + " вакансий, "
            f"ИИ — {tokens * 100 // max(1, self.billing.ai_token_limit(user))}% дневного лимита",
        )

    async def show_help(self, user: User) -> None:
        await self.reply(user, r.HELP, main_menu())

    # --- команды (для тех, кто привык к командам) ----------------------------

    async def cmd_menu(self, user: User, args: str) -> None:
        await self.reply(user, "Главное меню 👇", main_menu())

    async def cmd_search(self, user: User, args: str) -> None:
        await self.show_search(user)

    async def cmd_settings(self, user: User, args: str) -> None:
        await self.show_search(user)

    async def cmd_filters(self, user: User, args: str) -> None:
        await self.show_filters(user)

    async def cmd_plan(self, user: User, args: str) -> None:
        await self.show_plan(user)

    async def cmd_help(self, user: User, args: str) -> None:
        await self.show_help(user)

    async def cmd_stats(self, user: User, args: str) -> None:
        await self.show_stats(user)

    # --- мои данные ------------------------------------------------------------

    async def cmd_privacy(self, user: User, args: str) -> None:
        await self.reply(
            user,
            r.privacy_text(
                self.settings.guard.inactive_days, self.settings.billing.support_contact
            ),
            inline_keyboard([button("🗑 Удалить мои данные", data="del:ask")]),
        )

    async def cmd_delete_me(self, user: User, args: str) -> None:
        await self._ask_delete(user)

    async def _ask_delete(self, user: User, msg: dict[str, Any] | None = None) -> None:
        text = (
            "<b>🗑 Удалить мои данные?</b>\n\n"
            "Удалю резюме, профиль, фильтры и историю вакансий. Восстановить их будет нельзя. "
            "Подписка Pro, если есть, сохранится до конца оплаченного срока, автопродление "
            "отключу."
        )
        markup = inline_keyboard(
            [button("🗑 Да, удалить", data="del:yes"), button("Отмена", data="del:no")]
        )
        if msg:
            await self.edit(msg, text, markup)
        else:
            await self.reply(user, text, markup)

    async def delete_user_data(self, user: User) -> None:
        """Удаление по просьбе пользователя или из админки."""
        if user.sub_charge_id:
            try:
                await self.api.set_star_subscription_canceled(user.id, user.sub_charge_id, True)
                self.db.set_subscription(user.id, None)
            except TelegramError as exc:
                log.warning("cancel subscription of %s failed: %s", user.id, exc)
        self.db.delete_user_data(user.id)
        for state in (self._awaiting, self._profile_mode, self._pending_patch, self._cap_notified):
            state.pop(user.id, None)
        user.onboarded = False

    async def _cb_delete_data(self, user: User, cb: dict, value: str, msg: dict) -> None:
        if value == "ask":
            await self.api.answer_callback(cb["id"])
            await self._ask_delete(user, msg or None)
            return
        if value != "yes":
            await self.api.answer_callback(cb["id"], "Отменено")
            if msg:
                await self.api.edit_reply_markup(msg["chat"]["id"], msg["message_id"], None)
            return
        await self.delete_user_data(user)
        await self.api.answer_callback(cb["id"], "Удалено")
        text = "✅ Ваши данные удалены. Чтобы начать заново — /start."
        if msg:
            await self.edit(msg, text, None)
        else:
            await self.reply(user, text)
        await self.notify_admins(f"🗑 Пользователь <code>{user.id}</code> удалил свои данные")

    async def _update_list(self, user: User, args: str, attr: str, title: str) -> None:
        f = user.filters
        if args:
            values = [] if args == "-" else parse_list(args)
            if attr == "queries":
                limit = self.billing.plan(user).max_queries
                if len(values) > limit:
                    await self.reply(
                        user,
                        f"На вашем тарифе — до {limit} направлений, сохранил первые {limit}. "
                        f"В 💎 Pro — до {PRO.max_queries}.",
                    )
                    values = values[:limit]
            setattr(f, attr, values)
            self.db.save_filters(user.id, f)
        values = getattr(f, attr)
        await self.reply(user, f"{title}: {escape(', '.join(values)) if values else '—'}")

    async def cmd_query(self, user: User, args: str) -> None:
        await self._update_list(user, args, "queries", "🔍 Направления")

    async def cmd_include(self, user: User, args: str) -> None:
        await self._update_list(user, args, "include", "✅ Слова в названии")

    async def cmd_exclude(self, user: User, args: str) -> None:
        await self._update_list(user, args, "exclude", "🚫 Стоп-слова")

    async def cmd_skills(self, user: User, args: str) -> None:
        await self._update_list(user, args, "skills", "🧩 Навыки")

    async def cmd_level(self, user: User, args: str) -> None:
        if args:
            tokens = [] if args == "-" else re.split(r"[\s,;]+", args.lower())
            valid = {lvl.value for lvl in Level}
            bad = [t for t in tokens if t and t not in valid]
            if bad:
                await self.reply(
                    user,
                    f"Неизвестный уровень: {escape(', '.join(bad))}. Доступны: {', '.join(valid)}",
                )
                return
            user.filters.levels = [t for t in dict.fromkeys(tokens) if t]
            self._save_filter_edit(user, "levels")
        await self.reply(user, f"🎓 Уровень: {', '.join(user.filters.levels) or 'любой'}")

    async def cmd_country(self, user: User, args: str) -> None:
        if args:
            tokens = [] if args == "-" else re.split(r"[\s,;]+", args.upper())
            bad = [t for t in tokens if t and t not in COUNTRIES]
            if bad:
                await self.reply(
                    user, f"Неизвестно: {escape(', '.join(bad))}. Доступны: {' '.join(COUNTRIES)}"
                )
                return
            user.filters.countries = [t for t in dict.fromkeys(tokens) if t]
            self._save_filter_edit(user, "countries")
        shown = ", ".join(COUNTRIES[c] for c in user.filters.countries) or "любые"
        await self.reply(user, f"🌍 Страны: {shown}")

    async def cmd_remote(self, user: User, args: str) -> None:
        user.filters.remote_only = not user.filters.remote_only
        self._save_filter_edit(user, "remote_only")
        await self.reply(user, f"🏠 Только удалёнка: {'да' if user.filters.remote_only else 'нет'}")

    async def cmd_salary(self, user: User, args: str) -> None:
        if args:
            try:
                salary = parse_salary(args)
            except InputError as exc:
                await self.reply(user, str(exc))
                return
            if salary is None:
                await self.reply(
                    user, "Формат: <code>/salary 1500</code> или <code>/salary 1500 strict</code>"
                )
                return
            user.filters.min_salary_usd = salary
            user.filters.require_salary = "strict" in args.lower().split()
            self._save_filter_edit(user, "min_salary_usd")
        f = user.filters
        text = f"от {f.min_salary_usd}$" if f.min_salary_usd else "не важна"
        if f.min_salary_usd and f.require_salary:
            text += ", без указанной зарплаты — скрывать"
        await self.reply(user, f"💰 Зарплата: {text}")

    async def cmd_threshold(self, user: User, args: str) -> None:
        if args:
            value = parse_int(args)
            if value is None or not 0 <= value <= 100:
                await self.reply(user, "Число от 0 до 100, например <code>/threshold 60</code>")
                return
            user.filters.ai_threshold = value
            self.db.save_filters(user.id, user.filters)
        await self.reply(user, f"🤖 Порог ИИ: {user.filters.ai_threshold}/100")

    async def cmd_sources(self, user: User, args: str) -> None:
        await self.reply(user, self._sources_text(), self._sources_markup(user.filters))

    async def cmd_pause(self, user: User, args: str) -> None:
        self.db.set_paused(user.id, True)
        await self.reply(user, "⏸ Поиск на паузе. Возобновить — в разделе 🔎 Мой поиск.")

    async def cmd_unpause(self, user: User, args: str) -> None:
        self.db.set_paused(user.id, False)
        await self.reply(user, "▶️ Поиск возобновлён.")

    # --- площадки ------------------------------------------------------------

    def _sources_text(self) -> str:
        missing = [REGISTRY[s].title for s in SOURCE_ORDER if s not in self.available_sources]
        note = f"\n<i>Скоро: {escape(', '.join(missing))}</i>" if missing else ""
        return "<b>🔎 Площадки</b>\nНажмите, чтобы включить или выключить." + note

    def _sources_markup(self, f: UserFilters) -> dict[str, Any]:
        enabled = set(f.sources or self.available_sources)
        options = {name: REGISTRY[name].title for name in self.available_sources}
        return self._toggle_markup(
            "f", "src", options, list(enabled), nav=[button("⬅️ Назад", data="f:menu")]
        )

    # --- резюме --------------------------------------------------------------

    def _resume_count(self, user: User) -> int:
        return len(self.db.get_resumes(user.id))

    async def cmd_resume(self, user: User, args: str) -> None:
        if args:
            await self._save_resume(user, args, title=f"Резюме {self._resume_count(user) + 1}")
            return
        self._awaiting[user.id] = "resume"
        await self.reply(user, "📄 Пришлите резюме файлом (PDF / .txt) или текстом.")

    async def cmd_resumes(self, user: User, args: str) -> None:
        await self.show_resumes(user)

    async def show_resumes(self, user: User) -> None:
        text, markup = self._resumes_view(user)
        await self.reply(user, text, markup)

    def _resumes_view(self, user: User) -> tuple[str, dict[str, Any] | None]:
        resumes = self.db.get_resumes(user.id)
        limit = self.billing.plan(user).max_resumes
        if not resumes:
            return (
                "<b>📄 Резюме</b>\n\nПока пусто. Просто пришлите PDF в этот чат — "
                "ИИ разберёт его и будет сравнивать с каждой вакансией.",
                None,
            )
        lines = [
            f"<b>📄 Резюме</b> ({len(resumes)} из {limit})",
            "Под каждую вакансию выбираю то, с которым больше шансов на оффер.",
            "",
        ]
        for res in resumes:
            skills = ", ".join(res.skills[:8]) or "⏳ профиль не готов"
            icon = "📎" if res.file_id else "📝"
            lines.append(f"{icon} <b>{escape(res.title)}</b>\n    {escape(skills)}")
        lines += ["", "Добавить — пришлите ещё один PDF."]
        rows = [[button(f"🗑 {res.title[:30]}", data=f"rdel:{res.id}")] for res in resumes]
        return "\n".join(lines), inline_keyboard(*rows)

    async def _receive_document(self, user: User, doc: dict[str, Any]) -> None:
        name = doc.get("file_name") or "resume"
        if (doc.get("file_size") or 0) > MAX_FILE_BYTES:
            await self.reply(user, "Файл слишком большой (максимум 3 МБ).")
            return
        limit = self.settings.guard.max_uploads_per_day
        if not self._is_admin(user) and self.db.count_today(user.id, "upload") >= limit:
            await self.reply(user, f"На сегодня лимит — {limit} файлов. Попробуйте завтра.")
            return
        self.db.bump(user.id, "upload")
        try:
            data = await self.api.download_file(doc["file_id"], max_bytes=MAX_FILE_BYTES)
            text = extract_text(data, name)
        except (ResumeFileError, ValueError, TelegramError) as exc:
            await self.reply(user, f"Не удалось прочитать «{escape(name)}»: {escape(str(exc))}")
            return
        await self._save_resume(
            user, text, title=title_from_filename(name), file_id=doc["file_id"], file_name=name
        )

    async def _save_resume(
        self,
        user: User,
        text: str,
        *,
        title: str,
        file_id: str | None = None,
        file_name: str | None = None,
    ) -> None:
        self._awaiting.pop(user.id, None)
        text = text.strip()[:MAX_RESUME_CHARS]
        if len(text) < 50 or not looks_like_resume(text):
            if not user.onboarded or file_id is None:
                self._awaiting[user.id] = "resume"
            await self.reply(
                user,
                "Это не похоже на резюме 🤔 Пришлите PDF или текст с опытом и навыками.",
            )
            return
        summary = ""
        resume = self.db.find_resume(user.id, text)
        if resume is not None:
            # тот же файл ещё раз — не плодим копии
            await self.reply(user, f"Это резюме уже загружено: «{escape(resume.title)}».")
            summary = _summary(resume)
        else:
            limit = self.billing.plan(user).max_resumes
            if self._resume_count(user) >= limit:
                extra = f" В 💎 Pro — до {PRO.max_resumes}." if limit < PRO.max_resumes else ""
                await self.reply(user, f"Уже {limit} резюме — удалите лишнее в 📄 Резюме.{extra}")
                return
            resume_id = self.db.add_resume(
                user.id, title, text, file_id=file_id, file_name=file_name
            )
            resume = self.db.get_resume(user.id, resume_id)
            assert resume is not None
            if self.on_resume:
                summary = await self._read_resume(user, resume)
            else:
                await self.reply(user, f"✅ Резюме «{escape(title)}» сохранено.")
        understood = self.on_resume and not self.db.get_profile(user.id).is_empty
        if not user.onboarded:
            self._profile_mode[user.id] = "ob"
            if understood:
                # сначала человек проверяет и правит то, что понял ИИ, потом — вопросы
                self._apply_profile(user)
                await self.show_profile(user, title="👤 Вот что я понял из резюме", summary=summary)
            else:
                await self._ask_next(user)
        elif understood:
            self._apply_profile(user)
            await self.show_profile(user, title="👤 Профиль дополнен из резюме", summary=summary)

    async def _read_resume(self, user: User, resume: Resume) -> str:
        """Разбор резюме ИИ. Удалось — краткое содержание для карточки профиля (её
        покажет вызывающий, одним сообщением); нет — ответ ИИ-модуля уходит как есть."""
        assert self.on_resume is not None
        await self.reply(user, f"⏳ Читаю «{escape(resume.title)}»…")
        answer = await self.on_resume(user, resume)
        parsed = self.db.get_resume(user.id, resume.id)
        if parsed is None or not parsed.profile:
            await self.reply(user, answer)
            return ""
        return _summary(parsed)

    # --- текстовый ввод -------------------------------------------------------

    async def _ask_salary(self, user: User, field: str, text: str) -> int | None:
        """Сумма из ответа или None — тогда переспрашиваем, оставаясь в том же поле."""
        try:
            salary = parse_salary(text)
            problem = None if salary is not None else "Нужно число в долларах, например 1200."
        except InputError as exc:
            salary, problem = None, str(exc)
        if problem:
            self._awaiting[user.id] = field
            await self.reply(user, f"{problem} Попробуйте ещё раз.")
        return salary

    async def _receive_input(self, user: User, field: str, text: str) -> None:
        f = user.filters
        if field == "resume":
            await self._save_resume(user, text, title=f"Резюме {self._resume_count(user) + 1}")
            return
        if field == "city_pq":
            profile = self.db.get_profile(user.id)
            city = clean_text(text, MAX_CITY_LEN)
            if not city:
                self._awaiting[user.id] = field
                await self.reply(user, "Напишите название города, например <b>Минск</b>.")
                return
            profile.city = city
            profile.only_home_city = True
            self.db.save_profile(user.id, profile)
            self._apply_profile(user)
            await self.show_fmtcity(user)
            return
        if field == "wishes_pq":
            profile = self.db.get_profile(user.id)
            profile.wishes = "" if text.strip() == "-" else clean_text(text, MAX_WISHES_LEN)
            self.db.save_profile(user.id, profile)
            await self.show_profile(user)
            return
        if field == "nl":
            await self._nl_edit(user, text)
            return
        if field == "queries_pq":
            for q in parse_list(text):
                if err := self._toggle_query(user, q, add_only=True):
                    await self.reply(user, err)
                    break
            await self._show_question(user, "directions", None)
            return
        if field == "salary_pq":
            salary = await self._ask_salary(user, field, text)
            if salary is None:
                return
            profile = self.db.get_profile(user.id)
            profile.salary_usd = salary
            profile.mark("salary")
            self.db.save_profile(user.id, profile)
            await self._after_answer(user, None)
            return
        if field == "skills_pq":
            profile = self.db.get_profile(user.id)
            profile.skills = (
                [] if text.strip() == "-" else clean_list(_SPLIT.split(text), max_items=40)
            )
            self.db.save_profile(user.id, profile)
            f.skills = profile.skills
            self.db.save_filters(user.id, f)
            await self.show_profile(user)
            return
        if field == "queries":
            errors = [
                err for q in parse_list(text) if (err := self._toggle_query(user, q, add_only=True))
            ]
            if errors:
                await self.reply(
                    user, errors[0], inline_keyboard([button("💎 Pro", data="plan:show")])
                )
            await self.reply(user, "🔍 Направления", self._directions_markup(user, ctx="f"))
            return
        if field == "exclude":
            f.exclude = [] if text.strip() == "-" else parse_list(text)
            self.db.save_filters(user.id, f)
            await self.reply(user, f"🚫 Стоп-слова: {escape(', '.join(f.exclude)) or '—'}")
        elif field == "salary":
            salary = await self._ask_salary(user, field, text)
            if salary is None:
                return
            f.min_salary_usd = salary
            self._save_filter_edit(user, "min_salary_usd")
            await self.reply(
                user,
                f"💰 Зарплата: {f'от {f.min_salary_usd}$' if f.min_salary_usd else 'не важна'}",
            )
        text_menu, markup = self._filters_menu(user)
        await self.reply(user, text_menu, markup)

    # --- оплата ---------------------------------------------------------------

    async def handle_pre_checkout(self, query: dict[str, Any]) -> None:
        cfg = self.settings.billing
        ok = (
            query.get("currency") == "XTR"
            and str(query.get("invoice_payload", "")).startswith("pro:")
            and int(query.get("total_amount") or 0) == cfg.pro_price_stars
            and cfg.payments_enabled
        )
        await self.api.answer_pre_checkout(
            query["id"], ok, None if ok else "Цена изменилась — откройте 💎 Подписка ещё раз."
        )

    async def _on_successful_payment(self, user: User, payment: dict[str, Any]) -> None:
        recurring_renewal = payment.get("is_recurring") and not payment.get("is_first_recurring")
        until = self.billing.on_payment(user, payment)
        if recurring_renewal:
            await self.reply(user, f"💎 Pro продлён до {r.date_ru(until)}. Спасибо, что с нами!")
        else:
            await self.reply(
                user,
                f"🎉 <b>Pro подключён до {r.date_ru(until)}!</b>\n\n"
                "Все направления и резюме, вакансии без лимита, ИИ-оценка, отклик с письмом "
                "и проверка каждые 30 минут. Подписка продлевается автоматически, отменить — "
                "в 💎 Подписка.",
                main_menu(),
            )
        await self.notify_admins(
            f"💰 Оплата: {escape(user.username)} (<code>{user.id}</code>) — "
            f"{payment.get('total_amount')} ⭐, Pro до {r.date_ru(until)}"
        )

    # --- админка -------------------------------------------------------------

    async def cmd_admin(self, user: User, args: str) -> None:
        if not self._is_admin(user):
            return await self.reply(user, "Только для администратора.")
        s = self.db.business_stats()
        cfg = self.settings.billing
        await self.reply(
            user,
            "<b>🛠 Админка</b>\n\n"
            f"👥 Пользователей: <b>{s['users']}</b> (активны за неделю: {s['active_week']})\n"
            f"💎 На Pro: {s['pro']}, из них платят: {s['paying']} "
            f"(автопродление: {s['subscriptions']})\n"
            f"⭐ Выручка за месяц: <b>{s['stars_month']}</b>, всего: {s['stars_total']}\n\n"
            f"Доступ: <code>{cfg.access_mode}</code>, пробный: {cfg.trial_days} дн., "
            f"цена: {cfg.pro_price_stars} ⭐\n\n"
            "/invite — одноразовая ссылка\n"
            "/users — список\n"
            "<code>/grant ID ДНЕЙ</code> — выдать Pro\n"
            "<code>/refund ID</code> — вернуть последний платёж\n"
            "<code>/ban ID</code>, <code>/unban ID</code> — заблокировать / разблокировать"
            + (
                "\n\n🖥 Веб-админка: "
                f"<code>http://{self.settings.admin_web.host}:{self.settings.admin_web.port}</code>"
                if self.settings.admin_web.enabled
                else ""
            ),
        )

    async def cmd_ban(self, user: User, args: str) -> None:
        await self._set_ban(user, args, True)

    async def cmd_unban(self, user: User, args: str) -> None:
        await self._set_ban(user, args, False)

    async def _set_ban(self, user: User, args: str, banned: bool) -> None:
        if not self._is_admin(user):
            return await self.reply(user, "Только для администратора.")
        target_id = parse_int(args.split()[0]) if args.split() else None
        if target_id is None:
            return await self.reply(
                user, f"Формат: <code>/{'ban' if banned else 'unban'} ID</code>"
            )
        if target_id in self.settings.admin_ids:
            return await self.reply(user, "Администратора забанить нельзя.")
        target = self.db.get_user(target_id)
        if target is None:
            return await self.reply(user, "Пользователь не найден.")
        self.db.set_banned(target_id, banned)
        name = escape(target.username or str(target_id))
        await self.reply(user, f"{'🚫 Забанен' if banned else '✅ Разбанен'}: {name}")

    async def cmd_invite(self, user: User, args: str) -> None:
        if not self._is_admin(user):
            return await self.reply(user, "Только для администратора.")
        code = self.db.create_invite(user.id)
        await self.reply(user, f"Одноразовая ссылка:\nhttps://t.me/{self.username}?start={code}")

    async def cmd_users(self, user: User, args: str) -> None:
        if not self._is_admin(user):
            return await self.reply(user, "Только для администратора.")
        lines = []
        for u in self.db.all_users():
            plan = self.billing.plan(u)
            badge = "💎" if plan is PRO else ("🆓" if u.allowed else "🔒")
            lines.append(
                f"{badge}{'⏸' if u.paused else ''} <code>{u.id}</code> {escape(u.username)} — "
                f"{len(u.filters.queries)} напр."
            )
        await self.reply(user, "\n".join(lines[-80:]) or "Пользователей нет")

    async def cmd_grant(self, user: User, args: str) -> None:
        if not self._is_admin(user):
            return await self.reply(user, "Только для администратора.")
        parts = [parse_int(p) for p in args.split()]
        if len(parts) != 2 or parts[0] is None or parts[1] is None:
            return await self.reply(user, "Формат: <code>/grant ID ДНЕЙ</code>")
        if abs(parts[1]) > MAX_GRANT_DAYS:
            return await self.reply(user, f"Не больше {MAX_GRANT_DAYS} дней за раз.")
        target = self.db.get_user(parts[0])
        if target is None:
            return await self.reply(user, "Пользователь не найден.")
        self.db.set_allowed(target.id, True)
        until = self.billing.extend(target, parts[1])
        await self.reply(user, f"✅ {escape(target.username)}: Pro до {r.date_ru(until)}")
        try:
            await self.api.send_message(
                target.id,
                f"🎁 Вам подключён 💎 Pro до {r.date_ru(until)}!",
                reply_markup=main_menu(),
            )
        except TelegramError as exc:
            log.info("grant notice failed: %s", exc)

    async def cmd_refund(self, user: User, args: str) -> None:
        if not self._is_admin(user):
            return await self.reply(user, "Только для администратора.")
        target_id = parse_int(args)
        if target_id is None:
            return await self.reply(user, "Формат: <code>/refund ID</code>")
        args = str(target_id)
        charge = self.db.last_payment(target_id)
        if charge is None:
            return await self.reply(user, "Платежей не найдено.")
        try:
            await self.api.refund_star_payment(int(args), charge)
        except TelegramError as exc:
            return await self.reply(user, f"Не удалось: {escape(exc.description)}")
        self.db.set_subscription(int(args), None)
        await self.reply(
            user, "✅ Звёзды возвращены. Pro не отозван — при необходимости /grant ID -N"
        )

    # --- кнопки --------------------------------------------------------------

    async def handle_callback(self, cb: dict[str, Any]) -> None:
        user = self._user_from(cb["from"])
        data = cb.get("data") or ""
        prefix, _, value = data.partition(":")
        msg = cb.get("message") or {}

        if user.banned:
            await self.api.answer_callback(cb["id"])
            return

        if prefix in ("allow", "deny") and self._is_admin(user):
            target_id = parse_int(value)
            if target_id is None:
                await self.api.answer_callback(cb["id"])
                return
            target = self.db.get_user(target_id)
            self.db.set_allowed(target_id, prefix == "allow")
            await self.api.answer_callback(cb["id"], "Готово")
            if msg:
                await self.api.edit_reply_markup(msg["chat"]["id"], msg["message_id"], None)
            if prefix == "allow" and target:
                await self.api.send_message(
                    target.id, "✅ Доступ открыт! Нажмите /start, чтобы настроить поиск."
                )
            return

        if not user.allowed:
            await self.api.answer_callback(cb["id"])
            return

        handler = {
            "nope": self._cb_nope,
            "rdel": self._cb_resume_delete,
            "ob": self._cb_onboarding,
            "pq": self._cb_question,
            "pe": self._cb_profile,
            "st": self._cb_stats,
            "pf": self._cb_fmtcity,
            "nl": self._cb_nl,
            "f": self._cb_filters,
            "plan": self._cb_plan,
            "sub": self._cb_subscription,
            "src": self._cb_sources_legacy,
            "del": self._cb_delete_data,
            "back": self._cb_comeback,
        }.get(prefix)
        if handler:
            await handler(user, cb, value, msg)
            return
        if prefix in self.extra_buttons:
            await self.extra_buttons[prefix][1](user, cb, value)
            return
        await self.api.answer_callback(cb["id"])

    async def _cb_nope(self, user: User, cb: dict, value: str, msg: dict) -> None:
        if value.isascii() and value.isdigit():
            self.db.set_status(user.id, int(value), "disliked")
        await self.api.answer_callback(cb["id"], "Скрыто")
        if msg:
            first = msg.get("reply_markup", {}).get("inline_keyboard", [[]])[0][:1]
            await self.api.edit_reply_markup(
                msg["chat"]["id"], msg["message_id"], inline_keyboard(first)
            )

    async def _cb_resume_delete(self, user: User, cb: dict, value: str, msg: dict) -> None:
        deleted = value.isascii() and value.isdigit() and self.db.delete_resume(user.id, int(value))
        await self.api.answer_callback(cb["id"], "Удалено" if deleted else "Уже удалено")
        if msg:
            text, markup = self._resumes_view(user)
            await self.edit(msg, text, markup)

    async def _cb_comeback(self, user: User, cb: dict, value: str, msg: dict) -> None:
        """«С возвращением»: продолжить с прежними настройками или начать заново."""
        await self.api.answer_callback(cb["id"])
        if value == "keep":
            self.db.set_stopped(user.id, False)
            self.db.set_paused(user.id, False)
            user.stopped_at, user.paused = None, False
            if msg:
                await self.edit(msg, "▶️ Продолжаем с прежними настройками.", None)
            await self.cmd_start(user, "")
        elif value == "reset":
            await self.reset_setup(user)
            if msg:
                await self.edit(msg, "🔄 Начинаем заново — прежние настройки стёрты.", None)
            await self.cmd_start(user, "")
        elif value == "cancel" and msg:
            await self.edit(msg, "Оставили всё как есть.", None)

    async def cmd_reset(self, user: User, args: str) -> None:
        await self.reply(
            user,
            "<b>🔄 Настроить поиск заново?</b>\n\n"
            "Сотру резюме, профиль и фильтры и задам вопросы с начала. "
            "Подписка и история вакансий сохранятся.",
            inline_keyboard(
                [button("🔄 Да, заново", data="back:reset"), button("Отмена", data="back:cancel")]
            ),
        )

    async def _cb_onboarding(self, user: User, cb: dict, value: str, msg: dict) -> None:
        await self.api.answer_callback(cb["id"])
        if value in ("manual", "next") and not user.onboarded:
            self._awaiting.pop(user.id, None)
            self._profile_mode[user.id] = "ob"
            await self._ask_next(user, msg or None)

    async def _cb_question(self, user: User, cb: dict, value: str, msg: dict) -> None:
        """Ответы на вопросы анкеты профиля."""
        action, _, arg = value.partition(":")
        profile = self.db.get_profile(user.id)
        note = None
        answered = False
        rerender: str | None = None
        if action == "dir" and arg in r.DIRECTIONS:
            note = self._toggle_query(user, r.DIRECTIONS[arg][1])
            rerender = "directions"
        elif (
            action == "dirx"
            and arg.isascii()
            and arg.isdigit()
            and int(arg) < len(user.filters.queries)
        ):
            self._toggle_query(user, user.filters.queries[int(arg)])
            rerender = "directions"
        elif action == "custom":
            self._awaiting[user.id] = "queries_pq"
            await self.reply(
                user,
                "✍️ Напишите, что ищете — через запятую.\n"
                "Например: <i>python developer, тестировщик, аналитик данных</i>",
            )
        elif action == "level" and arg in r.LEVEL_BUTTONS:
            profile.toggle_level(arg)
            user.filters.levels = profile.target_levels()
            self.db.save_filters(user.id, user.filters)
            rerender = "level"
        elif action == "fmt" and arg in WORK_FORMATS:
            fmts = profile.work_formats
            profile.work_formats = [x for x in fmts if x != arg] if arg in fmts else [*fmts, arg]
            rerender = "work_formats"
        elif action == "reloc":
            profile.relocation = arg == "yes"
            if profile.relocation:
                rerender = "relocation_countries"
            else:
                profile.relocation_countries = []
                profile.mark("relocation")
                answered = True
        elif action == "rc" and arg in RELOCATION_COUNTRIES:
            rc = profile.relocation_countries
            profile.relocation_countries = [x for x in rc if x != arg] if arg in rc else [*rc, arg]
            rerender = "relocation_countries"
        elif action == "sal" and arg in {str(v) for v in (0, *SALARY_PRESETS)}:
            profile.salary_usd = int(arg)
            profile.mark("salary")
            answered = True
        elif action == "salc":
            self._awaiting[user.id] = "salary_pq"
            await self.reply(user, "💰 Напишите сумму в $ в месяц, например <b>1300</b>.")
        elif action == "next":
            if arg == "directions" and not user.filters.queries:
                note = "Выберите хотя бы одно направление"
            elif arg == "work_formats" and not profile.work_formats:
                note = "Выберите хотя бы один формат"
            elif arg == "level" and not profile.target_levels():
                note = "Выберите хотя бы один уровень"
            elif arg in ("directions", "level", "work_formats", "relocation_countries"):
                if arg == "level":
                    profile.set_levels(profile.target_levels())
                profile.mark("relocation" if arg == "relocation_countries" else arg)
                answered = True
        self.db.save_profile(user.id, profile)
        await self.api.answer_callback(cb["id"], note)
        if rerender:
            await self._show_question(user, rerender, msg or None)
        elif answered:
            await self._after_answer(user, msg or None)

    async def _cb_profile(self, user: User, cb: dict, value: str, msg: dict) -> None:
        """Кнопки экрана профиля."""
        await self.api.answer_callback(cb["id"])
        if value == "show":
            self._profile_mode.pop(user.id, None)
            await self.show_profile(user, msg or None)
        elif value in ("directions", "level", "work_formats", "relocation", "salary"):
            self._profile_mode[user.id] = "edit"
            await self._show_question(user, value, msg or None)
        elif value == "skills":
            profile = self.db.get_profile(user.id)
            self._awaiting[user.id] = "skills_pq"
            await self.reply(
                user,
                f"🧩 Сейчас: {escape(', '.join(profile.skills)) or '—'}\n\n"
                "Напишите навыки через запятую — по ним вакансии отбираются бесплатно, до ИИ.",
            )
        elif value == "fmtcity":
            await self.show_fmtcity(user, msg or None)
        elif value == "wishes":
            profile = self.db.get_profile(user.id)
            self._awaiting[user.id] = "wishes_pq"
            await self.reply(
                user,
                f"📝 Сейчас: {escape(profile.wishes) or '—'}\n\n"
                "Напишите пожелания, которые не влезают в кнопки: район, метро, график, "
                "тип компании… Например: <i>Фрунзенский район или рядом с м. Пушкинская, "
                "без ночных смен</i>. ИИ учтёт это при оценке. «-» — очистить.",
            )
        elif value == "nl":
            self._awaiting[user.id] = "nl"
            await self.reply(
                user,
                "✍️ Напишите, что поменять, своими словами. Например:\n"
                "<i>хочу только гибрид в Минске, во Фрунзенском районе, от 1000$</i>",
            )
        elif value == "fill_missing":
            self._profile_mode[user.id] = "fill"
            await self._ask_next(user, msg or None)
        elif value == "fill" and self.on_resume:
            resumes = self.db.get_resumes(user.id)
            if not resumes:
                return
            summary = await self._read_resume(user, resumes[-1])
            self._apply_profile(user)
            await self.show_profile(user, summary=summary)

    async def _cb_stats(self, user: User, cb: dict, value: str, msg: dict) -> None:
        await self.api.answer_callback(cb["id"])
        await self.show_stats(user)

    async def _cb_filters(self, user: User, cb: dict, value: str, msg: dict) -> None:
        action, _, arg = value.partition(":")
        f = user.filters
        note = None
        back = [button("⬅️ Назад", data="f:menu")]
        if action == "menu":
            text, markup = self._filters_menu(user)
            await self.edit(msg, text, markup)
        elif action == "dir":
            if arg in r.DIRECTIONS:
                note = self._toggle_query(user, r.DIRECTIONS[arg][1])
            await self.edit(
                msg,
                "<b>🔍 Направления</b>\nЧто ищем? Можно несколько.",
                self._directions_markup(user, ctx="f"),
            )
        elif action == "dirx" and arg.isascii() and arg.isdigit() and int(arg) < len(f.queries):
            self._toggle_query(user, f.queries[int(arg)])
            await self.edit(
                msg,
                "<b>🔍 Направления</b>\nЧто ищем? Можно несколько.",
                self._directions_markup(user, ctx="f"),
            )
        elif action == "custom":
            self._awaiting[user.id] = "queries"
            await self.reply(user, "✍️ Напишите направления через запятую.")
        elif action == "lvl":
            if arg in r.LEVEL_BUTTONS:
                f.levels = (
                    [x for x in f.levels if x != arg] if arg in f.levels else [*f.levels, arg]
                )
                self._save_filter_edit(user, "levels")
            await self.edit(
                msg,
                "<b>🎓 Уровень</b>\nНичего не выбрано — любой.",
                self._toggle_markup("f", "lvl", r.LEVEL_BUTTONS, f.levels, nav=back),
            )
        elif action == "cty":
            if arg in COUNTRIES:
                f.countries = (
                    [x for x in f.countries if x != arg]
                    if arg in f.countries
                    else [*f.countries, arg]
                )
                self._save_filter_edit(user, "countries")
            await self.edit(
                msg,
                "<b>🌍 Страны</b>\nНичего не выбрано — все.",
                self._toggle_markup("f", "cty", COUNTRIES, f.countries, nav=back),
            )
        elif action == "remote":
            f.remote_only = not f.remote_only
            self._save_filter_edit(user, "remote_only")
            text, markup = self._filters_menu(user)
            await self.edit(msg, text, markup)
        elif action == "salary":
            self._awaiting[user.id] = "salary"
            await self.reply(
                user, "💰 Минимальная зарплата в $ в месяц? Например <b>1200</b>. 0 — не важно."
            )
        elif action == "exclude":
            self._awaiting[user.id] = "exclude"
            current = escape(", ".join(f.exclude)) or "—"
            await self.reply(
                user,
                f"🚫 Сейчас: {current}\n\nНапишите стоп-слова через запятую — вакансии с ними "
                "в названии или компании я пропущу. «-» — очистить.",
            )
        elif action == "src":
            if arg in self.available_sources:
                enabled = list(f.sources or self.available_sources)
                enabled = [s for s in enabled if s != arg] if arg in enabled else [*enabled, arg]
                if not enabled:
                    note = "Нужна хотя бы одна площадка"
                else:
                    # все включены — храним пустой список, чтобы новые подключались сами
                    f.sources = [] if set(enabled) >= set(self.available_sources) else enabled
                    self.db.save_filters(user.id, f)
            await self.edit(msg, self._sources_text(), self._sources_markup(f))
        elif action == "thr":
            if arg in ("40", "50", "60", "70", "80"):
                f.ai_threshold = int(arg)
                self.db.save_filters(user.id, f)
            options = {str(v): f"{v}" for v in (40, 50, 60, 70, 80)}
            await self.edit(
                msg,
                "<b>🤖 Порог ИИ</b>\nПрисылать вакансии с оценкой не ниже… "
                "Ниже порог — больше вакансий, выше — только самые подходящие.",
                self._toggle_markup(
                    "f", "thr", options, [str(f.ai_threshold)], nav=back, per_row=5
                ),
            )
        elif action in ("pause", "unpause"):
            self.db.set_paused(user.id, action == "pause")
            note = "Поиск на паузе" if action == "pause" else "Поиск возобновлён"
            if msg:
                await self.api.edit_reply_markup(msg["chat"]["id"], msg["message_id"], None)
        await self.api.answer_callback(cb["id"], note)

    async def _cb_plan(self, user: User, cb: dict, value: str, msg: dict) -> None:
        await self.api.answer_callback(cb["id"])
        await self.show_plan(user)

    async def _cb_subscription(self, user: User, cb: dict, value: str, msg: dict) -> None:
        if value == "cancel" and user.sub_charge_id:
            try:
                await self.api.set_star_subscription_canceled(user.id, user.sub_charge_id, True)
            except TelegramError as exc:
                await self.api.answer_callback(cb["id"], "Не получилось, попробуйте позже")
                log.warning("cancel subscription %s failed: %s", user.id, exc)
                return
            self.db.set_subscription(user.id, None)
            user.sub_charge_id = None
            until = r.date_ru(user.plan_until) if user.plan_until else "конца периода"
            await self.api.answer_callback(cb["id"], f"Автопродление отменено, Pro — до {until}")
            await self.show_plan(user, msg or None)
            return
        await self.api.answer_callback(cb["id"])

    async def _cb_sources_legacy(self, user: User, cb: dict, value: str, msg: dict) -> None:
        # старые сообщения /sources со кнопками «src:<name>»
        await self._cb_filters(user, cb, f"src:{value}", msg)


_PRESET_QUERIES = {q for _, q in r.DIRECTIONS.values()}


def _summary(resume: Resume) -> str:
    """Краткое содержание из разбора резюме (для карточки профиля)."""
    try:
        data = json.loads(resume.profile) if resume.profile else {}
    except ValueError:
        return ""
    return str(data.get("summary") or "")[:400] if isinstance(data, dict) else ""
