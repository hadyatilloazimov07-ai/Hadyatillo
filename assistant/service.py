"""Core logic: decide whether to answer a business message, generate and send/preview."""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
from collections import deque
from dataclasses import asdict, dataclass
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Deque, Dict, List, Optional

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError
from aiogram.types import BusinessConnection, Message

from .claude_cli import ClaudeCLI, ClaudeError, ClaudeLimitError
from .config import Settings
from .edutizim import BalanceLookup, EduTizimClient
from .language import detect_language
from .prompts import SKIP_TOKEN, build_system_prompt, build_user_prompt
from .store import HistoryStore, JsonFile, RuntimeState, SeenMessages, ensure_private_dir

log = logging.getLogger("assistant")

TELEGRAM_CHUNK = 4000
ERROR_ALERT_INTERVAL = 600  # seconds between "Claude failed" notes to the owner
_COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)


class Outcome(str, Enum):
    PREVIEWED = "previewed"
    SENT = "sent"
    DUPLICATE = "duplicate"
    IGNORED_BOT = "ignored_bot"
    NO_CONNECTION = "no_connection"
    FOREIGN_CONNECTION = "foreign_connection"
    CONNECTION_DISABLED = "connection_disabled"
    UNSUPPORTED = "unsupported"
    OWNER_MESSAGE = "owner_message"
    OWNER_ACTIVE = "owner_active"
    PAUSED = "paused"
    LIMITED = "limited"
    SKIPPED = "skipped"
    PERMISSION_DENIED = "permission_denied"
    ERROR = "error"


@dataclass
class Preview:
    connection_id: str
    chat_id: int
    chat_name: str
    language: Optional[str]
    incoming: str
    reply: str
    note: Optional[str] = None


def print_preview(preview: Preview) -> None:
    lines = [
        "=" * 60,
        f"PREVIEW, not sent | chat {preview.chat_id} ({preview.chat_name}) | lang={preview.language or '?'}",
        f"  They wrote: {preview.incoming}",
        f"  Reply:      {preview.reply}",
    ]
    if preview.note:
        lines.append(f"  Note:       {preview.note}")
    lines.append("=" * 60)
    log.info("\n".join(lines))


def describe(message: Message) -> Optional[str]:
    """Text for the prompt; media becomes a short tag; service messages give None."""
    if message.text:
        return message.text
    caption = f" {message.caption}" if message.caption else ""
    if message.sticker:
        return f"[sticker] {message.sticker.emoji or ''}".rstrip()
    if message.voice:
        return "[voice message]"
    if message.video_note:
        return "[video message]"
    if message.audio:
        return f"[audio]{caption}"
    if message.photo:
        return f"[photo]{caption}"
    if message.animation:
        return f"[GIF]{caption}"
    if message.video:
        return f"[video]{caption}"
    if message.document:
        name = message.document.file_name or "file"
        return f"[file {name}]{caption}"
    if message.location:
        return "[location]"
    if message.contact:
        return "[contact]"
    if message.poll:
        return f"[poll] {message.poll.question}"
    if message.dice:
        return f"[dice] {message.dice.emoji}"
    return None


def can_reply(connection: BusinessConnection) -> bool:
    rights = getattr(connection, "rights", None)
    if rights is not None:
        return bool(getattr(rights, "can_reply", False))
    return bool(getattr(connection, "can_reply", False))


def read_profile(path: Path) -> str:
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError:
        return ""
    return _COMMENT_RE.sub("", text).strip()


def profile_is_empty(path: Path) -> bool:
    """True when profile.md has no filled-in values (only the template)."""
    for line in read_profile(path).splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("-") and line.rstrip().endswith(":"):
            continue
        return False
    return True


def split_text(text: str, size: int = TELEGRAM_CHUNK) -> List[str]:
    return [text[i : i + size] for i in range(0, len(text), size)] or [text]


def _chat_name(message: Message) -> str:
    chat = message.chat
    name = " ".join(p for p in (chat.first_name, chat.last_name) if p) or chat.title or ""
    if not name and message.from_user:
        name = message.from_user.full_name
    return name or str(chat.id)


def _fmt_time(ts: float) -> str:
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M")


class Assistant:
    def __init__(
        self,
        settings: Settings,
        generator: Any,
        *,
        clock: Callable[[], float] = time.time,
        preview_sink: Callable[[Preview], None] = print_preview,
        balance_lookup: Optional[BalanceLookup] = None,
    ) -> None:
        self.settings = settings
        self.balance_lookup = balance_lookup
        self.generator = generator
        self.clock = clock
        self.preview_sink = preview_sink
        data_dir = ensure_private_dir(settings.data_dir)
        self.history = HistoryStore(data_dir / "history.json", settings.history_limit)
        self.seen = SeenMessages(data_dir / "seen.json")
        self.state = RuntimeState(data_dir / "state.json")
        self.previews_path = data_dir / "previews.jsonl"
        self._connections: Dict[str, BusinessConnection] = {}
        self._takeover: Dict[str, float] = {}
        self._locks: Dict[str, asyncio.Lock] = {}
        self._last_error_alert: Optional[float] = None
        self.outcomes: Deque[Outcome] = deque(maxlen=100)

    # ------------------------------------------------------------------ updates

    async def on_business_connection(self, connection: BusinessConnection) -> None:
        self._connections[connection.id] = connection
        if connection.user.id != self.settings.owner_id:
            log.warning(
                "Business connection %s belongs to another account (user=%s); it will be ignored",
                connection.id, connection.user.id,
            )
            return
        log.info(
            "Business connection %s: user=%s enabled=%s can_reply=%s",
            connection.id, connection.user.id, connection.is_enabled, can_reply(connection),
        )

    async def on_business_message(self, bot: Bot, message: Message) -> Outcome:
        outcome = await self._handle(bot, message)
        self.outcomes.append(outcome)
        log.debug("business message %s in chat %s -> %s", message.message_id, message.chat.id, outcome.value)
        return outcome

    async def _handle(self, bot: Bot, message: Message) -> Outcome:
        conn_id = message.business_connection_id
        if not conn_id:
            return Outcome.NO_CONNECTION
        chat_id = message.chat.id
        # Mark as seen before any await so parallel duplicates are dropped.
        if not self.seen.first_time(f"{conn_id}:{chat_id}:{message.message_id}"):
            return Outcome.DUPLICATE
        sender = message.from_user
        if message.sender_business_bot is not None or (sender is not None and sender.is_bot):
            return Outcome.IGNORED_BOT

        connection = self._connections.get(conn_id)
        if connection is None:
            try:
                connection = await bot.get_business_connection(business_connection_id=conn_id)
            except TelegramAPIError as exc:
                log.warning("Could not look up business connection %s: %s", conn_id, exc)
                return Outcome.NO_CONNECTION
            self._connections[conn_id] = connection
        if connection.user.id != self.settings.owner_id:
            return Outcome.FOREIGN_CONNECTION
        if not connection.is_enabled:
            return Outcome.CONNECTION_DISABLED

        text = describe(message)
        if text is None:
            return Outcome.UNSUPPORTED

        key = HistoryStore.key(conn_id, chat_id)
        now = self.clock()
        if sender is not None and sender.id == self.settings.owner_id:
            self.history.add(conn_id, chat_id, "owner", sender.full_name, text, None, now)
            if self.settings.owner_takeover_minutes > 0:
                self._takeover[key] = now + self.settings.owner_takeover_minutes * 60
            return Outcome.OWNER_MESSAGE

        language = detect_language(text) or self.history.last_language(conn_id, chat_id)
        name = sender.full_name if sender is not None else _chat_name(message)
        self.history.add(conn_id, chat_id, "other", name, text, language, now)

        lock = self._locks.setdefault(key, asyncio.Lock())
        async with lock:
            blocked = self._blocked(key)
            if blocked is not None:
                return blocked

            history = self.history.get(conn_id, chat_id)
            crm_note = None
            if self.balance_lookup is not None:
                recent = "\n".join(i["text"] for i in history[-6:] if i.get("role") == "other")
                crm_note = await self.balance_lookup.note_for(key, text, recent)
            system_prompt = build_system_prompt(
                read_profile(self.settings.profile_path), datetime.fromtimestamp(now),
                self.settings.payment_card, self.settings.payment_card_holder,
            )
            user_prompt = build_user_prompt(history, language, crm_note)
            try:
                reply = await self.generator.generate(system_prompt, user_prompt)
            except ClaudeLimitError as exc:
                return await self._on_limit(bot, exc)
            except ClaudeError as exc:
                log.error("Claude failed for chat %s: %s", chat_id, exc)
                await self._alert_error(bot, str(exc))
                return Outcome.ERROR
            except Exception as exc:  # keep polling alive whatever happens
                log.exception("Unexpected error while generating a reply for chat %s", chat_id)
                await self._alert_error(bot, f"{type(exc).__name__}: {exc}")
                return Outcome.ERROR

            reply = (reply or "").strip()
            if not reply or reply.strip('"').strip() == SKIP_TOKEN:
                log.info("Chat %s: no reply needed (%s)", chat_id, SKIP_TOKEN)
                return Outcome.SKIPPED

            preview = Preview(conn_id, chat_id, _chat_name(message), language, text, reply)
            if not self.settings.auto_send:
                self._emit_preview(preview)
                return Outcome.PREVIEWED
            return await self._send(bot, key, connection, preview)

    async def _send(self, bot: Bot, key: str, cached: BusinessConnection, preview: Preview) -> Outcome:
        blocked = self._blocked(key)
        if blocked is not None:
            reasons = {
                Outcome.OWNER_ACTIVE: "not sent: you wrote in this chat while the reply was being generated",
                Outcome.PAUSED: "not sent: the bot was paused",
                Outcome.LIMITED: "not sent: Claude usage limit is active",
            }
            preview.note = reasons.get(blocked, f"not sent: {blocked.value}")
            self._emit_preview(preview)
            return blocked

        # Ask Telegram again right before sending: rights may have changed.
        try:
            fresh = await bot.get_business_connection(business_connection_id=preview.connection_id)
        except TelegramAPIError as exc:
            preview.note = f"not sent: could not re-check the business connection ({exc})"
            self._emit_preview(preview)
            return Outcome.PERMISSION_DENIED
        self._connections[preview.connection_id] = fresh
        problem = None
        if fresh.user.id != self.settings.owner_id:
            problem = "the connection belongs to another account"
        elif not fresh.is_enabled:
            problem = "the business connection is disabled"
        elif not can_reply(fresh):
            problem = "the bot has no permission to reply (enable it in Telegram Business -> Chatbots)"
        if problem:
            preview.note = f"not sent: {problem}"
            log.warning("Chat %s: %s", preview.chat_id, problem)
            self._emit_preview(preview)
            return Outcome.PERMISSION_DENIED

        try:
            for chunk in split_text(preview.reply):
                await bot.send_message(preview.chat_id, chunk, business_connection_id=preview.connection_id)
        except TelegramAPIError as exc:
            log.error("Sending to chat %s failed: %s", preview.chat_id, exc)
            return Outcome.ERROR
        self.history.add(preview.connection_id, preview.chat_id, "owner", "You", preview.reply, None, self.clock())
        log.info("Chat %s: reply sent (%d chars)", preview.chat_id, len(preview.reply))
        return Outcome.SENT

    def _blocked(self, key: str) -> Optional[Outcome]:
        now = self.clock()
        if self.state.paused:
            return Outcome.PAUSED
        until = self.state.limit_until
        if until is not None:
            if until > now:
                return Outcome.LIMITED
            self.state.limit_until = None
        takeover = self._takeover.get(key)
        if takeover is not None:
            if takeover > now:
                return Outcome.OWNER_ACTIVE
            del self._takeover[key]
        return None

    async def _on_limit(self, bot: Bot, exc: ClaudeLimitError) -> Outcome:
        now = self.clock()
        if exc.resets_at and exc.resets_at > now:
            until = exc.resets_at
        else:
            until = now + self.settings.limit_cooldown_minutes * 60
        self.state.limit_until = until
        log.error(
            "Claude subscription usage limit reached; replies paused until %s. CLI said: %s",
            _fmt_time(until), exc.detail,
        )
        if self.settings.auto_send:
            await self._notify_owner(
                bot,
                f"⚠️ Claude obuna limiti tugadi. Bot {_fmt_time(until)} gacha javob bermaydi. "
                "Darrov davom ettirish uchun: /resume",
            )
        return Outcome.LIMITED

    async def _alert_error(self, bot: Bot, detail: str) -> None:
        if not self.settings.auto_send:
            return
        now = self.clock()
        if self._last_error_alert is not None and now - self._last_error_alert < ERROR_ALERT_INTERVAL:
            return
        self._last_error_alert = now
        await self._notify_owner(bot, f"⚠️ Javob tayyorlab bo'lmadi: {detail[:500]}")

    async def _notify_owner(self, bot: Bot, text: str) -> None:
        try:
            await bot.send_message(self.settings.owner_id, text)
        except TelegramAPIError as exc:
            log.warning("Could not notify the owner: %s", exc)

    def _emit_preview(self, preview: Preview) -> None:
        self.preview_sink(preview)
        record = dict(asdict(preview), ts=self.clock())
        try:
            fd = os.open(self.previews_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "a", encoding="utf-8") as handle:
                handle.write(json.dumps(record, ensure_ascii=False) + "\n")
        except OSError as exc:
            log.warning("Could not write %s: %s", self.previews_path, exc)

    # ------------------------------------------------------------ owner control

    def pause(self) -> None:
        self.state.paused = True
        log.info("Paused by the owner")

    def resume(self) -> None:
        self.state.paused = False
        self.state.limit_until = None
        self._takeover.clear()
        log.info("Resumed by the owner")

    def status_text(self) -> str:
        mode = "AUTO-SEND" if self.settings.auto_send else "PREVIEW: replies are only shown in the logs"
        state = "paused" if self.state.paused else "running"
        lines = [f"Mode: {mode}", f"State: {state}"]
        until = self.state.limit_until
        if until is not None and until > self.clock():
            lines.append(f"Claude usage limit: until {_fmt_time(until)}")
        else:
            lines.append("Claude usage limit: none")
        lines.append(f"Chats in history: {self.history.chat_count()}")
        return "\n".join(lines)

    def start_text(self, bot_username: Optional[str] = None) -> str:
        username = f"@{bot_username}" if bot_username else "shu botning username'i"
        mode = (
            "AUTO-SEND — javoblar sizning nomingizdan avtomatik yuboriladi"
            if self.settings.auto_send
            else "PREVIEW — javoblar yuborilmaydi, faqat server logida ko'rinadi"
        )
        state = "pauzada (/resume bilan davom ettiring)" if self.state.paused else "ishlayapti"
        minutes = self.settings.owner_takeover_minutes
        lines = [
            "Salom! Men sizning shaxsiy yordamchingizman.",
            "Sizga kimdir Telegramda yozsa, profile.md dagi ma'lumotlar asosida "
            "sizning uslubingizda javob tayyorlayman.",
            "",
            f"Rejim: {mode}",
            f"Holat: {state}",
            "",
            "Qanday ulanadi:",
            "1. Telegram → Settings → Telegram Business → Chatbots",
            f"2. {username} ni kiriting",
            "3. Bot qaysi chatlarni ko'rishini shu yerda tanlang",
            "4. Xabarlarga javob berish ruxsatini yoqing",
            "(Telegram Premium kerak)",
            "",
            "Buyruqlar:",
            "/pause — javob berishni to'xtatish",
            "/resume — davom ettirish",
            "/status — holat",
            "",
            "Bilib qo'ying:",
        ]
        if minutes > 0:
            lines.append(f"• Chatda o'zingiz yozsangiz, bot o'sha chatda {minutes} daqiqa jim turadi.")
        lines.append("• Pul, qarz va uchrashuvlarga rozilik bermaydi, va'da qilmaydi.")
        lines.append("• profile.md faylini to'ldirish kerak — bot sizni shu fayldan taniydi.")
        if not self.settings.auto_send:
            lines.append("• Javoblarni haqiqatan yuborish uchun AUTO_SEND=true qiling.")
        return "\n".join(lines)


def make_claude_cli(settings: Settings) -> ClaudeCLI:
    return ClaudeCLI(
        settings.claude_bin,
        workdir=settings.data_dir / "claude-run",
        model=settings.claude_model,
        effort=settings.claude_effort,
        timeout=settings.claude_timeout,
        oauth_token=settings.claude_oauth_token,
        auth_label=settings.claude_auth_mode,
    )


def make_balance_lookup(settings: Settings) -> Optional[BalanceLookup]:
    if not settings.edutizim_enabled:
        return None
    client = EduTizimClient(
        settings.edutizim_api_key or "",
        settings.edutizim_token or "",
        branch_ids=settings.edutizim_branch_ids,
        base_url=settings.edutizim_base_url,
    )
    return BalanceLookup(client, path=settings.data_dir / "verified_students.json")


def build_assistant(settings: Settings, **kwargs: Any) -> Assistant:
    kwargs.setdefault("balance_lookup", make_balance_lookup(settings))
    return Assistant(settings, make_claude_cli(settings), **kwargs)
