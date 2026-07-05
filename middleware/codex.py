"""Truncation math + Responses-API request builders.

Re-implemented from poc_continue_thinking_codex.py (used as a reference spec,
not copied). The 518n-2 detector, the continuation-input shape, and the
deterministic continue pair all live here so proxy.py stays focused on the
streaming state machine.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

DEFAULT_TRUNCATION_STEP = 518
ENCRYPTED_INCLUDE = "reasoning.encrypted_content"


# --- 518*n - 2 truncation fingerprint ---------------------------------------


def is_truncation_pattern(
    tokens: int | None, step: int = DEFAULT_TRUNCATION_STEP
) -> bool:
    """True iff reasoning_tokens lands exactly on step*n - 2 (516, 1034, ...)."""
    return tokens is not None and tokens >= step - 2 and (tokens + 2) % step == 0


def tier_n(tokens: int | None, step: int = DEFAULT_TRUNCATION_STEP) -> int | None:
    """The tier n for a truncation-pattern token count, else None."""
    if not is_truncation_pattern(tokens, step):
        return None
    assert tokens is not None
    return (tokens + 2) // step


def should_continue(
    tokens: int | None,
    *,
    min_n: int = 1,
    max_n: int = 0,
    step: int = DEFAULT_TRUNCATION_STEP,
) -> bool:
    """Continue iff truncated AND min_n <= tier_n <= max_n (max_n=0 means no cap)."""
    n = tier_n(tokens, step)
    if n is None:
        return False
    if n < min_n:
        return False
    if max_n and n > max_n:
        return False
    return True


def reasoning_tokens(usage: dict[str, Any] | None) -> int | None:
    details = (usage or {}).get("output_tokens_details") or {}
    val = details.get("reasoning_tokens")
    return int(val) if val is not None else None


# --- synthetic continue pair ------------------------------------------------


def continue_call_id(reasoning_id: str) -> str:
    """Deterministic call_id derived from the reasoning id it follows.

    Same reasoning id => same pair, so the within-turn tail and the (optional)
    cross-turn repair emit byte-identical bytes (prompt-cache stable).
    """
    return "call_" + hashlib.sha1(reasoning_id.encode("utf-8")).hexdigest()[:24]


def continue_pair(
    reasoning_id: str,
    *,
    tool_name: str,
    output_text: str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """A synthetic (function_call, function_call_output) that nudges the model
    to resume reasoning. Never declared as a real tool (NO_TOOLS mode)."""
    call_id = continue_call_id(reasoning_id)
    call = {
        "type": "function_call",
        "call_id": call_id,
        "name": tool_name,
        "arguments": json.dumps({"continue": True}),
    }
    output = {
        "type": "function_call_output",
        "call_id": call_id,
        "output": output_text,
    }
    return call, output


def commentary_message(text: str) -> dict[str, Any]:
    """A single phase:"commentary" assistant message — the clean continuation
    provocation (the default, replacing the function_call/_output pair).

    `phase` is an official Responses-API field (Literal["commentary",
    "final_answer"]); agents preserve it cross-turn, and it carries no synthetic
    tool, so it is safe to surface downstream (forward_marker). Verified live to
    re-ingest the replayed reasoning and defeat 518n-2 truncation identically to
    the tool pair.
    """
    return {
        "type": "message",
        "role": "assistant",
        "content": [{"type": "output_text", "text": text}],
        "phase": "commentary",
    }


# --- payload assembly -------------------------------------------------------


def merge_include(include: Any, *, force_encrypted: bool) -> list[str]:
    items: list[str] = []
    if isinstance(include, list):
        items = [str(x) for x in include]
    if force_encrypted and ENCRYPTED_INCLUDE not in items:
        items.append(ENCRYPTED_INCLUDE)
    return items


def build_round_payload(
    base_body: dict[str, Any],
    *,
    input_items: list[Any],
    force_include_encrypted: bool,
) -> dict[str, Any]:
    """Take the agent's request body and shape it for one upstream round.

    We never invent model/instructions/reasoning/tools — those are the agent's.
    We only: force stream=True (we always stream upstream), ensure encrypted
    reasoning is in `include`, set the round's `input`, and always drop
    `previous_response_id`. Every round this proxy opens upstream is a fresh,
    unrelated HTTP request, so the real API never has a session to resolve
    that id against (see `resolve_previous_response_id`, which must already
    have spliced any chained history into `input_items` by the time this
    runs).
    """
    body = dict(base_body)
    body["stream"] = True
    body["input"] = input_items
    if force_include_encrypted or base_body.get("include"):
        body["include"] = merge_include(
            base_body.get("include"), force_encrypted=force_include_encrypted
        )
    body.pop("previous_response_id", None)
    return body


def resolve_previous_response_id(
    body: dict[str, Any], chain_store: Any
) -> tuple[dict[str, Any], str | None, bool]:
    """Splice a client-supplied `previous_response_id` chain locally, and
    always strip the field before the request goes anywhere near upstream.

    This proxy never keeps a persistent upstream connection -- every round it
    opens is a fresh, unrelated HTTP request -- so upstream has no session to
    resolve `previous_response_id` against; it 400s with `Unsupported
    parameter: previous_response_id`. Codex (>= ~0.142, chained
    `responses_websockets` turns) relies on exactly that resolution to avoid
    resending the whole transcript on every tool-loop step or follow-up turn,
    so we resolve it ourselves from `chain_store` (id -> that turn's effective
    full input, recorded by the fold loop) instead.

    Returns `(new_body, previous_id, hit)`:
    - `previous_id` is None when the request carried no `previous_response_id`
      (`new_body is body` in that case -- nothing to do).
    - `hit` is False on a cache miss (unknown/expired id, e.g. after a proxy
      restart): `previous_response_id` is still dropped so the request doesn't
      hard-fail, but `input` is left as the caller sent it -- best effort,
      possibly missing earlier context.
    """
    prev_id = body.get("previous_response_id")
    if not prev_id:
        return body, None, False
    cached = chain_store.get(prev_id) if chain_store is not None else None
    new_body = {k: v for k, v in body.items() if k != "previous_response_id"}
    if cached is not None:
        new_body["input"] = [*cached, *(body.get("input") or [])]
        return new_body, prev_id, True
    return new_body, prev_id, False


def declares_continue_tool(body: dict[str, Any], tool_name: str) -> bool:
    """Collision rule: the agent itself DECLARES a tool with our continue name
    in its `tools` array (not merely referencing it in input history)."""
    for tool in body.get("tools") or []:
        if isinstance(tool, dict) and tool.get("name") == tool_name:
            return True
    return False


def reasoning_enabled(body: dict[str, Any]) -> bool:
    """Reasoning is ON by default — these models reason even with no `reasoning`
    field. Only an explicit opt-out (`reasoning: false`) disables it; absent /
    empty / dict all count as enabled."""
    return body.get("reasoning") is not False


def repair_followup_input(
    input_items: list[Any],
    id_store,
    *,
    tool_name: str,
    output_text: str,
) -> list[Any]:
    """repair_followup="stateful": re-insert a continue pair AFTER each reasoning
    item whose id we recorded during a prior turn's continuation. Keyed strictly
    by recorded id (never by adjacency, which would corrupt naturally consecutive
    reasoning items). Idempotent: skips if the pair is already present."""
    out: list[Any] = []
    n = len(input_items)
    for i, item in enumerate(input_items):
        out.append(item)
        if not (isinstance(item, dict) and item.get("type") == "reasoning"):
            continue
        rid = item.get("id")
        if not rid or rid not in id_store:
            continue
        call_id = continue_call_id(rid)
        nxt = input_items[i + 1] if i + 1 < n else None
        already = (
            isinstance(nxt, dict)
            and nxt.get("type") == "function_call"
            and nxt.get("call_id") == call_id
        )
        if already:
            continue
        call, output = continue_pair(rid, tool_name=tool_name, output_text=output_text)
        out.append(call)
        out.append(output)
    return out
