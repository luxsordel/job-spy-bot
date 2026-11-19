from __future__ import annotations

from datetime import UTC, datetime

import pytest

from jobspybot.config import Settings
from jobspybot.filters import UserFilters
from jobspybot.models import Vacancy
from jobspybot.pipeline import MAX_SEND_PER_CYCLE, Pipeline
from jobspybot.sources import SourceUnavailable


def vac(source: str, ext: str, title: str, company: str = "Acme", **kw) -> Vacancy:
    return Vacancy(
        source=source,
        external_id=ext,
        title=title,
        url=f"https://x/{ext}",
        company=company,
        published_at=datetime.now(UTC),
        **kw,
    )


class FakeSource:
    def __init__(self, name: str, results: dict[str, list[Vacancy]] | Exception):
        self.name = name
        self.results = results
        self.calls: list[str] = []

    async def search(self, query: str) -> list[Vacancy]:
        self.calls.append(query)
        if isinstance(self.results, Exception):
            raise self.results
        return self.results.get(query, [])


class FakeNotifier:
    def __init__(self, fail_for: set[int] | None = None):
        self.sent: list[tuple[int, str]] = []
        self.fail_for = fail_for or set()

    async def send_vacancy(self, user, vacancy, delivery_id, extra=""):
        if user.id in self.fail_for:
            raise RuntimeError("blocked by user")
        self.sent.append((user.id, vacancy.uid))


def add_user(db, uid: int, **filters) -> None:
    db.ensure_user(uid, f"u{uid}", allowed=True)
    db.save_filters(uid, UserFilters(**filters))


@pytest.fixture
def pipeline_factory(db):
    def make(sources, notifier=None):
        return Pipeline(db, {s.name: s for s in sources}, notifier or FakeNotifier(), Settings())

    return make


async def test_shared_query_is_fetched_once_and_cross_source_duplicates_dropped(
    db, pipeline_factory
):
    a = FakeSource("a", {"python": [vac("a", "1", "Python Dev"), vac("a", "2", "Senior Python")]})
    b = FakeSource("b", {"python": [vac("b", "9", "Python dev", company="ACME")]})
    add_user(db, 1, queries=["Python"], exclude=["senior"])
    add_user(db, 2, queries=["python "])
    notifier = FakeNotifier()
    p = pipeline_factory([a, b], notifier)

    stats = await p.run_cycle()

    assert a.calls == ["python"] and b.calls == ["python"]
    sent_1 = {uid for user, uid in notifier.sent if user == 1}
    sent_2 = {uid for user, uid in notifier.sent if user == 2}
    assert len(sent_1) == 1  # a:1 и b:9 — одна и та же вакансия, a:2 — стоп-слово
    assert len(sent_2) == 2
    assert stats.filtered == 1

    # повторный цикл ничего не присылает
    notifier.sent.clear()
    await p.run_cycle()
    assert notifier.sent == []


async def test_user_sources_are_respected(db, pipeline_factory):
    a = FakeSource("a", {"go": [vac("a", "1", "Go Dev")]})
    b = FakeSource("b", {"go": [vac("b", "2", "Golang engineer")]})
    add_user(db, 1, queries=["go"], sources=["b"])
    notifier = FakeNotifier()
    await pipeline_factory([a, b], notifier).run_cycle()
    assert notifier.sent == [(1, "b:2")]
    assert a.calls == []


async def test_unavailable_source_backs_off(db, pipeline_factory):
    bad = FakeSource("bad", SourceUnavailable("403"))
    add_user(db, 1, queries=["x", "y"])
    p = pipeline_factory([bad])
    stats = await p.run_cycle()
    assert "bad" in stats.errors
    assert bad.calls == ["x"]  # после первой ошибки остальные запросы не шлём
    await p.run_cycle()
    assert bad.calls == ["x"]  # и в следующем цикле тоже


async def test_flood_cap_and_rest_deferred(db, pipeline_factory):
    many = [vac("a", str(i), f"Python {i}", company=f"C{i}") for i in range(MAX_SEND_PER_CYCLE + 5)]
    src = FakeSource("a", {"python": many})
    add_user(db, 1, queries=["python"])
    notifier = FakeNotifier()
    p = pipeline_factory([src], notifier)
    await p.run_cycle()
    assert len(notifier.sent) == MAX_SEND_PER_CYCLE
    await p.run_cycle()
    assert len(notifier.sent) == MAX_SEND_PER_CYCLE + 5


async def test_low_local_score_is_filtered(db, pipeline_factory):
    src = FakeSource("a", {"dev": [vac("a", "1", "Python Django dev"), vac("a", "2", "PHP dev")]})
    add_user(db, 1, queries=["dev"], skills=["python", "django"], min_local_score=30)
    notifier = FakeNotifier()
    await pipeline_factory([src], notifier).run_cycle()
    assert notifier.sent == [(1, "a:1")]


async def test_failed_send_is_not_retried_forever(db, pipeline_factory):
    src = FakeSource("a", {"python": [vac("a", "1", "Python")]})
    add_user(db, 1, queries=["python"])
    notifier = FakeNotifier(fail_for={1})
    p = pipeline_factory([src], notifier)
    stats = await p.run_cycle()
    assert stats.sent == 0
    assert db.stats(1) == {"error": 1}


async def test_changing_filters_reevaluates_filtered(db, pipeline_factory):
    src = FakeSource("a", {"python": [vac("a", "1", "Senior Python")]})
    add_user(db, 1, queries=["python"], exclude=["senior"])
    notifier = FakeNotifier()
    p = pipeline_factory([src], notifier)
    await p.run_cycle()
    assert notifier.sent == []
    db.save_filters(1, UserFilters(queries=["python"]))
    await p.run_cycle()
    assert notifier.sent == [(1, "a:1")]


async def test_source_health_and_silence_alert(db, pipeline_factory):
    from datetime import timedelta

    habr = FakeSource("habr", {"python": [vac("habr", "1", "Python dev")]})
    praca = FakeSource("praca_by", SourceUnavailable("403"))
    pipeline = pipeline_factory([habr, praca])
    alerts: list[tuple[str, str, float | None]] = []
    pipeline.on_alert = lambda key, text, cooldown: alerts.append((key, text, cooldown))
    add_user(db, 1, queries=["python"])
    await pipeline.run_cycle()
    assert pipeline.status["habr"].last_count == 1 and pipeline.status["habr"].found_at
    assert pipeline.status["praca_by"].error == "403" and not pipeline.status["praca_by"].found_at
    assert alerts == []  # только что запустились — рано тревожить
    later = datetime.now(UTC) + timedelta(hours=13)
    assert pipeline.check_silence(["habr", "praca_by"], now=later) == ["habr", "praca_by"]
    keys = [k for k, _, _ in alerts]
    assert keys == ["source:habr", "source:praca_by"] and "403" in alerts[1][1]
    assert pipeline.last_cycle is not None and pipeline.last_cycle.fetched == 1
