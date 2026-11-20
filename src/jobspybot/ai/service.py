"""ИИ-анализ: профиль из резюме, оценка вакансий, сопроводительные письма.

Как экономятся токены:
1. В модель попадают только вакансии, прошедшие бесплатный отсев и локальный скоринг,
   и не больше `AI_MAX_PER_CYCLE` за цикл на пользователя.
2. Резюме один раз сжимается в короткий профиль; дальше используется только он.
3. Описание вакансии чистится от «воды» и обрезается (`compress.py`).
4. Ответ — короткий JSON с жёстким `max_tokens`.
5. Результат кэшируется по (вакансия, набор профилей): одна и та же вакансия второй раз
   не оценивается.
6. Дневной лимит токенов на пользователя.
7. Сопроводительное письмо — только по кнопке.
8. Несколько резюме сравниваются с вакансией одним запросом, а не отдельным на каждое.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import time
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass, field
from html import escape
from typing import Any

from jobspybot import feedback
from jobspybot.ai.client import LLMClient, LLMError, parse_json
from jobspybot.ai.compress import compress_description
from jobspybot.config import AISettings
from jobspybot.db import Database, Resume, User, utcnow
from jobspybot.filters import LEVEL_TITLES, local_score
from jobspybot.models import Vacancy
from jobspybot.resume_files import redact_contacts

log = logging.getLogger(__name__)

#: сколько резюме максимум отправлять в модель для одной вакансии
MAX_RESUMES_PER_EVAL = 3
#: резюме с таким сходством навыков — по сути одно и то же (две версии одного резюме)
DUPLICATE_SKILLS = 0.7


def distinct_resumes(resumes: list[Resume]) -> list[Resume]:
    """Без почти одинаковых резюме: каждое лишнее удваивает цену оценки, а нового не даёт.

    Из похожих остаётся самое новое; порядок остальных не меняется (стабильный кэш).
    """
    kept: list[tuple[Resume, set[str]]] = []
    for resume in reversed(resumes):
        skills = {s.lower() for s in resume.skills}
        duplicate = skills and any(
            len(skills & other) / len(skills | other) >= DUPLICATE_SKILLS
            for _, other in kept
            if other
        )
        if not duplicate:
            kept.append((resume, skills))
    ids = {r.id for r, _ in kept}
    return [r for r in resumes if r.id in ids]


PROFILE_SYSTEM = """Разбери резюме для автоматического подбора вакансий.
Ответь только JSON; чего нет в резюме — null или пустое значение, ничего не выдумывай:
{"summary": "до 300 символов: роль, опыт в годах, главное", \
"name": "имя кандидата", \
"headline": "роль одной строкой, напр. Junior Python backend-разработчик", \
"level": "intern|junior|middle|senior|lead", \
"experience_years": число лет практического опыта, \
"skills": ["до 20 технологий/навыков в нижнем регистре"], \
"languages": ["язык и уровень"], \
"education": ["каждое образование отдельной строкой: учебное заведение, специальность, \
год окончания или «не окончено»; курсы тоже"], \
"city": "город проживания", "country": "страна проживания, ISO-код из 2 букв", \
"citizenship": "гражданство, ISO-код из 2 букв, если указано", \
"work_formats": ["remote|hybrid|office — только если в резюме указано"], \
"relocation": true/false/null, "relocation_countries": ["ISO-коды"], \
"salary_usd": ожидания в $ в месяц числом или null, \
"directions": ["2–4 поисковых запроса для сайтов вакансий, напр. python developer"], \
"wants": "до 150 символов: что ищет; пусто, если не указано"}"""

EVAL_SYSTEM = """Ты оцениваешь, насколько вакансия подходит кандидату. У кандидата одно или \
несколько резюме: выбери то, с которым больше всего шансов получить оффер, и оцени по нему.
Учитывай гражданство и обстоятельства кандидата (строка «Кандидат»): если вакансия для него \
недоступна или рискованна (нужно гражданство/разрешение на работу без визовой поддержки, только \
резиденты страны, ограничения по гражданству, не тот формат работы), отметь это в "by".
После «ищет:» в строке «Кандидат» — роли, на которые он хочет откликаться. Если вакансия — \
другая профессия (например, бизнес-аналитик, продажи или поддержка для того, кто ищет \
разработку или тестирование), ставь score не выше 20, даже если в тексте встречаются те же \
слова (AI, Python).
После «понравились:» и «скрыл:» — недавние реакции кандидата на присланные вакансии: \
похожие на скрытые оценивай ниже, похожие на понравившиеся — выше.
Ответь только JSON:
{"best": номер резюме, "score": 0-100, "by": "ok|risk|no", "verdict": "до 90 символов", \
"pros": ["до 2 пунктов по 50 символов"], "cons": ["до 2 пунктов по 50 символов"], \
"by_note": "до 70 символов или пусто"}
Пиши коротко, без повторов.
"""

PROFILE_EDIT_SYSTEM = """Пользователь бота поиска работы пишет, что хочет изменить в поиске.
Верни только JSON с полями, которые нужно изменить (остальные не включай):
{"work_formats": ["remote|hybrid|office"], "only_home_city": true/false, \
"strict_format": true/false, "city": "город", \
"levels": ["полный новый список грейдов вакансий из intern junior middle senior lead"], \
"relocation": true/false, "relocation_countries": ["ISO-коды из BY RU KZ GE PL"], \
"salary_usd": число в $ в месяц, "experience_years": лет опыта числом, \
"wishes": "район, метро, график и прочее, что не влезло в поля", \
"directions_add": ["поисковые запросы"], "directions_remove": ["запросы"], \
"exclude_add": ["стоп-слова"], "unclear": "что непонятно, если просьба неясна"}
«Только гибрид» → work_formats ["hybrid"]. Район или метро → wishes и only_home_city: true.
«Ещё и мидл» → levels: текущие грейды + middle; «без стажировок» → levels без intern.
«Опыт год», «у меня 2 года опыта» → experience_years; грейды при этом не меняй.
Текущий профиль: """

LETTER_SYSTEM = """Напиши сопроводительное письмо к вакансии от лица кандидата, на языке \
вакансии, до 900 символов. Конкретно: 2–3 факта из профиля, которые закрывают требования \
вакансии. Без шаблонных фраз и выдуманных фактов. Только текст письма.
"""


@dataclass(slots=True)
class Analysis:
    score: int
    verdict: str = ""
    pros: list[str] = field(default_factory=list)
    cons: list[str] = field(default_factory=list)
    by: str = "ok"
    by_note: str = ""
    best: int = 1  # номер резюме в запросе (с 1)
    resume_id: int | None = None
    resume_title: str = ""  # заполняется, только если резюме несколько

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
        try:
            best = int(float(data.get("best", 1)))
        except (TypeError, ValueError):
            best = 1
        return cls(
            best=best,
            score=max(0, min(100, score)),
            verdict=str(data.get("verdict") or "")[:200],
            pros=strs("pros", 3),
            cons=strs("cons", 3),
            by=by if by in ("ok", "risk", "no") else "ok",
            by_note=str(data.get("by_note") or "")[:160],
        )

    def render(self) -> str:
        lines = [f"🤖 <b>{self.score}/100</b> — {escape(self.verdict)}"]
        if self.resume_title:
            lines.append(f"📄 Подавать: «{escape(self.resume_title)}»")
        if self.pros:
            lines.append("➕ " + escape("; ".join(self.pros)))
        if self.cons:
            lines.append("➖ " + escape("; ".join(self.cons)))
        if self.by != "ok" or self.by_note:
            icon = {"ok": "🇧🇾", "risk": "⚠️", "no": "⛔"}[self.by]
            lines.append(f"{icon} {escape(self.by_note or 'есть ограничения для граждан РБ')}")
        return "\n".join(lines)


_SALVAGE = {
    "score": re.compile(r'"score"\s*:\s*"?(\d{1,3})'),
    "best": re.compile(r'"best"\s*:\s*"?(\d{1,2})'),
    "by": re.compile(r'"by"\s*:\s*"(ok|risk|no)"'),
    "verdict": re.compile(r'"verdict"\s*:\s*"((?:[^"\\]|\\.)*)"'),
    "by_note": re.compile(r'"by_note"\s*:\s*"((?:[^"\\]|\\.)*)"'),
}


def salvage_json(text: str) -> dict[str, Any] | None:
    """Достаёт ключевые поля из ответа, обрезанного по max_tokens. Без балла — None."""
    found = {k: m.group(1) for k, rx in _SALVAGE.items() if (m := rx.search(text))}
    return found if "score" in found else None


def profiles_hash(resumes: list[Resume], context: str = "") -> str:
    raw = context + "\n" + "\n".join(f"{r.id}:{r.profile}" for r in resumes)
    return hashlib.sha1(raw.encode()).hexdigest()[:12]


def profiles_block(resumes: list[Resume]) -> str:
    return "\n".join(f"[{i}] «{r.title}»: {r.profile}" for i, r in enumerate(resumes, 1))


def vacancy_prompt(v: Vacancy, description: str) -> str:
    head = [
        f"Вакансия: {v.title}",
        f"Компания: {v.company or '—'}",
        f"Локация: {v.location or '—'}{' (удалённо)' if v.remote else ''}",
    ]
    if v.address:
        head.append(f"Адрес: {v.address}")
    if v.work_formats:
        head.append("Формат: " + ", ".join(v.work_formats))
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
        #: дневной лимит токенов для пользователя (зависит от тарифа)
        self.token_limit: Callable[[User], int] = lambda _user: settings.daily_tokens_per_user
        #: оповещение админу (ключ для дедупликации, текст) — подключается в app.py
        self.on_alert: Callable[[str, str], Any] | None = None
        #: кому отдаётся резерв общего бюджета (Pro) — подключается в app.py
        self.is_priority: Callable[[User], bool] = lambda _user: False
        self._failures = 0

    # --- бюджет --------------------------------------------------------------

    def has_budget(self, user: User) -> bool:
        if self.db.tokens_today(user.id) >= self.token_limit(user):
            return False
        return self.global_budget_allows(user)

    def global_budget_allows(self, user: User) -> bool:
        """Общий дневной бюджет бота; последние `pro_reserve_pct` % — только для Pro."""
        total = self.settings.daily_tokens_total
        if not total:
            return True
        spent = self.db.tokens_since(utcnow().date().isoformat())
        if spent >= total:
            self._budget_alert("исчерпан — ИИ выключен до полуночи UTC", spent, total)
            return False
        reserve_from = total * (100 - self.settings.pro_reserve_pct) // 100
        if spent >= reserve_from and not self.is_priority(user):
            self._budget_alert(
                f"израсходовано {100 - self.settings.pro_reserve_pct} % — остаток только для Pro",
                spent,
                total,
            )
            return False
        return True

    def _budget_alert(self, what: str, spent: int, total: int) -> None:
        if self.on_alert:
            # ключ с датой — не чаще раза в день на каждый порог
            key = f"ai-budget:{what[:10]}:{utcnow().date().isoformat()}"
            self.on_alert(
                key, f"🤖 Общий дневной бюджет ИИ {what} ({spent:,} из {total:,} токенов)"
            )

    def _provider_failed(self, exc: LLMError) -> None:
        """Подряд несколько ошибок или кончились деньги — сообщить админу."""
        self._failures += 1
        text = str(exc)
        no_money = any(
            w in text.lower() for w in ("http 402", "insufficient", "balance", "quota", "billing")
        )
        if self.on_alert and (no_money or self._failures >= 3):
            reason = "закончился баланс или квота" if no_money else "несколько ошибок подряд"
            self.on_alert("ai", f"🤖 ИИ-провайдер недоступен ({reason}):\n{text[:300]}")

    async def _ask(
        self, user: User, system: str, prompt: str, *, what: str = "запрос", **kw: Any
    ) -> str:
        started = time.monotonic()
        try:
            reply = await self.llm.chat(system, prompt, **kw)
        except LLMError as exc:
            if str(exc).startswith(("no JSON", "bad JSON", "JSON is not")):
                raise
            self._provider_failed(exc)
            fallback = self.settings.fallback_model
            if not fallback or fallback == self.llm.model:
                raise
            log.warning("model %s failed (%s), trying %s", self.llm.model, exc, fallback)
            reply = await self.llm.chat(system, prompt, model=fallback, **kw)
        self._failures = 0
        self.db.add_tokens(user.id, reply.tokens)
        log.info(
            "ИИ %s для %s: %d токенов за %.1f с, у него за сегодня %s из %s",
            what,
            user.id,
            reply.tokens,
            time.monotonic() - started,
            f"{self.db.tokens_today(user.id):,}".replace(",", " "),
            f"{self.token_limit(user):,}".replace(",", " "),
        )
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

    # --- резюме -------------------------------------------------------------

    def profiled(self, user: User) -> list[Resume]:
        return [r for r in self.db.get_resumes(user.id) if r.profile]

    @staticmethod
    def shortlist(resumes: list[Resume], vacancy: Vacancy) -> list[Resume]:
        """Бесплатный предотбор: в модель идут только самые близкие к вакансии резюме."""
        resumes = distinct_resumes(resumes)
        if len(resumes) <= MAX_RESUMES_PER_EVAL:
            return resumes
        ranked = sorted(resumes, key=lambda r: local_score(vacancy, r.skills), reverse=True)
        top = {r.id for r in ranked[:MAX_RESUMES_PER_EVAL]}
        return [r for r in resumes if r.id in top]  # стабильный порядок — стабильный кэш

    def is_ready(self, user: User) -> bool:
        """Есть по чему оценивать: разобранное резюме или хотя бы профиль и направления."""
        return bool(self.profiled(user)) or bool(user.filters.queries)

    def candidate_profiles(self, user: User) -> list[Resume]:
        """Разобранные резюме, а без них — профиль соискателя вместо резюме.

        Иначе человек со слабым или не разобранным резюме получал бы вакансии вообще без
        оценки ИИ — всё подряд, где встретилось слово из запроса.
        """
        resumes = self.profiled(user)
        if resumes:
            return resumes
        p = self.db.get_profile(user.id)
        data = {
            "summary": p.headline or "резюме нет или оно не разобрано",
            "level": ", ".join(p.target_levels()),
            "skills": (p.skills or user.filters.skills)[:20],
            "wants": p.wishes,
        }
        return [
            Resume(
                id=0,
                user_id=user.id,
                title="Профиль",
                text="",
                profile=json.dumps(data, ensure_ascii=False),
                file_id=None,
                file_name=None,
            )
        ]

    async def build_profile(self, user: User, resume: Resume) -> str:
        """Хук для бота: вызывается после загрузки резюме, возвращает ответ пользователю."""
        if not self.has_budget(user):
            return "✅ Резюме сохранено, но дневной лимит ИИ исчерпан — профиль соберу завтра."
        try:
            data = parse_json(
                await self._ask(
                    user,
                    PROFILE_SYSTEM,
                    redact_contacts(resume.text),
                    what="разбор резюме",
                    max_tokens=1000,
                    json_mode=True,
                )
            )
        except LLMError as exc:
            log.warning("profile for %s failed: %s", user.id, exc)
            return "✅ Резюме сохранено, но ИИ сейчас недоступен. Загрузите его ещё раз позже."

        skills = [str(s).lower()[:40] for s in data.get("skills") or [] if s][:20]
        profile = json.dumps(
            {
                "summary": str(data.get("summary") or "")[:400],
                "level": str(data.get("level") or ""),
                "skills": skills,
                "languages": [str(x)[:40] for x in data.get("languages") or []][:5],
                "education": [str(x)[:120] for x in data.get("education") or []][:4],
                "wants": str(data.get("wants") or "")[:200],
            },
            ensure_ascii=False,
        )
        self.db.set_resume_profile(resume.id, profile)
        resume.profile = profile
        # всё, что удалось понять о человеке, дополняет его профиль соискателя
        user_profile = self.db.get_profile(user.id)
        user_profile.merge_extracted(data)
        self.db.save_profile(user.id, user_profile)
        summary = escape(str(data.get("summary") or ""))
        return (
            f"✅ Резюме «{escape(resume.title)}» разобрано:\n\n"
            f"<i>{summary}</i>\n\n"
            f"🧩 Навыки: {escape(', '.join(skills)) or '—'}"
        )

    # --- оценка --------------------------------------------------------------

    async def evaluate(self, user: User, vacancy: Vacancy) -> Analysis | None:
        """Оценка вакансии и выбор лучшего резюме. None — нет профилей, лимит или ошибка."""
        resumes = self.shortlist(self.candidate_profiles(user), vacancy)
        context = self.db.get_profile(user.id).context_line()
        if user.filters.queries:
            context += "; ищет: " + ", ".join(user.filters.queries[:10])
        reactions = feedback.context(
            self.db.reaction_titles(user.id, "liked", feedback.CONTEXT_TITLES),
            self.db.reaction_titles(user.id, "disliked", feedback.CONTEXT_TITLES),
        )
        if reactions:
            context += "; " + reactions
        key = profiles_hash(resumes, context)
        cached = self.db.get_ai_cache(vacancy.uid, key)
        if cached:
            log.info("ИИ оценка «%s» для %s: из кэша", vacancy.title[:60], user.id)
            return Analysis(**json.loads(cached))
        if not self.has_budget(user):
            return None
        description = await self._description(vacancy)
        data: dict[str, Any] | None = None
        # вторая попытка — только если провайдер вернул обрывок без балла
        for attempt in (1, 2):
            try:
                text = await self._ask(
                    user,
                    f"{EVAL_SYSTEM}\nКандидат: {context}\n\nРезюме кандидата:\n"
                    + profiles_block(resumes),
                    vacancy_prompt(vacancy, description),
                    what=f"оценка «{vacancy.title[:60]}»",
                    # с запасом: оплачиваются только реально сгенерированные токены,
                    # а обрезанный посередине JSON — это выброшенный запрос
                    max_tokens=450,
                    json_mode=True,
                )
            except LLMError as exc:
                log.warning("evaluate %s for %s failed: %s", vacancy.uid, user.id, exc)
                return None
            try:
                data = parse_json(text)
            except LLMError:
                data = salvage_json(text)
            if data is not None:
                break
            log.info(
                "evaluate %s: unusable reply (attempt %d): %r", vacancy.uid, attempt, text[:80]
            )
        if data is None:
            return None
        analysis = Analysis.from_data(data)
        best = resumes[analysis.best - 1] if 1 <= analysis.best <= len(resumes) else resumes[0]
        analysis.resume_id = best.id or None
        if len(resumes) > 1:
            analysis.resume_title = best.title
        self.db.set_ai_cache(vacancy.uid, key, json.dumps(asdict(analysis), ensure_ascii=False))
        return analysis

    async def best_resume(self, user: User, vacancy: Vacancy) -> Resume | None:
        """Резюме для отклика: выбранное ИИ (из кэша оценки) или единственное."""
        resumes = self.db.get_resumes(user.id)
        if len(resumes) <= 1:
            return resumes[0] if resumes else None
        analysis = await self.evaluate(user, vacancy)
        chosen = analysis and next((r for r in resumes if r.id == analysis.resume_id), None)
        return chosen or resumes[0]

    # --- правка профиля словами ---------------------------------------------

    async def parse_profile_edit(self, user: User, text: str) -> dict[str, Any] | None:
        """Фраза пользователя → правка профиля (JSON). None — лимит или ошибка ИИ."""
        if not self.has_budget(user):
            return None
        profile = self.db.get_profile(user.id)
        current = json.dumps(
            {
                "city": profile.city,
                "work_formats": profile.work_formats,
                "only_home_city": profile.only_home_city,
                "levels": profile.target_levels(),
                "relocation": profile.relocation,
                "salary_usd": profile.salary_usd,
                "experience_years": profile.experience_years,
                "wishes": profile.wishes,
                "directions": user.filters.queries,
            },
            ensure_ascii=False,
        )
        try:
            reply = await self._ask(
                user,
                PROFILE_EDIT_SYSTEM + current,
                text[:500],
                what="правка профиля",
                max_tokens=300,
                json_mode=True,
            )
            return parse_json(reply)
        except LLMError as exc:
            log.warning("profile edit for %s failed: %s", user.id, exc)
            return None

    # --- письмо --------------------------------------------------------------

    async def cover_letter(self, user: User, vacancy: Vacancy, resume: Resume) -> str:
        if not self.has_budget(user):
            return "Дневной лимит ИИ исчерпан, попробуйте завтра."
        description = await self._description(vacancy)
        try:
            return await self._ask(
                user,
                f"{LETTER_SYSTEM}\nКандидат: {self.db.get_profile(user.id).context_line()}\n"
                f"Профиль кандидата:\n{resume.profile or redact_contacts(resume.text[:3000])}",
                vacancy_prompt(vacancy, description),
                what="сопроводительное письмо",
                max_tokens=600,
                temperature=0.5,
            )
        except LLMError as exc:
            log.warning("letter for %s failed: %s", user.id, exc)
            return "ИИ сейчас недоступен, попробуйте позже."
