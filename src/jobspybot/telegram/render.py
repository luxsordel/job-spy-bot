"""Оформление сообщений (Telegram HTML)."""

from __future__ import annotations

from html import escape

from jobspybot.filters import COUNTRIES, LEVEL_TITLES, UserFilters
from jobspybot.models import Vacancy
from jobspybot.sources import REGISTRY


def vacancy_card(v: Vacancy, extra: str = "") -> str:
    lines = [f'<b><a href="{escape(v.url, quote=True)}">{escape(v.title)}</a></b>']
    where = " · ".join(
        filter(
            None,
            [
                f"🏢 {escape(v.company)}" if v.company else "",
                f"📍 {escape(v.location)}" if v.location else "",
                "🏠 удалённо" if v.remote else "",
            ],
        )
    )
    if where:
        lines.append(where)
    meta = " · ".join(
        filter(
            None,
            [
                f"💰 {escape(v.salary_text())}" if v.salary_text() else "",
                f"🎓 {LEVEL_TITLES[v.level]}" if v.level else "",
            ],
        )
    )
    if meta:
        lines.append(meta)
    source = REGISTRY[v.source].title if v.source in REGISTRY else v.source
    date = v.published_at.strftime("%d.%m") if v.published_at else ""
    lines.append(f"🔎 {escape(source)}{' · ' + date if date else ''}")
    if v.skills:
        lines.append("🧩 " + escape(", ".join(v.skills[:8])))
    if extra:
        lines.append("")
        lines.append(extra)
    return "\n".join(lines)


def _list(items: list[str], empty: str = "—") -> str:
    return escape(", ".join(items)) if items else empty


def settings_text(
    f: UserFilters, available_sources: list[str], *, paused: bool, resumes: int
) -> str:
    sources = f.sources or available_sources
    countries = ", ".join(COUNTRIES.get(c, c) for c in f.countries) or "любые"
    salary = f"от {f.min_salary_usd}$" if f.min_salary_usd else "не важна"
    if f.min_salary_usd and f.require_salary:
        salary += " (без зарплаты — скрывать)"
    return "\n".join(
        [
            "<b>Ваши настройки</b>" + (" ⏸ <i>на паузе</i>" if paused else ""),
            "",
            f"🔍 Запросы: {_list(f.queries, '<i>не заданы — /query</i>')}",
            f"✅ Слова в названии: {_list(f.include, 'любые')}",
            f"🚫 Стоп-слова: {_list(f.exclude)}",
            f"🎓 Уровень: {_list(f.levels, 'любой')}",
            f"🌍 Страны: {escape(countries)}",
            f"🏠 Только удалёнка: {'да' if f.remote_only else 'нет'}",
            f"💰 Зарплата: {salary}",
            f"🔎 Площадки: {_list([REGISTRY[s].title for s in sources if s in REGISTRY])}",
            f"🧩 Навыки: {_list(f.skills, '<i>не заданы — /skills</i>')}",
            f"📄 Резюме: {resumes or 'нет — пришлите PDF'}" + (" — /resumes" if resumes else ""),
            f"🤖 Порог ИИ: {f.ai_threshold}/100",
        ]
    )


HELP = """<b>job-spy-bot</b> — присылаю свежие вакансии с BY / RU / KZ / GE / PL площадок.

<b>Что искать</b>
/query <i>python developer; c++; тестировщик</i> — что искать, можно сразу несколько направлений
/include <i>python, backend</i> — хотя бы одно слово в названии
/exclude <i>senior, 1с, php</i> — стоп-слова
/level <i>junior middle</i> — уровни (intern junior middle senior lead)
/country <i>BY PL REMOTE</i> — страны (BY RU KZ GE PL REMOTE)
/remote — только удалёнка (вкл/выкл)
/salary <i>1500</i> — минимум в $ (0 — выключить)
<code>/salary 1500 strict</code> — ещё и скрывать вакансии без зарплаты
/sources — выбрать площадки
/skills <i>python, sql, docker</i> — навыки для отбора лучших вакансий

<b>ИИ</b>
Пришлите PDF / .txt — это новое резюме (до 10, под разные направления).
ИИ выберет лучшее под каждую вакансию.
/resume — резюме текстом, /resumes — список и удаление
/threshold <i>60</i> — присылать только вакансии с оценкой ИИ не ниже

<b>Прочее</b>
/settings — текущие настройки
/stats — статистика за неделю
/pause и /unpause — приостановить рассылку
Команда без аргумента показывает значение, «-» очищает: <code>/exclude -</code>"""
