"""ИИ-анализ: профиль из резюме, оценка вакансий, сопроводительные письма.

Как экономятся токены:
1. В модель попадают только вакансии, прошедшие бесплатный отсев и локальный скоринг,
   и не больше `AI_MAX_PER_CYCLE` за цикл на пользователя.
2. Резюме один раз сжимается в короткий профиль; дальше используется только он.
3. Описание вакансии чистится от «воды» и обрезается (`compress.py`).
4. Ответ — короткий JSON с жёстким `max_tokens`.
5. Результат кэшируется по (вакансия, профиль): одна и та же вакансия для того же
   профиля второй раз не оценивается.
6. Дневной лимит токенов на пользователя.
7. Сопроводительное письмо — только по кнопке.
"""

from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass, field
from html import escape
from typing import Any

from jobspybot.ai.client import LLMClient, LLMError, parse_json
from jobspybot.ai.compress import compress_description
from jobspybot.config import AISettings
from jobspybot.db import Database, User
from jobspybot.filters import LEVEL_TITLES
from jobspybot.models import Vacancy

log = logging.getLogger(__name__)

PROFILE_SYSTEM = """Сожми резюме в профиль кандидата для автоматического подбора вакансий.
Ответь только JSON:
{"summary": "до 300 символов: роль, опыт в годах, главное", \
"level": "intern|junior|middle|senior|lead", \
"skills": ["до 20 технологий/навыков в нижнем регистре"], \
"languages": ["язык и уровень"], \
"wants": "до 150 символов: что ищет — формат, страна, зарплата; пусто, если не указано"}"""

EVAL_SYSTEM = """Ты оцениваешь, насколько вакансия подходит кандидату. Кандидат — гражданин \
Беларуси: если вакансия для него недоступна или рискованна (нужно гражданство/разрешение на \
работу без визовой поддержки, только резиденты страны, ограничения для граждан РБ), отметь это.
Ответь только JSON:
{"score": 0-100, "verdict": "до 100 символов", "pros": ["до 3 пунктов по 60 символов"], \
"cons": ["до 3 пунктов по 60 символов"], "by": "ok|risk|no", "by_note": "до 80 символов"}

Профиль кандидата:
"""

LETTER_SYSTEM = """Напиши сопроводительное письмо к вакансии от лица кандидата, на языке \
вакансии, до 900 символов. Конкретно: 2–3 факта из профиля, которые закрывают требования \
вакансии. Без шаблонных фраз и выдуманных фактов. Только текст письма.

Профиль кандидата:
"""


@dataclass(slots=True)
class Analysis:
    score: int
    verdict: str = ""
    pros: list[str] = field(default_factory=list)
    cons: list[str] = field(default_factory=list)
    by: str = "ok"
    by_note: str = ""

    @classmethod
    def from_data(cls, data: dict[str, Any]) -> Analysis:
        def strs(key: str, limit: int) -> list[str]:
            items = data.get(key) or []
            return [str(x)[:120] for x in items if x][:limit] if isinstance(items, list) else []

        try:
            score = int(float(data.get("score", 0)))
        except (TypeError, ValueError):
            score = 0
        by = str(data.get("by") or "ok").lower()
        return cls(
            score=max(0, min(100, score)),
            verdict=str(data.get("verdict") or "")[:200],
            pros=strs("pros", 3),
            cons=strs("cons", 3),
            by=by if by in ("ok", "risk", "no") else "ok",
            by_note=str(data.get("by_note") or "")[:160],
        )

    def render(self) -> str:
        lines = [f"🤖 <b>{self.score}/100</b> — {escape(self.verdict)}"]
        if self.pros:
            lines.append("➕ " + escape("; ".join(self.pros)))
        if self.cons:
            lines.append("➖ " + escape("; ".join(self.cons)))
        if self.by != "ok" or self.by_note:
            icon = {"ok": "🇧🇾", "risk": "⚠️", "no": "⛔"}[self.by]
            lines.append(f"{icon} {escape(self.by_note or 'есть ограничения для граждан РБ')}")
        return "\n".join(lines)


def profile_hash(profile: str) -> str:
    return hashlib.sha1(profile.encode()).hexdigest()[:12]


def vacancy_prompt(v: Vacancy, description: str) -> str:
    head = [
        f"Вакансия: {v.title}",
        f"Компания: {v.company or '—'}",
        f"Локация: {v.location or '—'}{' (удалённо)' if v.remote else ''}",
    ]
    if v.salary_text():
        head.append(f"Зарплата: {v.salary_text()}")
    if v.level:
        head.append(f"Уровень: {LEVEL_TITLES[v.level]}")
    if v.skills:
        head.append("Навыки: " + ", ".join(v.skills[:15]))
    return "\n".join(head) + ("\n\n" + description if description else "")


Describe = Callable[[Vacancy], Awaitable[str]]


class AIService:
    def __init__(self, db: Database, llm: LLMClient, settings: AISettings) -> None:
        self.db = db
        self.llm = llm
        self.settings = settings
        self.describe: Describe | None = None

    # --- бюджет --------------------------------------------------------------

    def has_budget(self, user: User) -> bool:
        return self.db.tokens_today(user.id) < self.settings.daily_tokens_per_user

    async def _ask(self, user: User, system: str, prompt: str, **kw: Any) -> str:
        reply = await self.llm.chat(system, prompt, **kw)
        self.db.add_tokens(user.id, reply.tokens)
        log.debug("llm user=%s tokens=%d", user.id, reply.tokens)
        return reply.text

    async def _description(self, vacancy: Vacancy) -> str:
        text = vacancy.description or ""
        if not text and self.describe:
            try:
                text = await self.describe(vacancy)
            except Exception as exc:
                log.info("description for %s failed: %r", vacancy.uid, exc)
        text = text or vacancy.snippet
        return compress_description(text, self.settings.max_description_chars)

    # --- профиль -------------------------------------------------------------

    async def build_profile(self, user: User, resume: str) -> str:
        """Хук для бота: вызывается после загрузки резюме, возвращает ответ пользователю."""
        if not self.has_budget(user):
            return "✅ Резюме сохранено, но дневной лимит ИИ исчерпан — профиль соберу завтра."
        try:
            data = parse_json(
                await self._ask(user, PROFILE_SYSTEM, resume, max_tokens=500, json_mode=True)
            )
        except LLMError as exc:
            log.warning("profile for %s failed: %s", user.id, exc)
            return "✅ Резюме сохранено, но ИИ сейчас недоступен. Попробуйте /resume позже."

        skills = [str(s).lower()[:40] for s in data.get("skills") or [] if s][:20]
        profile = json.dumps(
            {
                "summary": str(data.get("summary") or "")[:400],
                "level": str(data.get("level") or ""),
                "skills": skills,
                "languages": [str(x)[:40] for x in data.get("languages") or []][:5],
                "wants": str(data.get("wants") or "")[:200],
            },
            ensure_ascii=False,
        )
        self.db.save_resume(user.id, resume, profile)
        user.profile = profile
        if skills and not user.filters.skills:
            user.filters.skills = skills
            self.db.save_filters(user.id, user.filters)

        summary = escape(str(data.get("summary") or ""))
        return (
            "✅ Профиль готов:\n\n"
            f"<i>{summary}</i>\n\n"
            f"🧩 Навыки: {escape(', '.join(skills)) or '—'}\n"
            "Навыки используются для бесплатного отбора — поправить: /skills"
        )

    # --- оценка --------------------------------------------------------------

    async def evaluate(self, user: User, vacancy: Vacancy) -> Analysis | None:
        """Оценка вакансии или None (нет профиля, лимит, ошибка) — тогда шлём без оценки."""
        if not user.profile:
            return None
        key = profile_hash(user.profile)
        cached = self.db.get_ai_cache(vacancy.uid, key)
        if cached:
            return Analysis(**json.loads(cached))
        if not self.has_budget(user):
            return None
        description = await self._description(vacancy)
        try:
            text = await self._ask(
                user,
                EVAL_SYSTEM + user.profile,
                vacancy_prompt(vacancy, description),
                max_tokens=250,
                json_mode=True,
            )
            analysis = Analysis.from_data(parse_json(text))
        except LLMError as exc:
            log.warning("evaluate %s for %s failed: %s", vacancy.uid, user.id, exc)
            return None
        self.db.set_ai_cache(vacancy.uid, key, json.dumps(asdict(analysis), ensure_ascii=False))
        return analysis

    # --- письмо --------------------------------------------------------------

    async def cover_letter(self, user: User, vacancy: Vacancy) -> str:
        if not user.profile:
            return "Сначала загрузите резюме: /resume"
        if not self.has_budget(user):
            return "Дневной лимит ИИ исчерпан, попробуйте завтра."
        description = await self._description(vacancy)
        try:
            return await self._ask(
                user,
                LETTER_SYSTEM + user.profile,
                vacancy_prompt(vacancy, description),
                max_tokens=600,
                temperature=0.5,
            )
        except LLMError as exc:
            log.warning("letter for %s failed: %s", user.id, exc)
            return "ИИ сейчас недоступен, попробуйте позже."
