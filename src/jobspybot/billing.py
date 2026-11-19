"""Тарифы, пробный период и подписка через Telegram Stars.

Тариф определяется датой `plan_until`: пока она в будущем — Pro, иначе Free.
Администраторы всегда на Pro. Автопродление Stars-подписки приходит как новый
`successful_payment` и просто сдвигает `plan_until` ещё на 30 дней.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from jobspybot.config import Settings
from jobspybot.db import Database, User, utcnow

SUBSCRIPTION_DAYS = 30
SUBSCRIPTION_PERIOD_SECONDS = SUBSCRIPTION_DAYS * 24 * 3600  # единственный период Stars


@dataclass(frozen=True, slots=True)
class Plan:
    name: str
    title: str
    max_queries: int  # направлений поиска
    max_resumes: int
    daily_vacancies: int | None  # None — без ограничений
    ai_tokens_per_day: int | None  # None — берётся AI_DAILY_TOKENS_PER_USER
    apply_button: bool  # «Отклик»: лучшее резюме + сопроводительное
    check_every_min: int


FREE = Plan(
    name="free",
    title="Free",
    max_queries=1,
    max_resumes=1,
    daily_vacancies=5,
    ai_tokens_per_day=5_000,  # ≈3 оценки ИИ
    apply_button=False,
    check_every_min=180,
)
PRO = Plan(
    name="pro",
    title="Pro",
    max_queries=10,
    max_resumes=10,
    daily_vacancies=None,
    ai_tokens_per_day=None,
    apply_button=True,
    check_every_min=30,
)


class Billing:
    def __init__(self, db: Database, settings: Settings) -> None:
        self.db = db
        self.settings = settings
        self.cfg = settings.billing

    # --- тариф ---------------------------------------------------------------

    def plan(self, user: User) -> Plan:
        if user.id in self.settings.admin_ids:
            return PRO
        if user.plan_until and user.plan_until > utcnow():
            return PRO
        return FREE

    def is_trial(self, user: User) -> bool:
        return self.plan(user) is PRO and user.trial_used and not self.db.has_payments(user.id)

    def ai_token_limit(self, user: User) -> int:
        plan = self.plan(user)
        return plan.ai_tokens_per_day or self.settings.ai.daily_tokens_per_user

    def is_due(self, user: User, now: datetime | None = None) -> bool:
        """Пора ли проверять вакансии для пользователя (Free — реже)."""
        if user.last_check is None:
            return True
        now = now or utcnow()
        # небольшой допуск, чтобы не пропускать цикл из-за секунд
        interval = timedelta(minutes=self.plan(user).check_every_min) - timedelta(minutes=2)
        return now - user.last_check >= interval

    def remaining_today(self, user: User) -> int | None:
        limit = self.plan(user).daily_vacancies
        if limit is None:
            return None
        return max(0, limit - self.db.sent_today(user.id))

    def notice(self, user: User, now: datetime | None = None) -> tuple[str, str] | None:
        """Напоминание о подписке: (ключ для защиты от повторов, текст) или None."""
        if user.id in self.settings.admin_ids or not user.plan_until:
            return None
        now = now or utcnow()
        until = user.plan_until
        stamp = until.date().isoformat()
        if until <= now and user.notified != f"expired:{stamp}":
            return (
                f"expired:{stamp}",
                "⏳ Pro закончился — вы на бесплатном тарифе: 1 направление и 5 вакансий в день.\n"
                "Вернуть все функции — в разделе 💎 Подписка.",
            )
        if (
            now < until <= now + timedelta(days=1)
            and not user.sub_charge_id
            and user.notified != f"soon:{stamp}"
        ):
            what = "пробный Pro" if self.is_trial(user) else "Pro"
            return (
                f"soon:{stamp}",
                f"⏰ Завтра заканчивается {what}. Продлить можно в разделе 💎 Подписка — "
                "поиск продолжит работать без ограничений.",
            )
        return None

    # --- выдача Pro ----------------------------------------------------------

    def start_trial(self, user: User) -> bool:
        """Пробный Pro для нового пользователя (один раз)."""
        if user.trial_used or self.cfg.trial_days <= 0:
            return False
        until = utcnow() + timedelta(days=self.cfg.trial_days)
        self.db.set_plan(user.id, until, trial_used=True)
        user.plan_until, user.trial_used = until, True
        return True

    def extend(self, user: User, days: int) -> datetime:
        """Продлить Pro: от текущей даты окончания, если она в будущем."""
        base = user.plan_until if user.plan_until and user.plan_until > utcnow() else utcnow()
        until = base + timedelta(days=days)
        self.db.set_plan(user.id, until)
        user.plan_until = until
        return until

    def on_payment(self, user: User, payment: dict) -> datetime:
        """Обработка successful_payment (первая оплата или автопродление)."""
        charge_id = payment.get("telegram_payment_charge_id", "")
        is_new = self.db.record_payment(
            user.id,
            charge_id=charge_id,
            amount=int(payment.get("total_amount") or 0),
            currency=payment.get("currency", ""),
            recurring=bool(payment.get("is_recurring")),
        )
        if not is_new:  # Telegram может повторить апдейт — второй раз не продлеваем
            return user.plan_until or utcnow()
        expires = payment.get("subscription_expiration_date")
        if expires:
            until = datetime.fromtimestamp(int(expires), tz=utcnow().tzinfo)
            if not user.plan_until or until > user.plan_until:
                self.db.set_plan(user.id, until)
                user.plan_until = until
        else:
            until = self.extend(user, SUBSCRIPTION_DAYS)
        if payment.get("is_recurring") and charge_id:
            self.db.set_subscription(user.id, charge_id)
            user.sub_charge_id = charge_id
        return user.plan_until or until
