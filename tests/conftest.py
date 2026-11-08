"""Offline test harness: a recording Telegram session, a fake generator and a fake clock."""
from __future__ import annotations

import itertools
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

import pytest
from aiogram import Bot, Dispatcher
from aiogram.client.session.base import BaseSession
from aiogram.exceptions import TelegramBadRequest
from aiogram.methods import GetBusinessConnection, GetMe, SendMessage
from aiogram.types import BusinessConnection, Message, Update, User

from assistant.config import load_settings
from assistant.handlers import build_router
from assistant.service import Assistant, Preview

BOT_TOKEN = "123456:TEST-token"
OWNER_ID = 111
STRANGER_ID = 500
CONN_ID = "conn-1"
BASE_TIME = 1_800_000_000.0


class RecordingSession(BaseSession):
    def __init__(self) -> None:
        super().__init__()
        self.requests: List[Any] = []
        self.connection: Optional[Dict[str, Any]] = None
        self._ids = itertools.count(10_000)

    async def close(self) -> None:
        return None

    async def make_request(self, bot: Bot, method: Any, timeout: Optional[int] = None) -> Any:
        self.requests.append(method)
        if isinstance(method, GetBusinessConnection):
            if self.connection is None:
                raise TelegramBadRequest(method=method, message="Bad Request: business connection not found")
            return BusinessConnection.model_validate(self.connection)
        if isinstance(method, SendMessage):
            return Message.model_validate(
                {
                    "message_id": next(self._ids),
                    "date": int(BASE_TIME),
                    "chat": {"id": method.chat_id, "type": "private"},
                    "text": method.text,
                }
            )
        if isinstance(method, GetMe):
            return User(id=123456, is_bot=True, first_name="Assistant", username="my_assistant_bot")
        return True

    async def stream_content(self, *args: Any, **kwargs: Any):  # pragma: no cover
        raise AssertionError("tests must not download anything")
        yield b""

    def of(self, kind: type) -> List[Any]:
        return [r for r in self.requests if isinstance(r, kind)]

    @property
    def sent(self) -> List[SendMessage]:
        return self.of(SendMessage)


class FakeGenerator:
    def __init__(self, items: Optional[List[Any]] = None, default: str = "Hi! Talk soon.") -> None:
        self.items = list(items or [])
        self.default = default
        self.calls: List[Dict[str, str]] = []
        self.hook: Optional[Callable[[], Any]] = None

    async def generate(self, system_prompt: str, user_prompt: str) -> str:
        self.calls.append({"system": system_prompt, "user": user_prompt})
        if self.hook is not None:
            hook, self.hook = self.hook, None
            await hook()
        item = self.items.pop(0) if self.items else self.default
        if isinstance(item, BaseException):
            raise item
        return item


class Clock:
    def __init__(self, now: float = BASE_TIME) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def connection_payload(
    *, user_id: int = OWNER_ID, enabled: bool = True, can_reply: bool = True, conn_id: str = CONN_ID
) -> Dict[str, Any]:
    payload: Dict[str, Any] = {
        "id": conn_id,
        "user": {"id": user_id, "is_bot": False, "first_name": "Owner"},
        "user_chat_id": user_id,
        "date": int(BASE_TIME),
        "is_enabled": enabled,
        # Both shapes: new `rights` and the legacy flag for older aiogram.
        "can_reply": can_reply,
    }
    payload["rights"] = {"can_reply": True} if can_reply else {}
    return payload


class Harness:
    def __init__(self, tmp_path: Path, overrides: Optional[Dict[str, str]] = None, items: Optional[List[Any]] = None) -> None:
        self.tmp_path = tmp_path
        values = {
            "TELEGRAM_BOT_TOKEN": BOT_TOKEN,
            "OWNER_ID": str(OWNER_ID),
            "DATA_DIR": str(tmp_path / "data"),
            "PROFILE_PATH": str(tmp_path / "profile.md"),
            "CLAUDE_BIN": str(tmp_path / "no-claude"),
        }
        values.update(overrides or {})
        (tmp_path / "profile.md").write_text("# Men haqimda\n- Ism: Test Owner\n", encoding="utf-8")
        self.settings = load_settings(tmp_path / ".env", overrides=values)
        self.session = RecordingSession()
        self.bot = Bot(BOT_TOKEN, session=self.session)
        self.generator = FakeGenerator(items)
        self.clock = Clock()
        self.previews: List[Preview] = []
        self._update_ids = itertools.count(1)
        self._message_ids = itertools.count(1)
        self._build()

    def _build(self) -> None:
        self.assistant = Assistant(self.settings, self.generator, clock=self.clock, preview_sink=self.previews.append)
        self.dp = Dispatcher()
        self.dp.include_router(build_router(self.assistant))

    def restart(self) -> None:
        self._build()

    @property
    def outcomes(self) -> List[str]:
        return [o.value for o in self.assistant.outcomes]

    async def feed(self, payload: Dict[str, Any]) -> None:
        payload = dict(payload)
        payload.setdefault("update_id", next(self._update_ids))
        update = Update.model_validate(payload, context={"bot": self.bot})
        await self.dp.feed_update(self.bot, update)

    def set_connection(self, **kwargs: Any) -> None:
        """Change what Telegram answers to getBusinessConnection, without an update."""
        self.session.connection = connection_payload(**kwargs)

    async def connect(self, **kwargs: Any) -> None:
        self.set_connection(**kwargs)
        await self.feed({"business_connection": connection_payload(**kwargs)})

    def business_payload(
        self,
        text: Optional[str] = "Salom, qalesan?",
        *,
        from_id: int = STRANGER_ID,
        chat_id: Optional[int] = None,
        message_id: Optional[int] = None,
        connection_id: str = CONN_ID,
        is_bot: bool = False,
        via_business_bot: bool = False,
        name: str = "Ali",
        extra: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        chat = chat_id if chat_id is not None else (STRANGER_ID if from_id == OWNER_ID else from_id)
        message: Dict[str, Any] = {
            "message_id": message_id if message_id is not None else next(self._message_ids),
            "date": int(self.clock.now),
            "chat": {"id": chat, "type": "private", "first_name": "Ali"},
            "from": {"id": from_id, "is_bot": is_bot, "first_name": "Owner" if from_id == OWNER_ID else name},
            "business_connection_id": connection_id,
        }
        if text is not None:
            message["text"] = text
        if via_business_bot:
            message["sender_business_bot"] = {"id": 123456, "is_bot": True, "first_name": "Assistant"}
        if extra:
            message.update(extra)
        return {"business_message": message}

    async def business_message(self, text: Optional[str] = "Salom, qalesan?", **kwargs: Any) -> None:
        await self.feed(self.business_payload(text, **kwargs))

    async def direct_message(self, text: str, *, from_id: int = OWNER_ID) -> None:
        message: Dict[str, Any] = {
            "message_id": next(self._message_ids),
            "date": int(self.clock.now),
            "chat": {"id": from_id, "type": "private", "first_name": "Someone"},
            "from": {"id": from_id, "is_bot": False, "first_name": "Someone"},
            "text": text,
        }
        if text.startswith("/"):
            command = text.split()[0]
            message["entities"] = [{"type": "bot_command", "offset": 0, "length": len(command)}]
        await self.feed({"message": message})


@pytest.fixture
def make_harness(tmp_path: Path) -> Callable[..., Harness]:
    def factory(overrides: Optional[Dict[str, str]] = None, items: Optional[List[Any]] = None) -> Harness:
        return Harness(tmp_path, overrides, items)

    return factory
