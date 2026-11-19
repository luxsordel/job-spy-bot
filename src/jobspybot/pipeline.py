"""Цикл мониторинга: опрос площадок → отсев → (ИИ) → рассылка."""

from __future__ import annotations

import asyncio
import logging
import time
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Protocol

from jobspybot.ai.service import Analysis
from jobspybot.billing import Billing
from jobspybot.config import Settings
from jobspybot.db import Database, User
from jobspybot.filters import check, local_score
from jobspybot.models import Vacancy
from jobspybot.relevance import Fit, mismatch_reason, role_fit, user_families
from jobspybot.sources import Source, SourceUnavailable

log = logging.getLogger(__name__)

#: не больше стольких вакансий одному пользователю за цикл — защита от флуда
MAX_SEND_PER_CYCLE = 15
UNAVAILABLE_BACKOFF = 6 * 3600
ERROR_BACKOFF = 30 * 60


class Evaluator(Protocol):
    def is_ready(self, user: User) -> bool: ...

    async def evaluate(self, user: User, vacancy: Vacancy) -> Analysis | None: ...


class Notifier(Protocol):
    async def send_vacancy(
        self, user: User, vacancy: Vacancy, delivery_id: int, extra: str = ""
    ) -> None: ...


@dataclass
class CycleStats:
    fetched: int = 0
    new: int = 0
    sent: int = 0
    filtered: int = 0
    ai_calls: int = 0
    ai_rejected: int = 0
    ai_deferred: int = 0
    errors: dict[str, str] = field(default_factory=dict)


@dataclass
class SourceStatus:
    """Здоровье площадки — для оповещений и веб-админки."""

    checked_at: datetime | None = None
    last_count: int = 0
    found_at: datetime | None = None  # когда последний раз нашлась хоть одна вакансия
    error: str = ""
    error_at: datetime | None = None


Alert = Callable[[str, str, float | None], object]


class Pipeline:
    def __init__(
        self,
        db: Database,
        sources: dict[str, Source],
        notifier: Notifier,
        settings: Settings,
        evaluator: Evaluator | None = None,
        billing: Billing | None = None,
    ) -> None:
        self.db = db
        self.sources = sources
        self.notifier = notifier
        self.settings = settings
        self.evaluator = evaluator
        self.billing = billing
        self._backoff: dict[str, float] = {}
        self.status: dict[str, SourceStatus] = {name: SourceStatus() for name in sources}
        self.started_at = datetime.now(UTC)
        #: оповещение админу: (ключ, текст, пауза между повторами в секундах)
        self.on_alert: Alert | None = None
        self.last_cycle: CycleStats | None = None
        self.last_cycle_at: datetime | None = None

    async def describe(self, vacancy: Vacancy) -> str:
        """Полное описание (с кэшем в БД). Запрашивается только для прошедших отсев."""
        if vacancy.description:
            return vacancy.description
        stored = self.db.get_vacancy(vacancy.uid)
        if stored and stored.description:
            vacancy.description = stored.description
            return stored.description
        source = self.sources.get(vacancy.source)
        if source is None:
            return vacancy.snippet
        text = await source.fetch_description(vacancy)
        vacancy.description = text
        self.db.set_description(vacancy.uid, text)
        return text

    # --- сбор ----------------------------------------------------------------

    def queries(self, user: User) -> list[str]:
        """Запросы пользователя в пределах его тарифа."""
        qs = user.filters.queries
        return qs[: self.billing.plan(user).max_queries] if self.billing else qs

    def _plan(self, users: list[User]) -> dict[str, set[str]]:
        """Какие запросы на какой площадке нужны (общие запросы разных людей — один раз)."""
        plan: dict[str, set[str]] = defaultdict(set)
        for user in users:
            for name in user.filters.sources or self.sources:
                if name in self.sources:
                    for q in self.queries(user):
                        plan[name].add(q.strip().lower())
        return plan

    async def _fetch_source(
        self, name: str, queries: set[str], stats: CycleStats
    ) -> dict[str, list[Vacancy]]:
        if self._backoff.get(name, 0) > time.monotonic():
            return {}
        source = self.sources[name]
        status = self.status.setdefault(name, SourceStatus())
        result: dict[str, list[Vacancy]] = {}
        for query in sorted(queries):
            try:
                result[query] = await source.search(query)
            except SourceUnavailable as exc:
                log.warning("%s unavailable: %s", name, exc)
                stats.errors[name] = str(exc)
                self._backoff[name] = time.monotonic() + UNAVAILABLE_BACKOFF
                break
            except Exception as exc:
                log.warning("%s search %r failed: %r", name, query, exc)
                stats.errors[name] = repr(exc)
                self._backoff[name] = time.monotonic() + ERROR_BACKOFF
                break
        now = datetime.now(UTC)
        status.checked_at = now
        status.last_count = sum(len(v) for v in result.values())
        if status.last_count:
            status.found_at = now
        if name in stats.errors:
            status.error, status.error_at = stats.errors[name][:300], now
        return result

    def check_silence(self, names: list[str], now: datetime | None = None) -> list[str]:
        """Площадки, которые дольше `source_silence_hours` не отдают вакансий."""
        hours = self.settings.guard.source_silence_hours
        if not hours:
            return []
        now = now or datetime.now(UTC)
        silent = []
        for name in names:
            status = self.status.setdefault(name, SourceStatus())
            since = status.found_at or self.started_at
            if now - since >= timedelta(hours=hours):
                silent.append(name)
                if self.on_alert:
                    last = f"\nПоследняя ошибка: {status.error}" if status.error else ""
                    self.on_alert(
                        f"source:{name}",
                        f"🔇 Площадка {name} не отдаёт вакансий больше {hours} ч.{last}",
                        hours * 3600,
                    )
        return silent

    async def fetch(
        self, users: list[User], stats: CycleStats
    ) -> dict[tuple[str, str], list[Vacancy]]:
        plan = self._plan(users)
        # площадки опрашиваются параллельно, запросы внутри площадки — последовательно
        results = await asyncio.gather(
            *(self._fetch_source(name, queries, stats) for name, queries in plan.items())
        )
        found: dict[tuple[str, str], list[Vacancy]] = {}
        for name, per_query in zip(plan, results, strict=True):
            for query, vacancies in per_query.items():
                found[(name, query)] = vacancies
                stats.fetched += len(vacancies)
        all_vacancies = {v.uid: v for vs in found.values() for v in vs}
        stats.new = len(self.db.save_vacancies(all_vacancies.values()))
        return found

    # --- отбор ---------------------------------------------------------------

    def wanted_roles(self, user: User) -> frozenset[str]:
        """Семейства ролей, которые ищет человек (по направлениям и навыкам)."""
        skills = list(user.filters.skills)
        for resume in self.db.get_resumes(user.id):
            skills += resume.skills
        return user_families(self.queries(user), skills)

    def candidates(
        self, user: User, found: dict[tuple[str, str], list[Vacancy]], stats: CycleStats
    ) -> list[tuple[int, Vacancy, Fit]]:
        """Прошедшие первичный отсев вакансии с локальным баллом, лучшие — первыми.

        Вакансии другой профессии (бизнес-аналитик для вайбкодера) отсеиваются здесь же:
        площадки ищут по тексту и отдают всё, где слово просто встретилось.
        """
        f = user.filters
        names = set(f.sources or self.sources)
        queries = {q.strip().lower() for q in self.queries(user)}
        pool: dict[str, Vacancy] = {}
        for (name, query), vacancies in found.items():
            if name in names and query in queries:
                for v in vacancies:
                    pool.setdefault(v.uid, v)

        # навыки каждого резюме — отдельный набор: QA-вакансия проходит за счёт QA-резюме,
        # а не тонет в общей куче навыков Python и C++
        skill_sets = [r.skills for r in self.db.get_resumes(user.id) if r.skills]
        if f.skills:
            skill_sets.append(f.skills)

        wanted = self.wanted_roles(user)
        passed: list[tuple[int, Vacancy, Fit]] = []
        seen_fp: set[str] = set()
        for v in pool.values():
            if v.fingerprint in seen_fp or self.db.is_known(user.id, v):
                continue
            seen_fp.add(v.fingerprint)
            reason = check(v, f, max_age_days=self.settings.max_vacancy_age_days)
            fit = role_fit(v.title, wanted)
            if reason is None and fit is Fit.MISMATCH:
                reason = mismatch_reason(v.title)
            score = max((local_score(v, s) for s in skill_sets), default=local_score(v, []))
            if reason is None and skill_sets and score < f.min_local_score:
                reason = f"мало совпадений навыков ({score})"
            if reason:
                self.db.record(user.id, v, "filtered", reason, score)
                stats.filtered += 1
                continue
            passed.append((score, v, fit))

        # явное совпадение роли — первым, потом по навыкам и свежести
        passed.sort(
            key=lambda c: (
                c[2] is Fit.MATCH,
                c[0],
                c[1].published_at.timestamp() if c[1].published_at else 0,
            ),
            reverse=True,
        )
        # остальные не записываются — дойдут в следующих циклах
        return passed[:MAX_SEND_PER_CYCLE]

    # --- рассылка ------------------------------------------------------------

    async def deliver(self, user: User, vacancy: Vacancy, score: int, extra: str = "") -> bool:
        self.db.record(user.id, vacancy, "pending", score=score)
        delivery_id = self.db.delivery_id(user.id, vacancy.uid)
        assert delivery_id is not None
        try:
            await self.notifier.send_vacancy(user, vacancy, delivery_id, extra)
        except Exception as exc:
            log.warning("send to %s failed: %r", user.id, exc)
            self.db.set_status(user.id, delivery_id, "error")
            return False
        self.db.set_status(user.id, delivery_id, "sent")
        return True

    async def process_user(
        self, user: User, found: dict[tuple[str, str], list[Vacancy]], stats: CycleStats
    ) -> None:
        candidates = self.candidates(user, found, stats)
        remaining = self.billing.remaining_today(user) if self.billing else None
        if remaining is not None:
            # дневной лимит тарифа: остальное не записываем — придёт завтра
            if remaining == 0 and candidates:
                hook = getattr(self.notifier, "daily_limit_reached", None)
                if hook:
                    await hook(user)
            candidates = candidates[:remaining]
        use_ai = self.evaluator is not None and self.evaluator.is_ready(user)
        if use_ai:
            # дорогой этап — только лучшие по локальному баллу, остальные подождут
            candidates = candidates[: self.settings.ai.max_per_cycle]
        for score, vacancy, fit in candidates:
            extra = ""
            if use_ai:
                assert self.evaluator is not None
                analysis = await self.evaluator.evaluate(user, vacancy)
                if analysis is None and fit is not Fit.MATCH:
                    # ИИ не смог оценить (кончился дневной лимит, сбой провайдера), а по
                    # названию роль не ясна — не шлём наугад, вакансия подождёт следующего цикла
                    stats.ai_deferred += 1
                    continue
                if analysis is not None:
                    stats.ai_calls += 1
                    if analysis.score < user.filters.ai_threshold or analysis.by == "no":
                        self.db.record(
                            user.id, vacancy, "ai_rejected", analysis.verdict, analysis.score
                        )
                        stats.ai_rejected += 1
                        continue
                    extra = analysis.render()
            if await self.deliver(user, vacancy, score, extra):
                stats.sent += 1

    async def run_cycle(self) -> CycleStats:
        stats = CycleStats()
        users = [u for u in self.db.active_users() if u.filters.queries]
        if self.billing:
            # бесплатный тариф проверяется реже
            billing = self.billing
            users = [u for u in users if billing.is_due(u)]
        if not users:
            return stats
        found = await self.fetch(users, stats)
        self.check_silence(sorted(self._plan(users)))
        for user in users:
            try:
                await self.process_user(user, found, stats)
            except Exception:
                log.exception("processing user %s failed", user.id)
            self.db.set_last_check(user.id)
        log.info(
            "cycle: fetched=%d new=%d filtered=%d ai=%d ai_rejected=%d ai_deferred=%d sent=%d"
            " errors=%s",
            stats.fetched,
            stats.new,
            stats.filtered,
            stats.ai_calls,
            stats.ai_rejected,
            stats.ai_deferred,
            stats.sent,
            list(stats.errors),
        )
        self.last_cycle, self.last_cycle_at = stats, datetime.now(UTC)
        return stats

    async def run_forever(self, stop: asyncio.Event, first_delay: float = 10.0) -> None:
        delay = first_delay
        cycles = 0
        while not stop.is_set():
            try:
                await asyncio.wait_for(stop.wait(), timeout=delay)
                break
            except TimeoutError:
                pass
            try:
                await self.run_cycle()
            except Exception:
                log.exception("cycle failed")
            cycles += 1
            if cycles % 48 == 0:
                self.db.purge_old()
            delay = self.settings.poll_interval_min * 60
