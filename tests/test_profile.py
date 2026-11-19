from __future__ import annotations

from jobspybot.filters import UserFilters
from jobspybot.profile import UserProfile, apply_patch, apply_to_filters, remember_override

EXTRACTED = {
    "name": "Владимир",
    "headline": "Junior Python backend-разработчик",
    "level": "junior",
    "experience_years": 1.5,
    "skills": ["python", "fastapi", "postgresql"],
    "languages": ["английский C1"],
    "city": "Минск",
    "country": "by",
    "work_formats": ["office", "hybrid", "remote", "space"],
    "relocation": None,
    "salary_usd": None,
    "directions": ["python developer", "backend developer"],
}


def test_merge_fills_profile_from_resume():
    p = UserProfile()
    changed = p.merge_extracted(EXTRACTED)
    assert p.name == "Владимир" and p.level == "junior" and p.experience_years == 1.5
    assert p.country == "BY" and p.work_formats == ["office", "hybrid", "remote"]
    assert p.relocation is None and p.salary_usd is None
    assert {"name", "level", "skills", "work_formats", "directions"} <= set(changed)
    # из резюме нет ответа про релокацию и зарплату — спросим только это
    assert p.missing(UserFilters()) == ["relocation", "salary"]


def test_merge_does_not_override_answers_and_unions_lists():
    p = UserProfile(level="middle", answered=["level"], skills=["Python"])
    p.merge_extracted({**EXTRACTED, "skills": ["python", "docker"], "level": "junior"})
    assert p.level == "middle"
    assert p.skills == ["Python", "docker"]
    p.merge_extracted({"directions": ["qa engineer"], "salary_usd": "2000"})
    assert p.directions == ["python developer", "backend developer", "qa engineer"]
    assert p.salary_usd == 2000


def test_merge_ignores_garbage():
    p = UserProfile()
    p.merge_extracted(
        {
            "experience_years": "много",
            "skills": "python",
            "salary_usd": "?",
            "country": "Belarus",
            "level": "god",
        }
    )
    assert p.experience_years is None and p.skills == [] and p.salary_usd is None
    assert p.country == "" and p.level == ""


def test_apply_to_filters():
    p = UserProfile()
    p.merge_extracted(EXTRACTED)
    p.relocation, p.relocation_countries, p.salary_usd = True, ["PL", "GE"], 1500
    f = UserFilters()
    apply_to_filters(p, f, max_queries=1)
    assert f.queries == ["python developer"]
    assert f.levels == ["intern", "junior"]
    assert f.countries == ["BY", "PL", "GE", "REMOTE"]
    assert not f.remote_only and f.min_salary_usd == 1200
    # уже заданные направления профиль не перетирает
    f.queries = ["qa engineer"]
    apply_to_filters(p, f, max_queries=10)
    assert f.queries == ["qa engineer"]


def test_remote_only_profile():
    p = UserProfile(work_formats=["remote"], relocation=False)
    f = UserFilters()
    apply_to_filters(p, f, max_queries=5)
    assert f.countries == ["REMOTE"] and f.remote_only


def test_context_line():
    p = UserProfile(
        city="Минск",
        country="BY",
        work_formats=["remote"],
        relocation=True,
        relocation_countries=["PL"],
        salary_usd=1200,
    )
    line = p.context_line()
    assert "гражданство BY" in line and "релокации (PL)" in line and "1200$" in line
    assert UserProfile.from_json(p.to_json()) == p


def test_apply_to_filters_sets_formats_and_city():
    p = UserProfile(
        city="Минск", country="BY", work_formats=["hybrid"], only_home_city=True, strict_format=True
    )
    f = UserFilters()
    apply_to_filters(p, f, max_queries=5)
    assert f.formats == ["hybrid"] and f.strict_format
    assert (f.only_home_city, f.home_city, f.home_country) == (True, "Минск", "BY")
    assert f.countries == ["BY"]


def test_patch_from_phrase():
    from jobspybot.profile import apply_patch, preview_patch

    p = UserProfile(city="Минск", country="BY", work_formats=["remote", "office"])
    f = UserFilters(queries=["python developer"])
    patch = {
        "work_formats": ["hybrid"],
        "only_home_city": True,
        "wishes": "Фрунзенский район",
        "salary_usd": 1000,
        "directions_add": ["backend developer", "go developer"],
        "exclude_add": ["senior"],
        "garbage": 1,
    }
    lines = preview_patch(p, f, patch, max_queries=2)
    assert p.work_formats == ["remote", "office"] and f.queries == ["python developer"]
    assert any("гибрид" in x.lower() for x in lines)
    assert any("не влезло" in x for x in lines)

    apply_patch(p, f, patch, max_queries=2)
    assert p.work_formats == ["hybrid"] and p.only_home_city and p.wishes == "Фрунзенский район"
    assert f.formats == ["hybrid"] and f.only_home_city and f.min_salary_usd == 800
    assert f.queries == ["python developer", "backend developer"]
    assert f.exclude == ["senior"]
    assert "Фрунзенский район" in p.context_line()


def test_several_levels_and_any_level():
    p = UserProfile(level="junior")
    assert p.target_levels() == ["intern", "junior"]  # из резюме — диапазон
    p.toggle_level("middle")
    p.toggle_level("intern")
    assert p.levels == ["junior", "middle"] and p.target_levels() == ["junior", "middle"]
    f = UserFilters()
    apply_to_filters(p, f, max_queries=3)
    assert f.levels == ["junior", "middle"]
    # снять все — значит «любой уровень», а не откат к уровню из резюме
    p.set_levels([])
    apply_to_filters(p, f, max_queries=3)
    assert f.levels == [] and p.missing(f).count("level") == 0
    restored = UserProfile.from_json(p.to_json())
    assert restored.target_levels() == []


def test_patch_levels_from_phrase():
    p, f = UserProfile(level="junior"), UserFilters()
    apply_patch(p, f, {"levels": ["Middle", "junior", "god"]}, max_queries=3)
    assert f.levels == ["junior", "middle"]
    apply_patch(p, f, {"level": "senior"}, max_queries=3)
    assert f.levels == ["middle", "senior"]


def test_patch_rejects_unrealistic_salary():
    p, f = UserProfile(salary_usd=1000), UserFilters()
    changes = apply_patch(p, f, {"salary_usd": 10**40}, max_queries=3)
    assert p.salary_usd == 1000 and not any("зарплата" in c for c in changes)
    apply_patch(p, f, {"salary_usd": "1e400"}, max_queries=3)
    assert p.salary_usd == 1000


def test_manual_filter_edit_survives_profile_changes():
    """Снятая в фильтрах страна не возвращается от правки, не связанной со странами."""
    p = UserProfile(
        country="BY",
        work_formats=["office", "remote"],
        relocation=True,
        relocation_countries=["PL", "GE"],
        salary_usd=1000,
    )
    f = UserFilters()
    apply_to_filters(p, f, max_queries=3)
    assert f.countries == ["BY", "PL", "GE", "REMOTE"] and f.min_salary_usd == 800
    f.countries = ["BY", "PL", "REMOTE"]
    f.min_salary_usd = 1500
    remember_override(p, f, "countries")
    remember_override(p, f, "min_salary_usd")
    p.wishes = "без ночных смен"
    apply_to_filters(p, f, max_queries=3)
    assert f.countries == ["BY", "PL", "REMOTE"] and f.min_salary_usd == 1500
    # сменилась основа — релокация: страны снова выводятся из профиля
    p.relocation_countries = ["PL"]
    apply_to_filters(p, f, max_queries=3)
    assert f.countries == ["BY", "PL", "REMOTE"] and "countries" not in p.overrides
    p.salary_usd = 2000
    apply_to_filters(p, f, max_queries=3)
    assert f.min_salary_usd == 1600
