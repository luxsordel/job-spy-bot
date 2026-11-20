"""Цикл мониторинга: опрос площадок → отсев → (ИИ) → рассылка."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Protocol

from jobspybot import notify
from jobspybot.ai.service import Analysis
from jobspybot.billing import Billing
from jobspybot.config import Settings
from jobspybot.db import Database, User
from jobspybot.filters import check, local_score
from jobspybot.models import Vacancy
from jobspybot.relevance import Fit, mismatch_reason, role_fit, user_families
from jobspybot.sources import Source, SourceUnavailable

log = logging.getLogger(__name__)
# поиск по площадкам — отдельный раздел живого лога в админке
src_log = logging.getLogger("jobspybot.sources")


def who(user: User) -> str:
    return f"{user.id} @{user.username}" if user.username else str(user.id)


#: не больше стольких вакансий одному пользователю за цикл — защита от флуда
MAX_SEND_PER_CYCLE = 15
#: сколько отложенных за ночь вакансий присылать утром за один раз (остальные — позже)
MAX_FLUSH = 20
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
    queued: int = 0
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
        self.cycle_started: datetime | None = None  # не None, пока идёт цикл
        self.started_at = datetime.now(UTC)
        #: оповещение админу: (ключ, текст, пауза между повторами в секундах)
        self.on_alert: Alert | None = None
        self.last_cycle: CycleStats | None = None
        self.last_cycle_at: datetime | None = None
        #: текущее время (подменяется в тестах тихих часов и дайджеста)
        self.clock: Callable[[], datetime] = lambda: datetime.now(UTC)

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
        pause = self._backoff.get(name, 0) - time.monotonic()
        if pause > 0:
            src_log.info("%s: пропуск, пауза после ошибки ещё %d мин", name, pause // 60 + 1)
            return {}
        source = self.sources[name]
        status = self.status.setdefault(name, SourceStatus())
        result: dict[str, list[Vacancy]] = {}
        for query in sorted(queries):
            started = time.monotonic()
            try:
                result[query] = await source.search(query)
                src_log.info(
                    "%s «%s»: %d вакансий за %.1f с",
                    name,
                    query,
                    len(result[query]),
                    time.monotonic() - started,
                )
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
                reason = mismatch_reason(v.title, wanted)
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

    async def deliver(
        self,
        user: User,
        vacancy: Vacancy,
        score: int,
        extra: str = "",
        ai_score: int | None = None,
        stats: CycleStats | None = None,
    ) -> bool:
        if notify.hold(user.filters, self.clock()):
            # тихие часы или дайджест: отложить; оценка ИИ хранится в reason
            self.db.record(
                user.id, vacancy, "queued", extra, ai_score if ai_score is not None else score
            )
            if stats is not None:
                stats.queued += 1
            return False
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
        for i, (score, vacancy, _fit) in enumerate(candidates):
            extra = ""
            ai_score: int | None = None
            if use_ai:
                assert self.evaluator is not None
                analysis = await self.evaluator.evaluate(user, vacancy)
                if analysis is None:
                    # ИИ не смог оценить (кончился лимит, сбой провайдера). Без оценки не шлём
                    # ничего — даже «подходящее по названию»: по одному названию приходило
                    # совсем не то. Вакансии не записаны и дождутся, когда ИИ снова заработает;
                    # остальных в этом цикле не трогаем — сбой у них был бы тот же.
                    stats.ai_deferred += len(candidates) - i
                    await self._ai_unavailable(user)
                    break
                if analysis is not None:
                    stats.ai_calls += 1
                    if analysis.score < user.filters.ai_threshold or analysis.by == "no":
                        self.db.record(
                            user.id, vacancy, "ai_rejected", analysis.verdict, analysis.score
                        )
                        stats.ai_rejected += 1
                        log.info(
                            "ИИ отклонил для %s: «%s» (%d)",
                            who(user),
                            vacancy.title[:80],
                            analysis.score,
                        )
                        continue
                    extra = analysis.render()
                    ai_score = analysis.score
            if await self.deliver(user, vacancy, score, extra, ai_score, stats):
                stats.sent += 1
                log.info(
                    "→ %s: «%s» (%s, балл %d%s)",
                    who(user),
                    vacancy.title[:80],
                    vacancy.source,
                    score,
                    f", ИИ {ai_score}" if ai_score is not None else "",
                )

    async def _ai_unavailable(self, user: User) -> None:
        """Сообщить человеку, что кончился его лимит ИИ (сбой провайдера — дело админа)."""
        has_budget = getattr(self.evaluator, "has_budget", None)
        hook = getattr(self.notifier, "ai_limit_reached", None)
        if has_budget is None or hook is None or has_budget(user):
            return
        try:
            await hook(user)
        except Exception as exc:
            log.warning("ai limit notice to %s failed: %r", user.id, exc)

    # --- отложенные: тихие часы и дайджест ------------------------------------

    async def flush(self) -> int:
        """Отправить отложенное, если уже можно. Возвращает число отправленных вакансий."""
        now = self.clock()
        sent = 0
        for uid in self.db.users_with_queue():
            user = self.db.get_user(uid)
            if user is None or not user.allowed or user.banned or user.paused:
                continue
            try:
                sent += await self._flush_user(user, now)
            except Exception:
                log.exception("flush for user %s failed", uid)
        return sent

    async def _flush_user(self, user: User, now: datetime) -> int:
        f = user.filters
        if f.notify_mode == notify.DIGEST:
            if not notify.digest_due(f, now) or self.db.count_today(user.id, "digest"):
                return 0
            return await self._send_digest(user)
        if notify.in_quiet_hours(f, now):
            return 0
        sent = 0
        for delivery_id, vacancy, extra, _ in self.db.queued(user.id)[:MAX_FLUSH]:
            try:
                await self.notifier.send_vacancy(user, vacancy, delivery_id, extra)
            except Exception as exc:
                log.warning("send queued to %s failed: %r", user.id, exc)
                self.db.set_status(user.id, delivery_id, "error")
                continue
            self.db.set_status(user.id, delivery_id, "sent")
            sent += 1
        return sent

    async def _send_digest(self, user: User) -> int:
        queued = self.db.queued(user.id)
        if not queued:
            return 0
        # лучшие по оценке ИИ (без неё — по локальному баллу); с оценкой ИИ — выше
        queued.sort(key=lambda q: (bool(q[2]), q[3] or 0), reverse=True)
        top = queued[: notify.DIGEST_SIZE]
        text, count = notify.digest_text(
            [(v, score if extra else None) for _, v, extra, score in top], len(queued)
        )
        send = getattr(self.notifier, "send_digest", None)
        if send is None:
            return 0
        await send(user, text)
        self.db.bump(user.id, "digest")
        for i, (delivery_id, *_rest) in enumerate(queued):
            self.db.set_status(user.id, delivery_id, "sent" if i < count else "skipped")
        return count

    async def flush_forever(self, stop: asyncio.Event, every: float = 300) -> None:
        """Каждые 5 минут: не закончились ли тихие часы, не пора ли дайджест."""
        while not stop.is_set():
            try:
                await self.flush()
            except Exception:
                log.exception("flush failed")
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=every)

    async def run_cycle(self, *, force: bool = False) -> CycleStats:
        """Цикл поиска. `force` — проверить всех сразу, не дожидаясь интервала тарифа
        (внеочередной запуск из админки после деплоя)."""
        stats = CycleStats()
        users = [u for u in self.db.active_users() if u.filters.queries]
        if self.billing and not force:
            # бесплатный тариф проверяется реже
            billing = self.billing
            users = [u for u in users if billing.is_due(u)]
        if not users:
            log.info("cycle: некого проверять (нет активных пользователей с запросами)")
            return stats
        plan = self._plan(users)
        log.info(
            "cycle start: пользователей %d, площадок %d, запросов %d",
            len(users),
            len(plan),
            sum(len(q) for q in plan.values()),
        )
        self.cycle_started = datetime.now(UTC)
        try:
            found = await self.fetch(users, stats)
            log.info("сбор: получено %d, новых %d", stats.fetched, stats.new)
            self.check_silence(sorted(plan))
            for user in users:
                before = (stats.filtered, stats.ai_calls, stats.ai_rejected, stats.sent)
                try:
                    await self.process_user(user, found, stats)
                except Exception:
                    log.exception("processing user %s failed", user.id)
                self.db.set_last_check(user.id)
                after = (stats.filtered, stats.ai_calls, stats.ai_rejected, stats.sent)
                d = [b - a for a, b in zip(before, after, strict=True)]
                log.info(
                    "%s: отсеяно %d, оценил ИИ %d, отклонил ИИ %d, отправлено %d",
                    who(user),
                    *d,
                )
        finally:
            self.cycle_started = None
        log.info(
            "cycle: fetched=%d new=%d filtered=%d ai=%d ai_rejected=%d ai_deferred=%d sent=%d"
            " queued=%d errors=%s",
            stats.fetched,
            stats.new,
            stats.filtered,
            stats.ai_calls,
            stats.ai_rejected,
            stats.ai_deferred,
            stats.sent,
            stats.queued,
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
