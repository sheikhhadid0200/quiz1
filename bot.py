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
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)

load_dotenv(Path(__file__).resolve().parent / ".env")
BOT_TOKEN = os.getenv("BOT_TOKEN", "")
OPERATOR_CALLBACK_SECRET = os.getenv("OPERATOR_CALLBACK_SECRET", "")
BACKEND_INTERNAL_URL = os.getenv(
    "BACKEND_INTERNAL_URL", "http://127.0.0.1:8000"
).rstrip("/")

if not BOT_TOKEN or not OPERATOR_CALLBACK_SECRET:
    raise RuntimeError(
        "Set BOT_TOKEN and OPERATOR_CALLBACK_SECRET"
    )

bot = Bot(token=BOT_TOKEN)
dispatcher = Dispatcher()


@dispatcher.message(CommandStart())
async def register_operator(message: Message) -> None:
    if message.chat.type != "private":
        await message.answer("Для регистрации оператором откройте со мной личный чат.")
        return
    if message.from_user is None:
        await message.answer("Не удалось определить ваш аккаунт Telegram.")
        return

    try:
        async with httpx.AsyncClient(timeout=10) as client:
            response = await client.post(
                f"{BACKEND_INTERNAL_URL}/api/operators/register",
                json={
                    "user_id": message.from_user.id,
                    "chat_id": message.chat.id,
                },
                headers={"X-Operator-Secret": OPERATOR_CALLBACK_SECRET},
            )
        response.raise_for_status()
    except httpx.HTTPError:
        logger.exception("Could not register Telegram operator")
        await message.answer(
            "Не удалось зарегистрировать вас сейчас. Попробуйте позже."
        )
        return

    await message.answer(
        "Вы зарегистрированы как оператор. Новые заявки будут приходить сюда. "
        "Нажмите /start повторно, если хотите обновить регистрацию."
    )


@dispatcher.callback_query(F.data.startswith("tutor:"))
async def send_tutor_feedback(callback: CallbackQuery) -> None:
    if callback.message is None or callback.message.chat.type != "private":
        await callback.answer("Недостаточно прав.", show_alert=True)
        return

    try:
        _, action, session_id, answer_version = (
            callback.data or ""
        ).split(":", maxsplit=3)
        version = int(answer_version)
    except (ValueError, TypeError):
        await callback.answer("Некорректное действие.", show_alert=True)
        return
    if action not in {
        "send_task",
        "send_question",
        "error",
        "correct",
        "incorrect",
    }:
        await callback.answer("Неизвестное действие.", show_alert=True)
        return

    try:
        async with httpx.AsyncClient(timeout=10) as client:
            response = await client.post(
                f"{BACKEND_INTERNAL_URL}/api/operator/action",
                json={
                    "session_id": session_id,
                    "action": action,
                    "answer_version": version,
                    "user_id": callback.from_user.id,
                    "chat_id": callback.message.chat.id,
                },
                headers={"X-Operator-Secret": OPERATOR_CALLBACK_SECRET},
            )
        if response.status_code == 409:
            await callback.answer(
                "Ответ уже проверен или поступил новый ответ.", show_alert=True
            )
            return
        response.raise_for_status()
    except httpx.HTTPError:
        logger.exception("Could not submit operator action for %s", session_id)
        await callback.answer("Не удалось обновить страницу клиента.", show_alert=True)
        return

    await callback.answer("Действие отправлено.")
    if callback.message:
        await callback.message.edit_reply_markup(reply_markup=None)


async def main() -> None:
    await dispatcher.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
