"""Incremental SSE framing for a streaming proxy.

The PoC parsed the whole body at once; a live proxy cannot. `SSEAccumulator`
consumes byte chunks, reassembles SSE events across arbitrary chunk
boundaries, and returns parsed event objects as soon as each event completes.
`incremental_sse` wraps it for async byte iterators; the passthrough tee in
app.py feeds it chunk-by-chunk while forwarding the same bytes untouched.

Events:
  - dict  : the parsed JSON of a `data:` event
  - DONE  : the sentinel for a `data: [DONE]` terminal line
Malformed JSON data lines are skipped (lenient, matching PoC behavior).
"""
from __future__ import annotations

import json
from typing import Any, AsyncIterator

# Sentinel for the `data: [DONE]` terminal line (distinct from any dict event).
DONE = "[DONE]"


def _decode_line(raw: bytes) -> str:
    # SSE lines may end with \n or \r\n; the trailing \r is stripped by split
    # on \n + rstrip("\r").
    return raw.decode("utf-8", errors="replace").rstrip("\r")


class SSEAccumulator:
    """Stateful chunk-at-a-time SSE parser.

    An event is terminated by a blank line. Multiple `data:` lines within one
    event are concatenated with newlines (per the SSE spec). `event:` and
    comment (`:`) lines are ignored — the JSON payload carries its own `type`.
    """

    def __init__(self) -> None:
        self._buffer = b""
        self._data_lines: list[str] = []

    def _flush_event(self):
        if not self._data_lines:
            return None
        payload = "\n".join(self._data_lines)
        self._data_lines.clear()
        if payload == DONE:
            return ("done",)
        try:
            return ("event", json.loads(payload))
        except json.JSONDecodeError:
            return None

    def feed(self, chunk: bytes) -> list[Any]:
        """Consume one byte chunk; return the events it completed (dict | DONE)."""
        out: list[Any] = []
        if not chunk:
            return out
        self._buffer += chunk
        while b"\n" in self._buffer:
            raw, self._buffer = self._buffer.split(b"\n", 1)
            line = _decode_line(raw)

            if line == "":
                ev = self._flush_event()
                if ev is not None:
                    out.append(DONE if ev[0] == "done" else ev[1])
                continue
            if line.startswith(":"):
                continue  # comment
            if line.startswith("data:"):
                val = line[5:]
                if val.startswith(" "):
                    val = val[1:]
                self._data_lines.append(val)
            # `event:` / `id:` / `retry:` lines: ignored (type lives in JSON).
        return out

    def finish(self) -> list[Any]:
        """Flush a trailing event with no terminating blank line."""
        ev = self._flush_event()
        if ev is None:
            return []
        return [DONE if ev[0] == "done" else ev[1]]


async def incremental_sse(byte_iter: AsyncIterator[bytes]) -> AsyncIterator[Any]:
    """Frame an async byte stream into SSE events (see SSEAccumulator)."""
    acc = SSEAccumulator()
    async for chunk in byte_iter:
        for ev in acc.feed(chunk):
            yield ev
    for ev in acc.finish():
        yield ev


def serialize_event(event: dict[str, Any]) -> bytes:
    """Render one event downstream as `event: <type>\\ndata: <json>\\n\\n`,
    mirroring the upstream framing (both lines present)."""
    etype = event.get("type", "message")
    data = json.dumps(event, ensure_ascii=False, separators=(",", ":"))
    return f"event: {etype}\ndata: {data}\n\n".encode("utf-8")


def serialize_done() -> bytes:
    return b"data: [DONE]\n\n"
