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
    student_name: str
    stream_number: int
    course_number: int
    class_number: int
    stage: str = "awaiting_assignment"
    websocket: WebSocket | None = None
    created_at: float = field(default_factory=time.monotonic)
    answer_version: int = 0
    reviewed_version: int = 0
    awaiting_answer: bool = False
    finalized: bool = False
    latest_event: dict[str, str] | None = None


sessions: dict[str, Session] = {}
TASK_PROMPT = "Сколько будет 6 × 6?"
QUESTION_PROMPT = "2 + 2 = 4?"
ASSIGNMENT_ACTIONS = {
    "send_task": "Отправить задачу",
    "send_question": "Отправить вопрос",
    "error": "Ошибка, проверьте данные",
}
ANSWER_ACTIONS = {
    "correct": "Верно",
    "incorrect": "Не верно, попробуйте ещё",
}


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


class StudentProfile(BaseModel):
    student_name: str = Field(min_length=1)
    stream_number: str = Field(min_length=1)
    course_number: str = Field(min_length=1)
    class_number: str = Field(min_length=1)

    @field_validator(
        "student_name", "stream_number", "course_number", "class_number",
        mode="before",
    )
    @classmethod
    def trim_text(cls, value: str) -> str:
        return value.strip() if isinstance(value, str) else value


class AnswerUpdate(BaseModel):
    answer: str = Field(min_length=1)

    @field_validator("answer", mode="before")
    @classmethod
    def trim_text(cls, value: str) -> str:
        return value.strip() if isinstance(value, str) else value


class OperatorAction(BaseModel):
    session_id: str
    action: Literal["send_task", "send_question", "error", "correct", "incorrect"]
    answer_version: int = Field(ge=0)
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
async def create_submission(profile: StudentProfile) -> dict[str, str]:
    require_configuration()
    remove_expired_sessions()
    session_id = str(uuid.uuid4())
    sessions[session_id] = Session(
        student_name=profile.student_name,
        stream_number=profile.stream_number,
        course_number=profile.course_number,
        class_number=profile.class_number,
    )
    message = (
        "📚 Новый ученик ожидает задание\n"
        f"🆔 Урок: {session_id}\n"
        f"👤 Ученик: {profile.student_name}\n"
        f"📚 Поток: {profile.stream_number}\n"
        f"🎓 Курс: {profile.course_number}\n"
        f"🏫 Класс: {profile.class_number}\n\n"
        "Выберите, что отправить ученику:"
    )
    try:
        await notify_operators(
            session_id, message, ASSIGNMENT_ACTIONS, answer_version=0
        )
    except HTTPException:
        sessions.pop(session_id, None)
        raise

    return {"session_id": session_id}


async def notify_operators(
    session_id: str,
    message: str,
    actions: dict[str, str],
    answer_version: int,
) -> None:
    operator_chat_ids = get_operator_chat_ids()
    if not operator_chat_ids:
        raise HTTPException(
            status_code=503,
            detail=(
                "Пока нет репетиторов в боте. Откройте Telegram-бота и "
                "нажмите Start, чтобы зарегистрироваться."
            ),
        )

    keyboard = {
        "inline_keyboard": [
            [
                {
                    "text": label,
                    "callback_data": (
                        f"tutor:{action}:{session_id}:{answer_version}"
                    ),
                }
            ]
            for action, label in actions.items()
        ]
    }

    delivered = 0
    try:
        async with httpx.AsyncClient(timeout=10) as client:
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
                    "Telegram rejected tutor notification: HTTP %s, "
                    "error_code=%s, description=%s",
                    response.status_code,
                    telegram_response.get("error_code", "unknown")
                    if isinstance(telegram_response, dict)
                    else "unknown",
                    description,
                )
    except httpx.HTTPError as error:
        logger.warning(
            "Could not notify tutors for lesson %s (%s)",
            session_id,
            type(error).__name__,
        )

    if delivered == 0:
        raise HTTPException(
            status_code=502,
            detail="Не удалось отправить ответ репетитору. Попробуйте ещё раз.",
        )


@app.post("/api/sessions/{session_id}/answers", status_code=202)
async def submit_follow_up_answer(
    session_id: str, submission: AnswerUpdate
) -> dict[str, int]:
    remove_expired_sessions()
    session = sessions.get(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="Урок не найден.")
    if session.finalized:
        raise HTTPException(status_code=409, detail="Урок уже завершён.")
    if session.stage not in {"task", "question"} or not session.awaiting_answer:
        raise HTTPException(
            status_code=409, detail="Сейчас нельзя отправить ответ."
        )

    previous_version = session.answer_version
    previous_event = session.latest_event
    session.awaiting_answer = False
    session.answer_version += 1
    session.latest_event = None
    prompt = TASK_PROMPT if session.stage == "task" else QUESTION_PROMPT
    message = (
        "📝 Ответ ученика\n"
        f"🆔 Урок: {session_id}\n"
        f"👤 Ученик: {session.student_name}\n"
        f"📚 Поток: {session.stream_number}\n"
        f"🎓 Курс: {session.course_number}\n"
        f"🏫 Класс: {session.class_number}\n"
        f"📖 {prompt}\n"
        f"✍️ Ответ №{session.answer_version}: {submission.answer}\n\n"
        "Проверьте ответ:"
    )
    try:
        await notify_operators(
            session_id, message, ANSWER_ACTIONS, session.answer_version
        )
    except HTTPException:
        session.answer_version = previous_version
        session.awaiting_answer = True
        session.latest_event = previous_event
        raise
    return {"answer_version": session.answer_version}


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
    if session.finalized:
        raise HTTPException(status_code=409, detail="Lesson already finished")

    if payload.action in {"send_task", "send_question", "error"}:
        if payload.answer_version != 0 or session.stage != "awaiting_assignment":
            raise HTTPException(status_code=409, detail="Assignment already selected")
        if payload.action == "error":
            session.stage = "error"
            session.finalized = True
            session.latest_event = {
                "action": "change_screen",
                "target": "error",
                "feedback": "Ошибка, проверьте данные и попробуйте снова.",
            }
        else:
            session.stage = "task" if payload.action == "send_task" else "question"
            session.awaiting_answer = True
            session.latest_event = {
                "action": "change_screen",
                "target": session.stage,
                "prompt": TASK_PROMPT if session.stage == "task" else QUESTION_PROMPT,
            }
        if session.websocket is not None:
            await send_session_event(session, payload.session_id)
        return {"status": "ok"}

    if payload.answer_version != session.answer_version:
        raise HTTPException(status_code=409, detail="A newer answer is awaiting review")
    if session.reviewed_version == payload.answer_version:
        raise HTTPException(status_code=409, detail="This answer was already reviewed")
    if session.answer_version == 0 or session.awaiting_answer:
        raise HTTPException(status_code=409, detail="No answer is awaiting review")

    session.reviewed_version = payload.answer_version
    if payload.action == "incorrect":
        session.awaiting_answer = True
        feedback = "Не верно, попробуйте ещё."
        target = "incorrect"
    else:
        session.finalized = True
        feedback = "Верно! Спасибо за ответ."
        target = "correct"
    session.latest_event = {
        "action": "change_screen",
        "target": target,
        "stage": session.stage,
        "prompt": TASK_PROMPT if session.stage == "task" else QUESTION_PROMPT,
        "feedback": feedback,
    }
    if session.websocket is not None:
        await send_session_event(session, payload.session_id)

    return {"status": "ok"}


async def send_session_event(session: Session, session_id: str) -> None:
    if session.websocket is None or session.latest_event is None:
        return
    try:
        await session.websocket.send_json(session.latest_event)
    except (RuntimeError, WebSocketDisconnect):
        logger.info("WebSocket closed before decision delivery for %s", session_id)
        session.websocket = None


@app.websocket("/ws/{session_id}")
async def session_websocket(websocket: WebSocket, session_id: str) -> None:
    remove_expired_sessions()
    session = sessions.get(session_id)
    if session is None:
        await websocket.close(code=1008, reason="Unknown session")
        return

    await websocket.accept()
    session.websocket = websocket
    if session.latest_event is not None:
        await websocket.send_json(session.latest_event)

    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        if session.websocket is websocket:
            session.websocket = None
