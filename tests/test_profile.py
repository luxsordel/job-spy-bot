from __future__ import annotations

from jobspybot.filters import UserFilters
from jobspybot.profile import UserProfile, apply_to_filters

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
