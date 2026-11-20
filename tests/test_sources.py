from __future__ import annotations

import httpx

from jobspybot.models import Level
from jobspybot.sources import REGISTRY, SOURCE_ORDER
from jobspybot.sources.getmatch import GetMatch
from jobspybot.sources.habr import Habr
from jobspybot.sources.jobsge import JobsGe
from jobspybot.sources.justjoin import JustJoin
from jobspybot.sources.nofluffjobs import NoFluffJobs
from jobspybot.sources.praca import PracaBy
from jobspybot.sources.superjob import SuperJob
from tests.conftest import fixture_json, fixture_text, make_http


def json_handler(name: str):
    return lambda request: httpx.Response(200, json=fixture_json(name))


def html_handler(name: str):
    return lambda request: httpx.Response(200, text=fixture_text(name))


def test_source_order_covers_registry():
    assert set(SOURCE_ORDER) == set(REGISTRY)
    assert len(SOURCE_ORDER) == len(REGISTRY)


async def test_habr(settings):
    http = make_http(json_handler("habr_search.json"))
    first, second = await Habr(http, settings).search("python")
    assert first.url == "https://career.habr.com/vacancies/1000170506"
    assert first.currency == "RUB" and first.salary_from == 40000
    assert first.level is Level.INTERN and first.remote is True
    assert first.skills == ("Python", "FastAPI")
    assert second.location == "Новосибирск" and second.level is Level.SENIOR

    http = make_http(html_handler("habr_vacancy.html"))
    assert await Habr(http, settings).fetch_description(first) == "Задачи\n\n• Писать API"


async def test_getmatch_skips_inactive_and_keeps_description(settings):
    http = make_http(json_handler("getmatch_search.json"))
    (v,) = await GetMatch(http, settings).search("python")
    assert v.company == "VK" and v.country == "RU" and v.remote is True
    assert v.salary_from == 338000 and v.currency == "RUB"
    assert v.description == "Что делать: писать сервисы"
    # описание уже есть в выдаче — повторный запрос не нужен
    assert await GetMatch(http, settings).fetch_description(v) == v.description


async def test_superjob(settings):
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(200, json=fixture_json("superjob_search.json"))

    http = make_http(handler)
    (v,) = await SuperJob(http, settings).search("python")
    assert seen[0].headers["X-Api-App-Id"] == "sj-key"
    assert v.remote is True and v.salary_to is None and v.level is Level.MIDDLE
    assert v.published_at is not None


async def test_praca_by(settings):
    http = make_http(html_handler("praca_search.html"))
    first, second = await PracaBy(http, settings).search("python")
    assert first.external_id == "638670"
    assert first.url == "https://praca.by/vacancy/638670/"
    assert first.title == "Стажёр С++ / Программист"
    assert first.company == 'ООО "БелХард"'
    assert first.level is Level.JUNIOR and first.published_at.month == 11
    assert second.level is Level.SENIOR and second.remote is True


async def test_praca_by_retries_with_significant_words(settings):
    queries = []

    def handler(request):
        query = request.url.params["search[query]"]
        queries.append(query)
        html = fixture_text("praca_search.html") if query == "python" else "<ul></ul>"
        return httpx.Response(200, text=html)

    found = await PracaBy(make_http(handler), settings).search("Python Developer")
    assert queries == ["Python Developer", "python"] and len(found) == 2

    queries.clear()
    assert await PracaBy(make_http(handler), settings).search("python") and queries == ["python"]


async def test_jobs_ge_uses_logo_alt_for_company(settings):
    http = make_http(html_handler("jobsge_search.html"))
    first, second = await JobsGe(http, settings).search("python")
    assert first.external_id == "768931"
    assert first.company == "Combined Ratio Solutions"
    assert second.company == "Logiks Solutions"
    assert first.published_at.day == 17


async def test_justjoin_prefers_original_currency_and_converts_hourly(settings):
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(200, json=fixture_json("justjoin_search.json"))

    http = make_http(handler)
    (v,) = await JustJoin(http, settings).search("python")
    assert seen[0].url.params["keywords"] == "python"
    assert v.currency == "PLN" and v.salary_from == 170 * 168
    assert v.level is Level.MIDDLE and v.remote is True
    assert v.url == "https://justjoin.it/job-offer/rits-python-developer-krakow-python"


async def test_nofluffjobs_dedupes_and_normalizes(settings):
    http = make_http(json_handler("nofluffjobs_search.json"))
    lead, junior = await NoFluffJobs(http, settings).search("python")
    assert lead.salary_from == 150 * 168 and lead.level is Level.LEAD and lead.remote
    assert junior.salary_to == 10000 and junior.level is Level.JUNIOR

    http = make_http(json_handler("nofluffjobs_posting.json"))
    text = await NoFluffJobs(http, settings).fetch_description(lead)
    assert text == "We build data apps.\n\nОбязательно: Python, SQL\n\n• 5+ years"


async def test_work_formats_from_sources(settings):
    http = make_http(json_handler("justjoin_search.json"))
    (jj,) = await JustJoin(http, settings).search("python")
    assert jj.work_formats == ("remote",)
    http = make_http(json_handler("nofluffjobs_search.json"))
    lead, junior = await NoFluffJobs(http, settings).search("python")
    assert lead.work_formats == ("remote",) and junior.work_formats == ()
    http = make_http(json_handler("superjob_search.json"))
    (sj,) = await SuperJob(http, settings).search("python")
    assert sj.work_formats == ("remote",)
    http = make_http(json_handler("getmatch_search.json"))
    (gm,) = await GetMatch(http, settings).search("python")
    assert gm.work_formats == ("remote",)
