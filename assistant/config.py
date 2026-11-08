"""Settings loading.

Settings come from exactly one source and are never mixed:

* local: only the ``.env`` file (read with ``dotenv_values``; the shell
  environment is ignored on purpose);
* container: a mapping passed as ``source`` (``bot.py --settings-from-env``
  builds it from the process environment).

Nothing is ever written to ``os.environ``.
"""
from __future__ import annotations

import os
import re
import shutil
import stat
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Mapping, Optional

from dotenv import dotenv_values

PROJECT_DIR = Path(__file__).resolve().parent.parent

SETTING_KEYS = (
    "TELEGRAM_BOT_TOKEN",
    "OWNER_ID",
    "AUTO_SEND",
    "CLAUDE_CODE_OAUTH_TOKEN",
    "CLAUDE_BIN",
    "CLAUDE_MODEL",
    "CLAUDE_EFFORT",
    "CLAUDE_TIMEOUT_SECONDS",
    "LIMIT_COOLDOWN_MINUTES",
    "OWNER_TAKEOVER_MINUTES",
    "HISTORY_LIMIT",
    "DATA_DIR",
    "PROFILE_PATH",
)

EFFORT_LEVELS = ("low", "medium", "high", "xhigh", "max")
_TRUE = {"true", "1", "yes", "on"}
_FALSE = {"false", "0", "no", "off"}
_BOT_TOKEN_RE = re.compile(r"^\d+:[A-Za-z0-9_-]+$")

AUTH_MACHINE = "this machine's claude login"
AUTH_ENV_FILE = "subscription token from .env"
AUTH_ENVIRONMENT = "subscription token from environment variables"


class ConfigError(Exception):
    """Invalid or missing configuration. Messages never echo secret values."""


@dataclass(frozen=True)
class Settings:
    bot_token: str = field(repr=False)
    owner_id: int
    auto_send: bool
    claude_bin: str
    claude_model: Optional[str]
    claude_effort: str
    claude_timeout: int
    limit_cooldown_minutes: int
    owner_takeover_minutes: int
    history_limit: int
    data_dir: Path
    profile_path: Path
    claude_oauth_token: Optional[str] = field(default=None, repr=False)
    from_environment: bool = False

    @property
    def claude_auth_mode(self) -> str:
        if not self.claude_oauth_token:
            return AUTH_MACHINE
        return AUTH_ENVIRONMENT if self.from_environment else AUTH_ENV_FILE


def _where(env_file: Path, from_environment: bool) -> str:
    return "the environment variables" if from_environment else str(env_file)


def _bool(raw: str, key: str, default: bool, where: str) -> bool:
    value = raw.strip().lower()
    if not value:
        return default
    if value in _TRUE:
        return True
    if value in _FALSE:
        return False
    raise ConfigError(f"{key} must be true or false (check {where})")


def _int(raw: str, key: str, default: int, minimum: int, where: str) -> int:
    value = raw.strip()
    if not value:
        return default
    try:
        number = int(value)
    except ValueError:
        raise ConfigError(f"{key} must be a whole number (check {where})") from None
    if number < minimum:
        raise ConfigError(f"{key} must be at least {minimum} (check {where})")
    return number


def _path(raw: str, default: str, base: Path) -> Path:
    value = raw.strip() or default
    path = Path(value).expanduser()
    return path if path.is_absolute() else (base / path)


def _claude_bin(raw: str) -> str:
    value = raw.strip()
    if value:
        return str(Path(value).expanduser())
    found = shutil.which("claude")
    if found:
        return found
    return str(Path.home() / ".local" / "bin" / "claude")


def _oauth_token(raw: str, where: str) -> Optional[str]:
    value = raw.strip()
    if not value:
        return None
    if value.startswith("sk-ant-api"):
        raise ConfigError(
            "CLAUDE_CODE_OAUTH_TOKEN holds an Anthropic API key, not a subscription token. "
            f"Run `claude setup-token` and put its sk-ant-oat... token there (check {where})"
        )
    if not value.startswith("sk-ant-oat") or any(ch.isspace() for ch in value):
        raise ConfigError(
            "CLAUDE_CODE_OAUTH_TOKEN does not look like a `claude setup-token` token "
            f"(expected sk-ant-oat... on a single line; check {where})"
        )
    return value


def load_settings(
    env_file: Path,
    *,
    require_telegram: bool = True,
    overrides: Optional[Mapping[str, str]] = None,
    source: Optional[Mapping[str, str]] = None,
) -> Settings:
    env_file = Path(env_file)
    base = env_file.resolve().parent
    from_environment = source is not None

    values: Dict[str, str] = {}
    if from_environment:
        values.update({k: v for k, v in source.items() if v is not None})
    else:
        if not env_file.exists() and not overrides:
            raise ConfigError(
                f"{env_file} not found. Create it with: cp .env.example .env && chmod 600 .env"
            )
        if env_file.exists():
            values.update({k: v for k, v in dotenv_values(env_file).items() if v is not None})
    if overrides:
        values.update(overrides)

    where = _where(env_file, from_environment)

    def get(key: str) -> str:
        return str(values.get(key, "") or "")

    bot_token = get("TELEGRAM_BOT_TOKEN").strip()
    owner_raw = get("OWNER_ID").strip()
    if require_telegram:
        if not bot_token:
            raise ConfigError(f"TELEGRAM_BOT_TOKEN is empty (check {where})")
        if not _BOT_TOKEN_RE.match(bot_token):
            raise ConfigError(
                f"TELEGRAM_BOT_TOKEN does not look like a BotFather token <digits>:<text> (check {where})"
            )
        if not owner_raw:
            raise ConfigError(f"OWNER_ID is empty (check {where})")
    owner_id = 0
    if owner_raw:
        if not owner_raw.isdigit():
            raise ConfigError(f"OWNER_ID must be your numeric Telegram user id (check {where})")
        owner_id = int(owner_raw)

    effort = get("CLAUDE_EFFORT").strip().lower() or "low"
    if effort not in EFFORT_LEVELS:
        raise ConfigError(f"CLAUDE_EFFORT must be one of {', '.join(EFFORT_LEVELS)} (check {where})")

    return Settings(
        bot_token=bot_token,
        owner_id=owner_id,
        auto_send=_bool(get("AUTO_SEND"), "AUTO_SEND", False, where),
        claude_bin=_claude_bin(get("CLAUDE_BIN")),
        claude_model=get("CLAUDE_MODEL").strip() or None,
        claude_effort=effort,
        claude_timeout=_int(get("CLAUDE_TIMEOUT_SECONDS"), "CLAUDE_TIMEOUT_SECONDS", 120, 1, where),
        limit_cooldown_minutes=_int(get("LIMIT_COOLDOWN_MINUTES"), "LIMIT_COOLDOWN_MINUTES", 30, 1, where),
        owner_takeover_minutes=_int(get("OWNER_TAKEOVER_MINUTES"), "OWNER_TAKEOVER_MINUTES", 15, 0, where),
        history_limit=_int(get("HISTORY_LIMIT"), "HISTORY_LIMIT", 30, 1, where),
        data_dir=_path(get("DATA_DIR"), "data", base),
        profile_path=_path(get("PROFILE_PATH"), "profile.md", base),
        claude_oauth_token=_oauth_token(get("CLAUDE_CODE_OAUTH_TOKEN"), where),
        from_environment=from_environment,
    )


def env_file_warning(path: Path) -> Optional[str]:
    """Return a warning if the .env file is readable by group or others."""
    try:
        mode = os.stat(path).st_mode
    except OSError:
        return None
    if mode & (stat.S_IRWXG | stat.S_IRWXO):
        return (
            f"{path} is readable by other users (mode {stat.S_IMODE(mode):o}); "
            f"it holds secrets, run: chmod 600 {path}"
        )
    return None
