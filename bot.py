"""Entry point.

    python bot.py                       run the bot (settings from .env)
    python bot.py --settings-from-env   run in a container (settings from environment variables)
    python bot.py --check-claude        check the Claude CLI setup without Telegram
    python bot.py --try "text"          draft a reply to a made-up message without Telegram

Exit codes: 0 ok, 1 --check-claude/--try failed, 2 configuration or Telegram token error.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

from assistant.claude_cli import ClaudeError
from assistant.config import PROJECT_DIR, SETTING_KEYS, ConfigError, Settings, env_file_warning, load_settings
from assistant.language import detect_language
from assistant.prompts import SKIP_TOKEN, build_system_prompt, build_user_prompt, clean_reply
from assistant.service import build_assistant, make_balance_lookup, make_claude_cli, profile_is_empty, read_profile
from assistant.store import ensure_private_dir

ENV_FILE = PROJECT_DIR / ".env"
BAD_KEY_SOURCES = {"ANTHROPIC_API_KEY", "apiKeyHelper", "/login managed key"}

log = logging.getLogger("bot")


def settings_from_process_env(*, require_telegram: bool = True, env_file: Path = ENV_FILE) -> Settings:
    """Read SETTING_KEYS from os.environ, then remove them (even on error)."""
    try:
        source = {key: os.environ[key] for key in SETTING_KEYS if key in os.environ}
        return load_settings(env_file, require_telegram=require_telegram, source=source)
    finally:
        for key in SETTING_KEYS:
            os.environ.pop(key, None)


def _load(args: argparse.Namespace, require_telegram: bool) -> Settings:
    if args.settings_from_env:
        return settings_from_process_env(require_telegram=require_telegram)
    warning = env_file_warning(ENV_FILE)
    if warning:
        log.warning(warning)
    return load_settings(ENV_FILE, require_telegram=require_telegram)


def _prepare_dirs(settings: Settings) -> None:
    ensure_private_dir(settings.data_dir)
    ensure_private_dir(settings.data_dir / "claude-run")


def check_problems(report: Dict) -> List[str]:
    problems = []
    init = report.get("init")
    if not init:
        return ["the CLI did not emit an init event"]
    for key in ("tools", "mcp_servers", "plugins", "skills"):
        if init.get(key):
            problems.append(f"{key} should be empty but is {init.get(key)!r}")
    if init.get("apiKeySource") in BAD_KEY_SOURCES:
        problems.append(f"apiKeySource is {init.get('apiKeySource')!r}: an API key is in use, not the subscription")
    if init.get("apiProvider") not in ("firstParty", None):
        problems.append(f"apiProvider is {init.get('apiProvider')!r}, expected firstParty")
    return problems


async def run_check(settings: Settings) -> int:
    cli = make_claude_cli(settings)
    try:
        report = await cli.inspect()
    except ClaudeError as exc:
        print(json.dumps({"auth": settings.claude_auth_mode, "error": str(exc)}, indent=2, ensure_ascii=False))
        return 1
    print(json.dumps(report, indent=2, ensure_ascii=False))
    problems = check_problems(report)
    if problems:
        for problem in problems:
            print(f"FAIL: {problem}", file=sys.stderr)
        return 1
    print(f"OK: Claude answered using {report['auth']} (claude at {settings.claude_bin})")
    return 0


async def run_try(settings: Settings, text: str) -> int:
    cli = make_claude_cli(settings)
    language = detect_language(text)
    system_prompt = build_system_prompt(
        read_profile(settings.profile_path), datetime.now(), settings.payment_card, settings.payment_card_holder,
        settings.finance_phone, settings.service_phone,
    )
    crm_note = None
    lookup = make_balance_lookup(settings)
    if lookup is not None:
        crm_note = await lookup.note_for("try", text, text)
    user_prompt = build_user_prompt([{"role": "other", "name": "Friend", "text": text}], language, crm_note)
    try:
        reply = clean_reply(await cli.generate(system_prompt, user_prompt))
    except ClaudeError as exc:
        print(f"Claude error: {exc}", file=sys.stderr)
        return 1
    print(f"Language hint: {language or 'unknown'}")
    if crm_note:
        print(f"EduTizim:      {crm_note}")
    print(f"Message:       {text}")
    print(f"Reply:         {reply if reply != SKIP_TOKEN else '(no reply needed: ' + SKIP_TOKEN + ')'}")
    return 0


async def run_bot(settings: Settings) -> int:
    from aiogram import Bot, Dispatcher
    from aiogram.exceptions import TelegramUnauthorizedError

    from assistant.handlers import ALLOWED_UPDATES, build_router

    assistant = build_assistant(settings)
    bot = Bot(token=settings.bot_token)
    try:
        try:
            me = await bot.get_me()
        except TelegramUnauthorizedError:
            log.error("Telegram rejected TELEGRAM_BOT_TOKEN. Check it with @BotFather.")
            return 2
        log.info("Bot @%s is running", me.username)
        if settings.auto_send:
            log.warning("Mode: AUTO-SEND - replies are sent from your account automatically")
        else:
            log.info("Mode: PREVIEW - replies are only shown in the logs (set AUTO_SEND=true to send)")
        log.info("Claude auth: %s", settings.claude_auth_mode)
        log.info("Claude binary: %s", settings.claude_bin)
        log.info("Payment card for online payments: %s", "set" if settings.payment_card else "not set")
        log.info("Finance phone: %s; service phone: %s", settings.finance_phone or "not set", settings.service_phone or "not set")
        if settings.edutizim_enabled:
            log.info("EduTizim balance lookup: ON (%s)", settings.edutizim_base_url)
        else:
            log.info("EduTizim balance lookup: off (EDUTIZIM_API_KEY / EDUTIZIM_TOKEN not set)")
        if assistant.state.paused:
            log.warning("The bot starts PAUSED; send /resume to the bot to continue")
        if profile_is_empty(settings.profile_path):
            log.warning("%s is not filled in yet; replies will be generic", settings.profile_path)

        dp = Dispatcher()
        dp.include_router(build_router(assistant))
        await dp.start_polling(bot, allowed_updates=ALLOWED_UPDATES)
        return 0
    finally:
        await bot.session.close()


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Telegram Business personal assistant")
    parser.add_argument("--settings-from-env", action="store_true", help="read settings from environment variables (container)")
    parser.add_argument("--check-claude", action="store_true", help="check the Claude CLI setup, no Telegram")
    parser.add_argument("--try", dest="try_text", metavar="TEXT", help="draft a reply to TEXT, no Telegram")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    logging.Formatter.converter = time.localtime

    offline = args.check_claude or args.try_text is not None
    try:
        settings = _load(args, require_telegram=not offline)
    except ConfigError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 2
    _prepare_dirs(settings)

    if args.check_claude:
        return asyncio.run(run_check(settings))
    if args.try_text is not None:
        return asyncio.run(run_try(settings, args.try_text))
    try:
        return asyncio.run(run_bot(settings))
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    sys.exit(main())
