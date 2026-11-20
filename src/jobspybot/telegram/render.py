"""Тексты и экраны бота (Telegram HTML).

Стиль — минимализм: без эмодзи-украшений в каждой строке. Заголовок жирным, данные —
карточками в <blockquote>, длинные справки — в сворачиваемых цитатах, акценты — цветом
кнопок (Bot API 9.4). Эмодзи остаются только там, где несут смысл: флаги стран, 👍/👎,
⭐ (валюта Telegram), цветные точки статусов и предупреждения для граждан РБ.
Обращение — на «ты».
"""

from __future__ import annotations

from datetime import datetime
from html import escape

from jobspybot.billing import FREE, PRO, Plan
from jobspybot.filters import COUNTRIES, LEVEL_TITLES, UserFilters
from jobspybot.market import Range, country_title
from jobspybot.models import Level, Vacancy
from jobspybot.profile import RELOCATION_COUNTRIES, WORK_FORMATS, UserProfile
from jobspybot.sources import REGISTRY

BRAND = "JobSpy"

# --- общие элементы ------------------------------------------------------------

#: отметка выбранного варианта на кнопке (плюс синий цвет кнопки)
CHECK = "✓ "
BACK = "← Назад"
NEXT = "Далее →"
TO_PROFILE = "← К профилю"


def bar(value: int, total: int = 100, width: int = 10) -> str:
    """«▰▰▰▰▰▰▱▱▱▱» — шкала оценки и прогресса анкеты."""
    filled = max(0, min(width, round(value * width / total))) if total else 0
    return "▰" * filled + "▱" * (width - filled)


def card(*lines: str) -> str:
    """Блок данных в цитате (пустые строки выкидываются)."""
    body = "\n".join(x for x in lines if x)
    return f"<blockquote>{body}</blockquote>" if body else ""


def field(label: str, value: str) -> str:
    """«Уровень: <b>Junior</b>» — значение уже экранировано."""
    return f"{label}: <b>{value}</b>"


# --- главное меню (кнопки под полем ввода) -----------------------------------

BTN_SEARCH = "Поиск"
BTN_PROFILE = "Профиль"
BTN_RESUMES = "Резюме"
BTN_FILTERS = "Фильтры"
BTN_MARKET = "Рынок"
BTN_APPS = "Отклики"
BTN_PLAN = "Pro"
BTN_HELP = "Помощь"
BTN_STATS = "Статистика"
MENU_ROWS = [
    [BTN_SEARCH, BTN_PROFILE, BTN_RESUMES],
    [BTN_FILTERS, BTN_MARKET, BTN_APPS],
    [BTN_PLAN, BTN_HELP],
]
#: подписи старого меню: клавиатура живёт у человека до следующего обновления
LEGACY_MENU = {
    "🔎 Мой поиск": BTN_SEARCH,
    "📄 Резюме": BTN_RESUMES,
    "⚙️ Фильтры": BTN_FILTERS,
    "💎 Подписка": BTN_PLAN,
    "📊 Статистика": BTN_STATS,
    "❓ Помощь": BTN_HELP,
    "👤 Профиль": BTN_PROFILE,
    "📋 Отклики": BTN_APPS,
    "📈 Рынок": BTN_MARKET,
}
MENU_PLACEHOLDER = "Пришли резюме PDF или напиши, что поменять"

#: статусы отклика: (точка статуса, подпись)
APP_STATUS = {
    "applied": ("🔵", "Отклик отправлен"),
    "test": ("🟡", "Тестовое"),
    "interview": ("🟣", "Интервью"),
    "offer": ("🟢", "Оффер"),
    "rejected": ("⚪", "Отказ"),
}
APPS_EMPTY = (
    "<b>Отклики</b>\n\n"
    "Пока пусто. Откликаешься на вакансию — нажми под ней <b>+ В отклики</b>. "
    "Я запишу её сюда, буду вести статус — тестовое, интервью, оффер — "
    "и напомню, если работодатель молчит неделю."
)

# --- готовые направления для быстрого выбора ---------------------------------

#: ключ → (кнопка, поисковый запрос)
DIRECTIONS: dict[str, tuple[str, str]] = {
    "py": ("Python", "python developer"),
    "java": ("Java", "java developer"),
    "fe": ("Frontend / JS", "frontend developer"),
    "node": ("Node.js", "node.js developer"),
    "cpp": ("C / C++", "c++ developer"),
    "cs": ("C# / .NET", "c# developer"),
    "go": ("Go", "golang developer"),
    "php": ("PHP", "php developer"),
    "qa": ("QA / тестирование", "qa engineer"),
    "ops": ("DevOps", "devops engineer"),
    "data": ("Data / ML", "data scientist"),
    "mob": ("Mobile", "mobile developer"),
    "ai": ("AI / вайбкодинг", "ai developer"),
    "an": ("Аналитик", "системный аналитик"),
    "pm": ("Project / Product", "project manager"),
    "ux": ("UI/UX дизайн", "ui ux designer"),
}
LEVEL_BUTTONS = {
    Level.INTERN.value: "Стажёр",
    Level.JUNIOR.value: "Junior",
    Level.MIDDLE.value: "Middle",
    Level.SENIOR.value: "Senior",
    Level.LEAD.value: "Lead",
}


FORMAT_BADGES = {"remote": "удалённо", "hybrid": "гибрид", "office": "офис"}


def vacancy_card(v: Vacancy, extra: str = "") -> str:
    """Карточка вакансии: название-ссылка, где, сколько, откуда; оценка ИИ — цитатой."""
    lines = [f'<b><a href="{escape(v.url, quote=True)}">{escape(v.title)}</a></b>']
    formats = [FORMAT_BADGES[f] for f in v.work_formats if f in FORMAT_BADGES]
    if v.remote and not formats:
        formats = [FORMAT_BADGES["remote"]]
    where = " · ".join(x for x in (escape(v.company), escape(v.location), *formats) if x)
    if where:
        lines.append(where)
    salary = v.salary_text()
    meta = " · ".join(
        x
        for x in (
            f"<b>{escape(salary)}</b>" if salary else "",
            LEVEL_TITLES[v.level].capitalize() if v.level else "",
        )
        if x
    )
    if meta:
        lines.append(meta)
    if v.address:
        lines.append(escape(v.address))
    if v.skills:
        lines.append(escape(" · ".join(v.skills[:8])))
    source = REGISTRY[v.source].title if v.source in REGISTRY else v.source
    date = v.published_at.strftime("%d.%m") if v.published_at else ""
    lines.append(f"<i>{escape(source)}{' · ' + date if date else ''}</i>")
    if extra:
        lines.append(extra if extra.startswith("<blockquote") else card(extra))
    return "\n".join(lines)


def _list(items: list[str], empty: str = "—") -> str:
    return escape(", ".join(items)) if items else empty


def date_ru(dt: datetime) -> str:
    return dt.strftime("%d.%m.%Y")


def days_word(n: int) -> str:
    """«1 день», «3 дня», «7 дней»."""
    if n % 10 == 1 and n % 100 != 11:
        return f"{n} день"
    if n % 10 in (2, 3, 4) and n % 100 not in (12, 13, 14):
        return f"{n} дня"
    return f"{n} дней"


def _every(minutes: int) -> str:
    return f"{minutes // 60} ч" if minutes >= 60 else f"{minutes} мин"


# --- экраны ------------------------------------------------------------------


def welcome(name: str, trial_days: int) -> str:
    hello = f"Привет, {escape(name)}!" if name else "Привет!"
    trial = (
        f"\n<b>Pro на {days_word(trial_days)} — бесплатно.</b> Все функции, без ограничений."
        if trial_days
        else ""
    )
    return (
        f"<b>{hello}</b>\n"
        f"Я {BRAND} — ищу работу, пока ты занимаешься своими делами.\n\n"
        + card(
            "• 15 сайтов и Telegram-каналов: Беларусь, Россия, Казахстан, Грузия, Польша "
            "и удалёнка по миру",
            "• ИИ читает каждую вакансию и оценивает шанс на оффер под твоё резюме",
            "• Предупреждаю, если вакансия закрыта для граждан РБ",
            "• Подбираю лучшее резюме и пишу сопроводительное письмо",
        )
        + f"{trial}\n\n"
        "Настроим поиск за минуту.\n"
        "<i>Продолжая, ты соглашаешься с /privacy — что я храню и как это удалить.</i>"
    )


def onboarding_step(step: int, total: int, title: str, hint: str) -> str:
    return f"<b>{title}</b>\n{hint}\n\n{bar(step, total, total)}  {step} из {total}"


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
        salary += ", без вилки — скрывать"
    queries = f.queries
    over = len(queries) - plan.max_queries
    q_line = field("Направления", _list(queries[: plan.max_queries], "не заданы"))
    if over > 0:
        q_line += f" <i>(+{over} — в Pro)</i>"
    status = "⚪ На паузе" if paused else f"🟢 Активен · проверяю каждые {_every(check_every_min)}"
    return "\n".join(
        [
            "<b>Поиск</b>",
            status,
            "",
            card(
                q_line,
                field("Уровень", escape(levels)),
                field("Страны", escape(countries)),
                field("Только удалёнка", "да" if f.remote_only else "нет"),
                field("Зарплата", salary),
                field("Стоп-слова", _list(f.exclude)),
            ),
            card(
                field(
                    "Площадки",
                    f"{len([s for s in sources if s in REGISTRY])} из {len(available_sources)}",
                ),
                field("Резюме", str(resumes) if resumes else "нет"),
                field("Порог ИИ", f"{f.ai_threshold}/100"),
            ),
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
    p: UserProfile, queries: list[str], *, title: str = "Профиль", summary: str = ""
) -> str:
    unknown = "<i>не указано</i>"

    def val(label: str, value: str) -> str:
        # «не указано» — курсивом, без жирного
        return f"{label}: {value}" if value == unknown else field(label, value)

    head = " · ".join(escape(x) for x in (p.name, p.headline) if x)
    level = ", ".join(LEVEL_BUTTONS[lv] for lv in p.target_levels() if lv in LEVEL_BUTTONS)
    if not level and "levels" in p.answered:
        level = "любой"
    exp = f"опыт {_years(p.experience_years)}" if p.experience_years else ""
    level_line = " · ".join(x for x in (level, exp) if x) or unknown
    place = ", ".join(x for x in (p.city, p.country) if x)
    formats = ", ".join(WORK_FORMATS[f] for f in p.work_formats if f in WORK_FORMATS)
    if p.only_home_city and p.city and formats:
        formats += f" · только {p.city}"
    if p.relocation is True:
        where = ", ".join(RELOCATION_COUNTRIES.get(c, c) for c in p.relocation_countries)
        reloc = "да" + (f" — {escape(where)}" if where else "")
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
    lines.append("")
    # карточки: кто я → что умею → где и как работать → чего хочу
    lines.append(
        card(
            val("Уровень", level_line),
            *(["Образование:", *(f"• {escape(e)}" for e in p.education)] if p.education else []),
        )
    )
    lines.append(
        card(
            val("Навыки", _list(p.skills[:15], unknown)),
            val("Языки", _list(p.languages, unknown)),
        )
    )
    lines.append(
        card(
            val("Живу", escape(place) if place else unknown)
            + f" · гражданство {escape(p.citizenship)}",
            val("Формат", escape(formats) if formats else unknown),
            val("Релокация", reloc),
        )
    )
    lines.append(
        card(
            val("Зарплата", salary),
            val("Ищу", _list(queries or p.directions, unknown)),
            val("Пожелания", escape(p.wishes)) if p.wishes else "",
        )
    )
    return "\n".join(lines)


QUESTION_TEXTS = {
    "directions": ("Что ищем?", "Выбери направления или нажми «Свой вариант»."),
    "level": (
        "Какой уровень вакансий показывать?",
        "Можно несколько — например, Junior и Middle.",
    ),
    "work_formats": ("Какой формат работы подходит?", "Можно несколько."),
    "relocation": ("Рассматриваешь релокацию?", "Если да — буду показывать вакансии с переездом."),
    "relocation_countries": ("В какие страны можно переехать?", "Можно несколько."),
    "salary": ("Сколько хочешь получать?", "В $ в месяц, на руки. Меньше — не покажу."),
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
        kind = " · пробный" if trial else ""
        renew = "продлится автоматически" if subscribed else "без автопродления"
        head = f"<b>{BRAND} Pro</b>{kind}\nАктивен до {date_ru(until)}, {renew}"
    elif plan is PRO:
        head = f"<b>{BRAND} Pro</b>\nАктивен — администратор"
    else:
        head = f"<b>{BRAND} Free</b>\nБесплатный тариф"

    rows = [
        ("", "Free", "Pro"),
        ("Направлений", str(FREE.max_queries), str(PRO.max_queries)),
        ("Резюме", str(FREE.max_resumes), str(PRO.max_resumes)),
        ("Вакансий в день", _fmt_limit(FREE.daily_vacancies), _fmt_limit(PRO.daily_vacancies)),
        ("ИИ-оценка", "≈3 в день", "все"),
        ("Отклик + письмо", "—", "✓"),
        (
            "Проверка",
            f"раз в {_every(FREE.check_every_min)}",
            f"раз в {_every(PRO.check_every_min)}",
        ),
    ]
    table = "\n".join(f"{a:<16}{b:<11}{c}" for a, b, c in rows)
    lines = [head, "", f"<pre>{escape(table)}</pre>"]
    if price_stars and not subscribed:
        lines += [
            "",
            f"Pro — <b>{price_stars} ⭐ в месяц</b>. Оплата звёздами Telegram в два касания, "
            "отменить можно в любой момент — Pro доработает до конца оплаченного месяца.",
        ]
    if support:
        lines += ["", f"<i>Вопросы и идеи: {escape(support)}</i>"]
    return "\n".join(lines)


HELP = f"""<b>{BRAND}</b> — личный агент по поиску работы.

<b>Как это работает</b>
<blockquote>1. Каждые полчаса проверяю 15 сайтов и Telegram-каналы по твоим направлениям
2. Бесплатно отсеиваю лишнее: уровень, страну, стоп-слова, чужой стек
3. ИИ читает остальное и ставит балл под твоё резюме
4. Присылаю только то, где есть шанс. «Отклик» — лучшее резюме и письмо</blockquote>

<b>Разделы</b>
<blockquote expandable>{BTN_SEARCH} — что и где ищем, пауза
{BTN_PROFILE} — заполняется из резюме, правится кнопками или словами
{BTN_RESUMES} — загрузить PDF, список резюме
{BTN_FILTERS} — направления, уровень, страны, зарплата, площадки
{BTN_MARKET} — сколько платят по твоим направлениям
{BTN_APPS} — куда откликнулся и на каком этапе
{BTN_PLAN} — тариф и оплата</blockquote>

<b>Подсказки</b>
<blockquote expandable>• Пришли PDF — сам заполню профиль и настрою поиск
• Можно несколько резюме под разные роли — выберу лучшее под каждую вакансию
• Можно просто написать: <i>хочу только удалёнку от 1500$</i>
• 👎 под вакансией — скрыть, «+ В отклики» — записать в трекер откликов
• <code>/query python developer; тестировщик</code> — свои направления
• <code>/exclude senior, 1с</code> — стоп-слова</blockquote>

/privacy — что я храню · /delete_me — удалить всё"""


def privacy_text(inactive_days: int, support: str = "") -> str:
    """Политика конфиденциальности простыми словами."""
    keep = (
        f"• Если не заходишь {days_word(inactive_days)} и нет активной подписки — "
        "удаляю резюме, профиль и историю сам"
        if inactive_days
        else "• До твоей просьбы об удалении"
    )
    lines = [
        "<b>Твои данные</b>",
        "Не продаю и не передаю для рекламы. Храню только то, что нужно для подбора вакансий "
        "и откликов.",
        "",
        "<b>Что храню</b>",
        card(
            "• Telegram id и имя — чтобы присылать вакансии",
            "• Резюме (текст и ссылку на файл в Telegram), профиль, фильтры",
            "• Историю: какие вакансии прислал или отсеял — 30 дней",
            "• Расход ИИ по дням и платежи Telegram Stars",
        ),
        "<b>Кому передаю</b>",
        card(
            "• ИИ-провайдеру — текст резюме <b>без телефонов и email</b> и описания вакансий, "
            "для оценки и писем",
            "• Telegram — как любой бот",
        ),
        "<b>Сколько храню</b>",
        card(
            "• Пока ты пользуешься ботом",
            keep,
            "• После удаления остаются только id, срок подписки и платежи — "
            "для возвратов и чтобы пробный период нельзя было взять заново",
        ),
        "Удалить всё сейчас — кнопка ниже или /delete_me.",
    ]
    if support:
        lines += ["", f"<i>Вопросы: {escape(support)}</i>"]
    return "\n".join(lines)


# --- рынок зарплат ---------------------------------------------------------------

#: сколько направлений и стран показывать — чтобы сообщение не превращалось в простыню
MARKET_QUERIES = 3
MARKET_PLACES = 3
MARKET_EMPTY = (
    "<b>Рынок зарплат</b>\n\nСначала выбери направления поиска — покажу вилки по ним. "
    f"Это в разделе {BTN_FILTERS} → Направления."
)


def _money(usd: int) -> str:
    return f"{usd:,}".replace(",", " ")


def market_text(
    blocks: list[tuple[str, list[tuple[str, dict[str, Range]]]]], built_at: datetime
) -> str:
    """«python developer → 🇧🇾 Беларусь → junior: 800–1 400 $ · медиана 1 100 $ (23)»."""
    lines = [
        "<b>Рынок зарплат</b>",
        "<i>Вилки из вакансий за 30 дней, $ в месяц: середина рынка и медиана.</i>",
    ]
    for query, places in blocks:
        lines += ["", f"<b>{escape(query)}</b>"]
        body: list[str] = []
        for country, ranges in places:
            body.append(country_title(country))
            if not ranges:
                body.append("   мало вакансий с вилкой")
            for level, rng in ranges.items():
                name = "все грейды" if level == "all" else LEVEL_TITLES[Level(level)]
                body.append(
                    f"   {name}: <b>{_money(rng.low)}–{_money(rng.high)} $</b>"
                    f" · медиана {_money(rng.median)} $ ({rng.count})"
                )
        lines.append(card(*body))
    lines += [
        "",
        f"<i>Обновлено {built_at:%d.%m %H:%M} UTC. Вилки бывают до и после налогов, "
        "курсы валют примерные.</i>",
    ]
    return "\n".join(lines)
