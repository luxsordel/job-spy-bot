"""Совпадение роли: тестировщику-вайбкодеру не должен приходить бизнес-аналитик."""

from __future__ import annotations

import pytest

from jobspybot.ai.service import AIService
from jobspybot.config import Settings
from jobspybot.profile import UserProfile
from jobspybot.relevance import Fit, families, mismatch_reason, role_fit, user_families
from tests.test_ai import FakeLLM, make_ai, vac
from tests.test_pipeline import FakeNotifier, FakeSource, add_user

VIBE_QA = user_families(["вайбкодер", "тестировщик", "ai developer"])


def test_user_families_include_related_roles():
    assert {"ai", "qa", "python", "dev"} <= VIBE_QA
    assert "analyst" not in VIBE_QA and "sales" not in VIBE_QA
    assert user_families(["бухгалтер"]) == frozenset()  # роль не распознана — не фильтруем
    # технологии из навыков расширяют круг: питонисту со знанием Go подходит Go
    assert "go" in user_families(["python developer"], ["golang", "docker"])


@pytest.mark.parametrize(
    ("title", "fit"),
    [
        ("Бизнес-аналитик", Fit.MISMATCH),
        ("Системный аналитик", Fit.MISMATCH),
        ("Менеджер по продажам", Fit.MISMATCH),
        ("Оператор call-центра", Fit.MISMATCH),
        ("Java Developer", Fit.MISMATCH),
        ("Программист 1С", Fit.MISMATCH),
        ("Python-разработчик", Fit.MATCH),
        ("AI-инженер", Fit.MATCH),
        ("Специалист по внедрению ИИ", Fit.MATCH),
        ("Prompt engineer", Fit.MATCH),
        ("QA Engineer (manual)", Fit.MATCH),
        ("Тестировщик мобильных приложений", Fit.MATCH),
        ("Бизнес-аналитик AI-продуктов", Fit.MISMATCH),  # профессия важнее темы
        ("Менеджер по продажам ИИ-решений", Fit.MISMATCH),
        ("Разработчик", Fit.UNKNOWN),
        ("Стажёр в IT-компанию", Fit.UNKNOWN),
    ],
)
def test_role_fit_for_vibe_coder_and_tester(title, fit):
    assert role_fit(title, VIBE_QA) is fit


def test_word_boundaries():
    assert "java" not in families("JavaScript developer")
    assert "ai" not in families("Retail manager")  # «ai» внутри слова не считается
    assert "qa" in families("Junior QA")
    assert families("Агент по недвижимости") == set()
    assert mismatch_reason("Бизнес-аналитик") == "другая роль: аналитик"


async def test_pipeline_drops_other_professions(db):
    """Регрессия (тестировщик Миша): по запросу «вайбкодер» приходили бизнес-аналитики."""
    found = [
        vac("1", "Бизнес-аналитик", snippet="Работа с AI-инструментами, вайбкодинг"),
        vac("2", "Python-разработчик (AI)"),
        vac("3", "QA Engineer"),
        vac("4", "Менеджер по продажам ИИ-решений"),
        vac("5", "Разработчик"),
    ]
    src = FakeSource("a", {"вайбкодер": found})
    notifier = FakeNotifier()
    from jobspybot.pipeline import Pipeline

    add_user(db, 1, queries=["вайбкодер"])
    db.save_profile(1, UserProfile(skills=[]))
    pipeline = Pipeline(db, {"a": src}, notifier, Settings())
    stats = await pipeline.run_cycle()
    sent = {uid.split(":")[1] for _, uid in notifier.sent}
    # «вайбкодер» — это AI-разработка: Python, AI и просто «Разработчик» — да;
    # QA он в этом примере не искал, аналитик и продажи — нет
    assert sent == {"2", "5"}
    reasons = {d["title"]: d["reason"] for d in db.recent_deliveries(1)}
    assert reasons["Бизнес-аналитик"] == "другая роль: аналитик"
    assert reasons["Менеджер по продажам ИИ-решений"] == "другая роль: продажи"
    assert stats.filtered == 3


async def test_without_ai_budget_only_clear_matches_are_sent(db):
    """ИИ не смог оценить — шлём только то, где роль ясна по названию, остальное ждёт."""
    fake = FakeLLM()
    ai = make_ai(db, fake, daily_tokens_per_user=1)  # бюджет сразу исчерпан
    db.add_tokens(1, 10)
    src = FakeSource("a", {"ai developer": [vac("1", "AI developer"), vac("2", "Разработчик")]})
    notifier = FakeNotifier()
    from jobspybot.pipeline import Pipeline

    add_user(db, 1, queries=["ai developer"])
    db.add_tokens(1, 10)
    pipeline = Pipeline(db, {"a": src}, notifier, Settings(), evaluator=ai)
    stats = await pipeline.run_cycle()
    assert [uid for _, uid in notifier.sent] == ["a:1"]
    assert stats.ai_deferred == 1 and not fake.requests
    assert all(d["title"] != "Разработчик" for d in db.recent_deliveries(1))  # не записана


async def test_ai_scores_by_profile_when_resume_is_missing(db):
    fake = FakeLLM()
    ai: AIService = make_ai(db, fake)
    user = db.ensure_user(1, allowed=True)
    assert not ai.is_ready(user)
    add_user(db, 1, queries=["вайбкодер", "тестировщик"])
    db.save_profile(1, UserProfile(headline="Вайбкодер, ручное тестирование", skills=["cursor"]))
    user = db.get_user(1)
    assert ai.is_ready(user)
    analysis = await ai.evaluate(user, vac("1", "Python developer"))
    assert analysis is not None and analysis.resume_id is None
    system = fake.requests[0]["messages"][0]["content"]
    assert "ищет: вайбкодер, тестировщик" in system and "Вайбкодер, ручное" in system
    assert "другая профессия" in system
