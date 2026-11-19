from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from jobspybot.billing import FREE, PRO, Billing
from jobspybot.config import BillingSettings, Settings
from jobspybot.db import Database
from jobspybot.filters import UserFilters
from jobspybot.pipeline import Pipeline
from tests.test_pipeline import FakeNotifier, FakeSource, vac

ADMIN = 1


@pytest.fixture
def billing(db) -> Billing:
    return Billing(
        db, Settings(admin_ids=frozenset({ADMIN}), billing=BillingSettings(trial_days=7))
    )


def user(db, uid: int = 2):
    db.ensure_user(uid, allowed=True)
    return db.get_user(uid)


def test_plans(billing, db):
    u = user(db)
    assert billing.plan(u) is FREE
    assert billing.plan(user(db, ADMIN)) is PRO
    assert billing.start_trial(u) and billing.plan(u) is PRO and billing.is_trial(u)
    assert not billing.start_trial(u)  # пробный период один раз
    assert billing.plan(db.get_user(2)) is PRO  # сохранено в БД


def test_extend_from_future_date(billing, db):
    u = user(db)
    billing.start_trial(u)
    first = u.plan_until
    assert billing.extend(u, 30) - first == timedelta(days=30)
    assert billing.extend(user(db, 3), 10) > datetime.now(UTC) + timedelta(days=9)


def test_due_and_daily_limit(billing, db):
    u = user(db)
    assert billing.is_due(u)
    db.set_last_check(u.id)
    u = db.get_user(u.id)
    assert not billing.is_due(u)
    assert billing.is_due(u, datetime.now(UTC) + timedelta(hours=3))
    assert billing.remaining_today(u) == FREE.daily_vacancies
    billing.start_trial(u)
    assert billing.remaining_today(u) is None
    assert billing.is_due(u, datetime.now(UTC) + timedelta(minutes=30))


def test_payment_with_subscription_expiration(billing, db):
    u = user(db)
    expires = int((datetime.now(UTC) + timedelta(days=30)).timestamp())
    payment = {
        "telegram_payment_charge_id": "c1",
        "total_amount": 250,
        "currency": "XTR",
        "is_recurring": True,
        "subscription_expiration_date": expires,
    }
    until = billing.on_payment(u, payment)
    assert int(until.timestamp()) == expires and u.sub_charge_id == "c1"
    assert billing.on_payment(u, payment) == until  # повтор не продлевает
    assert not billing.is_trial(u)


def test_notices(billing, db):
    u = user(db)
    now = datetime.now(UTC)
    billing.start_trial(u)
    assert billing.notice(u, now) is None
    key, text = billing.notice(u, u.plan_until - timedelta(hours=12))
    assert key.startswith("soon:") and "пробный" in text
    db.set_notified(u.id, key)
    u = db.get_user(u.id)
    assert billing.notice(u, u.plan_until - timedelta(hours=12)) is None
    key, text = billing.notice(u, u.plan_until + timedelta(hours=1))
    assert key.startswith("expired:") and "бесплатном" in text
    assert billing.notice(user(db, ADMIN), now) is None


class LimitNotifier(FakeNotifier):
    def __init__(self):
        super().__init__()
        self.limited: list[int] = []

    async def daily_limit_reached(self, user):
        self.limited.append(user.id)


async def test_pipeline_respects_free_plan(billing, db: Database):
    many = [vac("a", str(i), f"Python {i}", company=f"C{i}") for i in range(8)]
    src = FakeSource("a", {"python": many, "go": [vac("a", "go", "Go dev")]})
    user(db)
    db.save_filters(2, UserFilters(queries=["python", "go"]))
    notifier = LimitNotifier()
    p = Pipeline(db, {"a": src}, notifier, Settings(), billing=billing)

    await p.run_cycle()
    assert src.calls == ["python"]  # Free — одно направление
    assert len(notifier.sent) == FREE.daily_vacancies

    # следующий цикл сразу — Free ещё не пора проверять
    await p.run_cycle()
    assert src.calls == ["python"]

    # через 3 часа — проверяем, но дневной лимит исчерпан
    db.set_last_check(2, datetime.now(UTC) - timedelta(hours=4))
    await p.run_cycle()
    assert len(notifier.sent) == FREE.daily_vacancies and notifier.limited == [2]


def test_admin_grant_is_not_trial(billing, db):
    u = user(db)
    billing.start_trial(u)
    assert billing.is_trial(u)
    billing.extend(u, 30)  # админ выдал Pro поверх пробного
    assert not billing.is_trial(db.get_user(u.id))
    _key, text = billing.notice(u, u.plan_until - timedelta(hours=12))
    assert "пробный" not in text


def test_old_trial_without_trial_until(billing, db):
    u = user(db)
    db.set_plan(u.id, datetime.now(UTC) + timedelta(days=3), trial_used=True)
    assert billing.is_trial(db.get_user(u.id))  # в пределах пробного срока от регистрации
    db.set_plan(u.id, datetime.now(UTC) + timedelta(days=60))
    assert not billing.is_trial(db.get_user(u.id))  # выдан админом до появления trial_until
