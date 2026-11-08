from __future__ import annotations

import asyncio
import json
import os
import stat

from aiogram.exceptions import TelegramBadRequest
from aiogram.methods import GetBusinessConnection, SendMessage

from assistant.claude_cli import ClaudeAuthError, ClaudeLimitError, ClaudeTimeout
from assistant.prompts import SKIP_TOKEN

from .conftest import CONN_ID, OWNER_ID, STRANGER_ID

AUTO = {"AUTO_SEND": "true"}


# ----------------------------------------------------------------- preview mode

async def test_preview_shows_reply_and_sends_nothing(make_harness):
    h = make_harness()
    await h.connect()
    await h.business_message("Salom, qalesan?")
    assert h.outcomes == ["previewed"]
    assert len(h.previews) == 1
    assert h.previews[0].reply == "Hi! Talk soon."
    assert h.previews[0].incoming == "Salom, qalesan?"
    assert h.session.requests == []  # known connection: no Telegram calls at all

    path = h.settings.data_dir / "previews.jsonl"
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    record = json.loads(path.read_text(encoding="utf-8").splitlines()[0])
    assert record["reply"] == "Hi! Talk soon."
    # The unsent reply is not part of the history.
    roles = [i["role"] for i in h.assistant.history.get(CONN_ID, STRANGER_ID)]
    assert roles == ["other"]


async def test_unknown_connection_is_looked_up_once_then_cached(make_harness):
    h = make_harness()
    h.set_connection()
    await h.business_message("Hi", message_id=1)
    await h.business_message("Are you there?", message_id=2)
    assert h.outcomes == ["previewed", "previewed"]
    assert [type(r) for r in h.session.requests] == [GetBusinessConnection]


async def test_connection_lookup_failure(make_harness):
    h = make_harness()
    await h.business_message("Hi")
    assert h.outcomes == ["no_connection"]
    assert h.generator.calls == []


async def test_foreign_and_disabled_connections_do_not_call_claude(make_harness):
    h = make_harness()
    await h.connect(user_id=999)
    await h.business_message("Hi")
    assert h.outcomes == ["foreign_connection"]

    h2 = make_harness()
    await h2.connect(enabled=False)
    await h2.business_message("Hi", message_id=99)  # same data dir: use a fresh id
    assert h2.outcomes == ["connection_disabled"]
    assert h.generator.calls == [] and h2.generator.calls == []


async def test_any_chat_gets_a_reply_without_allowlist(make_harness):
    h = make_harness()
    await h.connect()
    for chat in (501, 777777, 123456789):
        await h.business_message("Hello", from_id=chat)
    assert h.outcomes == ["previewed"] * 3


# --------------------------------------------------------------- owner takeover

async def test_owner_message_recorded_and_takeover(make_harness):
    h = make_harness()
    await h.connect()
    await h.business_message("I'll call you later", from_id=OWNER_ID, chat_id=STRANGER_ID)
    assert h.outcomes == ["owner_message"]
    assert h.generator.calls == []
    assert h.assistant.history.get(CONN_ID, STRANGER_ID)[0]["role"] == "owner"

    await h.business_message("ok, when?")
    assert h.outcomes[-1] == "owner_active"
    assert h.generator.calls == []

    h.clock.advance(15 * 60 + 1)
    await h.business_message("hello?")
    assert h.outcomes[-1] == "previewed"
    assert "[You]: I'll call you later" in h.generator.calls[0]["user"]


async def test_bot_messages_are_ignored(make_harness):
    h = make_harness()
    await h.connect()
    await h.business_message("auto reply", from_id=OWNER_ID, via_business_bot=True)
    await h.business_message("I am a bot", is_bot=True)
    assert h.outcomes == ["ignored_bot", "ignored_bot"]
    assert h.generator.calls == []


# ------------------------------------------------------------------- duplicates

async def test_duplicates_parallel_sequential_and_after_restart(make_harness):
    h = make_harness()
    await h.connect()
    payload = h.business_payload("Salom", message_id=42)
    await asyncio.gather(h.feed(payload), h.feed(payload))
    await h.feed(payload)
    assert sorted(h.outcomes) == ["duplicate", "duplicate", "previewed"]
    h.restart()
    await h.connect()
    await h.feed(payload)
    assert h.outcomes == ["duplicate"]
    assert len(h.generator.calls) == 1


# ---------------------------------------------------------------- message kinds

async def test_service_message_sticker_and_voice(make_harness):
    h = make_harness()
    await h.connect()
    await h.business_message(None, extra={"new_chat_title": "x"})
    assert h.outcomes == ["unsupported"]
    await h.business_message(
        None,
        extra={"sticker": {"file_id": "s", "file_unique_id": "s", "type": "regular", "width": 1,
                           "height": 1, "is_animated": False, "is_video": False, "emoji": "😀"}},
    )
    await h.business_message(None, extra={"voice": {"file_id": "v", "file_unique_id": "v", "duration": 3}})
    assert h.outcomes[1:] == ["previewed", "previewed"]
    assert h.previews[0].incoming == "[sticker] 😀"
    assert h.previews[1].incoming == "[voice message]"


async def test_history_is_separate_per_connection_and_chat(make_harness):
    h = make_harness()
    await h.connect()
    await h.feed({"business_connection": {**h.session.connection, "id": "conn-2"}})
    await h.business_message("secret for chat A", from_id=601)
    await h.business_message("hello from chat B", from_id=602)
    await h.business_message("hello on another connection", from_id=601, connection_id="conn-2")
    prompts = [c["user"] for c in h.generator.calls]
    assert "secret for chat A" not in prompts[1]
    assert "secret for chat A" not in prompts[2]
    assert h.assistant.history.chat_count() == 3


async def test_language_hints(make_harness):
    h = make_harness()
    await h.connect()
    await h.business_message("Привет, как дела?", from_id=701)
    await h.business_message("Hi, are you free tonight?", from_id=702)
    await h.business_message("Bugun bo'shmisan?", from_id=703)
    await h.business_message("👍", from_id=701)
    assert [p.language for p in h.previews] == ["ru", "en", "uz", "ru"]
    assert h.generator.calls[0]["user"].endswith("Their latest message is in Russian.")
    assert h.generator.calls[3]["user"].endswith("Their latest message is in Russian.")


async def test_skip_is_not_shown(make_harness):
    h = make_harness(items=[SKIP_TOKEN])
    await h.connect()
    await h.business_message("ok")
    assert h.outcomes == ["skipped"]
    assert h.previews == []


# -------------------------------------------------------------------- auto-send

async def test_auto_send_rechecks_connection_then_sends(make_harness):
    h = make_harness(AUTO)
    await h.connect()
    await h.business_message("Salom")
    assert h.outcomes == ["sent"]
    kinds = [type(r) for r in h.session.requests]
    assert kinds == [GetBusinessConnection, SendMessage]
    sent = h.session.sent[0]
    assert sent.business_connection_id == CONN_ID and sent.chat_id == STRANGER_ID
    history = h.assistant.history.get(CONN_ID, STRANGER_ID)
    assert [i["role"] for i in history] == ["other", "owner"]
    assert history[-1]["text"] == "Hi! Talk soon."


async def test_auto_send_splits_long_reply(make_harness):
    h = make_harness(AUTO, items=["x" * 9000])
    await h.connect()
    await h.business_message("tell me everything")
    assert [len(m.text) for m in h.session.sent] == [4000, 4000, 1000]


async def test_auto_send_refuses_without_rights(make_harness):
    h = make_harness(AUTO)
    await h.connect()
    h.set_connection(can_reply=False)
    await h.business_message("Salom", message_id=1)
    h.set_connection(enabled=False)
    await h.business_message("Salom?", message_id=2)
    assert h.outcomes == ["permission_denied", "permission_denied"]
    assert h.session.sent == []
    assert all(p.note and "not sent" in p.note for p in h.previews)


async def test_auto_send_stops_if_owner_writes_during_generation(make_harness):
    h = make_harness(AUTO)
    await h.connect()

    async def owner_writes():
        await h.business_message("I'm here, answering myself", from_id=OWNER_ID, chat_id=STRANGER_ID)

    h.generator.hook = owner_writes
    await h.business_message("Salom")
    assert h.outcomes == ["owner_message", "owner_active"]
    assert h.session.sent == []
    assert h.previews[0].note and "not sent" in h.previews[0].note


# ---------------------------------------------------------------- owner control

async def test_pause_resume_owner_only_and_persistent(make_harness):
    h = make_harness()
    await h.connect()
    await h.direct_message("/pause", from_id=STRANGER_ID)
    assert h.assistant.state.paused is False
    assert h.session.sent == []

    await h.direct_message("/pause")
    assert h.assistant.state.paused is True
    h.restart()
    await h.connect()
    await h.business_message("Salom")
    assert h.outcomes == ["paused"]

    await h.direct_message("/resume", from_id=STRANGER_ID)
    await h.direct_message("hello bot", from_id=STRANGER_ID)
    assert all(m.chat_id == OWNER_ID for m in h.session.sent)
    await h.direct_message("/resume")
    assert h.assistant.state.paused is False
    await h.direct_message("/status")
    assert "Mode: PREVIEW" in h.session.sent[-1].text


async def test_start_for_owner_and_stranger(make_harness):
    h = make_harness()
    await h.direct_message("/start")
    text = h.session.sent[-1].text
    assert "PREVIEW" in text and "ishlayapti" in text
    assert "Chatbots" in text and "/pause" in text and "/resume" in text and "/status" in text
    assert "@my_assistant_bot" in text
    assert "AUTO_SEND=true" in text

    await h.direct_message("/start", from_id=STRANGER_ID)
    stranger_text = h.session.sent[-1].text
    assert h.session.sent[-1].chat_id == STRANGER_ID
    assert "faqat egasi uchun" in stranger_text
    assert "Test Owner" not in stranger_text
    assert h.generator.calls == []

    h2 = make_harness(AUTO)
    h2.assistant.pause()
    await h2.direct_message("/start")
    text2 = h2.session.sent[-1].text
    assert "AUTO-SEND" in text2 and "pauzada" in text2


# ------------------------------------------------------------------------ limits

async def test_limit_waits_until_reset_and_persists(make_harness):
    h = make_harness()
    clock_reset = 1_800_000_000.0 + 3600
    h.generator.items = [ClaudeLimitError("usage limit reached", resets_at=clock_reset)]
    await h.connect()
    await h.business_message("one", message_id=1)
    await h.business_message("two", message_id=2)
    assert h.outcomes == ["limited", "limited"]
    assert len(h.generator.calls) == 1

    h.restart()
    await h.connect()
    await h.business_message("three", message_id=3)
    assert h.outcomes == ["limited"]

    h.clock.advance(3601)
    await h.business_message("four", message_id=4)
    assert h.outcomes[-1] == "previewed"
    assert h.session.sent == []  # preview mode: no owner alert


async def test_limit_without_reset_uses_cooldown_and_resume_clears(make_harness):
    h = make_harness(AUTO)
    h.generator.items = [ClaudeLimitError("rate limit")]
    await h.connect()
    await h.business_message("one", message_id=1)
    assert h.outcomes == ["limited"]
    assert h.assistant.state.limit_until == h.clock.now + 30 * 60
    owner_alerts = [m for m in h.session.sent if m.chat_id == OWNER_ID]
    assert len(owner_alerts) == 1 and "limit" in owner_alerts[0].text

    h.clock.advance(60)
    await h.business_message("two", message_id=2)
    assert h.outcomes[-1] == "limited"
    await h.direct_message("/resume")
    assert h.assistant.state.limit_until is None
    await h.business_message("three", message_id=3)
    assert h.outcomes[-1] == "sent"


async def test_timeout_and_auth_errors_send_nothing_to_chat(make_harness):
    h = make_harness(AUTO)
    h.generator.items = [ClaudeTimeout("slow"), ClaudeAuthError("not logged in"), ClaudeTimeout("slow")]
    await h.connect()
    for i in range(3):
        await h.business_message(f"msg {i}", message_id=i + 1)
    assert h.outcomes == ["error", "error", "error"]
    assert all(m.chat_id == OWNER_ID for m in h.session.sent)
    assert len(h.session.sent) == 1  # the owner is told once, not on every failure
