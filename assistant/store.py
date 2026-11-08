"""Small JSON-file stores kept under data/ (a Railway volume in production)."""
from __future__ import annotations

import json
import os
import tempfile
import time
from collections import OrderedDict
from pathlib import Path
from typing import Any, Dict, List, Optional


def ensure_private_dir(path: Path) -> Path:
    path = Path(path)
    path.mkdir(parents=True, exist_ok=True)
    os.chmod(path, 0o700)
    return path


class JsonFile:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)

    def read(self, default: Any) -> Any:
        try:
            with open(self.path, "r", encoding="utf-8") as handle:
                return json.load(handle)
        except (OSError, ValueError):
            return default

    def write(self, data: Any) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=".tmp-", dir=str(self.path.parent))
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(data, handle, ensure_ascii=False)
            os.replace(tmp, self.path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise


class HistoryStore:
    """Per business connection + chat conversation history."""

    def __init__(self, path: Path, limit: int) -> None:
        self._file = JsonFile(path)
        self.limit = limit
        data = self._file.read({})
        self._data: Dict[str, List[Dict[str, Any]]] = data if isinstance(data, dict) else {}

    @staticmethod
    def key(connection_id: str, chat_id: int) -> str:
        return f"{connection_id}:{chat_id}"

    def get(self, connection_id: str, chat_id: int) -> List[Dict[str, Any]]:
        return list(self._data.get(self.key(connection_id, chat_id), []))

    def add(
        self,
        connection_id: str,
        chat_id: int,
        role: str,
        name: str,
        text: str,
        lang: Optional[str] = None,
        ts: Optional[float] = None,
    ) -> None:
        key = self.key(connection_id, chat_id)
        items = self._data.setdefault(key, [])
        items.append({"role": role, "name": name, "text": text, "lang": lang, "ts": ts or time.time()})
        if len(items) > self.limit:
            del items[: len(items) - self.limit]
        self._file.write(self._data)

    def last_language(self, connection_id: str, chat_id: int) -> Optional[str]:
        for item in reversed(self._data.get(self.key(connection_id, chat_id), [])):
            if item.get("role") == "other" and item.get("lang"):
                return item["lang"]
        return None

    def chat_count(self) -> int:
        return len(self._data)


class SeenMessages:
    """Remembers processed message keys so duplicates are dropped, even after a restart."""

    def __init__(self, path: Path, capacity: int = 5000) -> None:
        self._file = JsonFile(path)
        self.capacity = capacity
        data = self._file.read([])
        self._keys: "OrderedDict[str, None]" = OrderedDict(
            (str(k), None) for k in (data if isinstance(data, list) else [])
        )

    def first_time(self, key: str) -> bool:
        if key in self._keys:
            return False
        self._keys[key] = None
        while len(self._keys) > self.capacity:
            self._keys.popitem(last=False)
        self._file.write(list(self._keys))
        return True


class RuntimeState:
    """Pause flag and usage-limit deadline, persisted across restarts."""

    def __init__(self, path: Path) -> None:
        self._file = JsonFile(path)
        data = self._file.read({})
        data = data if isinstance(data, dict) else {}
        self._paused = bool(data.get("paused", False))
        limit = data.get("limit_until")
        self._limit_until: Optional[float] = float(limit) if isinstance(limit, (int, float)) else None

    def _save(self) -> None:
        self._file.write({"paused": self._paused, "limit_until": self._limit_until})

    @property
    def paused(self) -> bool:
        return self._paused

    @paused.setter
    def paused(self, value: bool) -> None:
        self._paused = bool(value)
        self._save()

    @property
    def limit_until(self) -> Optional[float]:
        return self._limit_until

    @limit_until.setter
    def limit_until(self, value: Optional[float]) -> None:
        self._limit_until = value
        self._save()
