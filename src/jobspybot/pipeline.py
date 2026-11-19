"""Цикл мониторинга: опрос площадок → отсев → (ИИ) → рассылка."""

from __future__ import annotations

import asyncio
import logging
import time
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Protocol

from jobspybot.ai.service import Analysis
from jobspybot.config import Settings
from jobspybot.db import Database, User
from jobspybot.filters import check, local_score
from jobspybot.models import Vacancy
from jobspybot.sources import Source, SourceUnavailable

log = logging.getLogger(__name__)

#: не больше стольких вакансий одному пользователю за цикл — защита от флуда
MAX_SEND_PER_CYCLE = 15
UNAVAILABLE_BACKOFF = 6 * 3600
ERROR_BACKOFF = 30 * 60


class Evaluator(Protocol):
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
    errors: dict[str, str] = field(default_factory=dict)


class Pipeline:
    def __init__(
        self,
        db: Database,
        sources: dict[str, Source],
        notifier: Notifier,
        settings: Settings,
        evaluator: Evaluator | None = None,
    ) -> None:
        self.db = db
        self.sources = sources
        self.notifier = notifier
        self.settings = settings
        self.evaluator = evaluator
        self._backoff: dict[str, float] = {}

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

    def _plan(self, users: list[User]) -> dict[str, set[str]]:
        """Какие запросы на какой площадке нужны (общие запросы разных людей — один раз)."""
        plan: dict[str, set[str]] = defaultdict(set)
        for user in users:
            for name in user.filters.sources or self.sources:
                if name in self.sources:
                    for q in user.filters.queries:
                        plan[name].add(q.strip().lower())
        return plan

    async def _fetch_source(
        self, name: str, queries: set[str], stats: CycleStats
    ) -> dict[str, list[Vacancy]]:
        if self._backoff.get(name, 0) > time.monotonic():
            return {}
        source = self.sources[name]
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
        return result

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

    def candidates(
        self, user: User, found: dict[tuple[str, str], list[Vacancy]], stats: CycleStats
    ) -> list[tuple[int, Vacancy]]:
        """Прошедшие первичный отсев вакансии с локальным баллом, лучшие — первыми."""
        f = user.filters
        names = set(f.sources or self.sources)
        queries = {q.strip().lower() for q in f.queries}
        pool: dict[str, Vacancy] = {}
        for (name, query), vacancies in found.items():
            if name in names and query in queries:
                for v in vacancies:
                    pool.setdefault(v.uid, v)

        passed: list[tuple[int, Vacancy]] = []
        seen_fp: set[str] = set()
        for v in pool.values():
            if v.fingerprint in seen_fp or self.db.is_known(user.id, v):
                continue
            seen_fp.add(v.fingerprint)
            reason = check(v, f, max_age_days=self.settings.max_vacancy_age_days)
            score = local_score(v, f.skills)
            if reason is None and f.skills and score < f.min_local_score:
                reason = f"мало совпадений навыков ({score})"
            if reason:
                self.db.record(user.id, v, "filtered", reason, score)
                stats.filtered += 1
                continue
            passed.append((score, v))

        passed.sort(
            key=lambda sv: (sv[0], sv[1].published_at.timestamp() if sv[1].published_at else 0),
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
        use_ai = self.evaluator is not None and bool(user.profile)
        if use_ai:
            # дорогой этап — только лучшие по локальному баллу, остальные подождут
            candidates = candidates[: self.settings.ai.max_per_cycle]
        for score, vacancy in candidates:
            extra = ""
            if use_ai:
                assert self.evaluator is not None
                analysis = await self.evaluator.evaluate(user, vacancy)
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
        if not users:
            return stats
        found = await self.fetch(users, stats)
        for user in users:
            try:
                await self.process_user(user, found, stats)
            except Exception:
                log.exception("processing user %s failed", user.id)
        log.info(
            "cycle: fetched=%d new=%d filtered=%d ai=%d ai_rejected=%d sent=%d errors=%s",
            stats.fetched,
            stats.new,
            stats.filtered,
            stats.ai_calls,
            stats.ai_rejected,
            stats.sent,
            list(stats.errors),
        )
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
