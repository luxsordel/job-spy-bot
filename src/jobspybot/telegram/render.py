"""Тексты и экраны бота (Telegram HTML)."""

from __future__ import annotations

from datetime import datetime
from html import escape

from jobspybot.billing import FREE, PRO, Plan
from jobspybot.filters import COUNTRIES, LEVEL_TITLES, UserFilters
from jobspybot.models import Level, Vacancy
from jobspybot.sources import REGISTRY

BRAND = "Job Spy"

# --- главное меню (кнопки под полем ввода) -----------------------------------

BTN_SEARCH = "🔎 Мой поиск"
BTN_RESUMES = "📄 Резюме"
BTN_FILTERS = "⚙️ Фильтры"
BTN_PLAN = "💎 Подписка"
BTN_STATS = "📊 Статистика"
BTN_HELP = "❓ Помощь"
MENU_ROWS = [[BTN_SEARCH, BTN_RESUMES], [BTN_FILTERS, BTN_PLAN], [BTN_STATS, BTN_HELP]]

# --- готовые направления для быстрого выбора ---------------------------------

#: ключ → (кнопка, поисковый запрос)
DIRECTIONS: dict[str, tuple[str, str]] = {
    "py": ("🐍 Python", "python developer"),
    "java": ("☕ Java", "java developer"),
    "fe": ("🌐 Frontend / JS", "frontend developer"),
    "node": ("🟩 Node.js", "node.js developer"),
    "cpp": ("⚙️ C / C++", "c++ developer"),
    "cs": ("🟦 C# / .NET", "c# developer"),
    "go": ("🐹 Go", "golang developer"),
    "php": ("🐘 PHP", "php developer"),
    "qa": ("🧪 QA / тестирование", "qa engineer"),
    "ops": ("🛠 DevOps", "devops engineer"),
    "data": ("📊 Data / ML", "data scientist"),
    "mob": ("📱 Mobile", "mobile developer"),
    "ai": ("🤖 AI / вайбкодинг", "ai developer"),
    "an": ("🔍 Аналитик", "системный аналитик"),
    "pm": ("📋 Project / Product", "project manager"),
    "ux": ("🎨 UI/UX дизайн", "ui ux designer"),
}
LEVEL_BUTTONS = {
    Level.INTERN.value: "🌱 Стажёр",
    Level.JUNIOR.value: "🐣 Junior",
    Level.MIDDLE.value: "💼 Middle",
    Level.SENIOR.value: "🚀 Senior",
    Level.LEAD.value: "👑 Lead",
}


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


def date_ru(dt: datetime) -> str:
    return dt.strftime("%d.%m.%Y")


# --- экраны ------------------------------------------------------------------


def welcome(name: str, trial_days: int) -> str:
    hello = f"Привет, {escape(name)}! 👋" if name else "Привет! 👋"
    trial = (
        f"\n\n🎁 Вам открыт <b>Pro на {trial_days} дней</b> бесплатно — "
        "все функции без ограничений."
        if trial_days
        else ""
    )
    return (
        f"{hello}\n\n"
        f"Я <b>{BRAND}</b> — ищу работу вместо вас.\n\n"
        "🔎 Слежу за <b>10 площадками</b>: Беларусь, Россия, Казахстан, Грузия, Польша и удалёнка\n"
        "🤖 ИИ читает каждую вакансию и оценивает шанс на оффер под <b>ваше резюме</b>\n"
        "🇧🇾 Предупреждаю, если вакансия недоступна гражданам РБ\n"
        "📄 Выбираю лучшее из ваших резюме и пишу сопроводительное письмо"
        f"{trial}\n\n"
        "Настроим поиск за 1 минуту 👇"
    )


def onboarding_step(step: int, total: int, title: str, hint: str) -> str:
    bar = "●" * step + "○" * (total - step)
    return f"<b>Шаг {step} из {total}</b>  {bar}\n\n<b>{title}</b>\n{hint}"


def search_summary(
    f: UserFilters,
    available_sources: list[str],
    *,
    paused: bool,
    resumes: int,
    plan: Plan,
    check_every_min: int,
) -> str:
    sources = f.sources or available_sources
    countries = ", ".join(COUNTRIES.get(c, c) for c in f.countries) or "любые"
    levels = ", ".join(LEVEL_BUTTONS.get(lv, lv) for lv in f.levels) or "любой"
    salary = f"от {f.min_salary_usd}$" if f.min_salary_usd else "не важна"
    if f.min_salary_usd and f.require_salary:
        salary += " (без зарплаты — скрывать)"
    queries = f.queries
    over = len(queries) - plan.max_queries
    q_text = _list(queries[: plan.max_queries], "<i>не заданы</i>")
    if over > 0:
        q_text += f" <i>(+{over} — в Pro)</i>"
    every = f"{check_every_min // 60} ч" if check_every_min >= 60 else f"{check_every_min} мин"
    status = "⏸ <b>на паузе</b>" if paused else f"▶️ активен, проверка каждые {every}"
    return "\n".join(
        [
            f"<b>🔎 Мой поиск</b> — {status}",
            "",
            f"🔍 Направления: {q_text}",
            f"🎓 Уровень: {escape(levels)}",
            f"🌍 Страны: {escape(countries)}",
            f"🏠 Только удалёнка: {'да' if f.remote_only else 'нет'}",
            f"💰 Зарплата: {salary}",
            f"🚫 Стоп-слова: {_list(f.exclude)}",
            f"🔎 Площадки: {len([s for s in sources if s in REGISTRY])} "
            f"из {len(available_sources)}",
            f"📄 Резюме: {resumes or 'нет'}",
            f"🤖 Порог ИИ: {f.ai_threshold}/100",
        ]
    )


# совместимость со старой командой /settings
def settings_text(
    f: UserFilters, available_sources: list[str], *, paused: bool, resumes: int
) -> str:
    return search_summary(
        f, available_sources, paused=paused, resumes=resumes, plan=PRO, check_every_min=30
    )


def _fmt_limit(value: int | None, unit: str = "") -> str:
    return "∞" if value is None else f"{value}{unit}"


def plan_screen(
    plan: Plan,
    *,
    until: datetime | None,
    trial: bool,
    subscribed: bool,
    price_stars: int,
    support: str,
) -> str:
    if plan is PRO and until:
        kind = " (пробный)" if trial else ""
        renew = "продлится автоматически" if subscribed else "без автопродления"
        head = f"💎 Ваш тариф: <b>Pro{kind}</b> до {date_ru(until)} — {renew}"
    elif plan is PRO:
        head = "💎 Ваш тариф: <b>Pro</b> (администратор)"
    else:
        head = "🆓 Ваш тариф: <b>Free</b>"

    def every(p: Plan) -> str:
        m = p.check_every_min
        return f"{m // 60} ч" if m >= 60 else f"{m} мин"

    rows = [
        ("", "Free", "Pro"),
        ("Направлений", str(FREE.max_queries), str(PRO.max_queries)),
        ("Резюме", str(FREE.max_resumes), str(PRO.max_resumes)),
        ("Вакансий в день", _fmt_limit(FREE.daily_vacancies), _fmt_limit(PRO.daily_vacancies)),
        ("ИИ-оценка", "≈3 в день", "все"),
        ("Отклик + письмо", "—", "✓"),
        ("Проверка", f"раз в {every(FREE)}", f"раз в {every(PRO)}"),
    ]
    table = "\n".join(f"{a:<16}{b:<11}{c}" for a, b, c in rows)
    lines = [head, "", f"<pre>{escape(table)}</pre>"]
    if price_stars and not subscribed:
        lines += ["", f"Pro — <b>{price_stars} ⭐ в месяц</b>, оплата звёздами Telegram в 2 клика."]
        lines.append("Отменить можно в любой момент — Pro доработает до конца оплаченного месяца.")
    if support:
        lines += ["", f"Вопросы и предложения: {escape(support)}"]
    return "\n".join(lines)


HELP = f"""<b>{BRAND}</b> — ваш помощник в поиске работы.

<b>Как это работает</b>
1. Я каждые полчаса проверяю 10 площадок по вашим направлениям.
2. Бесплатно отсеиваю неподходящее: уровень, страна, стоп-слова, навыки.
3. ИИ читает оставшиеся вакансии и ставит балл под ваше резюме.
4. Присылаю только то, где есть шанс. Кнопка <b>📄 Отклик</b> — лучшее резюме и письмо.

<b>Меню</b>
{BTN_SEARCH} — что и где ищем
{BTN_RESUMES} — загрузить PDF, список резюме
{BTN_FILTERS} — направления, уровень, страны, зарплата…
{BTN_PLAN} — тарифы и оплата
{BTN_STATS} — что нашлось за неделю

<b>Подсказки</b>
• Резюме — просто пришлите PDF в чат. Можно несколько под разные роли.
• 👎 под вакансией — скрыть её.
• Свои направления: <code>/query python developer; тестировщик</code>
• Стоп-слова: <code>/exclude senior, 1с</code>"""
