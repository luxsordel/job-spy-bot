"""Минимальный клиент Telegram Bot API поверх httpx.

aiogram / python-telegram-bot удобнее, но тянут pydantic и десятки мегабайт RAM.
Боту нужно с десяток методов — их проще вызывать напрямую.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import httpx

log = logging.getLogger(__name__)


class TelegramError(Exception):
    def __init__(self, method: str, code: int, description: str) -> None:
        super().__init__(f"{method}: {code} {description}")
        self.code = code
        self.description = description


class TelegramAPI:
    def __init__(
        self,
        token: str,
        *,
        base_url: str = "https://api.telegram.org",
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._url = f"{base_url}/bot{token}"
        self._file_url = f"{base_url}/file/bot{token}"
        # таймаут больше long-poll (50 c), иначе getUpdates будет обрываться
        self._client = httpx.AsyncClient(timeout=httpx.Timeout(70.0), transport=transport)

    async def call(self, method: str, **params: Any) -> Any:
        payload = {k: v for k, v in params.items() if v is not None}
        for _ in range(3):
            resp = await self._client.post(f"{self._url}/{method}", json=payload)
            data = resp.json()
            if data.get("ok"):
                return data["result"]
            retry_after = (data.get("parameters") or {}).get("retry_after")
            if resp.status_code == 429 and retry_after:
                log.warning("Telegram flood limit, sleeping %ss", retry_after)
                await asyncio.sleep(float(retry_after))
                continue
            raise TelegramError(
                method, data.get("error_code", resp.status_code), data.get("description", "")
            )
        raise TelegramError(method, 429, "too many retries")

    async def get_updates(self, offset: int | None, timeout: int = 50) -> list[dict[str, Any]]:
        return await self.call(
            "getUpdates",
            offset=offset,
            timeout=timeout,
            allowed_updates=["message", "callback_query", "pre_checkout_query", "my_chat_member"],
        )

    async def send_message(
        self,
        chat_id: int,
        text: str,
        *,
        reply_markup: dict[str, Any] | None = None,
        parse_mode: str | None = "HTML",
    ) -> dict[str, Any]:
        return await self.call(
            "sendMessage",
            chat_id=chat_id,
            text=text[:4096],
            parse_mode=parse_mode,
            reply_markup=reply_markup,
            link_preview_options={"is_disabled": True},
        )

    async def send_document(
        self, chat_id: int, file_id: str, caption: str = "", parse_mode: str | None = "HTML"
    ) -> dict[str, Any]:
        return await self.call(
            "sendDocument",
            chat_id=chat_id,
            document=file_id,
            caption=caption[:1024] or None,
            parse_mode=parse_mode if caption else None,
        )

    async def edit_reply_markup(
        self, chat_id: int, message_id: int, reply_markup: dict[str, Any] | None
    ) -> None:
        await self.call(
            "editMessageReplyMarkup",
            chat_id=chat_id,
            message_id=message_id,
            reply_markup=reply_markup or {"inline_keyboard": []},
        )

    async def answer_callback(self, callback_id: str, text: str | None = None) -> None:
        await self.call("answerCallbackQuery", callback_query_id=callback_id, text=text)

    async def set_commands(self, commands: list[tuple[str, str]]) -> None:
        await self.call(
            "setMyCommands",
            commands=[{"command": c, "description": d} for c, d in commands],
        )

    # --- Telegram Stars -------------------------------------------------------

    async def create_invoice_link(
        self,
        *,
        title: str,
        description: str,
        payload: str,
        amount: int,
        subscription_period: int | None = None,
    ) -> str:
        """Ссылка на оплату звёздами; с subscription_period — подписка с автопродлением."""
        return await self.call(
            "createInvoiceLink",
            title=title[:32],
            description=description[:255],
            payload=payload[:128],
            currency="XTR",
            prices=[{"label": title[:32], "amount": amount}],
            subscription_period=subscription_period,
        )

    async def answer_pre_checkout(self, query_id: str, ok: bool, error: str | None = None) -> None:
        await self.call(
            "answerPreCheckoutQuery", pre_checkout_query_id=query_id, ok=ok, error_message=error
        )

    async def set_star_subscription_canceled(
        self, user_id: int, charge_id: str, canceled: bool
    ) -> None:
        await self.call(
            "editUserStarSubscription",
            user_id=user_id,
            telegram_payment_charge_id=charge_id,
            is_canceled=canceled,
        )

    async def refund_star_payment(self, user_id: int, charge_id: str) -> None:
        await self.call("refundStarPayment", user_id=user_id, telegram_payment_charge_id=charge_id)

    # --- профиль бота -------------------------------------------------------

    async def set_profile_texts(self, description: str, short_description: str) -> None:
        await self.call("setMyDescription", description=description[:512])
        await self.call("setMyShortDescription", short_description=short_description[:120])

    async def get_me(self) -> dict[str, Any]:
        return await self.call("getMe")

    async def download_file(self, file_id: str, max_bytes: int = 200_000) -> bytes:
        info = await self.call("getFile", file_id=file_id)
        if (info.get("file_size") or 0) > max_bytes:
            raise ValueError("файл слишком большой")
        resp = await self._client.get(f"{self._file_url}/{info['file_path']}")
        resp.raise_for_status()
        return resp.content[:max_bytes]

    async def aclose(self) -> None:
        await self._client.aclose()


def inline_keyboard(*rows: list[dict[str, str]]) -> dict[str, Any]:
    return {"inline_keyboard": [row for row in rows if row]}


def reply_keyboard(*rows: list[str], placeholder: str = "") -> dict[str, Any]:
    """Постоянная клавиатура под полем ввода (главное меню)."""
    markup: dict[str, Any] = {
        "keyboard": [[{"text": t} for t in row] for row in rows],
        "resize_keyboard": True,
        "is_persistent": True,
    }
    if placeholder:
        markup["input_field_placeholder"] = placeholder[:64]
    return markup


def button(text: str, *, data: str | None = None, url: str | None = None) -> dict[str, str]:
    btn = {"text": text}
    if url:
        btn["url"] = url
    else:
        btn["callback_data"] = (data or "")[:64]
    return btn
