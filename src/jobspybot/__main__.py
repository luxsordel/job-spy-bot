"""Точка входа: `python -m jobspybot [run|check-sources]`."""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys

from jobspybot.config import ConfigError, Settings, load_env
from jobspybot.http import Http
from jobspybot.sources import REGISTRY, SourceUnavailable, build_sources

log = logging.getLogger("jobspybot")


async def check_sources(settings: Settings, query: str) -> int:
    """Диагностика: прогнать поиск по всем площадкам и показать результат."""
    http = Http(user_agent=settings.user_agent, timeout=settings.http_timeout)
    sources = build_sources(http, settings)
    skipped = sorted(set(REGISTRY) - set(sources))
    if skipped:
        print(f"Не настроены или выключены: {', '.join(skipped)}")
    failed = 0
    try:
        for name, source in sources.items():
            try:
                found = await source.search(query)
            except SourceUnavailable as exc:
                failed += 1
                print(f"[{name}] недоступна: {exc}")
                continue
            except Exception as exc:
                failed += 1
                print(f"[{name}] ошибка: {exc!r}")
                continue
            print(f"[{name}] найдено {len(found)}")
            for v in found[:3]:
                print(f"    {v.title} — {v.company} ({v.location}) {v.salary_text()}  {v.url}")
    finally:
        await http.aclose()
    return 1 if failed else 0


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="job-spy-bot", description=__doc__)
    parser.add_argument("--env", default=".env", help="путь к .env (по умолчанию ./.env)")
    sub = parser.add_subparsers(dest="cmd")
    sub.add_parser("run", help="запустить бота (по умолчанию)")
    chk = sub.add_parser("check-sources", help="проверить работу площадок")
    chk.add_argument("query", nargs="?", default="python")
    args = parser.parse_args(argv)

    try:
        settings = Settings.from_env(load_env(args.env))
    except ConfigError as exc:
        sys.exit(f"Ошибка конфигурации: {exc}")

    logging.basicConfig(
        level=settings.log_level,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)

    if args.cmd == "check-sources":
        sys.exit(asyncio.run(check_sources(settings, args.query)))

    sys.exit("Команда run появится вместе с Telegram-ботом")


if __name__ == "__main__":
    main()
