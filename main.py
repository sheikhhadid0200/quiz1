import hmac
import logging
import os
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

import httpx
from dotenv import load_dotenv
from fastapi import FastAPI, Header, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field, field_validator

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parent
load_dotenv(ROOT / ".env")
BOT_TOKEN = os.getenv("BOT_TOKEN", "")
OPERATOR_CHAT_ID = os.getenv("OPERATOR_CHAT_ID", "")
OPERATOR_CALLBACK_SECRET = os.getenv("OPERATOR_CALLBACK_SECRET", "")
SESSION_TTL_SECONDS = 24 * 60 * 60
app = FastAPI(title="Operator Review")


@dataclass
class Session:
    action: str | None = None
    websocket: WebSocket | None = None
    created_at: float = field(default_factory=time.monotonic)


sessions: dict[str, Session] = {}


def remove_expired_sessions() -> None:
    cutoff = time.monotonic() - SESSION_TTL_SECONDS
    for session_id, session in list(sessions.items()):
        if session.created_at < cutoff:
            sessions.pop(session_id, None)


class Submission(BaseModel):
    client_nickname: str = Field(min_length=1, max_length=100)
    new_identifier: str = Field(min_length=1, max_length=200)
    current_date: str = Field(min_length=1, max_length=100)
    favorite_number: int = Field(ge=0, le=999_999_999)

    @field_validator("client_nickname", "new_identifier", "current_date", mode="before")
    @classmethod
    def trim_text(cls, value: str) -> str:
        return value.strip() if isinstance(value, str) else value


class OperatorAction(BaseModel):
    session_id: str
    action: Literal["success", "quiz_next", "error"]


def require_configuration() -> None:
    missing = [
        name
        for name, value in (
            ("BOT_TOKEN", BOT_TOKEN),
            ("OPERATOR_CHAT_ID", OPERATOR_CHAT_ID),
            ("OPERATOR_CALLBACK_SECRET", OPERATOR_CALLBACK_SECRET),
        )
        if not value
    ]
    if missing:
        raise HTTPException(
            status_code=503,
            detail=f"Server configuration is missing: {', '.join(missing)}",
        )


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(ROOT / "static" / "index.html")


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/api/submissions", status_code=201)
async def create_submission(submission: Submission) -> dict[str, str]:
    require_configuration()
    remove_expired_sessions()
    session_id = str(uuid.uuid4())
    sessions[session_id] = Session()
    message = (
        "📬 Поступила новая анкета!\n"
        f"🆔 ID Сессии: {session_id}\n\n"
        f"👤 Ник: {submission.client_nickname}\n"
        f"🔑 Новый идентификатор: {submission.new_identifier}\n"
        f"📅 Дата сегодня: {submission.current_date}\n"
        f"🔢 Любимое число: {submission.favorite_number}\n\n"
        "Выберите, какой следующий экран отобразить пользователю на сайте:"
    )
    keyboard = {
        "inline_keyboard": [
            [
                {
                    "text": "Экран 1: Успешный финиш",
                    "callback_data": f"op_screen:success:{session_id}",
                }
            ],
            [
                {
                    "text": "Экран 2: Дополнительный опрос",
                    "callback_data": f"op_screen:quiz_next:{session_id}",
                }
            ],
            [
                {
                    "text": "Экран 3: Ошибка / Отклонено",
                    "callback_data": f"op_screen:error:{session_id}",
                }
            ],
        ]
    }

    try:
        async with httpx.AsyncClient(timeout=10) as client:
            response = await client.post(
                f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
                json={
                    "chat_id": OPERATOR_CHAT_ID,
                    "text": message,
                    "reply_markup": keyboard,
                },
            )
            try:
                telegram_response = response.json()
            except ValueError:
                logger.warning(
                    "Telegram sendMessage returned invalid JSON (HTTP %s)",
                    response.status_code,
                )
                raise RuntimeError("Telegram returned an invalid response")
            if not isinstance(telegram_response, dict):
                telegram_response = {}
            if not response.is_success or not telegram_response.get("ok"):
                description = telegram_response.get("description", "no description")
                if isinstance(description, str) and BOT_TOKEN:
                    description = description.replace(BOT_TOKEN, "[redacted]")
                logger.warning(
                    "Telegram sendMessage rejected the request: HTTP %s, "
                    "error_code=%s, description=%s",
                    response.status_code,
                    telegram_response.get("error_code", "unknown"),
                    description,
                )
                raise RuntimeError("Telegram rejected the message")
    except httpx.HTTPError as error:
        sessions.pop(session_id, None)
        logger.warning(
            "Could not reach Telegram for session %s (%s)",
            session_id,
            type(error).__name__,
        )
        raise HTTPException(
            status_code=502,
            detail="Не удалось передать заявку оператору. Попробуйте позже.",
        )
    except RuntimeError:
        sessions.pop(session_id, None)
        raise HTTPException(
            status_code=502,
            detail="Не удалось передать заявку оператору. Попробуйте позже.",
        )

    return {"session_id": session_id}


@app.post("/api/operator/action")
async def operator_action(
    payload: OperatorAction,
    x_operator_secret: str = Header(default=""),
) -> dict[str, str]:
    remove_expired_sessions()
    if not OPERATOR_CALLBACK_SECRET or not hmac.compare_digest(
        x_operator_secret, OPERATOR_CALLBACK_SECRET
    ):
        raise HTTPException(status_code=401, detail="Invalid operator credential")

    session = sessions.get(payload.session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="Session not found")
    if session.action is not None:
        raise HTTPException(status_code=409, detail="Session already has a decision")

    session.action = payload.action
    if session.websocket is not None:
        try:
            await session.websocket.send_json(
                {"action": "change_screen", "target": payload.action}
            )
        except (RuntimeError, WebSocketDisconnect):
            logger.info("WebSocket closed before decision delivery for %s", payload.session_id)
            session.websocket = None

    return {"status": "ok"}


@app.websocket("/ws/{session_id}")
async def session_websocket(websocket: WebSocket, session_id: str) -> None:
    remove_expired_sessions()
    session = sessions.get(session_id)
    if session is None:
        await websocket.close(code=1008, reason="Unknown session")
        return

    await websocket.accept()
    session.websocket = websocket
    if session.action is not None:
        await websocket.send_json(
            {"action": "change_screen", "target": session.action}
        )

    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        if session.websocket is websocket:
            session.websocket = None
