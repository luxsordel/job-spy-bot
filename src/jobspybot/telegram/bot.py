"""Обработка команд и кнопок Telegram, отправка карточек вакансий."""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Awaitable, Callable
from html import escape
from typing import Any

from jobspybot.config import Settings
from jobspybot.db import Database, Resume, User
from jobspybot.filters import COUNTRIES, UserFilters
from jobspybot.models import Level, Vacancy
from jobspybot.resume_files import (
    MAX_FILE_BYTES,
    ResumeFileError,
    extract_text,
    title_from_filename,
)
from jobspybot.sources import REGISTRY, SOURCE_ORDER
from jobspybot.telegram.api import TelegramAPI, TelegramError, button, inline_keyboard
from jobspybot.telegram.render import HELP, settings_text, vacancy_card

log = logging.getLogger(__name__)

COMMANDS = [
    ("settings", "Текущие настройки"),
    ("query", "Поисковые запросы"),
    ("exclude", "Стоп-слова"),
    ("include", "Обязательные слова в названии"),
    ("level", "Уровень: junior, middle…"),
    ("country", "Страны"),
    ("remote", "Только удалёнка"),
    ("salary", "Минимальная зарплата, $"),
    ("sources", "Площадки"),
    ("skills", "Навыки для отбора"),
    ("resume", "Загрузить резюме (PDF / текст)"),
    ("resumes", "Мои резюме"),
    ("threshold", "Порог оценки ИИ"),
    ("stats", "Статистика"),
    ("pause", "Приостановить"),
    ("unpause", "Возобновить"),
    ("help", "Справка"),
]

_SPLIT = re.compile(r"[;,\n]+")
MAX_RESUME_CHARS = 8000
MAX_RESUMES = 10

ResumeHook = Callable[[User, Resume], Awaitable[str]]
CallbackHook = Callable[[User, dict[str, Any], str], Awaitable[None]]


def parse_list(raw: str) -> list[str]:
    return list(dict.fromkeys(p.strip() for p in _SPLIT.split(raw) if p.strip()))


class Bot:
    def __init__(
        self,
        api: TelegramAPI,
        db: Database,
        settings: Settings,
        available_sources: list[str],
    ) -> None:
        self.api = api
        self.db = db
        self.settings = settings
        self.available_sources = [s for s in SOURCE_ORDER if s in available_sources]
        self.username = ""
        self._awaiting_resume: set[int] = set()
        #: обработчик нового резюме (подключается модулем ИИ); возвращает ответ пользователю
        self.on_resume: ResumeHook | None = None
        #: дополнительные кнопки под вакансией и их обработчики: префикс → (текст, обработчик)
        self.extra_buttons: dict[str, tuple[str, CallbackHook]] = {}

    # --- жизненный цикл ------------------------------------------------------

    async def setup(self) -> None:
        me = await self.api.get_me()
        self.username = me.get("username", "")
        await self.api.set_commands(COMMANDS)

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
            user.id, vacancy_card(vacancy, extra), reply_markup=inline_keyboard(row)
        )

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
        elif "message" in update:
            await self.handle_message(update["message"])

    def _user_from(self, sender: dict[str, Any]) -> User:
        uid = sender["id"]
        name = sender.get("username") or sender.get("first_name") or ""
        return self.db.ensure_user(uid, name, allowed=uid in self.settings.admin_ids)

    async def reply(self, user: User, text: str, markup: dict[str, Any] | None = None) -> None:
        await self.api.send_message(user.id, text, reply_markup=markup)

    async def handle_message(self, msg: dict[str, Any]) -> None:
        chat = msg.get("chat") or {}
        if chat.get("type") != "private" or "from" not in msg:
            return
        user = self._user_from(msg["from"])
        text = (msg.get("text") or "").strip()

        if text.startswith("/"):
            cmd, _, args = text[1:].partition(" ")
            cmd = cmd.split("@", 1)[0].lower()
            args = args.strip()
        else:
            cmd, args = "", text

        if not user.allowed:
            await self._handle_guest(user, cmd, args)
            return

        if msg.get("document"):
            await self._receive_document(user, msg["document"])
            return

        if user.id in self._awaiting_resume and not cmd:
            await self._save_resume(user, text, title=f"Резюме {self._resume_count(user) + 1}")
            return

        handler = getattr(self, f"cmd_{cmd}", None) if cmd else None
        if handler is None:
            await self.reply(user, "Не понял команду. /help — список команд.")
            return
        await handler(user, args)

    async def _handle_guest(self, user: User, cmd: str, args: str) -> None:
        if cmd == "start" and args and self.db.use_invite(args, user.id):
            user.allowed = True
            await self.reply(user, "✅ Доступ открыт!\n\n" + HELP)
            return
        await self.reply(
            user, "🔒 Бот работает по приглашениям. Попросите ссылку у администратора."
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

    # --- команды -------------------------------------------------------------

    async def cmd_start(self, user: User, args: str) -> None:
        await self.reply(user, HELP)
        if not user.filters.queries:
            await self.reply(
                user, "Начните с запроса, например:\n<code>/query python developer</code>"
            )

    async def cmd_help(self, user: User, args: str) -> None:
        await self.reply(user, HELP)

    async def cmd_settings(self, user: User, args: str) -> None:
        await self.reply(
            user,
            settings_text(
                user.filters,
                self.available_sources,
                paused=user.paused,
                resumes=self._resume_count(user),
            ),
        )

    async def _update_list(self, user: User, args: str, attr: str, title: str) -> None:
        f = user.filters
        if args:
            setattr(f, attr, [] if args == "-" else parse_list(args))
            self.db.save_filters(user.id, f)
        values = getattr(f, attr)
        await self.reply(user, f"{title}: {escape(', '.join(values)) if values else '—'}")

    async def cmd_query(self, user: User, args: str) -> None:
        await self._update_list(user, args, "queries", "🔍 Запросы")

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

    def _sources_markup(self, f: UserFilters) -> dict[str, Any]:
        enabled = set(f.sources or self.available_sources)
        rows = [
            [
                button(
                    f"{'✅' if name in enabled else '▫️'} {REGISTRY[name].title}", data=f"src:{name}"
                )
            ]
            for name in self.available_sources
        ]
        return inline_keyboard(*rows)

    async def cmd_sources(self, user: User, args: str) -> None:
        missing = [REGISTRY[s].title for s in SOURCE_ORDER if s not in self.available_sources]
        note = f"\n<i>Не настроены на сервере: {escape(', '.join(missing))}</i>" if missing else ""
        await self.reply(
            user,
            "🔎 Площадки (нажмите, чтобы переключить):" + note,
            self._sources_markup(user.filters),
        )

    async def cmd_resume(self, user: User, args: str) -> None:
        if args:
            await self._save_resume(user, args, title=f"Резюме {self._resume_count(user) + 1}")
            return
        self._awaiting_resume.add(user.id)
        await self.reply(
            user,
            "📄 Пришлите резюме: файлом PDF / .txt / .md или текстом следующим сообщением.\n\n"
            f"Можно загрузить до {MAX_RESUMES} резюме под разные направления (Python, C++, QA…) — "
            "файлы можно присылать в любой момент, без команды. Под каждую вакансию ИИ "
            "выберет резюме с наибольшим шансом на оффер.",
        )

    def _resume_count(self, user: User) -> int:
        return len(self.db.get_resumes(user.id))

    async def _receive_document(self, user: User, doc: dict[str, Any]) -> None:
        self._awaiting_resume.discard(user.id)
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
        self._awaiting_resume.discard(user.id)
        text = text.strip()[:MAX_RESUME_CHARS]
        if len(text) < 50:
            await self.reply(user, "Слишком короткое резюме.")
            return
        if self._resume_count(user) >= MAX_RESUMES:
            await self.reply(user, f"Уже {MAX_RESUMES} резюме — удалите лишнее в /resumes.")
            return
        resume_id = self.db.add_resume(user.id, title, text, file_id=file_id, file_name=file_name)
        resume = self.db.get_resume(user.id, resume_id)
        assert resume is not None
        if self.on_resume:
            await self.reply(user, f"⏳ Анализирую «{escape(title)}»…")
            await self.reply(user, await self.on_resume(user, resume))
        else:
            await self.reply(user, f"✅ Резюме «{escape(title)}» сохранено.")

    def _resumes_view(self, user: User) -> tuple[str, dict[str, Any] | None]:
        resumes = self.db.get_resumes(user.id)
        if not resumes:
            return "Резюме пока нет. Пришлите PDF или /resume.", None
        lines = ["<b>Ваши резюме</b> (ИИ выбирает лучшее под каждую вакансию):", ""]
        for r in resumes:
            skills = ", ".join(r.skills[:8]) or "профиль не готов"
            icon = "📎" if r.file_id else "📝"
            lines.append(f"{icon} <b>{escape(r.title)}</b> — {escape(skills)}")
        rows = [[button(f"🗑 {r.title[:30]}", data=f"rdel:{r.id}")] for r in resumes]
        return "\n".join(lines), inline_keyboard(*rows)

    async def cmd_resumes(self, user: User, args: str) -> None:
        text, markup = self._resumes_view(user)
        await self.reply(user, text, markup)

    async def cmd_stats(self, user: User, args: str) -> None:
        s = self.db.stats(user.id)
        await self.reply(
            user,
            "<b>За 7 дней</b>\n"
            f"📨 Отправлено: {s.get('sent', 0)}\n"
            f"🧹 Отсеяно фильтрами: {s.get('filtered', 0)}\n"
            f"🤖 Отсеяно ИИ: {s.get('ai_rejected', 0)}\n"
            f"👎 Скрыто вами: {s.get('disliked', 0)}",
        )

    async def cmd_pause(self, user: User, args: str) -> None:
        self.db.set_paused(user.id, True)
        await self.reply(user, "⏸ Рассылка приостановлена. /unpause — возобновить.")

    async def cmd_unpause(self, user: User, args: str) -> None:
        self.db.set_paused(user.id, False)
        await self.reply(user, "▶️ Рассылка возобновлена.")

    # --- админские -----------------------------------------------------------

    async def cmd_invite(self, user: User, args: str) -> None:
        if user.id not in self.settings.admin_ids:
            return await self.reply(user, "Только для администратора.")
        code = self.db.create_invite(user.id)
        await self.reply(user, f"Одноразовая ссылка:\nhttps://t.me/{self.username}?start={code}")

    async def cmd_users(self, user: User, args: str) -> None:
        if user.id not in self.settings.admin_ids:
            return await self.reply(user, "Только для администратора.")
        lines = [
            f"{'✅' if u.allowed else '🔒'}{'⏸' if u.paused else ''} "
            f"<code>{u.id}</code> {escape(u.username)} — {len(u.filters.queries)} запр."
            for u in self.db.all_users()
        ]
        await self.reply(user, "\n".join(lines) or "Пользователей нет")

    # --- кнопки --------------------------------------------------------------

    async def handle_callback(self, cb: dict[str, Any]) -> None:
        user = self._user_from(cb["from"])
        data = cb.get("data") or ""
        prefix, _, value = data.partition(":")
        msg = cb.get("message") or {}

        if prefix in ("allow", "deny") and user.id in self.settings.admin_ids:
            target = int(value)
            self.db.set_allowed(target, prefix == "allow")
            await self.api.answer_callback(cb["id"], "Готово")
            if msg:
                await self.api.edit_reply_markup(msg["chat"]["id"], msg["message_id"], None)
            if prefix == "allow":
                await self.api.send_message(target, "✅ Доступ открыт!\n\n" + HELP)
            return

        if not user.allowed:
            await self.api.answer_callback(cb["id"])
            return

        if prefix == "nope" and value.isdigit():
            self.db.set_status(user.id, int(value), "disliked")
            await self.api.answer_callback(cb["id"], "Скрыто")
            if msg:
                await self.api.edit_reply_markup(
                    msg["chat"]["id"],
                    msg["message_id"],
                    inline_keyboard(
                        msg.get("reply_markup", {}).get("inline_keyboard", [[]])[0][:1]
                    ),
                )
            return

        if prefix == "src" and value in REGISTRY:
            f = user.filters
            enabled = list(f.sources or self.available_sources)
            enabled = [s for s in enabled if s != value] if value in enabled else [*enabled, value]
            if not enabled:
                await self.api.answer_callback(cb["id"], "Нужна хотя бы одна площадка")
                return
            # все площадки включены — храним пустой список, чтобы новые подключались сами
            f.sources = [] if set(enabled) >= set(self.available_sources) else enabled
            self.db.save_filters(user.id, f)
            await self.api.answer_callback(cb["id"])
            if msg:
                await self.api.edit_reply_markup(
                    msg["chat"]["id"], msg["message_id"], self._sources_markup(f)
                )
            return

        if prefix == "rdel" and value.isdigit():
            deleted = self.db.delete_resume(user.id, int(value))
            await self.api.answer_callback(cb["id"], "Удалено" if deleted else "Уже удалено")
            if msg:
                text, markup = self._resumes_view(user)
                await self.api.call(
                    "editMessageText",
                    chat_id=msg["chat"]["id"],
                    message_id=msg["message_id"],
                    text=text,
                    parse_mode="HTML",
                    reply_markup=markup,
                )
            return

        if prefix in self.extra_buttons:
            await self.extra_buttons[prefix][1](user, cb, value)
            return

        await self.api.answer_callback(cb["id"])
