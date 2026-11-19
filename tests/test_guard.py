from __future__ import annotations

import asyncio
import logging

import pytest

from jobspybot.filters import UserFilters
from jobspybot.guard import AdminAlerts, RateLimiter
from jobspybot.limits import (
    MAX_SALARY_USD,
    InputError,
    clean_list,
    parse_int,
    parse_salary,
    safe_salary,
)
from jobspybot.profile import UserProfile, apply_to_filters

# --- ограничения ввода ----------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("1200", 1200),
        ("от 1 500 $", 1500),
        ("1 500", 1500),
        ("1,500", 1500),
        ("1200-1500", 1200),
        ("1.5k", 1500),
        ("2 тыс", 2000),
        ("0", 0),
        ("не важно", None),
    ],
)
def test_parse_salary(text, expected):
    assert parse_salary(text) == expected


@pytest.mark.parametrize("text", ["99999999999999999999999999", "1" * 5000, "60000", "100k"])
def test_unrealistic_salary_is_rejected(text):
    with pytest.raises(InputError, match="Слишком большая"):
        parse_salary(text)


def test_safe_salary_handles_garbage():
    assert safe_salary(1500) == 1500
    assert safe_salary("2000") == 2000
    for bad in (10**30, float("inf"), float("nan"), -5, "1e400", "abc", True, None):
        assert safe_salary(bad) is None
    assert safe_salary(MAX_SALARY_USD) == MAX_SALARY_USD


def test_parse_int_only_ascii_digits():
    assert parse_int("42") == 42 and parse_int("-7") == -7
    assert parse_int("²") is None and parse_int("١٢") is None and parse_int("") is None
    assert parse_int("1" * 20) is None


def test_clean_list_limits():
    items = ["a" * 100, "B", "b", "", "  c  d  ", *[f"x{i}" for i in range(50)]]
    result = clean_list(items, max_items=5)
    assert result == ["a" * 60, "B", "c d", "x0", "x1"]


def test_broken_salary_in_db_is_repaired_on_load():
    """Регрессия: нереальная зарплата в профиле ломала бота на каждом действии."""
    p = UserProfile.from_json('{"salary_usd": 1' + "0" * 400 + ', "work_formats": ["remote"]}')
    f = UserFilters.from_json('{"min_salary_usd": 1' + "0" * 400 + ', "ai_threshold": 999}')
    assert p.salary_usd is None and f.min_salary_usd == 0 and f.ai_threshold == 60
    apply_to_filters(p, f, max_queries=3)  # не падает с OverflowError


# --- частота запросов -----------------------------------------------------------


def test_rate_limiter_sliding_window():
    limiter = RateLimiter(per_minute=3)
    assert all(limiter.allow(1, now=t) for t in (0, 1, 2))
    assert not limiter.allow(1, now=3)
    assert limiter.allow(2, now=3)  # у другого пользователя свой лимит
    assert limiter.allow(1, now=61)  # первое событие вышло из окна
    assert limiter.should_warn(1, now=3) and not limiter.should_warn(1, now=30)
    assert limiter.should_warn(1, now=64)


# --- оповещения админу ----------------------------------------------------------


async def test_alerts_dedup_and_delivery():
    alerts = AdminAlerts(cooldown_s=1800)
    logger = logging.getLogger("jobspybot.test_alerts")
    logger.addHandler(alerts)
    try:
        sent: list[str] = []
        stop = asyncio.Event()

        async def notify(text: str) -> None:
            sent.append(text)
            if len(sent) == 2:
                stop.set()

        task = asyncio.create_task(alerts.run(notify, stop))
        logger.error("cycle failed: boom")
        logger.error("cycle failed: boom")  # повтор в пределах паузы — не отправляется
        logger.warning("просто предупреждение")  # не ERROR — не отправляется
        alerts.alert("source:habr", "🔇 habr молчит", cooldown_s=0)
        await asyncio.wait_for(task, timeout=2)
    finally:
        logger.removeHandler(alerts)
    assert len(sent) == 2 and "boom" in sent[0] and "habr" in sent[1]
    assert alerts.recent[0].count == 2  # повтор учтён в истории для админки


def test_alert_history_is_bounded():
    alerts = AdminAlerts(keep=3)
    for i in range(10):
        alerts.alert(f"k{i}", f"t{i}")
    assert [a.text for a in alerts.recent] == ["t7", "t8", "t9"]
