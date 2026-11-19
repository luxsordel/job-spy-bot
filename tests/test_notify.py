"""Дайджест и тихие часы (#35)."""

from __future__ import annotations

from datetime import UTC, datetime

from jobspybot import notify
from jobspybot.config import Settings
from jobspybot.filters import UserFilters
from jobspybot.pipeline import Pipeline
from tests.test_bot import FakeTelegram, buttons, callback, make_bot, onboarded_user, shown
from tests.test_pipeline import FakeNotifier, FakeSource, add_user, vac

# 23:30 по Минску (UTC+3) и 08:10 утра
NIGHT = datetime(2026, 11, 20, 20, 30, tzinfo=UTC)
MORNING = datetime(2026, 11, 21, 5, 10, tzinfo=UTC)


def make_pipeline(db, source, notifier):
    return Pipeline(db, {source.name: source}, notifier, Settings())


class DigestNotifier(FakeNotifier):
    def __init__(self):
        super().__init__()
        self.digests: list[tuple[int, str]] = []

    async def send_digest(self, user, text):
        self.digests.append((user.id, text))


def test_quiet_hours_and_timezone():
    f = UserFilters(quiet_hours=True, home_country="BY")
    assert notify.in_quiet_hours(f, NIGHT) and not notify.in_quiet_hours(f, MORNING)
    assert notify.hold(f, NIGHT) and not notify.hold(f, MORNING)
    # в Казахстане (UTC+5) в то же время уже 01:30, а в Польше (UTC+1) — 21:30
    assert notify.utc_offset(UserFilters(home_country="KZ")) == 5
    assert not notify.in_quiet_hours(UserFilters(quiet_hours=True, utc_offset=1), NIGHT)
    assert not notify.hold(UserFilters(), NIGHT)  # по умолчанию шлём сразу и ночью


def test_filters_roundtrip_and_garbage():
    f = UserFilters.from_json('{"notify_mode": "x", "digest_hour": 99, "utc_offset": 77}')
    assert (f.notify_mode, f.digest_hour, f.utc_offset) == ("instant", 9, None)
    f = UserFilters(notify_mode="digest", digest_hour=18, quiet_hours=True, utc_offset=4)
    assert UserFilters.from_json(f.to_json()) == f


def test_digest_text_fits_telegram():
    many = [(vac("a", str(i), "Очень длинное название " * 10), 80) for i in range(30)]
    text, count = notify.digest_text(many, 30)
    assert len(text) < 4096 and 0 < count < 30 and f"{count} лучших из 30" in text


async def test_night_vacancies_wait_for_morning(db):
    src = FakeSource(
        "a",
        {"python": [vac("a", "1", "Python Dev"), vac("a", "2", "Python Engineer", company="B")]},
    )
    add_user(db, 1, queries=["python"], quiet_hours=True)
    notifier = FakeNotifier()
    p = make_pipeline(db, src, notifier)
    p.clock = lambda: NIGHT
    stats = await p.run_cycle()
    assert notifier.sent == [] and stats.queued == 2
    assert await p.flush() == 0  # ещё ночь
    p.clock = lambda: MORNING
    assert await p.flush() == 2
    assert {uid for _, uid in notifier.sent} == {"a:1", "a:2"}
    assert db.queued(1) == [] and db.stats(1).get("sent") == 2
    # второй цикл не присылает те же вакансии повторно
    await p.run_cycle()
    assert len(notifier.sent) == 2


async def test_daily_digest(db):
    jobs = [vac("a", str(i), f"Python {i}", company=f"C{i}") for i in range(12)]
    src = FakeSource("a", {"python": jobs})
    add_user(db, 1, queries=["python"], notify_mode="digest", digest_hour=9)
    notifier = DigestNotifier()
    p = make_pipeline(db, src, notifier)
    p.clock = lambda: MORNING  # 08:10 по Минску — ещё рано
    await p.run_cycle()
    assert notifier.sent == [] and len(db.queued(1)) == 12
    assert await p.flush() == 0
    at_nine = datetime(2026, 11, 21, 6, 5, tzinfo=UTC)
    p.clock = lambda: at_nine
    assert await p.flush() == notify.DIGEST_SIZE
    ((uid, text),) = notifier.digests
    assert uid == 1 and "10 лучших из 12" in text and 'href="https://x/' in text
    assert db.queued(1) == []  # не вошедшие в дайджест не копятся
    assert await p.flush() == 0  # второй раз за день не шлём


async def test_notify_settings_screen(db):
    tg = FakeTelegram()
    bot = make_bot(db, tg)
    onboarded_user(db, 200, queries=["python"])
    await bot.handle_update(callback(200, "nt:show"))
    assert "Когда присылать" in shown(tg) and "UTC+3" in shown(tg)
    await bot.handle_update(callback(200, "nt:mode:digest"))
    assert "nt:hour:9" in {b.get("callback_data") for b in buttons(tg)}
    await bot.handle_update(callback(200, "nt:hour:18"))
    await bot.handle_update(callback(200, "nt:quiet"))
    await bot.handle_update(callback(200, "nt:tz:5"))
    f = db.get_user(200).filters
    assert (f.notify_mode, f.digest_hour, f.quiet_hours, f.utc_offset) == ("digest", 18, True, 5)
    await bot.handle_update(callback(200, "nt:hour:3"))  # не из списка — игнор
    assert db.get_user(200).filters.digest_hour == 18
