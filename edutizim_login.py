"""Set up the EduTizim balance lookup in .env without showing any secret.

    python edutizim_login.py            ask for the apiKey and the EduTizim login, save
                                        EDUTIZIM_API_KEY, EDUTIZIM_TOKEN, EDUTIZIM_BRANCH_IDS in .env
    python edutizim_login.py --copy KEY copy one value from .env to the clipboard (macOS),
                                        to paste it into Railway Variables

Secrets are typed with hidden input and never printed.
"""
from __future__ import annotations

import argparse
import asyncio
import getpass
import os
import subprocess
import sys
from pathlib import Path
from typing import Dict

import aiohttp
from dotenv import dotenv_values

from assistant.edutizim import DEFAULT_BASE_URL, EduTizimClient, EduTizimError

ENV_FILE = Path(__file__).resolve().parent / ".env"
COPYABLE = ("EDUTIZIM_API_KEY", "EDUTIZIM_TOKEN", "EDUTIZIM_BRANCH_IDS", "CLAUDE_CODE_OAUTH_TOKEN")


def save_env(values: Dict[str, str]) -> None:
    lines = ENV_FILE.read_text(encoding="utf-8").splitlines() if ENV_FILE.exists() else []
    done = set()
    for i, line in enumerate(lines):
        key = line.split("=", 1)[0].strip()
        if key in values:
            lines[i] = f"{key}={values[key]}"
            done.add(key)
    lines += [f"{k}={v}" for k, v in values.items() if k not in done]
    tmp = ENV_FILE.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")
    os.replace(tmp, ENV_FILE)
    os.chmod(ENV_FILE, 0o600)


async def post(session: aiohttp.ClientSession, path: str, api_key: str, body: Dict) -> Dict:
    async with session.post(f"{DEFAULT_BASE_URL}/{path}", headers={"apiKey": api_key}, json=body) as resp:
        data = await resp.json(content_type=None)
    if not isinstance(data, dict) or data.get("code") != 0:
        message = data.get("message") if isinstance(data, dict) else "unexpected answer"
        raise EduTizimError(f"EduTizim: {message}")
    return data.get("data") or {}


async def login() -> int:
    api_key = getpass.getpass("EduTizim apiKey (ko'rinmaydi): ").strip()
    phone = input("EduTizim'ga kiradigan telefon (+998...): ").strip().replace(" ", "")
    if not phone.startswith("+"):
        phone = "+998" + phone[-9:]
    password = getpass.getpass("Parol (ko'rinmaydi; parol bo'lmasa Enter bosing, SMS kod keladi): ")
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=20)) as session:
        if password:
            data = await post(session, "sign-with-password", api_key, {"phoneNumber": phone, "password": password})
        else:
            await post(session, "sign-in", api_key, {"phoneNumber": phone})
            otp = input("SMS kod: ").strip()
            data = await post(session, "sign-verify", api_key, {"phoneNumber": phone, "otp": otp})
    token = data.get("token")
    if not token:
        print("XATO: EduTizim token qaytarmadi.")
        return 1
    client = EduTizimClient(api_key, token)
    branches = await client.branches()
    ids = [str(b["_id"]) for b in branches if b.get("_id")]
    save_env({"EDUTIZIM_API_KEY": api_key, "EDUTIZIM_TOKEN": token, "EDUTIZIM_BRANCH_IDS": ",".join(ids)})
    print(f"OK: {data.get('fullName') or 'akkaunt'} bilan kirildi, .env ga saqlandi.")
    for b in branches:
        print(f"  filial: {b.get('name')}")
    return 0


def copy(key: str) -> int:
    if key not in COPYABLE:
        print(f"Faqat shular: {', '.join(COPYABLE)}")
        return 2
    value = (dotenv_values(ENV_FILE).get(key) or "").strip()
    if not value:
        print(f"{key} .env da bo'sh.")
        return 1
    subprocess.run(["pbcopy"], input=value, text=True, check=True)
    print(f"OK: {key} buferga nusxalandi ({len(value)} belgi). Railway'ga joylang.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--copy", metavar="KEY")
    args = parser.parse_args()
    if args.copy:
        return copy(args.copy)
    try:
        return asyncio.run(login())
    except (EduTizimError, aiohttp.ClientError, asyncio.TimeoutError) as exc:
        print(f"XATO: {exc}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
