"""SQLite-backed session storage with dict-like access.

Live session objects stay in memory so existing code can keep mutating them in place; ``flush()`` (called at the
end of every request) writes changed sessions to SQLite and deletes removed or expired ones. On start-up the
non-expired sessions are reloaded, so a restart never extends a session's lifetime and never revives an expired one.

* ``hash_keys=True`` stores only the SHA-256 of the session id (cookie values are not recoverable from the DB).
* Secret or bulky transient values (QR tokens and PNGs) are never persisted; they are 30-second artefacts.
"""
from __future__ import annotations

import base64
import hashlib
import json
import threading
import time
from typing import Callable, Iterator

VOLATILE_KEYS = frozenset({"demo_token", "demo_qr_png", "qr_png", "token", "qr_token"})

SCHEMA = """
CREATE TABLE IF NOT EXISTS session_store (
    namespace TEXT NOT NULL,
    session_key TEXT NOT NULL,
    data TEXT NOT NULL,
    expires_at REAL NOT NULL,
    PRIMARY KEY (namespace, session_key)
)
"""


def _encode(value: object) -> object:
    if isinstance(value, bytes):
        return {"__bytes__": base64.b64encode(value).decode("ascii")}
    raise TypeError(f"not serializable: {type(value).__name__}")


def _decode(obj: dict) -> object:
    if set(obj) == {"__bytes__"}:
        return base64.b64decode(obj["__bytes__"])
    return obj


class PersistentSessions:
    def __init__(
        self,
        connect: Callable,
        namespace: str,
        expires_at: Callable[[dict], float],
        hash_keys: bool = False,
    ) -> None:
        self._connect = connect
        self._namespace = namespace
        self._expires_at = expires_at
        self._hash_keys = hash_keys
        self._lock = threading.RLock()
        self._live: dict[str, dict] = {}
        self._saved: dict[str, str] = {}
        with self._connect() as connection:
            connection.execute(SCHEMA)
            now = time.time()
            connection.execute("DELETE FROM session_store WHERE namespace = ? AND expires_at <= ?", (namespace, now))
            rows = connection.execute(
                "SELECT session_key, data FROM session_store WHERE namespace = ?", (namespace,)
            ).fetchall()
        for key, data in rows:
            try:
                item = json.loads(data, object_hook=_decode)
            except ValueError:
                continue
            if isinstance(item, dict) and self._expires_at(item) > now:
                self._live[key] = item
                self._saved[key] = data

    def _key(self, key: str) -> str:
        return hashlib.sha256(key.encode()).hexdigest() if self._hash_keys else key

    def _serialize(self, item: dict) -> str:
        visible = {name: value for name, value in item.items() if name not in VOLATILE_KEYS}
        return json.dumps(visible, default=_encode, sort_keys=True)

    def get(self, key: str, default=None):
        if not isinstance(key, str) or not key:
            return default
        with self._lock:
            return self._live.get(self._key(key), default)

    def __getitem__(self, key: str) -> dict:
        item = self.get(key)
        if item is None:
            raise KeyError(key)
        return item

    def __setitem__(self, key: str, item: dict) -> None:
        with self._lock:
            self._live[self._key(key)] = item

    def __contains__(self, key: object) -> bool:
        return isinstance(key, str) and self.get(key) is not None

    def __len__(self) -> int:
        with self._lock:
            return len(self._live)

    def pop(self, key: str, *default):
        with self._lock:
            internal = self._key(key) if isinstance(key, str) else None
            if internal is not None and internal in self._live:
                return self._live.pop(internal)
        if default:
            return default[0]
        raise KeyError(key)

    def items(self) -> list[tuple[str, dict]]:
        with self._lock:
            return list(self._live.items())

    def values(self) -> list[dict]:
        with self._lock:
            return list(self._live.values())

    def __iter__(self) -> Iterator[str]:
        with self._lock:
            return iter(list(self._live))

    def clear(self) -> None:
        with self._lock:
            self._live.clear()

    def remove_where(self, predicate: Callable[[dict], bool]) -> int:
        with self._lock:
            doomed = [key for key, item in self._live.items() if predicate(item)]
            for key in doomed:
                del self._live[key]
            return len(doomed)

    def flush(self) -> None:
        """Persist changes; drop expired sessions. Safe to call often (unchanged sessions are skipped)."""
        now = time.time()
        with self._lock:
            for key in [key for key, item in self._live.items() if self._expires_at(item) <= now]:
                del self._live[key]
            changed = {}
            for key, item in self._live.items():
                data = self._serialize(item)
                if self._saved.get(key) != data:
                    changed[key] = (data, self._expires_at(item))
            removed = [key for key in self._saved if key not in self._live]
            if not changed and not removed:
                return
            with self._connect() as connection:
                for key, (data, expires) in changed.items():
                    connection.execute(
                        "INSERT OR REPLACE INTO session_store (namespace, session_key, data, expires_at) VALUES (?, ?, ?, ?)",
                        (self._namespace, key, data, expires),
                    )
                for key in removed:
                    connection.execute(
                        "DELETE FROM session_store WHERE namespace = ? AND session_key = ?", (self._namespace, key)
                    )
            for key, (data, _) in changed.items():
                self._saved[key] = data
            for key in removed:
                self._saved.pop(key, None)
