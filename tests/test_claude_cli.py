from __future__ import annotations

import json
import os
import stat
import sys
import textwrap
import time
from pathlib import Path

import pytest

from assistant.claude_cli import (
    ISOLATION_ENV,
    ClaudeAuthError,
    ClaudeCLI,
    ClaudeError,
    ClaudeLimitError,
    ClaudeTimeout,
    classify_failure,
    parse_result,
)

TOKEN = "sk-ant-oat01-" + "A1b2C3d4E5" * 6

FAKE_CLAUDE = r'''
import json, os, subprocess, sys, time

argv = sys.argv[1:]
stdin = sys.stdin.read()
mode = os.environ.get("FAKE_MODE", "ok")
record_path = os.environ["FAKE_RECORD"]
spf = argv[argv.index("--system-prompt-file") + 1]
cwd = os.getcwd()
record = {
    "argv": argv,
    "stdin": stdin,
    "env": dict(os.environ),
    "cwd": cwd,
    "cwd_listing": os.listdir(cwd),
    "cwd_mode": oct(os.stat(cwd).st_mode & 0o777),
    "pid": os.getpid(),
    "system_prompt_path": spf,
    "system_prompt": open(spf).read(),
    "system_prompt_mode": oct(os.stat(spf).st_mode & 0o777),
}
with open(record_path, "w") as fh:
    json.dump(record, fh)

fmt = argv[argv.index("--output-format") + 1]

def result(text, is_error=False, status=None):
    print(json.dumps({"type": "result", "subtype": "success", "is_error": is_error,
                      "result": text, "api_error_status": status}))

if fmt == "stream-json":
    print(json.dumps({"type": "system", "subtype": "init", "tools": [], "mcp_servers": [],
                      "plugins": [], "skills": [], "apiKeySource": "none",
                      "apiProvider": "firstParty", "model": "fake"}))
token = os.environ.get("CLAUDE_CODE_OAUTH_TOKEN", "")
if mode == "ok":
    result("  Hello there  \n")
elif mode == "sleep":
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    with open(record_path + ".child", "w") as fh:
        fh.write(str(child.pid))
    time.sleep(60)
elif mode == "limit_epoch":
    result("Claude AI usage limit reached|1999999999", True)
elif mode == "limit_text":
    result("You've hit your limit · resets 3pm (Asia/Tashkent)", True)
elif mode == "auth":
    result("Invalid API key: OAuth token " + token + " was rejected", True, 401)
elif mode == "crash":
    sys.stderr.write("boom: something broke\n")
    sys.exit(3)
elif mode == "empty":
    result("")
elif mode == "echo_token":
    result("token=" + (token or "none"))
'''


@pytest.fixture
def fake(tmp_path, monkeypatch):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    script = bin_dir / "claude"
    # The resolved interpreter path has no spaces, so it works as a shebang.
    script.write_text(f"#!{os.path.realpath(sys.executable)}\n" + FAKE_CLAUDE, encoding="utf-8")
    script.chmod(0o755)
    record = tmp_path / "record.json"
    monkeypatch.setenv("FAKE_RECORD", str(record))
    monkeypatch.setenv("FAKE_MODE", "ok")

    class Fake:
        path = script
        workdir = tmp_path / "data" / "claude-run"

        def cli(self, **kwargs):
            kwargs.setdefault("workdir", self.workdir)
            kwargs.setdefault("timeout", 10)
            return ClaudeCLI(str(script), **kwargs)

        def mode(self, value):
            monkeypatch.setenv("FAKE_MODE", value)

        def record(self):
            return json.loads(record.read_text())

        def child_pid(self):
            return int(Path(str(record) + ".child").read_text())

    return Fake()


async def test_reply_is_stripped(fake):
    assert await fake.cli().generate("system", "hi") == "Hello there"


async def test_text_only_on_stdin_and_profile_in_private_file(fake):
    cli = fake.cli()
    await cli.generate("PROFILE: secret hobby", "the message text")
    rec = fake.record()
    assert rec["stdin"] == "the message text"
    assert not any("the message text" in a for a in rec["argv"])
    assert not any("secret hobby" in a for a in rec["argv"])
    assert rec["system_prompt"] == "PROFILE: secret hobby"
    assert rec["system_prompt_mode"] == "0o600"
    assert not Path(rec["system_prompt_path"]).exists()
    assert os.listdir(fake.workdir) == []


async def test_temp_files_removed_on_error(fake):
    fake.mode("crash")
    with pytest.raises(ClaudeError):
        await fake.cli().generate("system", "hi")
    assert os.listdir(fake.workdir) == []


async def test_no_shell(fake, tmp_path):
    target = tmp_path / "pwned"
    text = f"$(touch {target}) `touch {target}`; touch {target}"
    await fake.cli().generate("system", text)
    assert not target.exists()
    assert fake.record()["stdin"] == text


async def test_isolation_flags(fake):
    await fake.cli(effort="medium", model="sonnet").generate("system", "hi")
    argv = fake.record()["argv"]
    expected = [
        "-p", "--safe-mode", "--settings", "--tools", "--strict-mcp-config", "--setting-sources",
        "--disable-slash-commands", "--no-session-persistence", "--system-prompt-file",
        "--output-format", "--effort",
    ]
    positions = [argv.index(flag) for flag in expected]
    assert positions == sorted(positions)
    assert argv[argv.index("--tools") + 1] == ""
    assert argv[argv.index("--setting-sources") + 1] == ""
    assert argv[argv.index("--output-format") + 1] == "json"
    assert argv[argv.index("--effort") + 1] == "medium"
    assert argv[argv.index("--model") + 1] == "sonnet"
    assert "--bare" not in argv and "--mcp-config" not in argv and "--verbose" not in argv
    settings = json.loads(argv[argv.index("--settings") + 1])
    assert settings["disableAllHooks"] is True
    assert settings["enabledPlugins"] and not any(settings["enabledPlugins"].values())


async def test_environment_is_scrubbed(fake, monkeypatch):
    for key, value in {
        "ANTHROPIC_API_KEY": "sk-ant-api03-xyz",
        "ANTHROPIC_BASE_URL": "https://example.invalid",
        "TELEGRAM_BOT_TOKEN": "1:abc",
        "CLAUDE_CODE_OAUTH_TOKEN": "sk-ant-oat01-from-parent-shell",
        "CLAUDE_CODE_SIMPLE": "1",
        "CLAUDE_CODE_USE_BEDROCK": "1",
        "CLAUDE_CODE_USE_VERTEX": "1",
        "CLAUDE_CODE_USE_FOUNDRY": "1",
        "CLAUDECODE": "1",
        "CLAUDE_CODE_ENTRYPOINT": "cli",
    }.items():
        monkeypatch.setenv(key, value)
    await fake.cli().generate("system", "hi")
    env = fake.record()["env"]
    assert not [k for k in env if k.startswith("ANTHROPIC_")]
    for key in ("TELEGRAM_BOT_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN", "CLAUDE_CODE_SIMPLE", "CLAUDE_CODE_USE_BEDROCK",
                "CLAUDE_CODE_USE_VERTEX", "CLAUDE_CODE_USE_FOUNDRY", "CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT"):
        assert key not in env
    assert env["HOME"] == os.environ["HOME"]
    for key, value in ISOLATION_ENV.items():
        assert env[key] == value


async def test_configured_token_only_reaches_child_env(fake):
    cli = fake.cli(oauth_token=TOKEN, auth_label="subscription token from .env")
    await cli.generate("system prompt", "hello")
    rec = fake.record()
    assert rec["env"]["CLAUDE_CODE_OAUTH_TOKEN"] == TOKEN
    assert not any(TOKEN in a for a in rec["argv"])
    assert TOKEN not in rec["stdin"] and TOKEN not in rec["system_prompt"]
    assert TOKEN not in repr(cli)


async def test_token_redacted_in_output(fake):
    fake.mode("echo_token")
    assert await fake.cli(oauth_token=TOKEN).generate("s", "u") == "token=[REDACTED]"


async def test_rejected_token_message(fake):
    fake.mode("auth")
    with pytest.raises(ClaudeAuthError) as info:
        await fake.cli(oauth_token=TOKEN).generate("s", "u")
    message = str(info.value)
    assert "claude setup-token" in message and "CLI said:" in message
    assert "[REDACTED]" in message and TOKEN not in message


async def test_auth_error_without_token(fake):
    fake.mode("auth")
    with pytest.raises(ClaudeAuthError) as info:
        await fake.cli().generate("s", "u")
    assert "/login" in str(info.value)


async def test_inspect_reports_auth(fake):
    report = await fake.cli(oauth_token=TOKEN, auth_label="subscription token from .env").inspect()
    assert report["auth"] == "subscription token from .env"
    assert report["init"]["plugins"] == [] and report["init"]["apiProvider"] == "firstParty"
    assert "--verbose" in fake.record()["argv"]
    assert fake.record()["stdin"] == "ping"
    assert (await fake.cli().inspect())["auth"] == "this machine's claude login"


async def test_runs_in_its_own_empty_private_dir(fake):
    await fake.cli().generate("s", "u")
    rec = fake.record()
    assert rec["cwd_listing"] == []
    assert rec["cwd_mode"] == "0o700"
    assert Path(rec["cwd"]).resolve().parent == fake.workdir.resolve()
    assert stat.S_IMODE(os.stat(fake.workdir).st_mode) == 0o700


async def test_timeout_kills_process_group(fake):
    fake.mode("sleep")
    started = time.monotonic()
    with pytest.raises(ClaudeTimeout):
        await fake.cli(timeout=1).generate("s", "u")
    assert time.monotonic() - started < 5
    pids = [fake.record()["pid"], fake.child_pid()]
    deadline = time.monotonic() + 3
    alive = pids
    while alive and time.monotonic() < deadline:
        alive = []
        for pid in pids:
            try:
                os.kill(pid, 0)
                alive.append(pid)
            except ProcessLookupError:
                pass
        time.sleep(0.05)
    assert alive == []


async def test_limit_with_epoch(fake):
    fake.mode("limit_epoch")
    with pytest.raises(ClaudeLimitError) as info:
        await fake.cli().generate("s", "u")
    assert info.value.resets_at == 1999999999
    assert "|" not in info.value.detail


async def test_limit_with_text(fake):
    fake.mode("limit_text")
    with pytest.raises(ClaudeLimitError) as info:
        await fake.cli().generate("s", "u")
    assert info.value.resets_at is None
    assert info.value.reset_text and info.value.reset_text.startswith("resets 3pm")
    assert "Claude subscription usage limit reached (resets 3pm" in str(info.value)


async def test_crash_and_empty(fake):
    fake.mode("crash")
    with pytest.raises(ClaudeError) as info:
        await fake.cli().generate("s", "u")
    assert "boom" in str(info.value)
    fake.mode("empty")
    with pytest.raises(ClaudeError):
        await fake.cli().generate("s", "u")


async def test_missing_binary(tmp_path):
    cli = ClaudeCLI(str(tmp_path / "nope" / "claude"), workdir=tmp_path / "w")
    with pytest.raises(ClaudeError) as info:
        await cli.generate("s", "u")
    assert "not found" in str(info.value)


def test_classify_failure():
    assert isinstance(classify_failure("anything", 429), ClaudeLimitError)
    for text in ("Usage limit reached", "You've hit your usage limit", "rate_limit_error",
                 "You're out of extra usage", "quota exceeded"):
        assert isinstance(classify_failure(text), ClaudeLimitError), text
    for text in ("Not logged in · Please run /login", "Invalid API key", "authentication_error",
                 "Unauthorized", "OAuth token has expired"):
        assert isinstance(classify_failure(text), ClaudeAuthError), text
    assert isinstance(classify_failure("x", 403), ClaudeAuthError)
    other = classify_failure("x" * 2000)
    assert type(other) is ClaudeError and len(str(other)) < 600
    returned = classify_failure("Not logged in")
    assert isinstance(returned, Exception)  # returned, not raised


def test_parse_result():
    ok = json.dumps({"type": "result", "subtype": "success", "is_error": False, "result": " hi "})
    assert parse_result(0, "noise\n" + ok + "\n\n", "") == "hi"
    with pytest.raises(ClaudeLimitError):
        parse_result(1, json.dumps({"subtype": "success", "is_error": True, "result": "usage limit reached"}), "")
    with pytest.raises(ClaudeError):
        parse_result(1, json.dumps({"subtype": "error_max_turns", "is_error": False, "result": "x"}), "")
    with pytest.raises(ClaudeError):
        parse_result(0, json.dumps({"subtype": "success", "is_error": False, "result": "  "}), "")
    with pytest.raises(ClaudeAuthError):
        parse_result(1, "not json", "Not logged in")
    with pytest.raises(ClaudeError) as info:
        parse_result(7, "", "")
    assert "exit code 7" in str(info.value)
