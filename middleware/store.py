"""In-memory LRU + TTL caches keyed by upstream response id.

`IdStore` records the reasoning ids after which a synthetic continue pair was
appended during a turn's continuation, so a later turn's request can have
those pairs re-inserted by id (never by adjacency).

`ChainStore` records each response id's effective full `input` (what the
agent sent plus everything the proxy answered with), so a later request that
chains off it via `previous_response_id` can be resolved locally -- see
`codex.resolve_previous_response_id`.

Both are single-instance, in-process caches (no cross-restart persistence).
"""

from __future__ import annotations

import time
from collections import OrderedDict
from typing import Any


class IdStore:
    def __init__(self, maxsize: int = 10000, ttl_seconds: float = 3600.0) -> None:
        self.maxsize = maxsize
        self.ttl = ttl_seconds
        self._d: "OrderedDict[str, float]" = OrderedDict()  # id -> expiry (monotonic)

    def add(self, key: str) -> None:
        now = time.monotonic()
        self._d[key] = now + self.ttl
        self._d.move_to_end(key)
        self._purge(now)

    def __contains__(self, key: str) -> bool:
        exp = self._d.get(key)
        if exp is None:
            return False
        if exp < time.monotonic():
            del self._d[key]
            return False
        self._d.move_to_end(key)
        return True

    def _purge(self, now: float) -> None:
        # drop expired from the front, then enforce size cap (LRU = oldest first)
        while self._d:
            k, exp = next(iter(self._d.items()))
            if exp < now:
                del self._d[k]
            else:
                break
        while len(self._d) > self.maxsize:
            self._d.popitem(last=False)


class ChainStore:
    """response_id -> the effective full `input` list a follow-up request
    chaining off that id via `previous_response_id` should be replayed on top
    of (that turn's own input plus everything the proxy answered with).

    This proxy never keeps a persistent upstream connection: every round it
    opens upstream is a fresh, unrelated HTTP request, so the real API has no
    session to resolve a client-supplied `previous_response_id` against (it
    replies `400 Unsupported parameter: previous_response_id`). Codex (>=
    ~0.142, chained `responses_websockets` turns) relies on exactly that
    resolution to avoid resending the whole transcript on every tool-loop step
    or follow-up turn, so the proxy resolves it here instead, locally.

    Smaller default size than `IdStore`: each entry holds a whole turn's input
    (potentially large, with embedded reasoning `encrypted_content`), not just
    an id.
    """

    def __init__(self, maxsize: int = 200, ttl_seconds: float = 3600.0) -> None:
        self.maxsize = maxsize
        self.ttl = ttl_seconds
        self._d: "OrderedDict[str, tuple[float, list[Any]]]" = OrderedDict()

    def set(self, key: str, items: list[Any]) -> None:
        if not key:
            return
        now = time.monotonic()
        self._d[key] = (now + self.ttl, list(items))
        self._d.move_to_end(key)
        self._purge(now)

    def get(self, key: str) -> list[Any] | None:
        entry = self._d.get(key)
        if entry is None:
            return None
        exp, items = entry
        if exp < time.monotonic():
            del self._d[key]
            return None
        self._d.move_to_end(key)
        return items

    def _purge(self, now: float) -> None:
        while self._d:
            k, (exp, _items) = next(iter(self._d.items()))
            if exp < now:
                del self._d[k]
            else:
                break
        while len(self._d) > self.maxsize:
            self._d.popitem(last=False)
