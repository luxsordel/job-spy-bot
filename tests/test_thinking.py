"""«Думаю…» пока работает ИИ, своя очередь у каждого человека, уведомление о лимите ИИ."""

from __future__ import annotations

import asyncio

from jobspybot.telegram.bot import update_sender
from tests.test_bot import callback, message


async def test_thinking_shows_progress_and_cleans_up(bot, tg, db):
    user = db.ensure_user(7, "u7", allowed=True)
    bot.thinking_tick = 0.01
    async with bot.thinking(user, "🤔 Думаю"):
        await asyncio.sleep(0.05)
    methods = [m for m, _ in tg.calls]
    assert tg.texts(7) == ["🤔 Думаю…"]
    assert "sendChatAction" in methods  # «печатает…» в шапке чата
    assert any(m == "editMessageText" and " с" in p["text"] for m, p in tg.calls)  # секундомер
    assert methods[-1] == "deleteMessage"  # ответ пришёл — заглушка убрана


async def test_thinking_cleans_up_on_error(bot, tg, db):
    user = db.ensure_user(7, "u7", allowed=True)
    try:
        async with bot.thinking(user):
            raise ValueError("boom")
    except ValueError:
        pass
    assert tg.calls[-1][0] == "deleteMessage"


async def test_ai_limit_notice_once_a_day(bot, tg, db):
    user = db.ensure_user(7, "u7", allowed=True)
    await bot.ai_limit_reached(user)
    await bot.ai_limit_reached(user)
    texts = tg.texts(7)
    assert len(texts) == 1 and "лимит ИИ" in texts[0] and "без оценки" in texts[0]


def test_update_sender():
    assert update_sender(message(5, "hi")) == 5
    assert update_sender(callback(6, "x")) == 6
    assert update_sender({"update_id": 1}) == 0


async def test_slow_user_does_not_block_others(bot):
    gate = asyncio.Event()
    order: list[str] = []

    async def handle(update):
        uid = update_sender(update)
        text = update["message"]["text"]
        if text == "slow":
            await gate.wait()
        order.append(f"{uid}:{text}")

    bot.handle_update = handle
    first = asyncio.create_task(bot._handle_in_order(message(1, "slow")))
    second = asyncio.create_task(bot._handle_in_order(message(1, "next")))
    other = asyncio.create_task(bot._handle_in_order(message(2, "hi")))
    await other
    assert order == ["2:hi"]  # другой человек не ждёт, пока ИИ думает над чужим
    gate.set()
    await asyncio.gather(first, second)
    assert order == ["2:hi", "1:slow", "1:next"]  # свои сообщения — по порядку
    assert bot._locks == {}  # замки не копятся
