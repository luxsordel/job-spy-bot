from __future__ import annotations

from datetime import UTC, datetime

import httpx
import pytest

from jobspybot.config import ConfigError, Settings, parse_dotenv
from jobspybot.http import HttpError
from jobspybot.models import Level, Vacancy, normalize_currency
from jobspybot.text import html_to_text, parse_day_month, parse_iso
from tests.conftest import make_http


def test_parse_dotenv_handles_quotes_comments_and_export():
    env = parse_dotenv(
        """
        # comment
        A=1
        export B="two words"
        C='x # not a comment'
        D=value # trailing comment
        BROKEN
        """
    )
    assert env == {"A": "1", "B": "two words", "C": "x # not a comment", "D": "value"}


def test_settings_from_env():
    s = Settings.from_env(
        {
            "TELEGRAM_BOT_TOKEN": "t",
            "ADMIN_IDS": "1, 2",
            "ENABLED_SOURCES": "HH_BY,habr",
            "AI_BASE_URL": "https://api.example.com/v1/",
            "AI_API_KEY": "k",
            "AI_MODEL": "m",
        }
    )
    assert s.admin_ids == frozenset({1, 2})
    assert s.enabled_sources == frozenset({"hh_by", "habr"})
    assert s.ai.base_url == "https://api.example.com/v1"
    assert s.ai.enabled


@pytest.mark.parametrize(
    "env",
    [{"ADMIN_IDS": "abc"}, {"POLL_INTERVAL_MIN": "1"}, {"POLL_INTERVAL_MIN": "x"}],
)
def test_settings_rejects_bad_values(env):
    with pytest.raises(ConfigError):
        Settings.from_env(env)


def test_currency_normalization():
    assert normalize_currency("RUR") == "RUB"
    assert normalize_currency("byr") == "BYN"
    assert normalize_currency("₽") == "RUB"
    assert normalize_currency(None) is None


def test_vacancy_roundtrip_and_fingerprint():
    v = Vacancy(
        source="x",
        external_id="1",
        title="Python-разработчик (Junior)",
        url="u",
        company='ООО "Ромашка"',
        level=Level.JUNIOR,
        skills=("Python",),
        salary_from=1000,
        currency="USD",
        published_at=datetime(2026, 11, 1, tzinfo=UTC),
    )
    assert Vacancy.from_dict(v.to_dict()) == v
    other = Vacancy(
        source="y", external_id="9", title="python разработчик junior", url="u", company="Ромашка"
    )
    assert v.fingerprint == other.fingerprint
    assert v.salary_text() == "от 1 000 USD"


def test_html_to_text_keeps_structure():
    html = "<p>Требования:</p><ul><li>Python</li><li>SQL</li></ul><script>x()</script>"
    assert html_to_text(html) == "Требования:\n\n• Python\n• SQL"


def test_parse_dates():
    assert parse_iso("2026-11-19T12:00:00+0300") == datetime(2026, 11, 19, 9, tzinfo=UTC)
    assert parse_iso("2026-11-19T09:15:06.653Z").tzinfo is not None
    assert parse_iso("garbage") is None
    now = datetime(2026, 11, 19, tzinfo=UTC)
    assert parse_day_month("5 ноября", now) == datetime(2026, 11, 5, tzinfo=UTC)
    assert parse_day_month("18 December", now) == datetime(2025, 12, 18, tzinfo=UTC)


async def test_http_retries_on_5xx_then_succeeds(monkeypatch):
    monkeypatch.setattr("jobspybot.http.asyncio.sleep", _no_sleep)
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(503) if len(calls) == 1 else httpx.Response(200, json={"ok": 1})

    http = make_http(handler)
    assert await http.get_json("https://example.com/x") == {"ok": 1}
    assert len(calls) == 2
    await http.aclose()


async def test_http_does_not_retry_4xx():
    http = make_http(lambda r: httpx.Response(404, text="nope"))
    with pytest.raises(HttpError) as err:
        await http.get_json("https://example.com/x")
    assert err.value.status == 404
    await http.aclose()


async def _no_sleep(_):
    return None


def test_db_save_vacancies_returns_only_new(db):
    v = Vacancy(source="s", external_id="1", title="t", url="u")
    assert db.save_vacancies([v]) == {"s:1"}
    assert db.save_vacancies([v]) == set()
    db.set_description("s:1", "desc")
    assert db.get_vacancy("s:1").description == "desc"
