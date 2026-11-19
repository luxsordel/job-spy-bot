"""Профиль соискателя: заполняется ИИ из резюме, недостающее — ответами на вопросы.

Профиль — источник правды для анкеты поиска: из него выводятся направления,
уровни, страны, удалёнка и зарплата (`apply_to_filters`). Его же видит ИИ при
оценке вакансий и написании писем (`context_line`).
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field, fields
from typing import Any

from jobspybot.filters import COUNTRIES, UserFilters
from jobspybot.models import Level

WORK_FORMATS = {"remote": "🏠 Удалёнка", "hybrid": "🔀 Гибрид", "office": "🏢 Офис"}
#: страны для релокации (без «удалёнки из любой страны»)
RELOCATION_COUNTRIES = {k: v for k, v in COUNTRIES.items() if k != "REMOTE"}
SALARY_PRESETS = (500, 800, 1200, 1500, 2000, 3000)

#: уровень кандидата → уровни вакансий, которые имеет смысл показывать
LEVEL_RANGE = {
    Level.INTERN.value: [Level.INTERN.value, Level.JUNIOR.value],
    Level.JUNIOR.value: [Level.INTERN.value, Level.JUNIOR.value],
    Level.MIDDLE.value: [Level.JUNIOR.value, Level.MIDDLE.value],
    Level.SENIOR.value: [Level.MIDDLE.value, Level.SENIOR.value],
    Level.LEAD.value: [Level.SENIOR.value, Level.LEAD.value],
}

#: порядок вопросов анкеты
QUESTIONS = ("directions", "level", "work_formats", "relocation", "salary")


@dataclass(slots=True)
class UserProfile:
    name: str = ""
    headline: str = ""  # роль одной строкой
    level: str = ""
    experience_years: float | None = None
    skills: list[str] = field(default_factory=list)
    languages: list[str] = field(default_factory=list)
    city: str = ""
    country: str = ""  # где живёт, ISO alpha-2
    citizenship: str = "BY"
    work_formats: list[str] = field(default_factory=list)
    relocation: bool | None = None
    relocation_countries: list[str] = field(default_factory=list)
    salary_usd: int | None = None  # 0 — не важно, None — неизвестно
    directions: list[str] = field(default_factory=list)  # поисковые запросы из резюме
    answered: list[str] = field(default_factory=list)  # вопросы, на которые ответил человек

    def to_json(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False)

    @classmethod
    def from_json(cls, raw: str | None) -> UserProfile:
        if not raw:
            return cls()
        data = json.loads(raw)
        known = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in data.items() if k in known})

    @property
    def is_empty(self) -> bool:
        return not (self.headline or self.skills or self.level)

    def mark(self, question: str) -> None:
        if question not in self.answered:
            self.answered.append(question)

    # --- слияние с данными из резюме -----------------------------------------

    def merge_extracted(self, data: dict[str, Any]) -> list[str]:
        """Заполняет пустые поля данными ИИ. Ответы человека не перезаписываются."""
        changed: list[str] = []

        def text(key: str, limit: int) -> str:
            value = data.get(key)
            return str(value).strip()[:limit] if value else ""

        def str_list(key: str, limit: int, item_len: int = 40) -> list[str]:
            items = data.get(key) or []
            if not isinstance(items, list):
                return []
            return [str(x).strip()[:item_len] for x in items if str(x).strip()][:limit]

        for attr, key, limit in (
            ("name", "name", 60),
            ("headline", "headline", 80),
            ("city", "city", 40),
        ):
            if not getattr(self, attr) and (value := text(key, limit)):
                setattr(self, attr, value)
                changed.append(attr)

        country = text("country", 2).upper()
        if not self.country and country in RELOCATION_COUNTRIES:
            self.country = country
            changed.append("country")
        citizenship = text("citizenship", 2).upper()
        if citizenship and len(citizenship) == 2 and "citizenship" not in self.answered:
            self.citizenship = citizenship

        level = text("level", 10).lower()
        if "level" not in self.answered and level in LEVEL_RANGE and not self.level:
            self.level = level
            changed.append("level")

        try:
            years = float(data.get("experience_years"))  # type: ignore[arg-type]
        except (TypeError, ValueError):
            years = None
        if self.experience_years is None and years is not None and 0 <= years < 60:
            self.experience_years = round(years, 1)
            changed.append("experience_years")

        for attr, key, limit in (("skills", "skills", 40), ("languages", "languages", 8)):
            current = getattr(self, attr)
            known = {s.lower() for s in current}
            added = [s for s in str_list(key, limit) if s.lower() not in known]
            if added:
                setattr(self, attr, [*current, *added][:limit])
                changed.append(attr)

        formats = [f for f in str_list("work_formats", 3) if f in WORK_FORMATS]
        if formats and not self.work_formats and "work_formats" not in self.answered:
            self.work_formats = formats
            changed.append("work_formats")

        relocation = data.get("relocation")
        if isinstance(relocation, bool) and "relocation" not in self.answered:
            if self.relocation is None:
                self.relocation = relocation
                changed.append("relocation")
            countries = [
                c.upper() for c in str_list("relocation_countries", 6) if c.upper() in COUNTRIES
            ]
            if relocation and countries and not self.relocation_countries:
                self.relocation_countries = countries

        try:
            salary = int(float(data.get("salary_usd")))  # type: ignore[arg-type]
        except (TypeError, ValueError):
            salary = None
        if salary and self.salary_usd is None and "salary" not in self.answered:
            self.salary_usd = max(0, salary)
            changed.append("salary_usd")

        directions = str_list("directions", 4, item_len=50)
        known = {d.lower() for d in self.directions}
        added = [d for d in directions if d.lower() not in known]
        if added:
            self.directions = [*self.directions, *added][:6]
            changed.append("directions")
        return changed

    # --- анкета ---------------------------------------------------------------

    def missing(self, filters: UserFilters) -> list[str]:
        """Вопросы, которые ещё нужно задать (в порядке анкеты)."""
        unknown = {
            "directions": not (filters.queries or self.directions),
            "level": not self.level,
            "work_formats": not self.work_formats,
            "relocation": self.relocation is None,
            "salary": self.salary_usd is None,
        }
        return [q for q in QUESTIONS if unknown[q] and q not in self.answered]

    # --- вывод ----------------------------------------------------------------

    def context_line(self) -> str:
        """Короткая строка для ИИ: обстоятельства кандидата, которых нет в резюме."""
        parts = [f"гражданство {self.citizenship or 'BY'}"]
        if self.city or self.country:
            parts.append(f"живёт: {', '.join(filter(None, [self.city, self.country]))}")
        if self.work_formats:
            parts.append("формат: " + ", ".join(self.work_formats))
        if self.relocation is True:
            where = ", ".join(self.relocation_countries) or "любая страна"
            parts.append(f"готов к релокации ({where})")
        elif self.relocation is False:
            parts.append("без релокации")
        if self.salary_usd:
            parts.append(f"ожидания от {self.salary_usd}$/мес")
        return "; ".join(parts)


def apply_to_filters(p: UserProfile, f: UserFilters, *, max_queries: int) -> None:
    """Выводит анкету поиска из профиля."""
    if p.directions and not f.queries:
        f.queries = p.directions[:max_queries]
    if p.level in LEVEL_RANGE:
        f.levels = list(LEVEL_RANGE[p.level])

    countries: list[str] = []
    if p.work_formats:
        if {"office", "hybrid"} & set(p.work_formats):
            home = p.country or p.citizenship
            if home in COUNTRIES:
                countries.append(home)
        if p.relocation:
            countries += [c for c in p.relocation_countries if c not in countries]
        if "remote" in p.work_formats:
            countries.append("REMOTE")
        f.countries = countries
        f.remote_only = p.work_formats == ["remote"]
    if p.salary_usd is not None:
        # чуть ниже ожиданий: вилки часто занижены, а торг возможен
        f.min_salary_usd = int(p.salary_usd * 0.8)
