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

    def __init__(self, scores: dict[str, int] | None = None, json_mode: bool = True):
        self.requests: list[dict] = []
        self.scores = scores or {}
        self.json_mode = json_mode

    def __call__(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        self.requests.append(body)
        if "response_format" in body and not self.json_mode:
            return httpx.Response(400, json={"error": "response_format unsupported"})
        system = body["messages"][0]["content"]
        user = body["messages"][1]["content"]
        if system.startswith("Сожми резюме"):
            content = json.dumps(PROFILE, ensure_ascii=False)
        elif "сопроводительное" in system:
            content = "Здравствуйте! Мой опыт с Django…"
        else:
            title = user.split("\n", 1)[0].removeprefix("Вакансия: ")
            score = self.scores.get(title, 80)
            content = (
                "```json\n"
                + json.dumps(
                    {"score": score, "verdict": "ок", "pros": ["Django"], "cons": [], "by": "ok"}
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
    assert "100/100" in Analysis.from_data({"score": 100}).render()


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


async def test_profile_built_and_skills_autofilled(db):
    fake = FakeLLM()
    ai = make_ai(db, fake)
    user = db.ensure_user(1, allowed=True)
    reply = await ai.build_profile(user, "резюме " * 20)
    stored = db.get_user(1)
    assert json.loads(stored.profile)["skills"] == ["python", "django", "postgresql"]
    assert stored.filters.skills == ["python", "django", "postgresql"]
    assert "Профиль готов" in reply
    assert db.tokens_today(1) == 360


async def test_evaluate_uses_cache_and_budget(db):
    fake = FakeLLM()
    ai = make_ai(db, fake, daily_tokens_per_user=1000)
    user = db.ensure_user(1, allowed=True)
    user.profile = json.dumps(PROFILE, ensure_ascii=False)
    v = vac("1", "Python Dev", description="Требования:\nDjango")
    db.save_vacancies([v])

    first = await ai.evaluate(user, v)
    again = await ai.evaluate(user, v)
    assert first == again and first.score == 80
    assert len(fake.requests) == 1  # второй раз — из кэша

    # профиль в system-сообщении, вакансия — в user: префикс общий для всех вакансий
    msgs = fake.requests[0]["messages"]
    assert '"python"' in msgs[0]["content"] and "Python Dev" in msgs[1]["content"]
    assert fake.requests[0]["max_tokens"] <= 450

    await ai.evaluate(user, vac("2", "Go Dev"))
    await ai.evaluate(user, vac("3", "Rust Dev"))  # 3*360 > 1000 — лимит
    assert await ai.evaluate(user, vac("4", "Java Dev")) is None
    assert len(fake.requests) == 3


async def test_json_mode_fallback(db):
    fake = FakeLLM(json_mode=False)
    ai = make_ai(db, fake)
    user = db.ensure_user(1, allowed=True)
    user.profile = "{}"
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
    db.save_filters(1, UserFilters(queries=["dev"], ai_threshold=60))
    db.save_resume(1, "резюме", json.dumps(PROFILE, ensure_ascii=False))
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


async def test_cover_letter(db):
    ai = make_ai(db, FakeLLM())
    user = db.ensure_user(1, allowed=True)
    assert "резюме" in await ai.cover_letter(user, vac("1", "X"))
    user.profile = "{}"
    assert "Django" in await ai.cover_letter(user, vac("1", "X"))


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
    user.profile = "{}"
    assert (await ai.evaluate(user, vac("1", "X"))).score == 71
