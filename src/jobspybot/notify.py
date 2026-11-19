"""Когда присылать вакансии: сразу, раз в день дайджестом, тихие часы (#35).

Вакансии, которые сейчас присылать нельзя, пайплайн не теряет, а откладывает
(статус доставки `queued`, в `reason` — оценка ИИ). `Pipeline.flush` отправляет их,
когда закончились тихие часы или наступил час дайджеста.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from html import escape

from jobspybot.filters import UserFilters
from jobspybot.models import Vacancy

INSTANT = "instant"
DIGEST = "digest"
#: тихие часы по местному времени: с 23:00 до 08:00
QUIET_FROM, QUIET_TO = 23, 8
DIGEST_SIZE = 10
DIGEST_HOURS = (8, 9, 12, 18, 21)
#: часовой пояс по стране проживания, если человек не выбрал свой
COUNTRY_UTC_OFFSET = {"BY": 3, "RU": 3, "KZ": 5, "GE": 4, "PL": 1}
UTC_OFFSETS = (1, 2, 3, 4, 5)


def utc_offset(f: UserFilters) -> int:
    if f.utc_offset is not None:
        return f.utc_offset
    return COUNTRY_UTC_OFFSET.get(f.home_country, 3)


def local_time(f: UserFilters, now: datetime | None = None) -> datetime:
    now = now or datetime.now(UTC)
    return now.astimezone(UTC) + timedelta(hours=utc_offset(f))


def in_quiet_hours(f: UserFilters, now: datetime | None = None) -> bool:
    if not f.quiet_hours:
        return False
    hour = local_time(f, now).hour
    return hour >= QUIET_FROM or hour < QUIET_TO


def hold(f: UserFilters, now: datetime | None = None) -> bool:
    """Отложить вакансию вместо отправки сейчас."""
    return f.notify_mode == DIGEST or in_quiet_hours(f, now)


def digest_due(f: UserFilters, now: datetime | None = None) -> bool:
    return f.notify_mode == DIGEST and local_time(f, now).hour == f.digest_hour


def describe(f: UserFilters) -> str:
    """Короткое описание режима для кнопки и экрана."""
    mode = f"раз в день в {f.digest_hour}:00" if f.notify_mode == DIGEST else "сразу"
    quiet = " · 🌙 ночью тихо" if f.quiet_hours else ""
    return mode + quiet


def digest_text(items: list[tuple[Vacancy, int | None]], total: int) -> tuple[str, int]:
    """Один текст на топ вакансий: название, компания, зарплата, балл ИИ, ссылка.

    Возвращает текст и сколько вакансий в него вошло (лимит сообщения — 4096 символов).
    """
    blocks: list[str] = []
    size = 0
    for i, (v, score) in enumerate(items, 1):
        meta = " · ".join(
            x
            for x in (
                escape(v.company[:60]),
                escape(v.salary_text() or ""),
                f"🤖 {score}" if score is not None else "",
            )
            if x
        )
        block = f'{i}. <a href="{escape(v.url, quote=True)}">{escape(v.title[:120])}</a>'
        if meta:
            block += f"\n    {meta}"
        if size + len(block) > 3700:
            break
        blocks.append(block)
        size += len(block) + 1
    head = f"<b>📬 Дайджест вакансий — {len(blocks)}"
    head += f" лучших из {total}</b>" if total > len(blocks) else "</b>"
    return head + "\n\n" + "\n".join(blocks), len(blocks)
