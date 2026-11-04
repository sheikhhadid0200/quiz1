import asyncio
import logging
import os
from pathlib import Path

import httpx
from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart
from dotenv import load_dotenv
from aiogram.types import CallbackQuery, Message

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

load_dotenv(Path(__file__).resolve().parent / ".env")
BOT_TOKEN = os.getenv("BOT_TOKEN", "")
OPERATOR_CHAT_ID = os.getenv("OPERATOR_CHAT_ID", "")
OPERATOR_CALLBACK_SECRET = os.getenv("OPERATOR_CALLBACK_SECRET", "")
BACKEND_INTERNAL_URL = os.getenv(
    "BACKEND_INTERNAL_URL", "http://127.0.0.1:8000"
).rstrip("/")
OPERATOR_USER_ID = os.getenv("OPERATOR_USER_ID", "")

if not BOT_TOKEN or not OPERATOR_CHAT_ID or not OPERATOR_CALLBACK_SECRET:
    raise RuntimeError(
        "Set BOT_TOKEN, OPERATOR_CHAT_ID, and OPERATOR_CALLBACK_SECRET"
    )

bot = Bot(token=BOT_TOKEN)
dispatcher = Dispatcher()


@dispatcher.message(CommandStart())
async def show_chat_id(message: Message) -> None:
    await message.answer(
        f"ID этого чата: {message.chat.id}\n"
        "Укажите это значение в OPERATOR_CHAT_ID на сервере."
    )


@dispatcher.callback_query(F.data.startswith("op_screen:"))
async def choose_screen(callback: CallbackQuery) -> None:
    if str(callback.message.chat.id) != OPERATOR_CHAT_ID:
        await callback.answer("Недостаточно прав.", show_alert=True)
        return
    if OPERATOR_USER_ID and str(callback.from_user.id) != OPERATOR_USER_ID:
        await callback.answer("Недостаточно прав.", show_alert=True)
        return

    try:
        _, action, session_id = (callback.data or "").split(":", maxsplit=2)
    except ValueError:
        await callback.answer("Некорректное действие.", show_alert=True)
        return
    if action not in {"success", "quiz_next", "error"}:
        await callback.answer("Неизвестный экран.", show_alert=True)
        return

    try:
        async with httpx.AsyncClient(timeout=10) as client:
            response = await client.post(
                f"{BACKEND_INTERNAL_URL}/api/operator/action",
                json={"session_id": session_id, "action": action},
                headers={"X-Operator-Secret": OPERATOR_CALLBACK_SECRET},
            )
        if response.status_code == 409:
            await callback.answer("Для этой заявки решение уже принято.", show_alert=True)
            return
        response.raise_for_status()
    except httpx.HTTPError:
        logger.exception("Could not submit operator action for %s", session_id)
        await callback.answer("Не удалось обновить страницу клиента.", show_alert=True)
        return

    await callback.answer("Экран клиента обновлён.")
    if callback.message:
        await callback.message.edit_reply_markup(reply_markup=None)


async def main() -> None:
    await dispatcher.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
