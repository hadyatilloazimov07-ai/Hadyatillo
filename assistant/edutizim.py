"""Read-only EduTizim (CRM) balance lookup.

The lookup is done by this code, never by Claude: when a chat message contains
a phone number, the student is searched by that phone in EduTizim and is only
accepted if the first AND last name written in the chat match the record.
Only the student's name and balance are passed on to the prompt.
"""
from __future__ import annotations

import asyncio
import logging
import re
import time
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional

import aiohttp

log = logging.getLogger("edutizim")

DEFAULT_BASE_URL = "https://backend.edutizim.uz/external-api"
LOOKUPS_PER_CHAT_PER_DAY = 5

_PHONE_RE = re.compile(r"(?:\+?\s*998[\s\-()]*)?(\d{2})[\s\-()]*(\d{3})[\s\-]*(\d{2})[\s\-]*(\d{2})(?!\d)")
_APOS_RE = re.compile(r"['ʻ’‘`ʼ]")
_CYR = {
    "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "yo", "ж": "j", "з": "z",
    "и": "i", "й": "y", "к": "k", "л": "l", "м": "m", "н": "n", "о": "o", "п": "p", "р": "r",
    "с": "s", "т": "t", "у": "u", "ф": "f", "х": "x", "ц": "ts", "ч": "ch", "ш": "sh", "щ": "sh",
    "ъ": "", "ы": "i", "ь": "", "э": "e", "ю": "yu", "я": "ya", "ў": "o", "қ": "q", "ғ": "g",
    "ҳ": "h",
}


class EduTizimError(Exception):
    pass


@dataclass
class StudentBalance:
    full_name: str
    balance: float


def extract_phone(text: str) -> Optional[str]:
    """Last 9 digits of the last Uzbek phone number in the text, or None."""
    found = None
    for match in _PHONE_RE.finditer(text or ""):
        found = "".join(match.groups())
    return found


def _normalise_word(word: str) -> str:
    word = "".join(_CYR.get(ch, ch) for ch in word.lower())
    word = _APOS_RE.sub("", word)
    return word.replace("h", "x")  # Hikmatillo == Xikmatillo


def _words(text: str) -> List[str]:
    return [_normalise_word(w) for w in re.findall(r"[^\W\d_]+(?:['ʻ’‘`ʼ][^\W\d_]+)*", text or "")]


def names_match(text: str, first_name: str, last_name: str) -> bool:
    """Both the first and the last name of the record must appear in the text."""
    words = set(_words(text))
    first = _words(first_name)
    last = _words(last_name)
    if not first or not last:
        return False
    return all(w in words for w in first) and all(w in words for w in last)


def _digits(phone: str) -> str:
    return re.sub(r"\D", "", phone or "")[-9:]


class EduTizimClient:
    def __init__(
        self,
        api_key: str,
        token: str,
        *,
        branch_ids: Iterable[str] = (),
        base_url: str = DEFAULT_BASE_URL,
        timeout: float = 15,
    ) -> None:
        self._api_key = api_key
        self._token = token
        self.branch_ids = [b for b in branch_ids if b]
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    def __repr__(self) -> str:
        return f"EduTizimClient(base_url={self.base_url!r}, branches={self.branch_ids!r})"

    def _headers(self, branch: Optional[str] = None) -> Dict[str, str]:
        headers = {"apiKey": self._api_key, "Authorization": f"Bearer {self._token}"}
        if branch:
            headers["branch"] = branch
        return headers

    async def _request(self, method: str, path: str, *, branch: Optional[str] = None, json: Any = None) -> Any:
        url = f"{self.base_url}/{path.lstrip('/')}"
        timeout = aiohttp.ClientTimeout(total=self.timeout)
        try:
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.request(method, url, headers=self._headers(branch), json=json) as resp:
                    if resp.status in (401, 403):
                        raise EduTizimError(
                            f"EduTizim rejected the credentials (HTTP {resp.status}); "
                            "check EDUTIZIM_API_KEY / EDUTIZIM_TOKEN"
                        )
                    payload = await resp.json(content_type=None)
        except aiohttp.ClientError as exc:
            raise EduTizimError(f"EduTizim request failed: {type(exc).__name__}") from None
        except asyncio.TimeoutError:
            raise EduTizimError("EduTizim did not answer in time") from None
        if not isinstance(payload, dict) or payload.get("code") not in (0, None):
            message = payload.get("message") if isinstance(payload, dict) else "unexpected answer"
            raise EduTizimError(f"EduTizim error: {str(message)[:200]}")
        return payload.get("data")

    async def branches(self) -> List[Dict[str, Any]]:
        data = await self._request("GET", "branches")
        return [b for b in (data or []) if isinstance(b, dict)]

    async def _branch_ids(self) -> List[str]:
        if not self.branch_ids:
            self.branch_ids = [str(b.get("_id")) for b in await self.branches() if b.get("_id")]
        return self.branch_ids

    async def find_students(self, phone9: str) -> List[Dict[str, Any]]:
        results: List[Dict[str, Any]] = []
        for branch in await self._branch_ids():
            data = await self._request(
                "POST", "student-list/students-pagin", branch=branch,
                json={"page": 1, "limit": 10, "search": phone9},
            )
            for item in (data or {}).get("data", []) if isinstance(data, dict) else []:
                if isinstance(item, dict) and _digits(item.get("phoneNumber", "")) == phone9:
                    results.append(item)
        return results

    async def balance_for(self, text: str, phone9: str) -> Optional[StudentBalance]:
        for item in await self.find_students(phone9):
            first, last = str(item.get("firstName") or ""), str(item.get("lastName") or "")
            if names_match(text, first, last):
                balance = item.get("balance")
                if isinstance(balance, (int, float)):
                    return StudentBalance(f"{first} {last}".strip(), float(balance))
        return None


def format_amount(value: float) -> str:
    return f"{abs(value):,.0f}".replace(",", " ")


def describe_balance(result: Optional[StudentBalance], error: Optional[str] = None) -> str:
    """Note for the prompt. It is the bot's own data, not part of the chat."""
    if error:
        return "The balance lookup failed. Say you will check the balance and write back later."
    if result is None:
        return (
            "No student matches BOTH this phone number and the first and last name given in the chat. "
            "Do not guess any balance. Ask them to send the phone number registered at the learning centre "
            "together with the student's first and last name, or say you will check and write back."
        )
    if result.balance < 0:
        state = f"debt of {format_amount(result.balance)} so'm"
    elif result.balance > 0:
        state = f"prepaid credit of {format_amount(result.balance)} so'm"
    else:
        state = "zero balance (no debt, nothing prepaid)"
    return f'Student "{result.full_name}": balance {result.balance:.0f} so\'m, i.e. {state}.'


class BalanceLookup:
    """Rate-limited lookups for the assistant (per chat)."""

    def __init__(self, client: EduTizimClient, *, clock=time.time, per_day: int = LOOKUPS_PER_CHAT_PER_DAY) -> None:
        self.client = client
        self.clock = clock
        self.per_day = per_day
        self._calls: Dict[str, List[float]] = {}

    async def note_for(self, chat_key: str, latest_text: str, context_text: str) -> Optional[str]:
        phone9 = extract_phone(latest_text)
        if phone9 is None:
            return None
        now = self.clock()
        calls = [t for t in self._calls.get(chat_key, []) if now - t < 86400]
        if len(calls) >= self.per_day:
            log.warning("Chat %s: EduTizim lookup limit reached", chat_key)
            return "Too many balance checks from this chat today. Say you will check and write back later."
        calls.append(now)
        self._calls[chat_key] = calls
        try:
            result = await self.client.balance_for(context_text, phone9)
        except EduTizimError as exc:
            log.error("Chat %s: %s", chat_key, exc)
            return describe_balance(None, error=str(exc))
        log.info("Chat %s: EduTizim lookup %s", chat_key, "matched" if result else "no match")
        return describe_balance(result)
