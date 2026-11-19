"""Telegram-интерфейс: меню, онбординг, фильтры, резюме, подписка, админка."""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Awaitable, Callable
from html import escape
from typing import Any

from jobspybot.billing import PRO, SUBSCRIPTION_PERIOD_SECONDS, Billing
from jobspybot.config import Settings
from jobspybot.db import Database, Resume, User, utcnow
from jobspybot.filters import COUNTRIES, UserFilters
from jobspybot.models import Level, Vacancy
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
    ("filters", "Фильтры"),
    ("resumes", "Мои резюме"),
    ("plan", "Подписка"),
    ("stats", "Статистика"),
    ("help", "Помощь"),
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
ONBOARDING_STEPS = 4

ResumeHook = Callable[[User, Resume], Awaitable[str]]
CallbackHook = Callable[[User, dict[str, Any], str], Awaitable[None]]


def parse_list(raw: str) -> list[str]:
    return list(dict.fromkeys(p.strip() for p in _SPLIT.split(raw) if p.strip()))


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
        #: обработчик нового резюме (подключается модулем ИИ); возвращает ответ пользователю
        self.on_resume: ResumeHook | None = None
        #: дополнительные кнопки под вакансией: префикс → (текст, обработчик)
        self.extra_buttons: dict[str, tuple[str, CallbackHook]] = {}
        self._menu_actions: dict[str, Callable[[User], Awaitable[None]]] = {
            r.BTN_SEARCH: self.show_search,
            r.BTN_RESUMES: self.show_resumes,
            r.BTN_FILTERS: self.show_filters,
            r.BTN_PLAN: self.show_plan,
            r.BTN_STATS: self.show_stats,
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
            for update in updates:
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
            if not user.allowed:
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

    # --- входящие ------------------------------------------------------------

    async def handle_update(self, update: dict[str, Any]) -> None:
        if "callback_query" in update:
            await self.handle_callback(update["callback_query"])
        elif "pre_checkout_query" in update:
            await self.handle_pre_checkout(update["pre_checkout_query"])
        elif "message" in update:
            await self.handle_message(update["message"])

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

        if cmd == "start" or (not user.onboarded and not cmd):
            await self.cmd_start(user, args, first_name=first_name)
            return

        handler = getattr(self, f"cmd_{cmd}", None) if cmd else None
        if handler is None:
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

    async def cmd_start(self, user: User, args: str, *, first_name: str = "") -> None:
        if user.onboarded:
            await self.reply(user, "Главное меню 👇", main_menu())
            await self.show_search(user)
            return
        trial = self.billing.start_trial(user) and self.settings.billing.trial_days
        await self.reply(user, r.welcome(first_name, trial or 0), main_menu())
        await self._onboarding(user, 1)

    async def _onboarding(self, user: User, step: int, msg: dict[str, Any] | None = None) -> None:
        f = user.filters
        if step == 1:
            text = r.onboarding_step(
                1,
                ONBOARDING_STEPS,
                "Что ищем?",
                "Выберите направления (можно несколько) или нажмите «✍️ Свой вариант».",
            )
            markup = self._directions_markup(user, ctx="ob", next_step=2)
        elif step == 2:
            text = r.onboarding_step(2, ONBOARDING_STEPS, "Какой уровень?", "Можно несколько.")
            markup = self._toggle_markup(
                "ob", "lvl", r.LEVEL_BUTTONS, f.levels, nav=[button("Далее ➡️", data="ob:go:3")]
            )
        elif step == 3:
            text = r.onboarding_step(
                3, ONBOARDING_STEPS, "Где искать?", "Не выбрано — значит во всех странах."
            )
            markup = self._toggle_markup(
                "ob", "cty", COUNTRIES, f.countries, nav=[button("Далее ➡️", data="ob:go:4")]
            )
        else:
            text = r.onboarding_step(
                4,
                ONBOARDING_STEPS,
                "Пришлите резюме PDF 📄",
                "ИИ сравнит его с каждой вакансией и скажет, есть ли шанс. "
                "Можно прислать несколько под разные роли — выберу лучшее для каждой вакансии.",
            )
            self._awaiting[user.id] = "resume"
            markup = inline_keyboard([button("Пропустить ➡️", data="ob:done")])
        if msg:
            await self.edit(msg, text, markup)
        else:
            await self.reply(user, text, markup)

    async def _finish_onboarding(self, user: User) -> None:
        self._awaiting.pop(user.id, None)
        self.db.set_onboarded(user.id)
        user.onboarded = True
        plan = self.billing.plan(user)
        every = plan.check_every_min
        await self.reply(
            user,
            "🎉 <b>Готово!</b> Поиск запущен.\n\n"
            f"Первая подборка придёт в течение {min(every, 30)} минут, дальше — по мере появления "
            "новых вакансий. Настройки всегда в меню ниже.",
            main_menu(),
        )

    # --- выборки (общие для онбординга и фильтров) ---------------------------

    def _directions_markup(self, user: User, *, ctx: str, next_step: int | None) -> dict[str, Any]:
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
        if next_step:
            nav.append(button("Далее ➡️", data=f"ob:go:{next_step}"))
        else:
            nav.append(button("⬅️ Назад", data="f:menu"))
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
        await self.reply(user, text, inline_keyboard([button("⚙️ Изменить", data="f:menu"), pause]))

    def _filters_menu(self, user: User) -> tuple[str, dict[str, Any]]:
        f = user.filters
        rows = [
            [
                button(f"🔍 Направления ({len(f.queries)})", data="f:dir"),
                button("🎓 Уровень", data="f:lvl"),
            ],
            [
                button("🌍 Страны", data="f:cty"),
                button(f"🏠 Удалёнка: {'да' if f.remote_only else 'нет'}", data="f:remote"),
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
            self.db.save_filters(user.id, user.filters)
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
            self.db.save_filters(user.id, user.filters)
        shown = ", ".join(COUNTRIES[c] for c in user.filters.countries) or "любые"
        await self.reply(user, f"🌍 Страны: {shown}")

    async def cmd_remote(self, user: User, args: str) -> None:
        user.filters.remote_only = not user.filters.remote_only
        self.db.save_filters(user.id, user.filters)
        await self.reply(user, f"🏠 Только удалёнка: {'да' if user.filters.remote_only else 'нет'}")

    async def cmd_salary(self, user: User, args: str) -> None:
        if args:
            parts = args.split()
            if not parts[0].isdigit():
                await self.reply(
                    user, "Формат: <code>/salary 1500</code> или <code>/salary 1500 strict</code>"
                )
                return
            user.filters.min_salary_usd = int(parts[0])
            user.filters.require_salary = "strict" in parts[1:]
            self.db.save_filters(user.id, user.filters)
        f = user.filters
        text = f"от {f.min_salary_usd}$" if f.min_salary_usd else "не важна"
        if f.min_salary_usd and f.require_salary:
            text += ", без указанной зарплаты — скрывать"
        await self.reply(user, f"💰 Зарплата: {text}")

    async def cmd_threshold(self, user: User, args: str) -> None:
        if args:
            if not args.isdigit() or not 0 <= int(args) <= 100:
                await self.reply(user, "Число от 0 до 100, например <code>/threshold 60</code>")
                return
            user.filters.ai_threshold = int(args)
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
        onboarding = self._awaiting.get(user.id) == "resume" and not user.onboarded
        self._awaiting.pop(user.id, None)
        text = text.strip()[:MAX_RESUME_CHARS]
        if len(text) < 50:
            await self.reply(user, "Слишком короткое резюме.")
            return
        limit = self.billing.plan(user).max_resumes
        if self._resume_count(user) >= limit:
            extra = f" В 💎 Pro — до {PRO.max_resumes}." if limit < PRO.max_resumes else ""
            await self.reply(user, f"Уже {limit} резюме — удалите лишнее в 📄 Резюме.{extra}")
            return
        resume_id = self.db.add_resume(user.id, title, text, file_id=file_id, file_name=file_name)
        resume = self.db.get_resume(user.id, resume_id)
        assert resume is not None
        if self.on_resume:
            await self.reply(user, f"⏳ Читаю «{escape(title)}»…")
            await self.reply(user, await self.on_resume(user, resume))
        else:
            await self.reply(user, f"✅ Резюме «{escape(title)}» сохранено.")
        if onboarding or not user.onboarded:
            await self._finish_onboarding(user)

    # --- текстовый ввод -------------------------------------------------------

    async def _receive_input(self, user: User, field: str, text: str) -> None:
        f = user.filters
        if field == "resume":
            await self._save_resume(user, text, title=f"Резюме {self._resume_count(user) + 1}")
            return
        if field in ("queries", "queries_ob"):
            errors = [
                err for q in parse_list(text) if (err := self._toggle_query(user, q, add_only=True))
            ]
            if errors:
                await self.reply(
                    user, errors[0], inline_keyboard([button("💎 Pro", data="plan:show")])
                )
            if field == "queries_ob":
                await self._onboarding(user, 1)
            else:
                await self.reply(
                    user,
                    "🔍 Направления",
                    self._directions_markup(user, ctx="f", next_step=None),
                )
            return
        if field == "exclude":
            f.exclude = [] if text.strip() == "-" else parse_list(text)
            self.db.save_filters(user.id, f)
            await self.reply(user, f"🚫 Стоп-слова: {escape(', '.join(f.exclude)) or '—'}")
        elif field == "salary":
            digits = re.sub(r"\D", "", text)
            if not digits:
                self._awaiting[user.id] = field
                await self.reply(user, "Нужно число в долларах, например 1200. 0 — не важно.")
                return
            f.min_salary_usd = int(digits)
            self.db.save_filters(user.id, f)
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
            "<code>/refund ID</code> — вернуть последний платёж",
        )

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
        parts = args.split()
        if len(parts) != 2 or not all(p.lstrip("-").isdigit() for p in parts):
            return await self.reply(user, "Формат: <code>/grant ID ДНЕЙ</code>")
        target = self.db.get_user(int(parts[0]))
        if target is None:
            return await self.reply(user, "Пользователь не найден.")
        self.db.set_allowed(target.id, True)
        until = self.billing.extend(target, int(parts[1]))
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
        if not args.isdigit():
            return await self.reply(user, "Формат: <code>/refund ID</code>")
        charge = self.db.last_payment(int(args))
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

        if prefix in ("allow", "deny") and self._is_admin(user):
            target = self.db.get_user(int(value))
            self.db.set_allowed(int(value), prefix == "allow")
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
            "f": self._cb_filters,
            "plan": self._cb_plan,
            "sub": self._cb_subscription,
            "src": self._cb_sources_legacy,
        }.get(prefix)
        if handler:
            await handler(user, cb, value, msg)
            return
        if prefix in self.extra_buttons:
            await self.extra_buttons[prefix][1](user, cb, value)
            return
        await self.api.answer_callback(cb["id"])

    async def _cb_nope(self, user: User, cb: dict, value: str, msg: dict) -> None:
        if value.isdigit():
            self.db.set_status(user.id, int(value), "disliked")
        await self.api.answer_callback(cb["id"], "Скрыто")
        if msg:
            first = msg.get("reply_markup", {}).get("inline_keyboard", [[]])[0][:1]
            await self.api.edit_reply_markup(
                msg["chat"]["id"], msg["message_id"], inline_keyboard(first)
            )

    async def _cb_resume_delete(self, user: User, cb: dict, value: str, msg: dict) -> None:
        deleted = value.isdigit() and self.db.delete_resume(user.id, int(value))
        await self.api.answer_callback(cb["id"], "Удалено" if deleted else "Уже удалено")
        if msg:
            text, markup = self._resumes_view(user)
            await self.edit(msg, text, markup)

    async def _cb_onboarding(self, user: User, cb: dict, value: str, msg: dict) -> None:
        action, _, arg = value.partition(":")
        f = user.filters
        note = None
        if action == "dir" and arg in r.DIRECTIONS:
            note = self._toggle_query(user, r.DIRECTIONS[arg][1])
            await self._onboarding(user, 1, msg)
        elif action == "dirx" and arg.isdigit() and int(arg) < len(f.queries):
            self._toggle_query(user, f.queries[int(arg)])
            await self._onboarding(user, 1, msg)
        elif action == "custom":
            self._awaiting[user.id] = "queries_ob"
            note = "Напишите направления через запятую"
            await self.reply(
                user,
                "✍️ Напишите, что ищете — через запятую.\nНапример: <i>python developer, "
                "тестировщик, аналитик данных</i>",
            )
        elif action == "lvl" and arg in r.LEVEL_BUTTONS:
            f.levels = [x for x in f.levels if x != arg] if arg in f.levels else [*f.levels, arg]
            self.db.save_filters(user.id, f)
            await self._onboarding(user, 2, msg)
        elif action == "cty" and arg in COUNTRIES:
            f.countries = (
                [x for x in f.countries if x != arg] if arg in f.countries else [*f.countries, arg]
            )
            self.db.save_filters(user.id, f)
            await self._onboarding(user, 3, msg)
        elif action == "go" and arg.isdigit():
            if int(arg) == 2 and not f.queries:
                note = "Выберите хотя бы одно направление"
            else:
                await self._onboarding(user, int(arg), msg)
        elif action == "done":
            if msg:
                await self.api.edit_reply_markup(msg["chat"]["id"], msg["message_id"], None)
            await self._finish_onboarding(user)
        await self.api.answer_callback(cb["id"], note)

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
                self._directions_markup(user, ctx="f", next_step=None),
            )
        elif action == "dirx" and arg.isdigit() and int(arg) < len(f.queries):
            self._toggle_query(user, f.queries[int(arg)])
            await self.edit(
                msg,
                "<b>🔍 Направления</b>\nЧто ищем? Можно несколько.",
                self._directions_markup(user, ctx="f", next_step=None),
            )
        elif action == "custom":
            self._awaiting[user.id] = "queries"
            await self.reply(user, "✍️ Напишите направления через запятую.")
        elif action == "lvl":
            if arg in r.LEVEL_BUTTONS:
                f.levels = (
                    [x for x in f.levels if x != arg] if arg in f.levels else [*f.levels, arg]
                )
                self.db.save_filters(user.id, f)
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
                self.db.save_filters(user.id, f)
            await self.edit(
                msg,
                "<b>🌍 Страны</b>\nНичего не выбрано — все.",
                self._toggle_markup("f", "cty", COUNTRIES, f.countries, nav=back),
            )
        elif action == "remote":
            f.remote_only = not f.remote_only
            self.db.save_filters(user.id, f)
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
            if arg.isdigit():
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
