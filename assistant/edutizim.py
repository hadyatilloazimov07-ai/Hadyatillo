"""Read-only EduTizim (CRM) balance lookup.

The lookup is done by this code, never by Claude: when a chat message contains
a phone number, the student is searched by that phone in EduTizim and is only
accepted if the first AND last name written in the chat match the record.
After that the chat counts as verified for a day: follow-up questions about
payments get the student's balance and a compact transaction history again.
Only that student's name, balance and history are passed on to the prompt.
"""
from __future__ import annotations

import asyncio
import logging
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Iterable, List, Optional, Tuple

import aiohttp

from .store import JsonFile

log = logging.getLogger("edutizim")

DEFAULT_BASE_URL = "https://backend.edutizim.uz/external-api"
LOOKUPS_PER_CHAT_PER_DAY = 5
FOLLOWUPS_PER_CHAT_PER_DAY = 20
VERIFIED_SECONDS = 24 * 3600
HISTORY_ROWS = 40
LOCAL_TZ = timezone(timedelta(hours=5))  # Uzbekistan

_PHONE_RE = re.compile(r"(?:\+?\s*998[\s\-()]*)?(\d{2})[\s\-()]*(\d{3})[\s\-]*(\d{2})[\s\-]*(\d{2})(?!\d)")
_APOS_RE = re.compile(r"['ʻ’‘`ʼ]")
_CYR = {
    "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "yo", "ж": "j", "з": "z",
    "и": "i", "й": "y", "к": "k", "л": "l", "м": "m", "н": "n", "о": "o", "п": "p", "р": "r",
    "с": "s", "т": "t", "у": "u", "ф": "f", "х": "x", "ц": "ts", "ч": "ch", "ш": "sh", "щ": "sh",
    "ъ": "", "ы": "i", "ь": "", "э": "e", "ю": "yu", "я": "ya", "ў": "o", "қ": "q", "ғ": "g",
    "ҳ": "h",
}
# Word stems (after _normalise_word) that mean "this is about money / my balance".
_MONEY_STEMS = (
    "tolov", "tolad", "tolag", "balans", "pul", "qarz", "nimaga", "nega", "tushuntir", "xisob",
    "summa", "yechil", "yechib", "oplat", "plat", "dolg", "pochemu", "dengi",
    "spisa", "schet",
)
_LESSON_TYPES = {"attendance", "cancelled", "unpaidAttendanceCancelled"}


ASK_FOR_IDENTITY = (
    "The balance can be checked automatically, but this chat has not identified a student yet. "
    "Do not say you cannot see the balance and do not say the owner will write later. Ask them to send "
    "the phone number registered at the learning centre together with the student's first and last name, "
    "and say you will check the balance right away."
)


class EduTizimError(Exception):
    pass


@dataclass
class StudentBalance:
    full_name: str
    balance: float
    student_id: str = ""
    branch: str = ""
    history: List[str] = field(default_factory=list)
    payment: str = ""


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


def asks_about_money(text: str) -> bool:
    return any(w.startswith(stem) for w in _words(text) for stem in _MONEY_STEMS)


def _digits(phone: str) -> str:
    return re.sub(r"\D", "", phone or "")[-9:]


def format_amount(value: float) -> str:
    return f"{abs(value):,.0f}".replace(",", " ")


def _signed(value: float) -> str:
    return ("+" if value > 0 else "-" if value < 0 else "") + format_amount(value)


def _date(raw: Any) -> str:
    try:
        parsed = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError:
        return str(raw)[:10]
    return parsed.astimezone(LOCAL_TZ).strftime("%Y-%m-%d")


def _delta(row: Dict[str, Any]) -> float:
    before, after = row.get("beforeAmount"), row.get("afterAmount")
    if isinstance(before, (int, float)) and isinstance(after, (int, float)):
        return float(after) - float(before)
    amount = row.get("amount")
    return float(amount) if isinstance(amount, (int, float)) else 0.0


def _name_of(value: Any) -> str:
    if isinstance(value, dict):
        return str(value.get("name") or "")
    return str(value or "")


def summarize_transactions(rows: List[Dict[str, Any]]) -> List[str]:
    """Rows oldest first -> short lines; consecutive lesson charges are grouped."""
    lines: List[str] = []
    run: List[Dict[str, Any]] = []

    def flush() -> None:
        if not run:
            return
        start = run[0].get("beforeAmount")
        end = run[-1].get("afterAmount")
        groups: Dict[Tuple[str, str, str], List[float]] = {}
        for row in run:
            key = (_name_of(row.get("group")) or "group", str(row.get("typeLabel") or row.get("type")),
                   str(row.get("attendanceStateLabel") or ""))
            groups.setdefault(key, []).append(_delta(row))
        parts = []
        for (group, label, state), deltas in groups.items():
            state_txt = f", status '{state}'" if state else ""
            parts.append(f"{len(deltas)} x '{label}' in group '{group}'{state_txt}: {_signed(sum(deltas))} so'm")
        first, last = _date(run[0].get("createdAt")), _date(run[-1].get("createdAt"))
        when = first if first == last else f"{first} .. {last}"
        balance = ""
        if isinstance(start, (int, float)) and isinstance(end, (int, float)):
            balance = f" (balance {_signed(start)} -> {_signed(end)})"
        net = sum(_delta(row) for row in run)
        lines.append(f"- {when}: lessons: " + "; ".join(parts) + f"; net change {_signed(net)} so'm" + balance)
        run.clear()

    for row in rows:
        if row.get("type") in _LESSON_TYPES:
            run.append(row)
            continue
        flush()
        label = str(row.get("typeLabel") or row.get("type") or "transaction")
        method = _name_of(row.get("paymentMethod"))
        method_txt = f" ({method})" if method else ""
        before, after = row.get("beforeAmount"), row.get("afterAmount")
        balance = ""
        if isinstance(before, (int, float)) and isinstance(after, (int, float)):
            balance = f" (balance {_signed(before)} -> {_signed(after)})"
        lines.append(f"- {_date(row.get('createdAt'))}: '{label}'{method_txt}: {_signed(_delta(row))} so'm{balance}")
    flush()
    return lines


def _parse(raw: Any) -> Optional[datetime]:
    try:
        return datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError:
        return None


def payment_guidance(rows: List[Dict[str, Any]], balance: float, now: Optional[datetime] = None) -> str:
    """How much to pay: the debt, the current lesson price and the monthly amount from the last 30 days."""
    now = now or datetime.now(timezone.utc)
    charges = [r for r in rows if r.get("type") == "attendance" and _delta(r) < 0]
    lines = []
    if balance < 0:
        lines.append(
            f"Amount due now: {format_amount(balance)} so'm. Paying exactly this brings the balance to 0."
        )
    else:
        lines.append("Nothing is due now: there is no debt.")
    if charges:
        last = charges[-1]
        price = -_delta(last)
        group = _name_of(last.get("group")) or "their group"
        since = now - timedelta(days=30)
        recent = [r for r in charges if (_parse(r.get("createdAt")) or now) >= since]
        refunds = [r for r in rows if r.get("type") == "cancelled" and _delta(r) > 0
                   and (_parse(r.get("createdAt")) or now) >= since]
        lessons = max(len(recent) - len(refunds), 0)
        monthly = lessons * price
        lines.append(f"Current lesson price in group '{group}': {format_amount(price)} so'm per lesson.")
        if lessons:
            lines.append(
                f"In the last 30 days {lessons} lessons were charged, about {format_amount(monthly)} so'm a month."
            )
            if balance < 0:
                lines.append(
                    f"To clear the debt and also cover the next month: about {format_amount(-balance + monthly)} so'm."
                )
            elif price:
                lines.append(f"The current balance covers about {int(balance // price)} more lessons.")
    lines.append(
        "When they ask how much to pay, answer with these numbers: first the amount due now, then the lesson "
        "price and the approximate monthly amount. Say the monthly amount is approximate."
    )
    return "\n".join(lines)


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
                    results.append(dict(item, _branch=branch))
        return results

    async def transactions(self, student_id: str, branch: str, limit: int = HISTORY_ROWS) -> List[Dict[str, Any]]:
        """Newest `limit` transactions, returned oldest first."""
        data = await self._request(
            "POST", "student/transactions-pagin", branch=branch,
            json={"studentId": student_id, "page": 1, "limit": limit, "sortBy": "createdAt", "sortOrder": -1},
        )
        rows = (data or {}).get("data", []) if isinstance(data, dict) else []
        rows = [r for r in rows if isinstance(r, dict)]
        rows.sort(key=lambda r: str(r.get("createdAt") or ""))
        return rows

    async def balance_for(self, text: str, phone9: str, student_id: Optional[str] = None) -> Optional[StudentBalance]:
        """Find the student by phone; accept by name match (or by a known id)."""
        for item in await self.find_students(phone9):
            first, last = str(item.get("firstName") or ""), str(item.get("lastName") or "")
            accepted = (student_id and item.get("_id") == student_id) or (
                not student_id and names_match(text, first, last)
            )
            balance = item.get("balance")
            if accepted and isinstance(balance, (int, float)):
                return StudentBalance(
                    f"{first} {last}".strip(), float(balance), str(item.get("_id") or ""), item["_branch"]
                )
        return None

    async def with_history(self, result: StudentBalance) -> StudentBalance:
        if result.student_id and result.branch:
            try:
                rows = await self.transactions(result.student_id, result.branch)
                result.history = summarize_transactions(rows)
                result.payment = payment_guidance(rows, result.balance)
            except EduTizimError as exc:
                log.warning("Could not read transactions: %s", exc)
        return result


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
    note = f'Student "{result.full_name}": current balance {result.balance:.0f} so\'m, i.e. {state}.'
    if result.payment:
        note += "\n" + result.payment
    if result.history:
        note += (
            "\nTransaction history (oldest first, most recent part only; amounts change the balance by the "
            "sign shown):\n" + "\n".join(result.history) + "\n"
            "If they only asked for the balance, give just the balance. If they ask why the balance is like "
            "this, explain it from this history in plain words: when and how much they paid, how many lessons "
            "were charged in which group and for how much, any refunds or cancelled charges, and how that "
            "leads to the current balance. The numbers you give must add up exactly (start balance + changes = current balance); a lesson whose status says it was cancelled but still has a minus amount was charged, and a separate 'cancelled' line with a plus amount gave that money back. Translate the labels into the reply language. Never mention staff "
            "names, internal ids or other students."
        )
    return note


@dataclass
class _Verified:
    phone9: str
    student_id: str
    since: float


class BalanceLookup:
    """Rate-limited lookups for the assistant (per chat)."""

    def __init__(
        self,
        client: EduTizimClient,
        *,
        clock=time.time,
        per_day: int = LOOKUPS_PER_CHAT_PER_DAY,
        followups_per_day: int = FOLLOWUPS_PER_CHAT_PER_DAY,
        path: Optional[Any] = None,
    ) -> None:
        self.client = client
        self._file = JsonFile(path) if path is not None else None
        self.clock = clock
        self.per_day = per_day
        self.followups_per_day = followups_per_day
        self._calls: Dict[str, List[float]] = {}
        self._followups: Dict[str, List[float]] = {}
        self._verified: Dict[str, _Verified] = {}
        if self._file is not None:
            saved = self._file.read({})
            for key, value in (saved.items() if isinstance(saved, dict) else []):
                try:
                    self._verified[key] = _Verified(str(value["phone9"]), str(value["student_id"]), float(value["since"]))
                except (KeyError, TypeError, ValueError):
                    continue

    def _save(self) -> None:
        if self._file is not None:
            now = self.clock()
            self._file.write({
                k: {"phone9": v.phone9, "student_id": v.student_id, "since": v.since}
                for k, v in self._verified.items() if now - v.since <= VERIFIED_SECONDS
            })

    def _allow(self, bucket: Dict[str, List[float]], chat_key: str, limit: int) -> bool:
        now = self.clock()
        calls = [t for t in bucket.get(chat_key, []) if now - t < 86400]
        if len(calls) >= limit:
            bucket[chat_key] = calls
            return False
        calls.append(now)
        bucket[chat_key] = calls
        return True

    async def note_for(self, chat_key: str, latest_text: str, context_text: str) -> Optional[str]:
        phone9 = extract_phone(latest_text)
        if phone9 is not None:
            if not self._allow(self._calls, chat_key, self.per_day):
                log.warning("Chat %s: EduTizim lookup limit reached", chat_key)
                return "Too many balance checks from this chat today. Say you will check and write back later."
            return await self._lookup(chat_key, context_text, phone9, None)

        verified = self._verified.get(chat_key)
        if verified is not None and self.clock() - verified.since > VERIFIED_SECONDS:
            self._verified.pop(chat_key, None)
            self._save()
            verified = None
        if not asks_about_money(latest_text):
            return None
        if verified is None:
            return ASK_FOR_IDENTITY
        if not self._allow(self._followups, chat_key, self.followups_per_day):
            return "Too many balance checks from this chat today. Say you will check and write back later."
        return await self._lookup(chat_key, context_text, verified.phone9, verified.student_id)

    async def _lookup(self, chat_key: str, text: str, phone9: str, student_id: Optional[str]) -> str:
        try:
            result = await self.client.balance_for(text, phone9, student_id)
            if result is not None:
                result = await self.client.with_history(result)
        except EduTizimError as exc:
            log.error("Chat %s: %s", chat_key, exc)
            return describe_balance(None, error=str(exc))
        log.info("Chat %s: EduTizim lookup %s", chat_key, "matched" if result else "no match")
        if result is not None and result.student_id:
            previous = self._verified.get(chat_key)
            since = previous.since if previous and previous.student_id == result.student_id else self.clock()
            self._verified[chat_key] = _Verified(phone9, result.student_id, since)
            self._save()
        return describe_balance(result)
