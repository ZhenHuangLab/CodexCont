#!/usr/bin/env python3
"""Offline tests for the continue_thinking middleware.

Run: .venv/Scripts/python.exe tests/test_middleware.py
No pytest dependency — a tiny runner prints PASS/FAIL per check.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = Path(__file__).resolve().parent / "fixtures"
sys.path.insert(0, str(ROOT))

from starlette.datastructures import Headers
from starlette.testclient import TestClient

from middleware.app import (
    _make_client,
    _resolve_upstream_url,
    _url_is_from_header,
    create_app,
)
from middleware.codex import (
    continue_call_id,
    is_truncation_pattern,
    reasoning_enabled,
    repair_followup_input,
    resolve_previous_response_id,
    should_continue,
    tier_n,
)
from middleware.config import load_config
from middleware.creds import build_upstream_headers, would_inject_authorization
from middleware.proxy import fold_stream
from middleware.sse import DONE, incremental_sse
from middleware.store import ChainStore, IdStore

# --- helpers ----------------------------------------------------------------


def make_sse(events: list[dict]) -> bytes:
    out = b""
    for ev in events:
        out += f"event: {ev['type']}\r\n".encode()
        out += b"data: " + json.dumps(ev).encode() + b"\r\n\r\n"
    return out


async def _aiter_once(data: bytes):
    yield data


async def parse_events(data: bytes) -> list:
    evs = []
    async for e in incremental_sse(_aiter_once(data)):
        evs.append(e)
    return evs


class FakeResp:
    def __init__(self, data: bytes, status: int = 200, chunk: int = 4096):
        self._data = data
        self.status_code = status
        self.headers: dict[str, str] = {}
        self._chunk = chunk

    async def aiter_bytes(self):
        for i in range(0, len(self._data), self._chunk):
            yield self._data[i : i + self._chunk]

    async def aread(self) -> bytes:
        return self._data

    async def aclose(self) -> None:
        pass


class FakeClient:
    """Returns the queued responses on successive send() calls; records the JSON
    body of each build_request (the per-continuation-round upstream payload)."""

    def __init__(self, responses: list[FakeResp]):
        self._responses = list(responses)
        self._i = 0
        self.payloads: list[dict] = []

    def build_request(self, *a, **k):
        content = k.get("content")
        if content is not None:
            try:
                self.payloads.append(json.loads(content))
            except (json.JSONDecodeError, TypeError):
                pass
        return ("req", a, k)

    async def send(self, req, stream=True):
        r = self._responses[self._i]
        self._i += 1
        return r

    async def aclose(self) -> None:
        pass


async def run_fold(cfg, base_body, first_resp, later_resps) -> list:
    client = FakeClient(later_resps)
    out = b""
    async for chunk in fold_stream(client, cfg, base_body, {}, first_resp):
        out += chunk
    return await parse_events(out)


async def run_fold_capture(cfg, base_body, first_resp, client) -> list:
    """Like run_fold but uses a caller-supplied client (to inspect client.payloads)."""
    out = b""
    async for chunk in fold_stream(client, cfg, base_body, {}, first_resp):
        out += chunk
    return await parse_events(out)


# --- test registry ----------------------------------------------------------

_RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    _RESULTS.append((name, bool(cond), detail))


# --- 1. truncation math -----------------------------------------------------


def test_truncation_math():
    for n, tok in enumerate([516, 1034, 1552, 2070, 2588], start=1):
        check(f"is_truncation({tok})", is_truncation_pattern(tok))
        check(f"tier_n({tok})=={n}", tier_n(tok) == n, str(tier_n(tok)))
    for bad in (515, 517, 0, None):
        check(f"not is_truncation({bad})", not is_truncation_pattern(bad))
    # window
    check("should_continue 516 default", should_continue(516, min_n=1, max_n=0))
    check(
        "should_continue 2588 max_n=3 blocked",
        not should_continue(2588, min_n=1, max_n=3),
    )
    check(
        "should_continue 516 min_n=2 blocked",
        not should_continue(516, min_n=2, max_n=0),
    )
    check("should_continue None", not should_continue(None, min_n=1, max_n=0))


# --- 2. SSE framing robustness ---------------------------------------------


async def test_sse_framing():
    data = (FIXTURES / "codex_poc_r1.sse.txt").read_bytes()
    whole = await parse_events(data)

    # odd-sized chunks must produce identical events
    async def chunked(src: bytes, size: int):
        for i in range(0, len(src), size):
            yield src[i : i + size]

    pieces = []
    async for e in incremental_sse(chunked(data, 7)):
        pieces.append(e)

    check(
        "sse whole-vs-chunked count",
        len(whole) == len(pieces),
        f"{len(whole)} vs {len(pieces)}",
    )
    types_w = [e.get("type") for e in whole if isinstance(e, dict)]
    types_c = [e.get("type") for e in pieces if isinstance(e, dict)]
    check("sse whole-vs-chunked types", types_w == types_c)
    check("sse has completed", "response.completed" in types_w)
    check("sse no spurious DONE", DONE not in whole)  # Codex sends no [DONE]


# --- 3. fold rewrite on real r1 + r2 captures -------------------------------


async def test_fold_real_captures():
    cfg = load_config(ROOT / "config.toml")
    cfg = replace(
        cfg, cont=replace(cfg.cont, max_continue=1)
    )  # r1 -> continue -> r2 -> stop

    r1 = FakeResp((FIXTURES / "codex_poc_r1.sse.txt").read_bytes())
    r2 = FakeResp((FIXTURES / "codex_poc_r2.sse.txt").read_bytes())
    base_body = {"model": "gpt-5.5", "input": [{"role": "user", "content": "q"}]}

    evs = await run_fold(cfg, base_body, r1, [r2])
    dict_evs = [e for e in evs if isinstance(e, dict)]
    types = [e.get("type") for e in dict_evs]

    check("fold one created", types.count("response.created") == 1)
    check("fold one in_progress", types.count("response.in_progress") == 1)
    check(
        "fold one terminal",
        sum(
            types.count(t)
            for t in ("response.completed", "response.failed", "response.incomplete")
        )
        == 1,
    )

    seqs = [e["sequence_number"] for e in dict_evs]
    check("fold seq monotonic 0..n", seqs == list(range(len(dict_evs))), str(seqs[:5]))

    # reasoning items forwarded at ds_oi 0 then 1
    rdone = [
        e
        for e in dict_evs
        if e.get("type") == "response.output_item.done"
        and (e.get("item") or {}).get("type") == "reasoning"
    ]
    check("fold 2 reasoning items", len(rdone) == 2, str(len(rdone)))
    # forward_marker defaults true → the commentary marker owns ds_oi 1
    check(
        "fold reasoning oi 0,2 (marker at 1)",
        [e["output_index"] for e in rdone] == [0, 2],
        str([e.get("output_index") for e in rdone]),
    )

    # message flushed (r2) at ds_oi 2; r1 message discarded
    deltas = "".join(
        e.get("delta", "")
        for e in dict_evs
        if e.get("type") == "response.output_text.delta"
    )
    check("fold r2 answer present", "答案是" in deltas or "21" in deltas, deltas[:40])
    check("fold r1 message discarded", "最少需要取出" not in deltas)

    created = next(e for e in dict_evs if e.get("type") == "response.created")
    completed = dict_evs[-1]
    created_id = (created.get("response") or {}).get("id")
    completed_id = (completed.get("response") or {}).get("id")
    check(
        "fold created/completed share id",
        created_id == completed_id,
        f"{created_id} vs {completed_id}",
    )
    out_items = (completed.get("response") or {}).get("output") or []
    check(
        "fold reconstructed output non-empty (4 items incl. marker)",
        len(out_items) == 4,
        str(len(out_items)),
    )
    # Agent-facing usage = single-response equivalent (NOT summed input).
    usage = (completed.get("response") or {}).get("usage") or {}
    check(
        "fold input = round1 (4582, not summed)",
        usage.get("input_tokens") == 4582,
        str(usage.get("input_tokens")),
    )
    check(
        "fold cached = round1 (3840)",
        (usage.get("input_tokens_details") or {}).get("cached_tokens") == 3840,
    )
    rt = (usage.get("output_tokens_details") or {}).get("reasoning_tokens")
    check("fold reasoning summed 3104", rt == 516 + 2588, str(rt))
    # output = summed reasoning + final round's non-reasoning (2947-2588=359)
    check(
        "fold output = reasoning + final msg",
        usage.get("output_tokens") == 3104 + (2947 - 2588),
        str(usage.get("output_tokens")),
    )
    check(
        "fold total = input + output",
        usage.get("total_tokens") == 4582 + 3104 + (2947 - 2588),
        str(usage.get("total_tokens")),
    )

    md = (completed.get("response") or {}).get("metadata") or {}
    check(
        "fold proxy_rounds has 2 entries",
        len(md.get("proxy_rounds") or []) == 2,
        str(md.get("proxy_rounds")),
    )
    check(
        "fold stopped_reason max_continue",
        md.get("proxy_stopped_reason") == "max_continue",
        str(md.get("proxy_stopped_reason")),
    )
    billed = md.get("proxy_billed_usage") or {}
    check(
        "fold billed input summed 9722",
        billed.get("input_tokens") == 4582 + 5140,
        str(billed.get("input_tokens")),
    )


# --- 3b. truncated tool call is discarded; clean tool call flushes ----------


def _round(rs_id, enc, reasoning_tokens_val, *, extra_items=None, msg=None):
    evs = [
        {
            "type": "response.created",
            "response": {
                "id": "resp_x",
                "status": "in_progress",
                "model": "gpt-5.5",
                "metadata": {},
            },
        },
        {"type": "response.in_progress", "response": {"id": "resp_x"}},
        {
            "type": "response.output_item.added",
            "output_index": 0,
            "item": {"id": rs_id, "type": "reasoning"},
        },
        {
            "type": "response.output_item.done",
            "output_index": 0,
            "item": {"id": rs_id, "type": "reasoning", "encrypted_content": enc},
        },
    ]
    oi = 1
    for it in extra_items or []:
        evs.append(
            {"type": "response.output_item.added", "output_index": oi, "item": it}
        )
        if it["type"] == "function_call":
            evs.append(
                {
                    "type": "response.function_call_arguments.delta",
                    "output_index": oi,
                    "item_id": it["id"],
                    "delta": it.get("arguments", "{}"),
                }
            )
        evs.append(
            {"type": "response.output_item.done", "output_index": oi, "item": it}
        )
        oi += 1
    if msg is not None:
        evs += [
            {
                "type": "response.output_item.added",
                "output_index": oi,
                "item": {"id": "msg_x", "type": "message"},
            },
            {
                "type": "response.content_part.added",
                "output_index": oi,
                "item_id": "msg_x",
                "content_index": 0,
                "part": {"type": "output_text"},
            },
            {
                "type": "response.output_text.delta",
                "output_index": oi,
                "item_id": "msg_x",
                "content_index": 0,
                "delta": msg,
            },
            {
                "type": "response.output_text.done",
                "output_index": oi,
                "item_id": "msg_x",
                "content_index": 0,
                "text": msg,
            },
            {
                "type": "response.content_part.done",
                "output_index": oi,
                "item_id": "msg_x",
                "content_index": 0,
                "part": {"type": "output_text", "text": msg},
            },
            {
                "type": "response.output_item.done",
                "output_index": oi,
                "item": {
                    "id": "msg_x",
                    "type": "message",
                    "content": [{"type": "output_text", "text": msg}],
                },
            },
        ]
    evs.append(
        {
            "type": "response.completed",
            "response": {
                "id": "resp_x",
                "status": "completed",
                "usage": {
                    "input_tokens": 100,
                    "output_tokens": 50,
                    "total_tokens": 150,
                    "output_tokens_details": {"reasoning_tokens": reasoning_tokens_val},
                },
            },
        }
    )
    return make_sse(evs)


async def test_truncated_tool_call_discarded():
    base = load_config(ROOT / "config.toml")
    # marker forwarding is covered elsewhere; keep the delta stream bare here
    cfg = replace(base, cont=replace(base.cont, forward_marker=False))
    base_body = {"model": "gpt-5.5", "input": [{"role": "user", "content": "q"}]}

    # Round A: truncated (516) + a real tool call. Round B: clean message.
    tool = {
        "id": "fc_a",
        "type": "function_call",
        "name": "shell",
        "call_id": "call_a",
        "arguments": '{"cmd":"ls"}',
    }
    rA = FakeResp(_round("rs_a", "ENC_A", 516, extra_items=[tool]))
    rB = FakeResp(_round("rs_b", "ENC_B", 999, msg="done"))

    evs = [e for e in await run_fold(cfg, base_body, rA, [rB]) if isinstance(e, dict)]
    has_fc = any((e.get("item") or {}).get("type") == "function_call" for e in evs)
    fc_args = any(
        e.get("type") == "response.function_call_arguments.delta" for e in evs
    )
    check("truncated tool call discarded (no fc item)", not has_fc)
    check("truncated tool call discarded (no fc args)", not fc_args)
    deltas = "".join(
        e.get("delta", "") for e in evs if e.get("type") == "response.output_text.delta"
    )
    check("clean round message flushed", deltas == "done", deltas)

    # Clean round ending in a tool call → must flush it through.
    rOnly = FakeResp(_round("rs_c", "ENC_C", 999, extra_items=[tool]))
    evs2 = [e for e in await run_fold(cfg, base_body, rOnly, []) if isinstance(e, dict)]
    has_fc2 = any((e.get("item") or {}).get("type") == "function_call" for e in evs2)
    check("clean round tool call flushed", has_fc2)


# --- commentary continuation (default) vs tool_pair --------------------------


async def test_commentary_continuation_payload():
    base = load_config(ROOT / "config.toml")  # method = "commentary" by default
    cfg = replace(base, cont=replace(base.cont, forward_marker=False))
    base_body = {"model": "gpt-5.5", "input": [{"role": "user", "content": "q"}]}
    rA = FakeResp(_round("rs_a", "ENC_A", 516, msg="trunc"))  # truncated → continue
    rB = FakeResp(_round("rs_b", "ENC_B", 999, msg="done"))  # clean → stop
    client = FakeClient([rB])
    evs = [
        e
        for e in await run_fold_capture(cfg, base_body, rA, client)
        if isinstance(e, dict)
    ]

    check(
        "commentary: one continuation round opened",
        len(client.payloads) == 1,
        str(len(client.payloads)),
    )
    inp = (client.payloads[0].get("input") if client.payloads else []) or []
    last = inp[-1] if inp else {}
    check(
        "commentary: marker is a phase:commentary assistant message",
        last.get("type") == "message"
        and last.get("role") == "assistant"
        and last.get("phase") == "commentary",
        str(last),
    )
    check(
        "commentary: marker text from config",
        (last.get("content") or [{}])[0].get("text") == cfg.cont.marker_text,
    )
    check(
        "commentary: no function_call injected in replay",
        not any(isinstance(x, dict) and x.get("type") == "function_call" for x in inp),
    )
    check(
        "commentary: prior reasoning replayed (encrypted)",
        any(
            isinstance(x, dict)
            and x.get("type") == "reasoning"
            and x.get("encrypted_content")
            for x in inp
        ),
    )
    # forward_marker=false → marker stays hidden from the downstream stream
    check(
        "commentary: marker hidden downstream when forward_marker=false",
        not any((e.get("item") or {}).get("phase") == "commentary" for e in evs),
    )


async def test_tool_pair_continuation_payload():
    base = load_config(ROOT / "config.toml")
    cfg = replace(base, cont=replace(base.cont, method="tool_pair"))
    base_body = {"model": "gpt-5.5", "input": [{"role": "user", "content": "q"}]}
    rA = FakeResp(_round("rs_a", "ENC_A", 516, msg="trunc"))
    rB = FakeResp(_round("rs_b", "ENC_B", 999, msg="done"))
    client = FakeClient([rB])
    await run_fold_capture(cfg, base_body, rA, client)

    inp = (client.payloads[0].get("input") if client.payloads else []) or []
    types = [x.get("type") for x in inp if isinstance(x, dict)]
    check(
        "tool_pair: function_call + output injected",
        "function_call" in types and "function_call_output" in types,
        str(types),
    )
    check(
        "tool_pair: no commentary message in replay",
        not any(isinstance(x, dict) and x.get("phase") == "commentary" for x in inp),
    )


async def test_forward_marker_emits_downstream():
    base = load_config(ROOT / "config.toml")
    cfg = replace(
        base, cont=replace(base.cont, method="commentary", forward_marker=True)
    )
    base_body = {"model": "gpt-5.5", "input": [{"role": "user", "content": "q"}]}
    rA = FakeResp(_round("rs_a", "ENC_A", 516, msg="trunc"))
    rB = FakeResp(_round("rs_b", "ENC_B", 999, msg="done"))
    evs = [e for e in await run_fold(cfg, base_body, rA, [rB]) if isinstance(e, dict)]

    done = [
        e
        for e in evs
        if e.get("type") == "response.output_item.done"
        and (e.get("item") or {}).get("phase") == "commentary"
    ]
    check(
        "forward_marker: one commentary item emitted downstream",
        len(done) == 1,
        str(len(done)),
    )
    delta = "".join(
        e.get("delta", "")
        for e in evs
        if e.get("type") == "response.output_text.delta"
        and e.get("item_id", "").startswith("msg_continue_")
    )
    check(
        "forward_marker: commentary delta carries marker text",
        delta == cfg.cont.marker_text,
        delta,
    )
    # reconstructed output carries the commentary item (so the agent echoes it)
    completed = evs[-1]
    out_items = (completed.get("response") or {}).get("output") or []
    phases = [it.get("phase") for it in out_items if isinstance(it, dict)]
    check(
        "forward_marker: commentary in reconstructed output",
        "commentary" in phases,
        str(phases),
    )
    # sequence numbers stay monotonic 0..n despite the injected item
    seqs = [e["sequence_number"] for e in evs]
    check(
        "forward_marker: seq monotonic with injected marker",
        seqs == list(range(len(evs))),
        str(seqs[:6]),
    )


# --- 2-fix. header transparency (#2) ----------------------------------------


def test_header_transparency():
    cfg = load_config(ROOT / "config.toml")
    client = _make_client()
    check("client invents no user-agent", "user-agent" not in client.headers)
    check("client invents no accept", "accept" not in client.headers)

    agent = [
        ("Authorization", "Bearer agent"),
        ("Content-Type", "application/json"),
        ("User-Agent", "codex_cli_rs/1.0"),
        ("Host", "drop.me"),
        ("Content-Length", "123"),
        ("Accept-Encoding", "gzip"),
        ("Responses-API-Base", "https://override/responses"),
        ("X-Custom", "keep"),
    ]
    out = build_upstream_headers(agent, cfg)
    low = {k.lower(): v for k, v in out.items()}
    check("hdr keeps content-type", low.get("content-type") == "application/json")
    check("hdr keeps user-agent", low.get("user-agent") == "codex_cli_rs/1.0")
    check("hdr keeps custom", low.get("x-custom") == "keep")
    check("hdr keeps authorization", low.get("authorization") == "Bearer agent")
    for dropped in ("host", "content-length", "accept-encoding", "responses-api-base"):
        check(f"hdr drops {dropped}", dropped not in low)


# --- upstream URL resolution via Responses-API-Base header ------------------


class _Req:
    def __init__(self, headers: dict):
        self.headers = Headers(headers)


def test_cli_toml_helpers():
    """middleware/cli.py's config.toml text-surgery + Codex openai_base_url
    wiring helpers -- pure string transforms, no filesystem/process access."""
    from middleware.cli import _set_toml_scalar, _unwire_codex_text, _wire_codex_text

    toml = (
        "[auth]\n"
        'mode = "passthrough"               # passthrough | inject | passthrough_then_inject\n'
        'access_token = ""                  # Bearer token\n'
        "\n"
        "[continue]\n"
        "enabled = true\n"
    )
    out = _set_toml_scalar(toml, "auth", "mode", "inject")
    check("toml scalar: replaces quoted value", 'mode = "inject"' in out, out)
    check(
        "toml scalar: keeps trailing comment",
        "# passthrough | inject | passthrough_then_inject" in out,
    )
    check("toml scalar: does not touch other section", "enabled = true" in out)

    quoted = _set_toml_scalar(toml, "auth", "access_token", 'sk-"quoted"')
    check("toml scalar: escapes embedded quote", '\\"quoted\\"' in quoted, quoted)

    numeric = _set_toml_scalar(toml, "server", "port", 9000, quote=False)
    check(
        "toml scalar: numeric + new section appended",
        "[server]" in numeric and "port = 9000" in numeric,
        numeric,
    )

    codex_cfg = 'model = "gpt-5.5"\n\n[model_providers.foo]\nbase_url = "https://x"\n'
    wired, replaced = _wire_codex_text(codex_cfg, "http://127.0.0.1:8787/v1")
    check("wire-codex: fresh insert is not a replace", replaced is False)
    check(
        "wire-codex: line present",
        'openai_base_url = "http://127.0.0.1:8787/v1"' in wired,
    )
    check(
        "wire-codex: inserted before the first table",
        wired.index("openai_base_url") < wired.index("[model_providers"),
    )

    rewired, replaced2 = _wire_codex_text(wired, "http://127.0.0.1:9999/v1")
    check("wire-codex: re-wiring replaces in place", replaced2 is True)
    key_lines = [ln for ln in rewired.splitlines() if ln.startswith("openai_base_url")]
    check(
        "wire-codex: exactly one key line after re-wiring",
        len(key_lines) == 1,
        str(key_lines),
    )

    restored, removed = _unwire_codex_text(rewired)
    check("unwire-codex: reports removal", removed is True)
    check(
        "unwire-codex: exact round-trip back to the original",
        restored == codex_cfg,
        repr(restored),
    )

    untouched, removed2 = _unwire_codex_text(codex_cfg)
    check(
        "unwire-codex: no-op when absent", removed2 is False and untouched == codex_cfg
    )


def test_paths_resolution():
    """middleware/paths.py: dev checkout vs installed package config locations."""
    from middleware import paths
    from middleware.paths import ENV_CONFIG, ENV_HOME

    old_config = os.environ.get(ENV_CONFIG)
    old_home = os.environ.get(ENV_HOME)
    try:
        os.environ.pop(ENV_CONFIG, None)
        os.environ.pop(ENV_HOME, None)

        if paths.is_dev_checkout():
            check(
                "paths: dev config in repo root",
                paths.config_path() == paths.PACKAGE_ROOT / "config.toml",
            )
            check(
                "paths: dev state in repo .codexcont",
                paths.state_dir() == paths.PACKAGE_ROOT / ".codexcont",
            )

        with patch("middleware.paths.is_dev_checkout", return_value=False):
            home = Path.home() / ".codexcont"
            check(
                "paths: installed config in ~/.codexcont",
                paths.config_path() == home / "config.toml",
            )
            check("paths: installed state in ~/.codexcont", paths.state_dir() == home)

            os.environ[ENV_HOME] = "/tmp/codexcont-test-home"
            custom_home = Path("/tmp/codexcont-test-home").resolve()
            check(
                "paths: CODEXCONT_HOME overrides data dir",
                paths.user_data_dir() == custom_home,
            )
            check(
                "paths: CODEXCONT_HOME config path",
                paths.config_path() == custom_home / "config.toml",
            )
            check(
                "paths: CODEXCONT_HOME backup dir",
                paths.backup_dir() == custom_home / "backup",
            )
            os.environ.pop(ENV_HOME, None)

        with patch("middleware.paths.is_dev_checkout", return_value=False):
            check(
                "paths: installed backup in ~/.codexcont/backup",
                paths.backup_dir() == Path.home() / ".codexcont" / "backup",
            )

        os.environ[ENV_CONFIG] = "/tmp/codexcont-custom.toml"
        check(
            "paths: CODEXCONT_CONFIG wins over mode",
            paths.config_path() == Path("/tmp/codexcont-custom.toml").resolve(),
        )
        os.environ.pop(ENV_CONFIG, None)

        example = paths.read_example_config()
        check(
            "paths: example config readable",
            example is not None and "[server]" in example,
        )
        check(
            "paths: example config path or bundle",
            paths.example_config_path().exists() or example is not None,
        )
    finally:
        if old_config is None:
            os.environ.pop(ENV_CONFIG, None)
        else:
            os.environ[ENV_CONFIG] = old_config
        if old_home is None:
            os.environ.pop(ENV_HOME, None)
        else:
            os.environ[ENV_HOME] = old_home


class _CannedHTTP:
    """Answers client.request() (the /v1/* passthrough path) with one canned
    response; records every call."""

    def __init__(self, status: int = 200, body: bytes = b"{}"):
        self.status = status
        self.body = body
        self.calls: list[tuple[str, str]] = []

    async def request(self, method, url, content=None, headers=None, timeout=None):
        self.calls.append((method, url))

        class _R:
            status_code = self.status
            content = self.body
            headers = {"content-type": "application/json"}

        return _R()

    async def aclose(self):
        pass


class _NoNetworkHTTP:
    async def request(self, *a, **k):
        raise ConnectionError("offline")

    async def aclose(self):
        pass


def test_models_endpoint():
    """GET /v1/models must 200 -- real upstream catalog when reachable,
    fallback to the configured cosmetic list when not (never a 404)."""
    cfg = load_config(ROOT / "config.toml")
    app = create_app(cfg)
    with TestClient(app) as client:
        # upstream unreachable -> cosmetic fallback list
        app.state.client = _NoNetworkHTTP()
        r = client.get("/v1/models", params={"client_version": "0.142.5"})
        check(
            "models list: 200 (no more 404 noise)",
            r.status_code == 200,
            str(r.status_code),
        )
        body = r.json()
        ids = [m.get("id") for m in body.get("data", [])]
        check(
            "models list: object=list",
            body.get("object") == "list",
            str(body.get("object")),
        )
        check("models list: ids match config", ids == list(cfg.models.ids), str(ids))

        # upstream reachable -> its real catalog is forwarded instead
        canned = _CannedHTTP(
            body=json.dumps(
                {"object": "list", "data": [{"id": "real-upstream-model"}]}
            ).encode()
        )
        app.state.client = canned
        r2 = client.get("/v1/models")
        check(
            "models list: real upstream catalog preferred",
            [m.get("id") for m in r2.json().get("data", [])]
            == ["real-upstream-model"],
            r2.text[:80],
        )
        check(
            "models list: upstream URL derived from responses base",
            bool(canned.calls) and canned.calls[0][1].endswith("/models"),
            str(canned.calls),
        )

        one = client.get(f"/v1/models/{cfg.models.ids[0]}")
        check("models get: 200", one.status_code == 200, str(one.status_code))
        check("models get: id echoed", one.json().get("id") == cfg.models.ids[0])

        health = client.get("/health")
        check("health: 200", health.status_code == 200, str(health.status_code))


def test_v1_catchall_passthrough():
    """Any other /v1/* call must reach the upstream base transparently."""
    cfg = load_config(ROOT / "config.toml")
    app = create_app(cfg)
    canned = _CannedHTTP(body=b'{"ok": true}')
    with TestClient(app) as client:
        app.state.client = canned
        r = client.get("/v1/some/unknown/endpoint?limit=5")
        check("v1 catch-all: upstream status/body relayed", r.status_code == 200)
        check("v1 catch-all: body forwarded", r.json() == {"ok": True}, r.text[:80])
        called = canned.calls[0][1] if canned.calls else ""
        check(
            "v1 catch-all: path + query forwarded to upstream base",
            called.endswith("/some/unknown/endpoint?limit=5"),
            called,
        )


def test_upstream_url_resolution():
    base = load_config(ROOT / "config.toml")
    fixed = replace(
        base, upstream=replace(base.upstream, mode="fixed", url="https://cfg/responses")
    )
    header = replace(
        base,
        upstream=replace(base.upstream, mode="header", url="https://cfg/responses"),
    )
    with_hdr = _Req({"Responses-API-Base": "https://override/v1"})
    no_hdr = _Req({})

    check(
        "fixed ignores header",
        _resolve_upstream_url(fixed, with_hdr) == "https://cfg/responses",
    )
    check(
        "header appends /responses to base",
        _resolve_upstream_url(header, with_hdr) == "https://override/v1/responses",
    )
    check(
        "header falls back to url",
        _resolve_upstream_url(header, no_hdr) == "https://cfg/responses",
    )
    check(
        "header trims trailing slash + case-insensitive",
        _resolve_upstream_url(header, _Req({"responses-api-base": "https://low/v1/"}))
        == "https://low/v1/responses",
    )
    check(
        "header full endpoint left as-is",
        _resolve_upstream_url(
            header, _Req({"Responses-API-Base": "https://x/v1/responses"})
        )
        == "https://x/v1/responses",
    )
    check(
        "header blank → fallback",
        _resolve_upstream_url(header, _Req({"Responses-API-Base": "   "}))
        == "https://cfg/responses",
    )

    # header_required: present → use it; absent/blank → None (caller returns 400)
    req = replace(
        base,
        upstream=replace(
            base.upstream, mode="header_required", url="https://cfg/responses"
        ),
    )
    check(
        "required appends /responses",
        _resolve_upstream_url(req, with_hdr) == "https://override/v1/responses",
    )
    check("required missing → None", _resolve_upstream_url(req, no_hdr) is None)
    check(
        "required blank → None",
        _resolve_upstream_url(req, _Req({"Responses-API-Base": " "})) is None,
    )


# --- security guard: never send config creds to a header-supplied URL --------


def test_auth_safety_guard():
    base = load_config(ROOT / "config.toml")

    def blocked(url_mode, auth_mode, token, has_hdr, has_auth):
        cfg = replace(
            base,
            upstream=replace(base.upstream, mode=url_mode),
            auth=replace(base.auth, mode=auth_mode, access_token=token),
        )
        h = {}
        if has_hdr:
            h["Responses-API-Base"] = "https://external/responses"
        if has_auth:
            h["Authorization"] = "Bearer agent"
        rq = _Req(h)
        from_hdr = _url_is_from_header(cfg, rq)
        inj = would_inject_authorization(
            cfg, agent_has_authorization=rq.headers.get("authorization") is not None
        )
        return from_hdr and inj  # the exact condition handle_responses rejects on

    # fixed url → always safe
    check(
        "guard: fixed+inject allow", not blocked("fixed", "inject", "TOK", True, False)
    )
    # header + passthrough → never injects → allow
    check(
        "guard: header+passthrough allow",
        not blocked("header", "passthrough", "TOK", True, False),
    )
    # header + inject + header present → block (even if agent has its own auth)
    check(
        "guard: header+inject+hdr block (noauth)",
        blocked("header", "inject", "TOK", True, False),
    )
    check(
        "guard: header+inject+hdr block (auth)",
        blocked("header", "inject", "TOK", True, True),
    )
    # header + inject, no header → config url → allow
    check(
        "guard: header+inject no-hdr allow",
        not blocked("header", "inject", "TOK", False, False),
    )
    # header + PtI + header + agent has own auth → allow (uses agent's)
    check(
        "guard: header+PtI+hdr+auth allow",
        not blocked("header", "passthrough_then_inject", "TOK", True, True),
    )
    # header + PtI + header + no agent auth → block (would inject config)
    check(
        "guard: header+PtI+hdr+noauth block",
        blocked("header", "passthrough_then_inject", "TOK", True, False),
    )
    # header_required + inject + header → block
    check(
        "guard: required+inject+hdr block",
        blocked("header_required", "inject", "TOK", True, False),
    )
    # empty configured token → nothing to leak → allow
    check("guard: empty token allow", not blocked("header", "inject", "", True, False))


# --- auth injection from config (#2 follow-up) ------------------------------


def test_auth_injection():
    base = load_config(ROOT / "config.toml")

    def hdrs(cfg, agent):
        return {k.lower(): v for k, v in build_upstream_headers(agent, cfg).items()}

    # passthrough_then_inject: inject token when agent sends none; empty account → no header
    cfg = replace(
        base,
        auth=replace(
            base.auth,
            mode="passthrough_then_inject",
            access_token="TOK",
            chatgpt_account_id="",
        ),
    )
    out = hdrs(cfg, [("x", "1")])
    check("inject token when missing", out.get("authorization") == "Bearer TOK")
    check("no account header when empty", "chatgpt-account-id" not in out)

    # passthrough_then_inject: agent's auth wins (not overridden)
    out2 = hdrs(cfg, [("Authorization", "Bearer AGENT")])
    check("fallback keeps agent auth", out2.get("authorization") == "Bearer AGENT")

    # inject: config overrides agent + adds account
    cfg2 = replace(
        base,
        auth=replace(
            base.auth, mode="inject", access_token="TOK", chatgpt_account_id="acct1"
        ),
    )
    out3 = hdrs(cfg2, [("Authorization", "Bearer AGENT")])
    check("inject overrides agent auth", out3.get("authorization") == "Bearer TOK")
    check("inject adds account", out3.get("chatgpt-account-id") == "acct1")

    # passthrough: never inject anything
    cfg3 = replace(
        base,
        auth=replace(
            base.auth,
            mode="passthrough",
            access_token="TOK",
            chatgpt_account_id="acct1",
        ),
    )
    out4 = hdrs(cfg3, [("x", "1")])
    check(
        "passthrough never injects",
        "authorization" not in out4 and "chatgpt-account-id" not in out4,
    )


# --- 4-fix. reasoning/stream gating (#4) ------------------------------------


def test_reasoning_gate():
    check(
        "reasoning_enabled dict", reasoning_enabled({"reasoning": {"effort": "high"}})
    )
    check("reasoning_enabled absent → true", reasoning_enabled({"input": []}))
    check("reasoning_enabled null → true", reasoning_enabled({"reasoning": None}))
    check("reasoning_enabled empty dict → true", reasoning_enabled({"reasoning": {}}))
    check(
        "reasoning_enabled explicit false → false",
        not reasoning_enabled({"reasoning": False}),
    )


# --- previous_response_id local chain resolution ----------------------------
# Every round this proxy opens upstream is a fresh, unrelated HTTP request, so
# upstream can never resolve a client-supplied `previous_response_id` (it
# 400s with `Unsupported parameter: previous_response_id`). Codex (>= ~0.142)
# chains tool-loop steps and follow-up turns this way, so the proxy must
# splice the cached history itself and always drop the field.


def test_resolve_previous_response_id():
    store = ChainStore()
    store.set(
        "resp_1",
        [{"role": "user", "content": "hello"}, {"type": "message", "id": "m1"}],
    )

    # absent: untouched, same object, nothing reported
    body = {"model": "gpt-5.5", "input": [{"role": "user", "content": "hi"}]}
    out, prev_id, hit = resolve_previous_response_id(body, store)
    check("resolve: no-op when absent", out is body and prev_id is None and not hit)

    # hit: cached items spliced ahead of the new input, id dropped
    body2 = {
        "model": "gpt-5.5",
        "previous_response_id": "resp_1",
        "input": [{"role": "user", "content": "follow-up"}],
    }
    out2, prev_id2, hit2 = resolve_previous_response_id(body2, store)
    check("resolve: hit reports the id", prev_id2 == "resp_1" and hit2)
    check(
        "resolve: previous_response_id stripped on hit",
        "previous_response_id" not in out2,
    )
    check(
        "resolve: cached items spliced ahead of new input",
        out2["input"]
        == [
            {"role": "user", "content": "hello"},
            {"type": "message", "id": "m1"},
            {"role": "user", "content": "follow-up"},
        ],
        str(out2["input"]),
    )

    # miss: id still stripped (never forwarded), input left as sent (best effort)
    body3 = {
        "model": "gpt-5.5",
        "previous_response_id": "resp_missing",
        "input": [{"role": "user", "content": "orphan"}],
    }
    out3, prev_id3, hit3 = resolve_previous_response_id(body3, store)
    check("resolve: miss reports the id", prev_id3 == "resp_missing" and not hit3)
    check(
        "resolve: miss still strips previous_response_id",
        "previous_response_id" not in out3,
    )
    check("resolve: miss keeps the caller's own input", out3["input"] == body3["input"])


# --- 3-fix. stateful follow-up repair (#3) ----------------------------------


def test_stateful_repair():
    store = IdStore()
    store.add("rs_keep")
    inp = [
        {"role": "user", "content": "q"},
        {"type": "reasoning", "id": "rs_keep", "encrypted_content": "E1"},
        {
            "type": "reasoning",
            "id": "rs_natural",
            "encrypted_content": "E2",
        },  # not recorded
        {"type": "message", "id": "msg"},
    ]
    out = repair_followup_input(
        inp, store, tool_name="continue_thinking", output_text="go"
    )

    # pair inserted right after rs_keep only
    idx = next(
        i for i, x in enumerate(out) if isinstance(x, dict) and x.get("id") == "rs_keep"
    )
    nxt = out[idx + 1]
    nxt2 = out[idx + 2]
    cid = continue_call_id("rs_keep")
    check(
        "stateful inserts call after recorded id",
        nxt.get("type") == "function_call" and nxt.get("call_id") == cid,
        str(nxt),
    )
    check(
        "stateful inserts output after call",
        nxt2.get("type") == "function_call_output" and nxt2.get("call_id") == cid,
    )

    # natural-consecutive reasoning (unrecorded) gets NO splice
    nidx = next(
        i
        for i, x in enumerate(out)
        if isinstance(x, dict) and x.get("id") == "rs_natural"
    )
    check(
        "stateful no splice for unrecorded id",
        out[nidx + 1].get("type") == "message",
        str(out[nidx + 1]),
    )

    # idempotent: re-running adds nothing
    out2 = repair_followup_input(
        out, store, tool_name="continue_thinking", output_text="go"
    )
    check("stateful idempotent", len(out2) == len(out), f"{len(out)} -> {len(out2)}")


# --- 7-fix. graceful EOF → incomplete (#7) ----------------------------------


async def test_eof_incomplete():
    cfg = load_config(ROOT / "config.toml")
    base_body = {"model": "gpt-5.5", "input": [{"role": "user", "content": "q"}]}
    # A round that streams reasoning + message but NO terminal event.
    events = [
        {
            "type": "response.created",
            "response": {"id": "resp_e", "status": "in_progress"},
        },
        {"type": "response.in_progress", "response": {"id": "resp_e"}},
        {
            "type": "response.output_item.added",
            "output_index": 0,
            "item": {"id": "rs_e", "type": "reasoning"},
        },
        {
            "type": "response.output_item.done",
            "output_index": 0,
            "item": {"id": "rs_e", "type": "reasoning", "encrypted_content": "E"},
        },
        {
            "type": "response.output_item.added",
            "output_index": 1,
            "item": {"id": "msg_e", "type": "message"},
        },
        {
            "type": "response.output_text.delta",
            "output_index": 1,
            "item_id": "msg_e",
            "content_index": 0,
            "delta": "partial",
        },
        {
            "type": "response.output_item.done",
            "output_index": 1,
            "item": {"id": "msg_e", "type": "message"},
        },
        # <-- no response.completed
    ]
    evs = [
        e
        for e in await run_fold(cfg, base_body, FakeResp(make_sse(events)), [])
        if isinstance(e, dict)
    ]
    term = evs[-1]
    check(
        "eof terminal is incomplete",
        term.get("type") == "response.incomplete",
        term.get("type"),
    )
    reason = ((term.get("response") or {}).get("incomplete_details") or {}).get(
        "reason"
    )
    check("eof reason upstream_eof", reason == "upstream_eof", str(reason))

    # buffered tentative output must NOT leak on EOF (only reasoning survives)
    leaked = any(e.get("type") == "response.output_text.delta" for e in evs)
    check("eof does not leak buffered message", not leaked)
    out_items = (term.get("response") or {}).get("output") or []
    check(
        "eof output is reasoning only",
        all(it.get("type") == "reasoning" for it in out_items) and len(out_items) == 1,
        str([it.get("type") for it in out_items]),
    )


# --- 8. WebSocket transport (bridges ws(s)://.../v1/responses to the same
#        fold/passthrough pipeline the POST route uses) ---------------------


def _ws_drain(ws, limit: int = 50) -> list[dict]:
    """Receive WS JSON text frames until a terminal/error event (or a safety
    cap, so a regression hangs the test instead of the whole suite)."""
    evs: list[dict] = []
    for _ in range(limit):
        ev = json.loads(ws.receive_text())
        evs.append(ev)
        if ev.get("type") in (
            "response.completed",
            "response.incomplete",
            "response.failed",
            "error",
        ):
            break
    return evs


def test_ws_fold_roundtrip():
    """A truncated round (516) folded with a clean round into ONE response.*
    event stream over the WebSocket route, same behavior as the POST route."""
    cfg = load_config(ROOT / "config.toml")
    app = create_app(cfg)
    tool = {
        "id": "fc_a",
        "type": "function_call",
        "name": "shell",
        "call_id": "call_a",
        "arguments": '{"cmd":"ls"}',
    }
    rA = FakeResp(_round("rs_a", "ENC_A", 516, extra_items=[tool]))
    rB = FakeResp(_round("rs_b", "ENC_B", 999, msg="done"))
    fake = FakeClient([rA, rB])  # round 1, then the continuation

    with TestClient(app) as client:
        app.state.client = fake
        with client.websocket_connect("/v1/responses") as ws:
            ws.send_text(
                json.dumps(
                    {
                        "type": "response.create",
                        "model": "gpt-5.5",
                        "input": [{"role": "user", "content": "q"}],
                    }
                )
            )
            evs = _ws_drain(ws)

    check(
        "ws fold: terminal event type",
        bool(evs) and evs[-1].get("type") == "response.completed",
        str(evs[-1].get("type")) if evs else "no events",
    )
    # round 1 must be shaped by build_round_payload, same as the POST route
    r1_payload = fake.payloads[0] if fake.payloads else {}
    check(
        "ws fold: round-1 payload merges encrypted include",
        "reasoning.encrypted_content" in (r1_payload.get("include") or []),
        str(r1_payload.get("include")),
    )
    has_fc = any((e.get("item") or {}).get("type") == "function_call" for e in evs)
    check("ws fold: truncated tool call discarded", not has_fc)
    deltas = "".join(
        e.get("delta", "")
        for e in evs
        if e.get("type") == "response.output_text.delta"
        and not e.get("item_id", "").startswith("msg_continue_")
    )
    check("ws fold: clean round message flushed", deltas == "done", deltas)
    # forward_marker defaults true → the commentary marker reaches the agent
    check(
        "ws fold: commentary marker forwarded by default",
        any((e.get("item") or {}).get("phase") == "commentary" for e in evs),
    )
    rounds = ((evs[-1].get("response") or {}).get("metadata") or {}).get("proxy_rounds")
    check(
        "ws fold: both rounds recorded in metadata",
        rounds
        == [
            {"round": 1, "reasoning_tokens": 516, "n": 1},
            {"round": 2, "reasoning_tokens": 999, "n": None},
        ],
        str(rounds),
    )
    check(
        "ws fold: no error frame sent",
        not any(e.get("type") == "error" for e in evs),
    )


def test_ws_passthrough():
    """reasoning=false must skip folding entirely: events relayed unchanged,
    with no renumbering and no continuation round opened."""
    cfg = load_config(ROOT / "config.toml")
    app = create_app(cfg)
    # reasoning_tokens matches the 518n-2 fingerprint on purpose: a
    # non-reasoning request must never engage the fold regardless.
    events = [
        {
            "type": "response.created",
            "response": {"id": "resp_p", "status": "in_progress"},
        },
        {
            "type": "response.output_item.added",
            "output_index": 0,
            "item": {"id": "msg_p", "type": "message"},
        },
        {
            "type": "response.output_text.delta",
            "output_index": 0,
            "item_id": "msg_p",
            "delta": "hi",
        },
        {
            "type": "response.completed",
            "response": {
                "id": "resp_p",
                "status": "completed",
                "usage": {"output_tokens_details": {"reasoning_tokens": 516}},
            },
        },
    ]
    raw = make_sse(events)

    with TestClient(app) as client:
        app.state.client = FakeClient([FakeResp(raw)])  # exactly one round available
        with client.websocket_connect("/v1/responses") as ws:
            ws.send_text(
                json.dumps(
                    {
                        "type": "response.create",
                        "model": "gpt-5.5",
                        "reasoning": False,
                        "input": [{"role": "user", "content": "q"}],
                    }
                )
            )
            evs = _ws_drain(ws)

    check("ws passthrough: events relayed unchanged", evs == events, str(evs))


def test_ws_round1_error():
    """A round-1 upstream 4xx must surface as one `{"type": "error", ...}`
    frame (the shape Codex's WebSocket client parses) instead of a hang."""
    cfg = load_config(ROOT / "config.toml")
    app = create_app(cfg)
    err_body = json.dumps(
        {"error": {"message": "invalid api key", "code": "invalid_api_key"}}
    ).encode()

    with TestClient(app) as client:
        app.state.client = FakeClient([FakeResp(err_body, status=401)])
        with client.websocket_connect("/v1/responses") as ws:
            ws.send_text(
                json.dumps(
                    {
                        "type": "response.create",
                        "model": "gpt-5.5",
                        "input": [{"role": "user", "content": "q"}],
                    }
                )
            )
            evs = _ws_drain(ws)

    check("ws error: exactly one event", len(evs) == 1, str(evs))
    ev = evs[0] if evs else {}
    check("ws error: type=error", ev.get("type") == "error", str(ev.get("type")))
    check("ws error: status echoed", ev.get("status") == 401, str(ev.get("status")))
    err = ev.get("error") or {}
    check(
        "ws error: message/code extracted from upstream body",
        err.get("message") == "invalid api key"
        and err.get("code") == "invalid_api_key",
        str(err),
    )


def test_ws_previous_response_id_chain():
    cfg = load_config(ROOT / "config.toml")
    app = create_app(cfg)
    r1 = FakeResp(_round("rs_a", "ENC_A", 999, msg="turn1 done"))
    r2 = FakeResp(_round("rs_b", "ENC_B", 999, msg="turn2 done"))
    fake = FakeClient([r1, r2])

    with TestClient(app) as client:
        app.state.client = fake
        with client.websocket_connect("/v1/responses") as ws:
            ws.send_text(
                json.dumps(
                    {
                        "type": "response.create",
                        "model": "gpt-5.5",
                        "input": [{"role": "user", "content": "turn-1-marker"}],
                    }
                )
            )
            evs1 = _ws_drain(ws)
            resp_id = (evs1[-1].get("response") or {}).get("id") if evs1 else None

            ws.send_text(
                json.dumps(
                    {
                        "type": "response.create",
                        "model": "gpt-5.5",
                        "previous_response_id": resp_id,
                        "input": [{"role": "user", "content": "turn-2-marker"}],
                    }
                )
            )
            evs2 = _ws_drain(ws)

    check("chain: turn1 got a response id", bool(resp_id), str(resp_id))
    check(
        "chain: turn2 completes cleanly",
        bool(evs2) and evs2[-1].get("type") == "response.completed",
        str(evs2[-1] if evs2 else "no events"),
    )
    sent = fake.payloads[-1] if fake.payloads else {}
    check(
        "chain: previous_response_id dropped before upstream",
        "previous_response_id" not in sent,
        str(sent.get("previous_response_id", "<absent>")),
    )
    blob = json.dumps(sent.get("input"))
    check(
        "chain: turn-1 input spliced into turn-2 upstream payload",
        "turn-1-marker" in blob and "turn-2-marker" in blob,
        blob,
    )


def _passthrough_round(resp_id: str, text: str) -> bytes:
    """A message-only stream whose terminal carries NO `output` (codex-backend
    shape), so chain recording must fall back to the output_item.done items."""
    events = [
        {
            "type": "response.created",
            "response": {"id": resp_id, "status": "in_progress"},
        },
        {
            "type": "response.output_item.added",
            "output_index": 0,
            "item": {"id": f"msg_{resp_id}", "type": "message"},
        },
        {
            "type": "response.output_text.delta",
            "output_index": 0,
            "item_id": f"msg_{resp_id}",
            "delta": text,
        },
        {
            "type": "response.output_item.done",
            "output_index": 0,
            "item": {
                "id": f"msg_{resp_id}",
                "type": "message",
                "content": [{"type": "output_text", "text": text}],
            },
        },
        {
            "type": "response.completed",
            "response": {"id": resp_id, "status": "completed"},
        },
    ]
    return make_sse(events)


def test_ws_passthrough_chain_recorded():
    """A passthrough WS turn (reasoning=false, never folded) must still record
    its chain, or the next chained turn silently loses all prior context."""
    cfg = load_config(ROOT / "config.toml")
    app = create_app(cfg)
    fake = FakeClient(
        [
            FakeResp(_passthrough_round("resp_pt1", "pt-answer")),
            FakeResp(_passthrough_round("resp_pt2", "pt-answer-2")),
        ]
    )

    with TestClient(app) as client:
        app.state.client = fake
        with client.websocket_connect("/v1/responses") as ws:
            ws.send_text(
                json.dumps(
                    {
                        "type": "response.create",
                        "model": "gpt-5.5",
                        "reasoning": False,
                        "input": [{"role": "user", "content": "pt-turn-1"}],
                    }
                )
            )
            _ws_drain(ws)
            ws.send_text(
                json.dumps(
                    {
                        "type": "response.create",
                        "model": "gpt-5.5",
                        "reasoning": False,
                        "previous_response_id": "resp_pt1",
                        "input": [{"role": "user", "content": "pt-turn-2"}],
                    }
                )
            )
            evs2 = _ws_drain(ws)

    check(
        "ws passthrough chain: turn2 completes cleanly",
        bool(evs2) and evs2[-1].get("type") == "response.completed",
        str(evs2[-1] if evs2 else "no events"),
    )
    sent = fake.payloads[-1] if fake.payloads else {}
    check(
        "ws passthrough chain: previous_response_id dropped before upstream",
        "previous_response_id" not in sent,
    )
    blob = json.dumps(sent.get("input"))
    check(
        "ws passthrough chain: turn-1 input AND output spliced into turn-2",
        "pt-turn-1" in blob and "pt-answer" in blob and "pt-turn-2" in blob,
        blob,
    )


def test_http_passthrough_chain_recorded():
    """Same as above for the POST route: the raw stream is teed through an SSE
    parser purely to record the chain; bytes reach the agent untouched."""
    cfg = load_config(ROOT / "config.toml")
    app = create_app(cfg)
    fake = FakeClient(
        [
            FakeResp(_passthrough_round("resp_h1", "h1-answer")),
            FakeResp(_passthrough_round("resp_h2", "h2-answer")),
        ]
    )

    with TestClient(app) as client:
        app.state.client = fake
        r1 = client.post(
            "/v1/responses",
            json={
                "model": "gpt-5.5",
                "stream": True,
                "reasoning": False,
                "input": [{"role": "user", "content": "h-turn-1"}],
            },
        )
        r2 = client.post(
            "/v1/responses",
            json={
                "model": "gpt-5.5",
                "stream": True,
                "reasoning": False,
                "previous_response_id": "resp_h1",
                "input": [{"role": "user", "content": "h-turn-2"}],
            },
        )

    check("http passthrough chain: turn1 200", r1.status_code == 200)
    check(
        "http passthrough chain: bytes relayed untouched",
        "h1-answer" in r1.text,
        r1.text[:80],
    )
    check("http passthrough chain: turn2 200", r2.status_code == 200)
    sent = fake.payloads[-1] if fake.payloads else {}
    check(
        "http passthrough chain: previous_response_id dropped before upstream",
        "previous_response_id" not in sent,
    )
    blob = json.dumps(sent.get("input"))
    check(
        "http passthrough chain: turn-1 input AND output spliced into turn-2",
        "h-turn-1" in blob and "h1-answer" in blob and "h-turn-2" in blob,
        blob,
    )


def test_compressed_request_body():
    """zstd/gzip-compressed POST bodies (Codex's built-in provider with request
    compression on) must be decompressed before parsing and forwarding."""
    import gzip

    import zstandard

    cfg = load_config(ROOT / "config.toml")
    for enc, compress in (
        ("gzip", gzip.compress),
        ("zstd", lambda b: zstandard.ZstdCompressor().compress(b)),
    ):
        app = create_app(cfg)
        fake = FakeClient([FakeResp(_passthrough_round(f"resp_{enc}", "z-answer"))])
        body = {
            "model": "gpt-5.5",
            "stream": True,
            "reasoning": False,
            "input": [{"role": "user", "content": f"{enc}-marker"}],
        }
        with TestClient(app) as client:
            app.state.client = fake
            r = client.post(
                "/v1/responses",
                content=compress(json.dumps(body).encode()),
                headers={"content-type": "application/json", "content-encoding": enc},
            )
        check(f"compressed body ({enc}): 200", r.status_code == 200, r.text[:80])
        sent = fake.payloads[-1] if fake.payloads else {}
        check(
            f"compressed body ({enc}): decompressed before forwarding",
            f"{enc}-marker" in json.dumps(sent.get("input")),
            str(sent)[:80],
        )


# --- runner -----------------------------------------------------------------


async def _main():
    test_truncation_math()
    await test_sse_framing()
    await test_fold_real_captures()
    await test_truncated_tool_call_discarded()
    await test_commentary_continuation_payload()
    await test_tool_pair_continuation_payload()
    await test_forward_marker_emits_downstream()
    test_header_transparency()
    test_cli_toml_helpers()
    test_paths_resolution()
    test_models_endpoint()
    test_v1_catchall_passthrough()
    test_upstream_url_resolution()
    test_auth_safety_guard()
    test_auth_injection()
    test_reasoning_gate()
    test_resolve_previous_response_id()
    test_stateful_repair()
    await test_eof_incomplete()
    test_ws_fold_roundtrip()
    test_ws_passthrough()
    test_ws_round1_error()
    test_ws_previous_response_id_chain()
    test_ws_passthrough_chain_recorded()
    test_http_passthrough_chain_recorded()
    test_compressed_request_body()


def main():
    asyncio.run(_main())
    passed = sum(1 for _, ok, _ in _RESULTS if ok)
    for name, ok, detail in _RESULTS:
        mark = "PASS" if ok else "FAIL"
        line = f"[{mark}] {name}"
        if not ok and detail:
            line += f"  -- {detail}"
        print(line)
    print(f"\n{passed}/{len(_RESULTS)} checks passed")
    sys.exit(0 if passed == len(_RESULTS) else 1)


if __name__ == "__main__":
    main()
