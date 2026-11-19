"""Совпадение роли: бесплатная проверка, что вакансия — та профессия, которую человек ищет.

Площадки ищут по тексту: на запрос «вайбкодер» или «ai developer» они отдают и бизнес-
аналитика, у которого в описании мелькнуло «AI». Поэтому названия вакансий и направления
человека раскладываются по «семействам» ролей (ai, python, qa, analyst, sales…), и вакансия
другой профессии отсеивается до ИИ.

Семейство `dev` («разработчик», «developer») — общее: само по себе оно не доказывает
совпадения, если у вакансии есть более конкретная роль («Java developer» для питониста).
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from enum import StrEnum
from functools import lru_cache

#: ключевые слова названий. Короткие (≤3 символов) и помеченные «!» — только целым словом,
#: остальные — как начало слова («тестиров» → тестировщик, тестирование)
ROLE_FAMILIES: dict[str, tuple[str, ...]] = {
    "ai": (
        "ai",
        "ии",
        "ml",
        "llm",
        "gpt",
        "genai",
        "rag",
        "nlp",
        "mlops",
        "нейросет",
        "machine learning",
        "машинн",
        "deep learning",
        "prompt",
        "промпт",
        "вайбкод",
        "вайб-код",
        "vibe",
        "ai-",
        "computer vision",
        "data scien",
        "искусственн",
        "artificial intelligence",
    ),
    "python": ("python", "питон", "django", "fastapi", "flask"),
    "backend": ("backend", "back-end", "back end", "бэкенд", "бекенд", "бэк-енд", "серверн"),
    "frontend": (
        "frontend",
        "front-end",
        "front end",
        "фронтенд",
        "фронт-енд",
        "react",
        "vue",
        "angular",
        "верстальщик",
        "верстка",
        "вёрстка",
    ),
    "fullstack": ("fullstack", "full-stack", "full stack", "фулстек", "фуллстек"),
    "js": ("javascript", "typescript", "node.js", "nodejs", "node!"),
    "java": ("java!", "kotlin", "spring!", "scala!"),
    "dotnet": ("c#", ".net", "dotnet", "asp.net", "unity!"),
    "php": ("php", "laravel", "symfony", "bitrix", "битрикс", "wordpress"),
    "go": ("golang", "go"),
    "cpp": ("c++", "embedded", "встраиваем", "микроконтрол", "firmware", "rust!"),
    "1c": ("1с", "1c"),
    "mobile": ("mobile", "мобильн", "android", "ios", "flutter", "swift!", "react native"),
    "qa": (
        "qa",
        "aqa",
        "sdet",
        "тестиров",
        "tester",
        "testing",
        "test engineer",
        "quality assurance",
        "автотест",
    ),
    "devops": (
        "devops",
        "sre",
        "системный администратор",
        "sysadmin",
        "администратор linux",
        "инфраструктур",
        "kubernetes",
        "cloud engineer",
        "platform engineer",
    ),
    "data": (
        "data engineer",
        "data analyst",
        "аналитик данных",
        "bi",
        "dwh",
        "etl",
        "big data",
        "дата-инженер",
        "дата инженер",
    ),
    "analyst": ("analyst", "аналитик", "business analysis", "бизнес-анализ"),
    "pm": (
        "project manager",
        "product manager",
        "продакт",
        "проджект",
        "руководитель проект",
        "менеджер проект",
        "менеджер продукт",
        "scrum master",
        "delivery manager",
        "product owner",
    ),
    "design": ("designer", "дизайнер", "ux", "ui", "ui/ux", "ux/ui", "motion!", "иллюстратор"),
    "sales": (
        "sales",
        "продаж",
        "account manager",
        "аккаунт-менеджер",
        "business development",
        "менеджер по работе с клиентами",
        "менеджер по продажам",
        "presale",
        "пресейл",
        "развитию бизнеса",
        "развития бизнеса",
        "bizdev",
    ),
    "support": (
        "support",
        "поддержк",
        "оператор",
        "call-центр",
        "колл-центр",
        "helpdesk",
        "служба заботы",
    ),
    "hr": ("hr", "recruit", "рекрут", "кадров", "talent!"),
    "marketing": (
        "marketing",
        "маркетолог",
        "маркетинг",
        "smm",
        "seo",
        "контент",
        "copywriter",
        "копирайтер",
        "таргетолог",
        "ppc",
        "pr",
        "связям с общественност",
    ),
    "security": ("security", "безопасност", "pentest", "пентест", "soc", "appsec"),
    "gamedev": ("gamedev", "game developer", "unreal", "геймдизайн", "game designer"),
    "dev": (
        "developer",
        "разработчик",
        "программист",
        "engineer",
        "инженер-программист",
        "software",
        "coder",
        "кодер",
        "dev",
    ),
}

#: близкие роли: кто ищет AI-разработку, тому подходят и Python-вакансии, и наоборот
RELATED: dict[str, frozenset[str]] = {
    "ai": frozenset({"python", "dev"}),
    "python": frozenset({"ai", "backend"}),
    "fullstack": frozenset({"frontend", "backend", "js"}),
    "frontend": frozenset({"js", "fullstack"}),
    "js": frozenset({"frontend", "fullstack"}),
    "data": frozenset({"python"}),
    # «backend» и «mobile» сами по себе язык не задают: Java, Go, .NET подтягиваются
    # только из навыков человека (TECH), а не по умолчанию
}

#: семейства-профессии: если такая есть в названии, а человек её не ищет, это другая работа,
#: даже если рядом стоит «AI» или «Python» («Менеджер по продажам ИИ-решений»)
PROFESSIONS = frozenset(
    {"analyst", "pm", "design", "sales", "support", "hr", "marketing", "qa", "devops"}
)

#: семейства-технологии: если технология есть в навыках человека, вакансии по ней подходят
TECH = frozenset(
    {"python", "js", "java", "dotnet", "php", "go", "cpp", "1c", "frontend", "backend", "mobile"}
)

FAMILY_TITLES = {
    "analyst": "аналитик",
    "pm": "менеджер проектов",
    "design": "дизайн",
    "sales": "продажи",
    "support": "поддержка",
    "hr": "HR",
    "marketing": "маркетинг",
    "1c": "1С",
    "devops": "DevOps",
}


class Fit(StrEnum):
    MATCH = "match"  # роль явно совпадает
    UNKNOWN = "unknown"  # по названию не понять — решает ИИ
    MISMATCH = "mismatch"  # другая профессия


@lru_cache(maxsize=1)
def _patterns() -> dict[str, re.Pattern[str]]:
    out = {}
    for family, words in ROLE_FAMILIES.items():
        parts = []
        for w in words:
            whole = w.endswith("!") or len(w) <= 3
            esc = re.escape(w.rstrip("!").lower())
            parts.append(rf"(?<![\w+#.]){esc}(?![\w+#])" if whole else rf"(?<![\w]){esc}")
        out[family] = re.compile("|".join(parts))
    return out


def families(text: str) -> set[str]:
    text = text.lower().replace("ё", "е")
    return {name for name, pattern in _patterns().items() if pattern.search(text)}


def user_families(queries: Iterable[str], skills: Iterable[str] = ()) -> frozenset[str]:
    """Что ищет человек: семейства направлений + близкие роли + технологии из навыков."""
    found: set[str] = set()
    for q in queries:
        found |= families(q)
    if not found:
        return frozenset()
    for f in list(found):
        found |= RELATED.get(f, frozenset())
    for skill in skills:
        found |= families(skill) & TECH
    if {"frontend", "backend"} <= found:
        found.add("fullstack")
    return frozenset(found)


def role_fit(title: str, wanted: frozenset[str]) -> Fit:
    """Подходит ли роль вакансии (по названию) тому, что ищет человек."""
    if not wanted:
        return Fit.UNKNOWN
    found = families(title)
    if not found:
        return Fit.UNKNOWN
    specific = found - {"dev"}
    if (specific & PROFESSIONS) - wanted:
        return Fit.MISMATCH
    if specific & wanted:
        return Fit.MATCH
    if not specific:
        # просто «Разработчик» / «Software engineer» — годится тем, кто ищет разработку
        return Fit.UNKNOWN if "dev" in wanted else Fit.MISMATCH
    return Fit.MISMATCH


def mismatch_reason(title: str) -> str:
    all_found = families(title) - {"dev"}
    found = sorted(all_found & PROFESSIONS) or sorted(all_found)
    names = ", ".join(FAMILY_TITLES.get(f, f) for f in found[:2])
    return f"другая роль: {names}" if names else "другая роль"
