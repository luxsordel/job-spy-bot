"""Тексты и экраны бота (Telegram HTML)."""

from __future__ import annotations

from datetime import datetime
from html import escape

from jobspybot.billing import FREE, PRO, Plan
from jobspybot.filters import COUNTRIES, LEVEL_TITLES, UserFilters
from jobspybot.market import Range, country_title
from jobspybot.models import Level, Vacancy
from jobspybot.profile import RELOCATION_COUNTRIES, WORK_FORMATS, UserProfile
from jobspybot.sources import REGISTRY

BRAND = "Job Spy"

# --- главное меню (кнопки под полем ввода) -----------------------------------

BTN_SEARCH = "🔎 Мой поиск"
BTN_RESUMES = "📄 Резюме"
BTN_FILTERS = "⚙️ Фильтры"
BTN_PLAN = "💎 Подписка"
BTN_STATS = "📊 Статистика"
BTN_HELP = "❓ Помощь"
BTN_PROFILE = "👤 Профиль"
BTN_APPS = "📋 Отклики"
BTN_MARKET = "📈 Рынок"
MENU_ROWS = [
    [BTN_SEARCH, BTN_PROFILE],
    [BTN_RESUMES, BTN_FILTERS, BTN_MARKET],
    [BTN_APPS, BTN_PLAN, BTN_HELP],
]

#: статусы отклика: (значок, подпись)
APP_STATUS = {
    "applied": ("✉️", "Отклик отправлен"),
    "test": ("📝", "Тестовое"),
    "interview": ("🗣", "Интервью"),
    "offer": ("🎉", "Оффер"),
    "rejected": ("❌", "Отказ"),
}
APPS_EMPTY = (
    "<b>📋 Мои отклики</b>\n\nПока пусто. Откликнулись на вакансию — нажмите под ней "
    "<b>✅ Откликнулся</b>: я запишу её сюда, буду вести статус (тестовое, интервью, оффер) "
    "и напомню, если работодатель молчит неделю."
)

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


FORMAT_BADGES = {"remote": "🏠 удалённо", "hybrid": "🔀 гибрид", "office": "🏢 офис"}


def vacancy_card(v: Vacancy, extra: str = "") -> str:
    lines = [f'<b><a href="{escape(v.url, quote=True)}">{escape(v.title)}</a></b>']
    where = " · ".join(
        filter(
            None,
            [
                f"🏢 {escape(v.company)}" if v.company else "",
                f"📍 {escape(v.location)}" if v.location else "",
                *[FORMAT_BADGES[f] for f in v.work_formats if f in FORMAT_BADGES],
                "🏠 удалённо" if v.remote and not v.work_formats else "",
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
    if v.address:
        lines.append(f"🗺 {escape(v.address)}")
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
        "Настроим поиск за 1 минуту 👇\n\n"
        "<i>Продолжая, вы соглашаетесь с /privacy — что я храню и как удалить данные.</i>"
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


def _years(value: float) -> str:
    n = int(value) if float(value).is_integer() else value
    if isinstance(n, float):
        return f"{n} года"
    if n % 10 == 1 and n % 100 != 11:
        return f"{n} год"
    if n % 10 in (2, 3, 4) and n % 100 not in (12, 13, 14):
        return f"{n} года"
    return f"{n} лет"


def profile_card(
    p: UserProfile, queries: list[str], *, title: str = "👤 Профиль", summary: str = ""
) -> str:
    unknown = "<i>не указано</i>"
    head = " · ".join(escape(x) for x in (p.name, p.headline) if x)
    level = ", ".join(LEVEL_BUTTONS[lv] for lv in p.target_levels() if lv in LEVEL_BUTTONS)
    if not level and "levels" in p.answered:
        level = "любой"
    exp = f"опыт {_years(p.experience_years)}" if p.experience_years else ""
    level_line = " · ".join(x for x in (level, exp) if x) or unknown
    place = ", ".join(x for x in (p.city, p.country) if x)
    formats = ", ".join(WORK_FORMATS[f] for f in p.work_formats if f in WORK_FORMATS)
    if p.relocation is True:
        where = ", ".join(RELOCATION_COUNTRIES.get(c, c) for c in p.relocation_countries)
        reloc = "да" + (f" — {where}" if where else "")
    elif p.relocation is False:
        reloc = "нет"
    else:
        reloc = unknown
    if p.salary_usd is None:
        salary = unknown
    elif p.salary_usd == 0:
        salary = "не важно"
    else:
        salary = f"от {p.salary_usd}$ в месяц"
    lines = [f"<b>{title}</b>"]
    if head:
        lines.append(head)
    if summary:
        lines.append(f"<i>{escape(summary)}</i>")
    # блоки через пустую строку: кто я → что умею → где и как работать → чего хочу
    lines += ["", f"📊 Уровень: {level_line}"]
    if p.education:
        lines.append("🎓 Образование:")
        lines += [f"    • {escape(e)}" for e in p.education]
    lines += [
        "",
        f"🧩 Навыки: {_list(p.skills[:15], unknown)}",
        f"🗣 Языки: {_list(p.languages, unknown)}",
        "",
        f"📍 Живу: {escape(place) if place else unknown} · гражданство {escape(p.citizenship)}",
        f"🏢 Формат: {escape(formats) if formats else unknown}"
        + (f" · только г. {escape(p.city)}" if p.only_home_city and p.city else ""),
        f"✈️ Релокация: {reloc}",
        "",
        f"💰 Ожидания: {salary}",
        f"🔍 Ищу: {_list(queries or p.directions, unknown)}",
    ]
    if p.wishes:
        lines.append(f"📝 Пожелания: {escape(p.wishes)}")
    return "\n".join(lines)


QUESTION_TEXTS = {
    "directions": ("Что ищем?", "Выберите направления или нажмите «✍️ Свой вариант»."),
    "level": (
        "Вакансии какого уровня показывать?",
        "Можно несколько — например, Junior и Middle. Затем «Далее».",
    ),
    "work_formats": ("Какой формат работы подходит?", "Можно несколько."),
    "relocation": ("Готовы к релокации?", "Если да — подскажу вакансии с переездом."),
    "relocation_countries": ("Куда готовы переехать?", "Можно несколько."),
    "salary": ("Сколько хотите получать?", "В $ в месяц «на руки». Ниже — не покажу."),
}


def question(key: str, step: int | None = None, total: int | None = None) -> str:
    title, hint = QUESTION_TEXTS[key]
    if step and total:
        return onboarding_step(step, total, title, hint)
    return f"<b>{title}</b>\n{hint}"


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
{BTN_SEARCH} — что и где ищем, статистика
{BTN_PROFILE} — ваш профиль: заполняется из резюме, правится кнопками
{BTN_RESUMES} — загрузить PDF, список резюме
{BTN_FILTERS} — направления, уровень, страны, зарплата…
{BTN_APPS} — куда откликнулись и на каком этапе
{BTN_MARKET} — сколько платят по вашим направлениям
{BTN_PLAN} — тарифы и оплата

<b>Подсказки</b>
• Резюме — просто пришлите PDF в чат: я сам заполню профиль и настрою поиск.
  Можно несколько резюме под разные роли.
• 👎 под вакансией — скрыть её, ✅ Откликнулся — записать в отклики.
• Свои направления: <code>/query python developer; тестировщик</code>
• Стоп-слова: <code>/exclude senior, 1с</code>

<b>Ваши данные</b>
/privacy — что я храню и кому передаю
/delete_me — удалить резюме, профиль и историю"""


def privacy_text(inactive_days: int, support: str = "") -> str:
    """Политика конфиденциальности простыми словами."""
    lines = [
        "<b>🔐 Ваши данные</b>",
        "",
        "<b>Что храню</b>",
        "• Telegram id и имя пользователя — чтобы присылать вакансии",
        "• Резюме (текст и ссылку на файл в Telegram), профиль, фильтры",
        "• Историю: какие вакансии прислал или отсеял — 30 дней",
        "• Расход ИИ по дням и платежи Telegram Stars",
        "",
        "<b>Зачем</b> — только чтобы подбирать вакансии и писать отклики. "
        "Не продаю и не передаю данные для рекламы.",
        "",
        "<b>Кому передаю</b>",
        "• ИИ-провайдеру — текст резюме <b>без телефонов и email</b> и описания вакансий, "
        "для оценки и писем",
        "• Telegram — как любой бот",
        "",
        "<b>Сколько храню</b>",
        "• Пока вы пользуетесь ботом",
        (
            f"• Если не заходите {inactive_days} дней и нет активной подписки — "
            "удаляю резюме, профиль и историю автоматически"
            if inactive_days
            else "• До вашей просьбы об удалении"
        ),
        "• После удаления остаются только id, срок подписки и платежи — "
        "для возвратов и чтобы пробный период нельзя было брать заново",
        "",
        "Удалить всё сейчас — кнопка ниже или /delete_me.",
    ]
    if support:
        lines += ["", f"Вопросы: {escape(support)}"]
    return "\n".join(lines)


# --- рынок зарплат ---------------------------------------------------------------

#: сколько направлений и стран показывать — чтобы сообщение не превращалось в простыню
MARKET_QUERIES = 3
MARKET_PLACES = 3
MARKET_EMPTY = (
    "<b>📈 Рынок зарплат</b>\n\nСначала выберите направления поиска — "
    "покажу вилки по ним. Это делается в ⚙️ Фильтры → 🔍 Направления."
)


def _money(usd: int) -> str:
    return f"{usd:,}".replace(",", " ")


def market_text(
    blocks: list[tuple[str, list[tuple[str, dict[str, Range]]]]], built_at: datetime
) -> str:
    """«python developer → 🇧🇾 Беларусь → junior: 800–1 400 $ · медиана 1 100 $ (23)»."""
    lines = [
        "<b>📈 Рынок зарплат</b>",
        "<i>По вилкам из вакансий за 30 дней, $ в месяц: середина рынка и медиана.</i>",
    ]
    for query, places in blocks:
        lines += ["", f"<b>{escape(query)}</b>"]
        for country, ranges in places:
            lines.append(country_title(country))
            if not ranges:
                lines.append("   мало вакансий с вилкой")
            for level, rng in ranges.items():
                name = "все грейды" if level == "all" else LEVEL_TITLES[Level(level)]
                lines.append(
                    f"   {name}: <b>{_money(rng.low)}–{_money(rng.high)} $</b>"
                    f" · медиана {_money(rng.median)} $ ({rng.count})"
                )
    lines += [
        "",
        f"<i>Обновлено {built_at:%d.%m %H:%M} UTC. Вилки бывают до и после налогов, "
        "курсы валют примерные.</i>",
    ]
    return "\n".join(lines)
