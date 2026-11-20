from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from jobspybot.ai.client import LLMClient, LLMError, parse_json
from jobspybot.ai.compress import compress_description
from jobspybot.ai.service import AIService, Analysis
from jobspybot.config import AISettings, Settings
from jobspybot.filters import UserFilters
from jobspybot.models import Vacancy
from jobspybot.pipeline import Pipeline
from tests.test_pipeline import FakeNotifier, FakeSource

PROFILE = {
    "summary": "Python-разработчик, 2 года, Django",
    "level": "junior",
    "skills": ["python", "django", "postgresql"],
    "languages": ["английский B1"],
    "wants": "удалёнка",
}


class FakeLLM:
    """OpenAI-совместимый сервер: отвечает по содержимому system-промпта."""

    def __init__(
        self,
        scores: dict[str, int] | None = None,
        json_mode: bool = True,
        best: dict[str, int] | None = None,
    ):
        self.requests: list[dict] = []
        self.scores = scores or {}
        self.best = best or {}
        self.json_mode = json_mode

    def __call__(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        self.requests.append(body)
        if "response_format" in body and not self.json_mode:
            return httpx.Response(400, json={"error": "response_format unsupported"})
        system = body["messages"][0]["content"]
        user = body["messages"][1]["content"]
        if system.startswith("Разбери резюме"):
            content = json.dumps(PROFILE, ensure_ascii=False)
        elif "сопроводительное" in system:
            content = "Здравствуйте! Мой опыт с Django…"
        else:
            title = user.split("\n", 1)[0].removeprefix("Вакансия: ")
            score = self.scores.get(title, 80)
            content = (
                "```json\n"
                + json.dumps(
                    {
                        "best": self.best.get(title, 1),
                        "score": score,
                        "verdict": "ок",
                        "pros": ["Django"],
                        "cons": [],
                        "by": "ok",
                    }
                )
                + "\n```"
            )
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": content}}],
                "usage": {"prompt_tokens": 300, "completion_tokens": 60, "total_tokens": 360},
            },
        )


def make_ai(db, fake: FakeLLM, **ai_kw) -> AIService:
    llm = LLMClient("https://llm.test/v1", "key", "model", transport=httpx.MockTransport(fake))
    return AIService(db, llm, AISettings(base_url="x", api_key="k", model="m", **ai_kw))


def vac(ext: str, title: str, **kw) -> Vacancy:
    # явное время публикации: при равном балле новые идут первыми, порядок детерминирован
    kw.setdefault("published_at", datetime.now(UTC) - timedelta(minutes=int(ext)))
    return Vacancy(
        source="a",
        external_id=ext,
        title=title,
        url=f"https://x/{ext}",
        company=f"C{ext}",
        **kw,
    )


def test_parse_json_variants():
    assert parse_json('```json\n{"a": 1}\n```') == {"a": 1}
    assert parse_json('Вот ответ: {"a": 2} спасибо') == {"a": 2}
    with pytest.raises(LLMError):
        parse_json("нет json")


def test_analysis_from_messy_data():
    a = Analysis.from_data({"score": "250", "pros": "не список", "by": "maybe", "cons": [1, ""]})
    assert a.score == 100 and a.pros == [] and a.by == "ok" and a.cons == ["1"]
    assert "ИИ · 100" in Analysis.from_data({"score": 100}).render()


def test_compress_drops_boilerplate_keeps_relocation():
    text = """Python Developer
О компании:
Мы — лидер рынка, 20 лет на рынке, офисы в 10 странах.
Требования:
• Python 3, Django
• Python 3, Django
Мы предлагаем:
• ДМС, печеньки, фитнес
• Помощь с релокацией в Польшу
Задачи:
• Писать API
🔥🔥🔥"""
    out = compress_description(text, 2000)
    assert "лидер рынка" not in out and "печеньки" not in out
    assert "релокацией" in out
    assert out.count("Python 3, Django") == 1
    assert "Писать API" in out and "🔥" not in out


def test_compress_truncates():
    out = compress_description("\n".join(f"строка номер {i} " * 5 for i in range(200)), 500)
    assert len(out) <= 500


def add_resume(db, user_id: int, title: str, profile: dict | None = None, **kw) -> int:
    rid = db.add_resume(user_id, title, "текст резюме " * 10, **kw)
    if profile is not None:
        db.set_resume_profile(rid, json.dumps(profile, ensure_ascii=False))
    return rid


async def test_profile_built_from_redacted_resume(db):
    fake = FakeLLM()
    ai = make_ai(db, fake)
    user = db.ensure_user(1, allowed=True)
    rid = db.add_resume(1, "Основное", "Иван, +375 (29) 123-45-67, ivan@mail.com, Python " * 3)
    reply = await ai.build_profile(user, db.get_resume(1, rid))
    stored = db.get_resume(1, rid)
    assert stored.skills == ["python", "django", "postgresql"]
    assert "«Основное» разобрано" in reply
    assert db.tokens_today(1) == 360
    sent = fake.requests[0]["messages"][1]["content"]
    assert "ivan@mail.com" not in sent and "123-45-67" not in sent


async def test_evaluate_uses_cache_and_budget(db):
    fake = FakeLLM()
    ai = make_ai(db, fake, daily_tokens_per_user=1000)
    user = db.ensure_user(1, allowed=True)
    add_resume(db, 1, "Основное", PROFILE)
    v = vac("1", "Python Dev", description="Требования:\nDjango")
    db.save_vacancies([v])

    first = await ai.evaluate(user, v)
    again = await ai.evaluate(user, v)
    assert first == again and first.score == 80
    assert first.resume_title == ""  # резюме одно — в карточке не показываем
    assert len(fake.requests) == 1  # второй раз — из кэша

    # профили в system-сообщении, вакансия — в user: префикс общий для всех вакансий
    msgs = fake.requests[0]["messages"]
    assert '"python"' in msgs[0]["content"] and "Python Dev" in msgs[1]["content"]
    assert fake.requests[0]["max_tokens"] <= 450

    await ai.evaluate(user, vac("2", "Go Dev"))
    await ai.evaluate(user, vac("3", "Rust Dev"))  # 3*360 > 1000 — лимит
    assert await ai.evaluate(user, vac("4", "Java Dev")) is None
    assert len(fake.requests) == 3


async def test_best_resume_is_chosen_from_shortlist(db):
    fake = FakeLLM(best={"QA Engineer": 2})
    ai = make_ai(db, fake)
    user = db.ensure_user(1, allowed=True)
    profiles = {
        "Python": ["python", "django"],
        "QA": ["qa", "selenium", "тестирование"],
        "C++": ["c++", "qt"],
        "Java": ["java", "spring"],
        "1C": ["1с"],
    }
    ids = {
        title: add_resume(db, 1, title, {**PROFILE, "skills": skills}, file_id=f"file-{title}")
        for title, skills in profiles.items()
    }
    v = vac("1", "QA Engineer", skills=("Selenium",), snippet="ручное тестирование")

    analysis = await ai.evaluate(user, v)
    system = fake.requests[0]["messages"][0]["content"]
    # в модель ушли только 3 резюме, и QA среди них
    assert system.count("»: ") == 3 and "«QA»" in system
    assert analysis.resume_id == ids["QA"]
    assert analysis.resume_title == "QA"
    assert "Резюме: «QA»" in analysis.render()
    best = await ai.best_resume(user, v)
    assert best.file_id == "file-QA"
    assert len(fake.requests) == 1  # best_resume взял выбор из кэша


async def test_near_duplicate_resumes_go_to_model_once(db):
    """Две версии одного резюме удваивали цену каждой оценки (у владельца ~6 000 токенов)."""
    fake = FakeLLM()
    ai = make_ai(db, fake)
    user = db.ensure_user(1, allowed=True)
    base = ["python", "fastapi", "sql", "docker", "git", "linux", "react", "c++", "qt", "java"]
    add_resume(db, 1, "Резюме", {**PROFILE, "skills": [*base, "ci", "автотесты"]})
    add_resume(db, 1, "Резюме (ИИ)", {**PROFILE, "skills": [*base, "ci/cd", "code review"]})
    add_resume(db, 1, "QA", {**PROFILE, "skills": ["qa", "selenium", "тестирование"]})
    await ai.evaluate(user, vac("1", "Python developer"))
    system = fake.requests[0]["messages"][0]["content"]
    # из двух версий осталась новая, другое по сути резюме (QA) — тоже
    assert system.count("»: ") == 2 and "«Резюме (ИИ)»" in system and "«QA»" in system


async def test_json_mode_fallback(db):
    fake = FakeLLM(json_mode=False)
    ai = make_ai(db, fake)
    user = db.ensure_user(1, allowed=True)
    add_resume(db, 1, "R", {})
    assert (await ai.evaluate(user, vac("1", "X"))).score == 80
    assert len(fake.requests) == 2
    await ai.evaluate(user, vac("2", "Y"))
    assert "response_format" not in fake.requests[-1]


async def test_pipeline_ai_stage(db):
    fake = FakeLLM(scores={"Python Dev": 90, "Django Dev": 30})
    ai = make_ai(db, fake, max_per_cycle=2)
    src = FakeSource(
        "a",
        {"dev": [vac("1", "Python Dev"), vac("2", "Django Dev"), vac("3", "Flask Dev")]},
    )

    async def describe(v):
        return "описание"

    ai.describe = describe
    db.ensure_user(1, allowed=True)
    db.save_filters(1, UserFilters(queries=["dev"], ai_threshold=60, min_local_score=0))
    add_resume(db, 1, "Основное", PROFILE)
    notifier = FakeNotifier()
    settings = Settings(ai=AISettings(max_per_cycle=2))
    p = Pipeline(db, {"a": src}, notifier, settings, evaluator=ai)

    stats = await p.run_cycle()
    assert stats.ai_calls == 2 and stats.ai_rejected == 1
    assert [uid for _, uid in notifier.sent] == ["a:1"]
    # третья вакансия не влезла в лимит ИИ на цикл — оценится в следующем
    await p.run_cycle()
    assert [uid for _, uid in notifier.sent] == ["a:1", "a:3"]
    assert len(fake.requests) == 3


async def test_local_score_uses_each_resume_separately(db):
    """QA-вакансия проходит отсев за счёт QA-резюме, даже если остальные резюме — про Python."""
    src = FakeSource(
        "a",
        {"x": [vac("1", "QA Engineer", snippet="selenium, тестирование"), vac("2", "Маркетолог")]},
    )
    db.ensure_user(1, allowed=True)
    db.save_filters(1, UserFilters(queries=["x"], min_local_score=30))
    for i, skills in enumerate(
        [
            ["python", "django", "fastapi", "postgresql", "docker"],
            ["c++", "qt", "winapi"],
            ["qa", "selenium", "тестирование"],
        ]
    ):
        add_resume(db, 1, f"R{i}", {"skills": skills})
    notifier = FakeNotifier()
    await Pipeline(db, {"a": src}, notifier, Settings()).run_cycle()
    assert [uid for _, uid in notifier.sent] == ["a:1"]


async def test_cover_letter_uses_given_resume(db):
    fake = FakeLLM()
    ai = make_ai(db, fake)
    user = db.ensure_user(1, allowed=True)
    rid = add_resume(db, 1, "QA", {**PROFILE, "summary": "QA-инженер"})
    letter = await ai.cover_letter(user, vac("1", "X"), db.get_resume(1, rid))
    assert "Django" in letter
    assert "QA-инженер" in fake.requests[0]["messages"][0]["content"]


def test_salvage_truncated_reply():
    from jobspybot.ai.service import salvage_json

    cut = '{"score": 38, "by": "no", "verdict": "Удалёнка только из РФ", "pros": ["Стек совп'
    data = salvage_json(cut)
    a = Analysis.from_data(data)
    assert (a.score, a.by, a.verdict) == (38, "no", "Удалёнка только из РФ")
    assert salvage_json('{"verdict": "без балла"') is None


async def test_evaluate_survives_truncated_json(db):
    class Truncating(FakeLLM):
        def __call__(self, request):
            resp = super().__call__(request)
            body = resp.json()
            body["choices"][0]["message"]["content"] = '{"score": 71, "by": "ok", "pros": ["Dja'
            return httpx.Response(200, json=body)

    ai = make_ai(db, Truncating())
    user = db.ensure_user(1, allowed=True)
    add_resume(db, 1, "R", {})
    assert (await ai.evaluate(user, vac("1", "X"))).score == 71


async def test_evaluate_retries_once_on_garbage(db):
    class Flaky(FakeLLM):
        def __call__(self, request):
            resp = super().__call__(request)
            if len(self.requests) == 1:
                body = resp.json()
                body["choices"][0]["message"]["content"] = '{"best'
                return httpx.Response(200, json=body)
            return resp

    fake = Flaky()
    ai = make_ai(db, fake)
    user = db.ensure_user(1, allowed=True)
    add_resume(db, 1, "R", {})
    assert (await ai.evaluate(user, vac("1", "X"))).score == 80
    assert len(fake.requests) == 2


async def test_parse_profile_edit(db):
    class EditLLM(FakeLLM):
        def __call__(self, request):
            body = json.loads(request.content)
            self.requests.append(body)
            content = (
                '{"work_formats": ["hybrid"], "only_home_city": true, "wishes": "Фрунзенский"}'
            )
            return httpx.Response(
                200,
                json={
                    "choices": [{"message": {"content": content}}],
                    "usage": {"total_tokens": 90},
                },
            )

    fake = EditLLM()
    ai = make_ai(db, fake)
    user = db.ensure_user(1, allowed=True)
    patch = await ai.parse_profile_edit(user, "только гибрид во Фрунзенском районе")
    assert patch["work_formats"] == ["hybrid"] and patch["only_home_city"] is True
    assert "Текущий профиль" in fake.requests[0]["messages"][0]["content"]
    assert fake.requests[0]["max_tokens"] <= 300


def test_vacancy_prompt_has_address_and_format():
    from jobspybot.ai.service import vacancy_prompt

    v = vac("1", "Dev", address="Минск, ул. Притыцкого (м. Пушкинская)", work_formats=("hybrid",))
    text = vacancy_prompt(v, "")
    assert "Адрес: Минск, ул. Притыцкого (м. Пушкинская)" in text and "Формат: hybrid" in text


async def test_global_budget_reserves_rest_for_pro(db):
    fake = FakeLLM()
    ai = make_ai(db, fake, daily_tokens_total=1000, pro_reserve_pct=20)
    alerts = []
    ai.on_alert = lambda key, text: alerts.append((key, text))
    free = db.ensure_user(1, allowed=True)
    pro = db.ensure_user(2, allowed=True)
    ai.is_priority = lambda user: user.id == 2
    assert ai.has_budget(free) and ai.has_budget(pro)
    db.add_tokens(3, 850)  # другие пользователи потратили 85 %
    assert not ai.has_budget(free) and ai.has_budget(pro)
    assert "только для Pro" in alerts[-1][1]
    db.add_tokens(3, 200)
    assert not ai.has_budget(pro)
    assert "исчерпан" in alerts[-1][1]
    rid = db.add_resume(1, "CV", "Python developer " * 10)
    reply = await ai.build_profile(free, db.get_resume(1, rid))
    assert "лимит ИИ исчерпан" in reply and fake.requests == []


async def test_fallback_model(db):
    fake = FakeLLM()
    seen = []

    def flaky(request):
        model = json.loads(request.content)["model"]
        seen.append(model)
        if model == "model":
            return httpx.Response(401, json={"error": "model unavailable"})
        return fake(request)

    llm = LLMClient("https://llm.test/v1", "key", "model", transport=httpx.MockTransport(flaky))
    ai = AIService(
        db, llm, AISettings(base_url="x", api_key="k", model="m", fallback_model="spare")
    )
    user = db.ensure_user(1, allowed=True)
    rid = db.add_resume(1, "CV", "Python developer " * 10)
    reply = await ai.build_profile(user, db.get_resume(1, rid))
    assert "разобрано" in reply and seen == ["model", "spare"]
