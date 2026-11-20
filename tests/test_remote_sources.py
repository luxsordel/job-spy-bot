"""Мировая удалёнка: Hirify, Remote.com, Himalayas, Remote OK, We Work Remotely, Working Nomads."""

from __future__ import annotations

from datetime import UTC, datetime

import httpx
import pytest

from jobspybot.filters import UserFilters, check
from jobspybot.geo import eligible, regions_text
from jobspybot.models import Level, Vacancy
from jobspybot.sources.base import matches_query
from jobspybot.sources.himalayas import Himalayas
from jobspybot.sources.hirify import Hirify
from jobspybot.sources.remotecom import RemoteCom
from jobspybot.sources.remoteok import RemoteOK
from jobspybot.sources.weworkremotely import WeWorkRemotely
from jobspybot.sources.workingnomads import WorkingNomads
from tests.conftest import fixture_json, fixture_text, make_http

NOW = datetime(2026, 11, 20, 12, tzinfo=UTC)


def json_handler(name: str, seen: list[httpx.Request] | None = None):
    def handler(request: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen.append(request)
        return httpx.Response(200, json=fixture_json(name))

    return handler


def text_handler(name: str):
    return lambda request: httpx.Response(200, text=fixture_text(name))


@pytest.mark.parametrize(
    ("regions", "by", "pl"),
    [
        (("Anywhere in the World",), True, True),
        (("USA Only",), False, False),
        (("Canada, United States",), False, False),
        (("Remote - US",), False, False),
        (("Europe Only",), None, True),
        (("LATAM, Europe, USA",), None, True),
        (("Poland",), False, True),
        (("Belarus", "Poland"), True, True),
        (("Worldwide", "except belarus"), False, True),
        (("Russia",), None, None),
        ((), None, None),
        # длинный перечень стран без Беларуси — явный отказ, даже с незнакомыми названиями
        (("Spain", "Portugal", "Brazil", "Aruba", "Bonaire"), False, False),
    ],
)
def test_eligible(regions, by, pl):
    assert eligible(regions, ["BY"]) is by
    assert eligible(regions, ["PL"]) is pl


def test_regions_text_is_short():
    text = regions_text([f"Country {i}" for i in range(30)])
    assert len(text) <= 60 and text.endswith("…")


def test_matches_query_ignores_generic_words():
    assert matches_query("python developer", "Senior Python Engineer")
    assert not matches_query("python developer", "Java Developer")
    assert matches_query("c++", "Senior C++ Engineer")
    assert matches_query("node.js", "Backend (Node.js)")
    assert not matches_query("go", "Google Ads manager")
    assert not matches_query("java", "JavaScript developer")
    assert matches_query("аналитик", "Ищем аналитиков данных")


def test_vacancy_regions_roundtrip():
    v = Vacancy("x", "1", "t", "u", regions=("USA",))
    assert Vacancy.from_dict(v.to_dict()).regions == ("USA",)
    assert Vacancy.from_dict({**v.to_dict(), "regions": None}).regions == ()


def remote_vac(**kw) -> Vacancy:
    base = dict(
        source="remoteok",
        external_id="1",
        title="Python Developer",
        url="u",
        country="REMOTE",
        remote=True,
        work_formats=("remote",),
        published_at=NOW,
    )
    return Vacancy(**{**base, **kw})


def test_filter_cuts_remote_not_for_candidate_country():
    f = UserFilters(countries=["BY", "REMOTE"], home_country="BY")
    assert check(remote_vac(regions=("USA Only",)), f, now=NOW) == "удалёнка только: USA Only"
    assert check(remote_vac(regions=("Worldwide",)), f, now=NOW) is None
    # неясно — решает ИИ
    assert check(remote_vac(regions=("Europe",)), f, now=NOW) is None
    # готов к релокации в Польшу — «только Польша» подходит
    f = UserFilters(countries=["BY", "PL", "REMOTE"], home_country="BY")
    assert check(remote_vac(regions=("Poland",)), f, now=NOW) is None


def test_filter_world_remote_needs_remote_country():
    f = UserFilters(countries=["BY"], home_country="BY")
    assert check(remote_vac(regions=("Worldwide",)), f, now=NOW) == "страна REMOTE"


async def test_remoteok_feed_is_fetched_once(settings):
    seen: list[httpx.Request] = []
    source = RemoteOK(make_http(json_handler("remoteok_feed.json", seen)), settings)
    (v,) = await source.search("python developer")
    assert await source.search("project manager")
    assert len(seen) == 1  # лента одна на цикл, запросы фильтруются у нас
    assert v.title == "Software Engineer AI Coding Tasks" and v.company == "Askable"
    assert v.url.startswith("https://remoteOK.com/remote-jobs/")
    assert v.country == "REMOTE" and v.remote is True and v.regions == ()
    # годовая зарплата → в месяц
    assert v.salary_from == 833 and v.salary_to is None and v.currency == "USD"
    assert "python" in v.skills and v.published_at is not None


async def test_himalayas(settings):
    seen: list[httpx.Request] = []
    http = make_http(json_handler("himalayas_search.json", seen))
    found = await Himalayas(http, settings).search("python")
    assert seen[0].url.params["q"] == "python"
    assert found[0].published_at > found[1].published_at  # от новых к старым
    us, worldwide = found
    assert worldwide.company == "Alien Intellect" and worldwide.level is Level.SENIOR
    assert worldwide.salary_from == 2000 and worldwide.salary_to == 3000
    assert worldwide.regions == ("Worldwide",) and worldwide.location == "Worldwide"
    assert worldwide.url.startswith("https://himalayas.app/companies/alien-intellect/jobs/")
    assert us.regions == ("United States",) and eligible(us.regions, ["BY"]) is False


async def test_weworkremotely(settings):
    source = WeWorkRemotely(make_http(text_handler("weworkremotely_feed.rss")), settings)
    (v,) = await source.search("python developer")
    assert v.company == "Vertican"
    assert v.title == "Senior Applied AI Engineer, Document Intelligence"
    assert v.regions == ("Anywhere in the World",) and v.skills == ("Python", "LLM")
    assert v.description == "Build document AI in Python."
    assert v.published_at == datetime(2026, 11, 20, 6, 2, 2, tzinfo=UTC)
    (support,) = await source.search("support")
    assert support.regions == ("USA Only",) and support.external_id == (
        "acme-customer-support-specialist"
    )


async def test_workingnomads(settings):
    source = WorkingNomads(make_http(json_handler("workingnomads_feed.json")), settings)
    (v,) = await source.search("python")
    assert v.company == "Lemon.io" and v.external_id == "1994915"
    assert v.regions == ("Europe",) and v.country == "REMOTE"


async def test_remotecom(settings):
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, text=fixture_text("remotecom_search.html"))

    proxify, northbeam, office = await RemoteCom(make_http(handler), settings).search("python")
    assert seen[0].url.params["query"] == "python"
    assert proxify.url == (
        "https://remote.com/jobs/proxify-c114ohln/senior-backend-developer-python-django-j12wcpci"
    )
    # суммы в центах, помесячно
    assert proxify.salary_from == 3000 and proxify.salary_to == 6000 and proxify.currency == "EUR"
    assert proxify.regions == ("Worldwide",) and proxify.level is Level.SENIOR
    assert northbeam.salary_from == 11667 and northbeam.level is Level.MIDDLE
    assert northbeam.regions == ("Canada", "United States") and northbeam.country == "REMOTE"
    # гибрид в Варшаве — вакансия Польши, без ограничений удалёнки
    assert office.country == "PL" and office.remote is False and office.regions == ()

    http = make_http(text_handler("remotecom_job.html"))
    text = await RemoteCom(http, settings).fetch_description(proxify)
    assert text == "About us:\nTalent has no borders."


async def test_remotecom_layout_change_is_reported(settings):
    from jobspybot.sources import SourceUnavailable

    http = make_http(lambda request: httpx.Response(200, text="<html></html>"))
    with pytest.raises(SourceUnavailable):
        await RemoteCom(http, settings).search("python")


async def test_hirify(settings):
    seen: list[httpx.Request] = []
    http = make_http(json_handler("hirify_search.json", seen))
    softbee, ru, uk, cis = await Hirify(http, settings).search("python developer")
    assert seen[0].url.params["search"] == "python developer"
    assert softbee.url == (
        "https://hirify.me/jobs/1448753-python-developer-middlesenior-ai-powered-products"
    )
    assert softbee.country == "REMOTE" and softbee.regions == ("Worldwide",)
    # почасовая ставка → в месяц; два грейда — уровень не задаём
    assert softbee.salary_from == 4200 and softbee.salary_to is None and softbee.level is None
    assert ru.country == "RU" and ru.regions == ("Russia",) and ru.company == ""
    assert ru.salary_from == 100000 and ru.currency == "RUB"
    # гибрид в Лондоне: страна вакансии, компания скрыта площадкой
    assert uk.country == "United Kingdom" and uk.regions == () and uk.company == ""
    assert uk.salary_from == 5000 and uk.level is Level.SENIOR and uk.location == "Лондон"
    assert cis.level is Level.JUNIOR and eligible(cis.regions, ["BY"]) is True

    http = make_http(lambda request: httpx.Response(200, json={"text": "<p>Задачи</p>"}))
    assert await Hirify(http, settings).fetch_description(ru) == "Задачи"


async def test_telegram_channels_parse_posts():
    from jobspybot.config import Settings
    from jobspybot.sources.telegram_channels import TelegramChannels

    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        return httpx.Response(200, text=fixture_text("telegram_channel.html"))

    settings = Settings(tg_channels=("remocate",))
    source = TelegramChannels(make_http(handler), settings)
    python, cro = await source.feed()  # реклама и статья отсеяны, свежие — первыми
    assert seen == ["/s/remocate"]

    assert cro.title == "CRO (Conversion Rate Optimization) Specialist"
    assert cro.company == "GipsyTeam" and cro.url == "https://t.me/remocate/6841"
    assert cro.remote is True and cro.work_formats == ("remote",)
    assert cro.published_at == datetime(2026, 11, 20, 8, 15, tzinfo=UTC)
    assert "Зарплата в долларах" in cro.description

    # «Новая вакансия» — заголовок, название берётся из следующей строки
    assert python.title == "Senior Python Developer (FastAPI)"
    assert python.skills == ("python", "fastapi") and python.work_formats == ("hybrid",)

    assert [v.external_id for v in await source.search("python developer")] == ["remocate/6844"]
    assert seen == ["/s/remocate"]  # лента кэшируется на цикл


def test_tg_channels_setting():
    from jobspybot.config import DEFAULT_TG_CHANNELS, ConfigError, Settings

    assert Settings.from_env({}).tg_channels == DEFAULT_TG_CHANNELS
    env = {"TG_CHANNELS": "@GeekJobs, https://t.me/forproducts, geekjobs"}
    assert Settings.from_env(env).tg_channels == ("geekjobs", "forproducts")
    assert Settings.from_env({"TG_CHANNELS": "-"}).tg_channels == ()
    with pytest.raises(ConfigError):
        Settings.from_env({"TG_CHANNELS": "bad channel!"})


def test_telegram_source_off_without_channels():
    from jobspybot.config import Settings
    from jobspybot.sources.telegram_channels import TelegramChannels

    assert not TelegramChannels.is_configured(Settings(tg_channels=()))
