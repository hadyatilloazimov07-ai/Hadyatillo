"""Runs Claude Code CLI (``claude -p``) as an isolated, tool-less text generator.

Every reply is one ``claude -p`` process. The conversation goes through stdin
only, the system prompt through a private temp file, and no shell is involved.
The subscription token (``claude setup-token``) is passed only through the
child environment. ``--bare`` is never used: it ignores OAuth logins.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import signal
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional

# Every plugin the CLI ships built in (read from the stream-json init event of
# claude 2.1.304). All of them are switched off for the bot.
BUILTIN_PLUGINS = (
    "cc-plugin-agents-md@builtin",
    "cc-plugin-telemetry@builtin",
    "cc-plugin-plugin-authoring@builtin",
)

ISOLATION_SETTINGS: Dict[str, Any] = {
    "disableAllHooks": True,
    "enabledPlugins": {name: False for name in BUILTIN_PLUGINS},
}

STRIPPED_ENV_KEYS = (
    "TELEGRAM_BOT_TOKEN",
    "CLAUDE_CODE_OAUTH_TOKEN",
    "CLAUDE_CODE_USE_BEDROCK",
    "CLAUDE_CODE_USE_VERTEX",
    "CLAUDE_CODE_USE_FOUNDRY",
    "CLAUDECODE",
    "CLAUDE_CODE_ENTRYPOINT",
    "CLAUDE_CODE_SIMPLE",  # set by --bare; disables OAuth login
    "EDUTIZIM_API_KEY",
    "EDUTIZIM_TOKEN",
)

ISOLATION_ENV = {
    "CLAUDE_CODE_DISABLE_AUTO_MEMORY": "1",
    "CLAUDE_CODE_DISABLE_CLAUDE_MDS": "1",
}

REDACTED = "[REDACTED]"

_LIMIT_RE = re.compile(
    r"usage limit|hit your (usage )?limit|limit reached|rate[ _-]?limit|out of (extra )?usage|quota",
    re.IGNORECASE,
)
_AUTH_RE = re.compile(
    r"not logged in|/login|invalid api key|authentication|unauthori[sz]ed|oauth token",
    re.IGNORECASE,
)
_EPOCH_RE = re.compile(r"\|(\d{9,11})")
_RESETS_RE = re.compile(r"(resets?\s+[^\n|.]+)", re.IGNORECASE)


class ClaudeError(Exception):
    """A Claude failure whose message is safe to show to the owner."""


class ClaudeTimeout(ClaudeError):
    pass


class ClaudeAuthError(ClaudeError):
    def __init__(self, message: str, detail: str = "") -> None:
        super().__init__(message)
        self.detail = detail


class ClaudeLimitError(ClaudeError):
    def __init__(self, detail: str, resets_at: Optional[float] = None, reset_text: Optional[str] = None) -> None:
        when = reset_text or "later"
        super().__init__(f"Claude subscription usage limit reached ({when})")
        self.detail = detail
        self.resets_at = resets_at
        self.reset_text = reset_text


def classify_failure(message: str, status: Optional[int] = None) -> ClaudeError:
    """Turn CLI error text into the matching exception (returned, not raised)."""
    text = (message or "").strip()
    if status == 429 or _LIMIT_RE.search(text):
        epoch = _EPOCH_RE.search(text)
        resets_at = float(epoch.group(1)) if epoch else None
        detail = _EPOCH_RE.sub("", text).strip()
        reset = _RESETS_RE.search(detail)
        reset_text = reset.group(1).strip() if reset else None
        return ClaudeLimitError(detail, resets_at=resets_at, reset_text=reset_text)
    if status in (401, 403) or _AUTH_RE.search(text):
        return ClaudeAuthError(
            "Claude CLI is not logged in: run `claude` once on this machine and use /login",
            detail=text[:300],
        )
    return ClaudeError(f"Claude CLI failed: {text[:500] or 'no output'}")


def _last_json_line(stdout: str) -> Optional[Dict[str, Any]]:
    for line in reversed(stdout.splitlines()):
        line = line.strip()
        if not line:
            continue
        try:
            data = json.loads(line)
        except ValueError:
            return None
        return data if isinstance(data, dict) else None
    return None


def parse_result(returncode: int, stdout: str, stderr: str) -> str:
    data = _last_json_line(stdout)
    if data is None:
        raise classify_failure(stderr.strip() or stdout.strip() or f"exit code {returncode}")
    if data.get("is_error") or data.get("subtype") != "success":
        message = " ".join(part for part in (str(data.get("result") or ""), stderr.strip()) if part)
        raise classify_failure(message, data.get("api_error_status"))
    result = str(data.get("result") or "").strip()
    if not result:
        raise ClaudeError("Claude CLI returned an empty reply")
    return result


def build_env(oauth_token: Optional[str] = None, base: Optional[Mapping[str, str]] = None) -> Dict[str, str]:
    source = os.environ if base is None else base
    env = {
        key: value
        for key, value in source.items()
        if not key.startswith("ANTHROPIC_") and key not in STRIPPED_ENV_KEYS
    }
    env.update(ISOLATION_ENV)
    if oauth_token:
        env["CLAUDE_CODE_OAUTH_TOKEN"] = oauth_token
    return env


class ClaudeCLI:
    def __init__(
        self,
        claude_bin: str,
        *,
        workdir: Path,
        model: Optional[str] = None,
        effort: str = "low",
        timeout: float = 120,
        oauth_token: Optional[str] = None,
        auth_label: Optional[str] = None,
    ) -> None:
        self.claude_bin = claude_bin
        self.workdir = Path(workdir)
        self.model = model
        self.effort = effort
        self.timeout = timeout
        self._oauth_token = oauth_token
        self._auth_label = auth_label

    def __repr__(self) -> str:
        return (
            f"ClaudeCLI(claude_bin={self.claude_bin!r}, model={self.model!r}, "
            f"effort={self.effort!r}, auth={self.auth_mode!r})"
        )

    @property
    def auth_mode(self) -> str:
        if self._auth_label:
            return self._auth_label
        return "subscription token" if self._oauth_token else "this machine's claude login"

    def build_command(self, system_prompt_file: str, output_format: str = "json") -> List[str]:
        cmd = [
            self.claude_bin, "-p",
            "--safe-mode",
            "--settings", json.dumps(ISOLATION_SETTINGS),
            "--tools", "",
            "--strict-mcp-config",
            "--setting-sources", "",
            "--disable-slash-commands",
            "--no-session-persistence",
            "--system-prompt-file", system_prompt_file,
            "--output-format", output_format,
            "--effort", self.effort,
        ]
        if output_format == "stream-json":
            cmd.append("--verbose")
        if self.model:
            cmd += ["--model", self.model]
        return cmd

    def _redact(self, text: str) -> str:
        if self._oauth_token and text:
            return text.replace(self._oauth_token, REDACTED)
        return text

    def _resolve_bin(self) -> str:
        path = Path(self.claude_bin)
        if path.is_file() and os.access(path, os.X_OK):
            return str(path)
        found = shutil.which(self.claude_bin)
        if found:
            return found
        raise ClaudeError(
            f"claude binary not found at {self.claude_bin}; install Claude Code CLI or set CLAUDE_BIN"
        )

    def _translate_auth(self, exc: ClaudeError) -> ClaudeError:
        if isinstance(exc, ClaudeAuthError) and self._oauth_token:
            detail = self._redact(exc.detail)
            return ClaudeAuthError(
                "Claude rejected the subscription token or it has expired: run `claude setup-token` "
                "on a computer with a browser and update CLAUDE_CODE_OAUTH_TOKEN. "
                f"CLI said: {detail}",
                detail=detail,
            )
        return exc

    async def _run(self, system_prompt: str, stdin_text: str, output_format: str) -> tuple:
        claude_bin = self._resolve_bin()
        self.workdir.mkdir(parents=True, exist_ok=True)
        os.chmod(self.workdir, 0o700)
        fd, prompt_path = tempfile.mkstemp(prefix="system-", suffix=".txt", dir=str(self.workdir))
        run_dir = None
        proc = None
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(system_prompt)
            os.chmod(prompt_path, 0o600)
            run_dir = tempfile.mkdtemp(prefix="run-", dir=str(self.workdir))
            cmd = self.build_command(prompt_path, output_format)
            cmd[0] = claude_bin
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=run_dir,
                env=build_env(self._oauth_token),
                start_new_session=True,
            )
            try:
                out, err = await asyncio.wait_for(
                    proc.communicate(stdin_text.encode("utf-8")), timeout=self.timeout
                )
            except asyncio.TimeoutError:
                await self._kill(proc)
                raise ClaudeTimeout(f"Claude CLI did not answer within {self.timeout:g}s") from None
            except asyncio.CancelledError:
                await self._kill(proc)
                raise
            return (
                proc.returncode,
                self._redact(out.decode("utf-8", "replace")),
                self._redact(err.decode("utf-8", "replace")),
            )
        except FileNotFoundError:
            raise ClaudeError(f"claude binary not found at {self.claude_bin}") from None
        finally:
            try:
                os.unlink(prompt_path)
            except OSError:
                pass
            if run_dir:
                shutil.rmtree(run_dir, ignore_errors=True)

    @staticmethod
    async def _kill(proc: "asyncio.subprocess.Process") -> None:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
        try:
            await asyncio.wait_for(proc.wait(), timeout=5)
        except (asyncio.TimeoutError, ProcessLookupError):
            pass

    async def generate(self, system_prompt: str, user_prompt: str) -> str:
        returncode, out, err = await self._run(system_prompt, user_prompt, "json")
        try:
            return parse_result(returncode, out, err)
        except ClaudeError as exc:
            raise self._translate_auth(exc) from None

    async def inspect(self) -> Dict[str, Any]:
        """Send a tiny request and report what the CLI loaded and which auth it used."""
        returncode, out, err = await self._run("Reply with exactly: OK", "ping", "stream-json")
        init: Optional[Dict[str, Any]] = None
        for line in out.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if isinstance(event, dict) and event.get("type") == "system" and event.get("subtype") == "init":
                init = {
                    key: event.get(key)
                    for key in ("tools", "mcp_servers", "plugins", "skills", "apiKeySource", "apiProvider", "model")
                }
        try:
            reply: Optional[str] = parse_result(returncode, out, err)
        except ClaudeError as exc:
            raise self._translate_auth(exc) from None
        return {"auth": self.auth_mode, "init": init, "reply": reply}
