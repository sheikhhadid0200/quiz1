import hmac
import logging
import os
import sqlite3
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator, Literal

import httpx
from dotenv import load_dotenv
from fastapi import FastAPI, Header, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field, field_validator

logging.basicConfig(level=logging.INFO)
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parent
load_dotenv(ROOT / ".env")
BOT_TOKEN = os.getenv("BOT_TOKEN", "")
OPERATOR_CALLBACK_SECRET = os.getenv("OPERATOR_CALLBACK_SECRET", "")
DATA_DIR = Path(os.getenv("DATA_DIR", str(ROOT / "data")))
DB_PATH = DATA_DIR / "app.sqlite3"
SESSION_TTL_SECONDS = 24 * 60 * 60
app = FastAPI(title="Operator Review")


@dataclass
class Session:
    action: str | None = None
    websocket: WebSocket | None = None
    created_at: float = field(default_factory=time.monotonic)


sessions: dict[str, Session] = {}


@contextmanager
def database_connection() -> Iterator[sqlite3.Connection]:
    connection = sqlite3.connect(DB_PATH, timeout=10)
    try:
        yield connection
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


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
    action: Literal["comment_demo", "push_demo", "success", "error"]
    user_id: int = Field(gt=0)
    chat_id: int = Field(gt=0)


class OperatorRegistration(BaseModel):
    user_id: int = Field(gt=0)
    chat_id: int = Field(gt=0)


def initialize_database() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with database_connection() as connection:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS operators (
                user_id INTEGER PRIMARY KEY,
                chat_id INTEGER NOT NULL,
                registered_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )


def get_operator_chat_ids() -> list[int]:
    with database_connection() as connection:
        rows = connection.execute(
            "SELECT chat_id FROM operators ORDER BY registered_at, user_id"
        ).fetchall()
    return [int(row[0]) for row in rows]


def is_registered_operator(user_id: int, chat_id: int) -> bool:
    with database_connection() as connection:
        row = connection.execute(
            "SELECT 1 FROM operators WHERE user_id = ? AND chat_id = ?",
            (user_id, chat_id),
        ).fetchone()
    return row is not None


def require_operator_secret(secret: str) -> None:
    if not OPERATOR_CALLBACK_SECRET or not hmac.compare_digest(
        secret, OPERATOR_CALLBACK_SECRET
    ):
        raise HTTPException(status_code=401, detail="Invalid operator credential")


initialize_database()


def require_configuration() -> None:
    missing = [
        name
        for name, value in (
            ("BOT_TOKEN", BOT_TOKEN),
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


@app.post("/api/operators/register", status_code=201)
async def register_operator(
    payload: OperatorRegistration,
    x_operator_secret: str = Header(default=""),
) -> dict[str, str]:
    require_operator_secret(x_operator_secret)
    with database_connection() as connection:
        connection.execute(
            """
            INSERT INTO operators (user_id, chat_id)
            VALUES (?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                chat_id = excluded.chat_id,
                registered_at = CURRENT_TIMESTAMP
            """,
            (payload.user_id, payload.chat_id),
        )
    return {"status": "registered"}


@app.post("/api/submissions", status_code=201)
async def create_submission(submission: Submission) -> dict[str, str]:
    require_configuration()
    remove_expired_sessions()
    operator_chat_ids = get_operator_chat_ids()
    if not operator_chat_ids:
        raise HTTPException(
            status_code=503,
            detail=(
                "Пока нет зарегистрированных операторов. Сначала каждый "
                "оператор должен открыть бота и отправить ему /start."
            ),
        )

    session_id = str(uuid.uuid4())
    sessions[session_id] = Session()
    failure_detail = "Не удалось передать заявку оператору. Попробуйте позже."
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
                    "text": "Комментарий",
                    "callback_data": f"op_screen:comment_demo:{session_id}",
                }
            ],
            [
                {
                    "text": "Пуш",
                    "callback_data": f"op_screen:push_demo:{session_id}",
                }
            ],
            [
                {
                    "text": "Ошибка",
                    "callback_data": f"op_screen:error:{session_id}",
                }
            ],
            [
                {
                    "text": "Успешно",
                    "callback_data": f"op_screen:success:{session_id}",
                }
            ],
        ]
    }

    try:
        async with httpx.AsyncClient(timeout=10) as client:
            delivered = 0
            for chat_id in operator_chat_ids:
                response = await client.post(
                    f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
                    json={
                        "chat_id": chat_id,
                        "text": message,
                        "reply_markup": keyboard,
                    },
                )
                try:
                    telegram_response = response.json()
                except ValueError:
                    telegram_response = {}
                if (
                    response.is_success
                    and isinstance(telegram_response, dict)
                    and telegram_response.get("ok")
                ):
                    delivered += 1
                    continue

                description = (
                    telegram_response.get("description", "no description")
                    if isinstance(telegram_response, dict)
                    else "invalid response"
                )
                logger.warning(
                    "Telegram rejected delivery to a registered operator: "
                    "HTTP %s, error_code=%s, description=%s",
                    response.status_code,
                    telegram_response.get("error_code", "unknown")
                    if isinstance(telegram_response, dict)
                    else "unknown",
                    description,
                )

        if delivered == 0:
            sessions.pop(session_id, None)
            raise HTTPException(
                status_code=502,
                detail=(
                    "Telegram не доставил заявку ни одному оператору. "
                    "Попросите операторов открыть бота и отправить /start."
                ),
            )
    except httpx.HTTPError as error:
        sessions.pop(session_id, None)
        logger.warning(
            "Could not reach Telegram for session %s (%s)",
            session_id,
            type(error).__name__,
        )
        raise HTTPException(
            status_code=502,
            detail=failure_detail,
        )

    return {"session_id": session_id}


@app.post("/api/operator/action")
async def operator_action(
    payload: OperatorAction,
    x_operator_secret: str = Header(default=""),
) -> dict[str, str]:
    remove_expired_sessions()
    require_operator_secret(x_operator_secret)
    if not is_registered_operator(payload.user_id, payload.chat_id):
        raise HTTPException(status_code=403, detail="Operator is not registered")

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
