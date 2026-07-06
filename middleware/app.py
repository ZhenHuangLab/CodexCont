"""Starlette app: route the agent's Responses request through the fold logic.

Only ACTS when continuation is enabled and the agent did not itself declare a
`continue_thinking` tool (collision rule). Otherwise it is a pure passthrough,
so it is safe in front of all traffic.
"""

from __future__ import annotations

import contextlib
import gzip
import json
import logging
import zlib
from typing import Any, AsyncGenerator

import httpx
import zstandard
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response, StreamingResponse
from starlette.routing import Route, WebSocketRoute
from starlette.websockets import WebSocket, WebSocketDisconnect

from .codex import (
    build_round_payload,
    declares_continue_tool,
    reasoning_enabled,
    repair_followup_input,
    resolve_previous_response_id,
)
from .config import Config
from .creds import build_upstream_headers, would_inject_authorization
from .proxy import fold_stream, open_passthrough, open_round
from .sse import DONE, SSEAccumulator, incremental_sse
from .store import ChainStore, IdStore

log = logging.getLogger("middleware.app")

# An HTTP Request and a WebSocket handshake both expose a case-insensitive
# `.headers` mapping, which is all the header-resolution helpers below need.
HeaderSource = Request | WebSocket


def _decompress_body(data: bytes, encoding: str | None) -> bytes:
    """Codex's built-in provider sends zstd-compressed request bodies when
    request compression is enabled (gzip/deflate for completeness). Decompress
    before parsing/forwarding; creds.py drops Content-Encoding upstream since
    the forwarded bytes are plain."""
    enc = (encoding or "").lower().strip()
    if not enc or enc == "identity":
        return data
    try:
        if enc == "zstd":
            return zstandard.ZstdDecompressor().decompressobj().decompress(data)
        if enc == "gzip":
            return gzip.decompress(data)
        if enc == "deflate":
            return zlib.decompress(data)
    except Exception as exc:
        raise ValueError(f"failed to decompress {enc} body: {exc}") from exc
    raise ValueError(f"unsupported content-encoding: {enc}")


def _header_base(request: HeaderSource) -> str | None:
    """The non-blank Responses-API-Base header value, or None (case-insensitive)."""
    v = request.headers.get("responses-api-base")
    v = v.strip() if v else ""
    return v or None


def _join_responses(base: str) -> str:
    """Build the Responses endpoint from a base URL (OpenAI base_url convention:
    `<base>/responses`). Lenient: if the value already ends in `/responses`
    (a full endpoint was passed), use it as-is."""
    base = base.rstrip("/")
    return base if base.endswith("/responses") else base + "/responses"


def _resolve_upstream_url(cfg: Config, request: HeaderSource) -> str | None:
    """Target URL for this request.

    - "fixed": always the configured URL (header ignored).
    - "header": the Responses-API-Base header (case-insensitive) is treated as a
      base URL and `/responses` is appended; overrides the configured URL when
      present, else the configured URL.
    - "header_required": the header MUST be present; returns None when it is
      absent/blank so the caller can reject the request (400).

    The header is stripped before forwarding upstream (build_upstream_headers).
    """
    if cfg.upstream.mode in ("header", "header_required"):
        base = _header_base(request)
        if base:
            return _join_responses(base)
        if cfg.upstream.mode == "header_required":
            return None
    return cfg.upstream.url


def _url_is_from_header(cfg: Config, request: HeaderSource) -> bool:
    return (
        cfg.upstream.mode in ("header", "header_required")
        and _header_base(request) is not None
    )


_TERMINAL_TYPES = ("response.completed", "response.failed", "response.incomplete")


class _ChainRecorder:
    """Record a passthrough turn's chain the way the fold path's
    `_record_chain` does, so a later turn chaining off this response via
    `previous_response_id` still resolves (see the WebSocket module note
    below). Feed it every parsed upstream event; on the terminal event it
    stores `[*input, *output]` under the response id."""

    def __init__(self, chain_store: Any, orig_input: list[Any]) -> None:
        self._store = chain_store
        self._input = orig_input
        self._items: list[dict[str, Any]] = []

    def observe(self, ev: Any) -> None:
        if self._store is None or not isinstance(ev, dict):
            return
        t = ev.get("type")
        if t == "response.output_item.done" and isinstance(ev.get("item"), dict):
            self._items.append(ev["item"])
        elif t in _TERMINAL_TYPES:
            resp = ev.get("response") or {}
            rid = resp.get("id")
            # Codex-backend terminals carry an empty `output`; fall back to
            # the output_item.done items collected along the way.
            output = resp.get("output") or self._items
            if rid:
                self._store.set(rid, [*self._input, *output])


async def _passthrough(
    client: httpx.AsyncClient,
    cfg: Config,
    request: Request,
    raw: bytes,
    url: str,
    body: dict[str, Any],
    chain_store: Any,
):
    """Pure proxy: forward the raw request and stream the raw response back.

    The bytes go downstream untouched, but an SSE parser tees off the stream
    so this turn's response id still lands in `chain_store` -- without it, a
    later turn chaining off a passthrough response via `previous_response_id`
    would silently lose all prior context."""
    headers = build_upstream_headers(request.headers.items(), cfg)
    resp = await open_passthrough(client, url, raw, headers)
    recorder = _ChainRecorder(chain_store, list(body.get("input") or []))
    acc = SSEAccumulator()

    async def body_iter():
        try:
            async for chunk in resp.aiter_bytes():
                for ev in acc.feed(chunk):
                    recorder.observe(ev)
                yield chunk
        finally:
            await resp.aclose()

    return StreamingResponse(
        body_iter(),
        status_code=resp.status_code,
        media_type=resp.headers.get("content-type", "text/event-stream"),
    )


def _fold_decision(cfg: Config, body: dict[str, Any]) -> tuple[bool, str | None]:
    """Whether this request should be folded, and if not, why -- shared by the
    HTTP and WebSocket entry points (WebSocket callers force `stream=True` on
    `body` beforehand, since the transport itself implies streaming)."""
    collision = cfg.cont.method == "tool_pair" and declares_continue_tool(
        body, cfg.cont.continue_tool_name
    )
    should_fold = (
        cfg.cont.enabled
        and bool(body.get("stream"))
        and reasoning_enabled(body)
        and not collision
    )
    if should_fold:
        return True, None
    why = (
        "disabled"
        if not cfg.cont.enabled
        else "non-stream"
        if not body.get("stream")
        else "non-reasoning"
        if not reasoning_enabled(body)
        else "declares-continue_thinking"
    )
    return False, why


async def handle_responses(request: Request) -> Response:
    cfg: Config = request.app.state.cfg
    client: httpx.AsyncClient = request.app.state.client

    raw = await request.body()
    try:
        raw = _decompress_body(raw, request.headers.get("content-encoding"))
        body: dict[str, Any] = json.loads(raw)
    except ValueError as exc:  # bad encoding or bad JSON
        return JSONResponse({"error": f"invalid request body: {exc}"}, status_code=400)
    if not isinstance(body, dict):
        return JSONResponse({"error": "body must be a JSON object"}, status_code=400)

    # Every round this proxy opens upstream is a fresh, unrelated HTTP request
    # (no persisted upstream connection/session), so a client-supplied
    # `previous_response_id` can never resolve there -- splice any chained
    # history we have cached locally and always drop the field before it goes
    # anywhere near upstream (fold path AND passthrough alike).
    body, prev_id, chain_hit = resolve_previous_response_id(
        body, request.app.state.chain_store
    )
    if prev_id:
        log.info(
            "previous_response_id=%s %s",
            prev_id,
            "resolved from local cache"
            if chain_hit
            else "MISS -- dropping (context may be incomplete)",
        )
        raw = json.dumps(body, ensure_ascii=False).encode("utf-8")

    url = _resolve_upstream_url(cfg, request)
    if url is None:
        return JSONResponse(
            {
                "error": "Responses-API-Base header is required (upstream mode=header_required)"
            },
            status_code=400,
        )

    # Safety: never send the proxy's configured credentials to a URL the request
    # itself supplied. If the base came from the header, the request must carry
    # its own Authorization (we won't inject ours toward an external URL).
    if _url_is_from_header(cfg, request) and would_inject_authorization(
        cfg, agent_has_authorization=request.headers.get("authorization") is not None
    ):
        log.warning(
            "blocked: Responses-API-Base override without own auth (model=%s)",
            body.get("model"),
        )
        return JSONResponse(
            {
                "error": "When overriding the upstream base (Responses-API-Base), the request must "
                "provide its own Authorization; the proxy will not send its configured "
                "credentials to an externally supplied URL."
            },
            status_code=400,
        )

    # Fold only a streaming, reasoning-enabled request that isn't a collision.
    # Everything else (non-reasoning, non-streaming, continuation disabled, or
    # the agent declaring its own continue_thinking) is a pure passthrough.
    # The collision rule only matters for the tool_pair method (we inject a tool);
    # commentary injects no tool, so a declared continue_thinking is irrelevant.
    should_fold, why = _fold_decision(cfg, body)
    if not should_fold:
        log.info(
            "passthrough (%s): model=%s path=%s url=%s",
            why,
            body.get("model"),
            request.url.path,
            url,
        )
        return await _passthrough(
            client, cfg, request, raw, url, body, request.app.state.chain_store
        )

    log.info(
        "fold start: model=%s path=%s url=%s input_items=%d",
        body.get("model"),
        request.url.path,
        url,
        len(body.get("input") or []),
    )

    # repair_followup="stateful": re-insert tool_pair continue pairs after recorded
    # ids (tool_pair only — commentary preserves cross-turn structure via forward_marker).
    if cfg.cont.repair_followup == "stateful" and cfg.cont.method == "tool_pair":
        body = {
            **body,
            "input": repair_followup_input(
                list(body.get("input") or []),
                request.app.state.id_store,
                tool_name=cfg.cont.continue_tool_name,
                output_text=cfg.cont.continue_output_text,
            ),
        }

    headers = build_upstream_headers(request.headers.items(), cfg)
    payload = build_round_payload(
        body,
        input_items=list(body.get("input") or []),
        force_include_encrypted=cfg.stream.force_include_encrypted,
    )

    # Open round 1 here so a non-2xx (e.g. bad auth) is mirrored with its real
    # status code rather than buried inside a 200 SSE stream.
    resp = await open_round(client, url, payload, headers)
    if resp.status_code >= 400:
        err = await resp.aread()
        await resp.aclose()
        return Response(
            err,
            status_code=resp.status_code,
            media_type=resp.headers.get("content-type"),
        )

    return StreamingResponse(
        fold_stream(
            client,
            cfg,
            body,
            headers,
            resp,
            request.app.state.id_store,
            url=url,
            chain_store=request.app.state.chain_store,
        ),
        media_type="text/event-stream",
    )


# --- WebSocket transport -----------------------------------------------------
#
# Codex (>= ~0.140, the "responses_websockets" feature) tries a WebSocket
# upgrade at `ws(s)://.../v1/responses` before falling back to HTTP; without a
# route here every turn 405s (uvicorn doesn't even attempt the upgrade without
# a websockets/wsproto library + a WebSocketRoute) and Codex burns several
# retries before it falls back. We speak the documented wire shape -- client
# sends `{"type": "response.create", ...body...}`, server answers with
# individual `response.*` event frames, see
# https://developers.openai.com/api/docs/guides/websocket-mode -- but
# internally still open one plain HTTP+SSE round per turn upstream, reusing
# the exact same fold/passthrough logic as the POST route. There is no
# persistent upstream WebSocket connection, so this buys "no fallback noise",
# not the extra latency win a true end-to-end WebSocket bridge would. Codex
# (>= ~0.142) chains turns with `previous_response_id` assuming exactly such a
# persistent session (tool-loop steps and follow-up messages alike send only
# the new delta + that id); since upstream can never resolve it against one of
# our disconnected per-round requests (400 `Unsupported parameter:
# previous_response_id`), we resolve it ourselves from a process-wide
# `ChainStore` (response id -> that turn's effective full input) shared by
# both transports -- see `codex.resolve_previous_response_id`.


def _ws_error_event(
    status: int, message: str, *, code: str | None = None
) -> dict[str, Any]:
    """`{"type": "error", ...}` in the shape Codex's WebSocket client parses
    (status + error.message/.code), used when a round can't be opened at all
    (mirrors what an HTTP 4xx/5xx response would carry on the POST route)."""
    err: dict[str, Any] = {"message": message}
    if code:
        err["code"] = code
    return {"type": "error", "status": status, "error": err}


def _upstream_error_message(raw: bytes) -> tuple[str, str | None]:
    """Best-effort (message, code) pulled from an upstream JSON error body."""
    text = raw[:2000].decode("utf-8", errors="replace")
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return text, None
    err = data.get("error") if isinstance(data, dict) else None
    if isinstance(err, dict) and isinstance(err.get("message"), str):
        code = err.get("code")
        return err["message"], code if isinstance(code, str) else None
    return text, None


async def _events_from_byte_stream(
    byte_iter: AsyncGenerator[bytes, None],
) -> AsyncGenerator[Any, None]:
    """`incremental_sse`, but guarantees `byte_iter.aclose()` runs even if the
    consumer stops iterating early (the agent disconnects mid-round), so
    `fold_stream`'s `finally: await response.aclose()` still fires instead of
    leaking the upstream connection."""
    async with contextlib.aclosing(byte_iter):
        async for ev in incremental_sse(byte_iter):
            yield ev


async def _ws_open_events(
    client: httpx.AsyncClient,
    url: str,
    body: dict[str, Any],
    headers: dict[str, str],
    chain_store: Any = None,
) -> AsyncGenerator[Any, None]:
    """Open one non-folding upstream round for a WebSocket turn and yield its
    parsed events (or a single `error` event if the round can't be opened).
    Every event also feeds a `_ChainRecorder`, so a passthrough turn can be
    chained off via `previous_response_id` just like a folded one."""
    resp = await open_round(client, url, body, headers)
    recorder = _ChainRecorder(chain_store, list(body.get("input") or []))
    try:
        if resp.status_code >= 400:
            message, code = _upstream_error_message(await resp.aread())
            yield _ws_error_event(resp.status_code, message, code=code)
            return
        async for ev in incremental_sse(resp.aiter_bytes()):
            if ev is not DONE:
                recorder.observe(ev)
                yield ev
    finally:
        await resp.aclose()


async def _handle_response_create(
    websocket: WebSocket,
    cfg: Config,
    client: httpx.AsyncClient,
    id_store: Any,
    chain_store: Any,
    body: dict[str, Any],
    headers: dict[str, str],
    url: str,
) -> None:
    """Run one `response.create` turn to completion, relaying `response.*` (or
    a single `error`) event as individual WebSocket JSON text frames."""
    body, prev_id, chain_hit = resolve_previous_response_id(body, chain_store)
    if prev_id:
        log.info(
            "ws previous_response_id=%s %s",
            prev_id,
            "resolved from local cache"
            if chain_hit
            else "MISS -- dropping (context may be incomplete)",
        )

    should_fold, why = _fold_decision(cfg, body)
    log.info(
        "ws %s: model=%s url=%s input_items=%d",
        "fold start" if should_fold else f"passthrough ({why})",
        body.get("model"),
        url,
        len(body.get("input") or []),
    )

    if should_fold:
        # Mirror the HTTP fold path exactly: the stateful tool_pair repair,
        # then a round-1 payload shaped by build_round_payload (forces
        # stream=True, merges `include: reasoning.encrypted_content`, drops
        # previous_response_id). Without the include merge, an agent that
        # doesn't request encrypted reasoning itself would leave round 1
        # without `encrypted_content` and continuation could never fire.
        if cfg.cont.repair_followup == "stateful" and cfg.cont.method == "tool_pair":
            body = {
                **body,
                "input": repair_followup_input(
                    list(body.get("input") or []),
                    id_store,
                    tool_name=cfg.cont.continue_tool_name,
                    output_text=cfg.cont.continue_output_text,
                ),
            }
        payload = build_round_payload(
            body,
            input_items=list(body.get("input") or []),
            force_include_encrypted=cfg.stream.force_include_encrypted,
        )
        resp = await open_round(client, url, payload, headers)
        if resp.status_code >= 400:
            message, code = _upstream_error_message(await resp.aread())
            await resp.aclose()
            await websocket.send_text(
                json.dumps(_ws_error_event(resp.status_code, message, code=code))
            )
            return
        event_source = _events_from_byte_stream(
            fold_stream(
                client,
                cfg,
                body,
                headers,
                resp,
                id_store,
                url=url,
                chain_store=chain_store,
            )
        )
    else:
        event_source = _ws_open_events(client, url, body, headers, chain_store)

    async with contextlib.aclosing(event_source) as events:
        async for ev in events:
            if ev is DONE or not isinstance(ev, dict):
                continue
            await websocket.send_text(json.dumps(ev, ensure_ascii=False))


async def handle_responses_ws(websocket: WebSocket) -> None:
    """WebSocket counterpart of `handle_responses`; see the module note above."""
    cfg: Config = websocket.app.state.cfg
    client: httpx.AsyncClient = websocket.app.state.client
    id_store = websocket.app.state.id_store
    chain_store = websocket.app.state.chain_store

    url = _resolve_upstream_url(cfg, websocket)
    if url is None:
        await websocket.close(code=1008)  # header_required, header missing/blank
        return
    if _url_is_from_header(cfg, websocket) and would_inject_authorization(
        cfg, agent_has_authorization=websocket.headers.get("authorization") is not None
    ):
        log.warning("ws blocked: Responses-API-Base override without own auth")
        await websocket.close(code=1008)
        return

    # openai-beta advertises WebSocket support to whatever it's sent to; strip
    # it before forwarding since the round we open upstream is always plain
    # SSE, never a WS upgrade.
    headers = build_upstream_headers(
        ((k, v) for k, v in websocket.headers.items() if k.lower() != "openai-beta"),
        cfg,
    )

    await websocket.accept()
    try:
        while True:
            raw_msg = await websocket.receive_text()
            try:
                envelope = json.loads(raw_msg)
            except (json.JSONDecodeError, UnicodeDecodeError):
                await websocket.send_text(
                    json.dumps(_ws_error_event(400, "invalid JSON body"))
                )
                continue
            if not isinstance(envelope, dict):
                await websocket.send_text(
                    json.dumps(_ws_error_event(400, "body must be a JSON object"))
                )
                continue
            if envelope.get("type") != "response.create":
                log.info("ws: ignoring frame type=%s", envelope.get("type"))
                continue

            body = {k: v for k, v in envelope.items() if k != "type"}
            body["stream"] = True  # implied by the transport; Codex never sends it here

            try:
                await _handle_response_create(
                    websocket, cfg, client, id_store, chain_store, body, headers, url
                )
            except (httpx.HTTPError, OSError) as exc:
                log.warning("ws: round failed to open: %r", exc)
                message = str(exc) or repr(exc)
                await websocket.send_text(
                    json.dumps(
                        _ws_error_event(502, f"upstream connection error: {message}")
                    )
                )
    except WebSocketDisconnect:
        pass


def _model_object(model_id: str, owned_by: str) -> dict[str, Any]:
    return {"id": model_id, "object": "model", "created": 0, "owned_by": owned_by}


def _upstream_base(url: str) -> str:
    """The upstream base for non-/responses calls (responses URL minus suffix)."""
    return url[: -len("/responses")] if url.endswith("/responses") else url


async def _proxy_v1(request: Request, suffix: str) -> Response | None:
    """Forward one non-/responses /v1/* call to the upstream base, or None
    when no upstream is resolvable / the auth safety guard applies (same rule
    as handle_responses: never send configured credentials to a URL the
    request itself supplied)."""
    cfg: Config = request.app.state.cfg
    client: httpx.AsyncClient = request.app.state.client
    url = _resolve_upstream_url(cfg, request)
    if url is None:
        return None
    if _url_is_from_header(cfg, request) and would_inject_authorization(
        cfg, agent_has_authorization=request.headers.get("authorization") is not None
    ):
        return None
    target = f"{_upstream_base(url)}/{suffix}"
    if request.url.query:
        target += "?" + request.url.query
    content = await request.body()
    if content:
        content = _decompress_body(content, request.headers.get("content-encoding"))
    headers = build_upstream_headers(request.headers.items(), cfg)
    upstream = await client.request(
        request.method,
        target,
        content=content or None,
        headers=headers,
        timeout=httpx.Timeout(60.0),
    )
    # httpx already decompressed the body; drop the now-stale framing headers.
    drop = {"content-encoding", "transfer-encoding", "connection", "content-length"}
    return Response(
        upstream.content,
        status_code=upstream.status_code,
        headers={k: v for k, v in upstream.headers.items() if k.lower() not in drop},
    )


async def handle_models(request: Request) -> Response:
    """GET /v1/models -- real upstream catalog when reachable, cosmetic list
    otherwise.

    Several clients (Codex included) periodically poll this endpoint, e.g. to
    populate a model picker. Try the upstream first (full /v1/* passthrough,
    real catalog); on any failure fall back to the `[models]` list from
    config.toml so the client still gets a 200. The advertised ids never
    restrict anything: a real request forwards whatever `model` it carries.
    """
    cfg: Config = request.app.state.cfg
    try:
        resp = await _proxy_v1(request, "models")
        if resp is not None and resp.status_code < 400:
            return resp
    except Exception as exc:
        log.info("models passthrough failed (%r); serving local list", exc)
    return JSONResponse(
        {
            "object": "list",
            "data": [_model_object(mid, cfg.models.owned_by) for mid in cfg.models.ids],
        }
    )


async def handle_v1_passthrough(request: Request) -> Response:
    """Transparent proxy for every other /v1/* call, so future/unknown
    endpoints reach the real upstream instead of 404ing at the middleware."""
    try:
        resp = await _proxy_v1(request, request.path_params["path"])
    except ValueError as exc:  # bad content-encoding
        return JSONResponse({"error": str(exc)}, status_code=400)
    except httpx.HTTPError as exc:
        return JSONResponse({"error": f"upstream error: {exc}"}, status_code=502)
    if resp is None:
        return JSONResponse(
            {"error": "no upstream resolvable for /v1 passthrough"}, status_code=400
        )
    return resp


async def handle_model(request: Request) -> Response:
    """GET /v1/models/{model_id} -- always reports the requested id as
    available, for the same cosmetic reason as handle_models above."""
    cfg: Config = request.app.state.cfg
    model_id = request.path_params["model_id"]
    return JSONResponse(_model_object(model_id, cfg.models.owned_by))


async def handle_health(_request: Request) -> Response:
    return JSONResponse({"status": "ok", "service": "codexcont"})


def _make_client() -> httpx.AsyncClient:
    """A client that does NOT invent a User-Agent or Accept of its own; those
    are forwarded from the agent or omitted. httpx still manages Host /
    Content-Length / Accept-Encoding / Connection (plan-allowed)."""
    client = httpx.AsyncClient(timeout=None)
    for h in ("user-agent", "accept"):
        if h in client.headers:
            del client.headers[h]
    return client


def create_app(cfg: Config) -> Starlette:
    @contextlib.asynccontextmanager
    async def lifespan(app: Starlette):
        app.state.cfg = cfg
        app.state.client = _make_client()
        app.state.id_store = IdStore()
        app.state.chain_store = ChainStore()
        try:
            yield
        finally:
            await app.state.client.aclose()

    routes = [
        Route(path, handle_responses, methods=["POST"])
        for path in cfg.server.listen_paths
    ]
    if cfg.server.enable_websocket:
        routes += [
            WebSocketRoute(path, handle_responses_ws)
            for path in cfg.server.listen_paths
        ]
    routes += [
        Route("/v1/models", handle_models, methods=["GET"]),
        Route("/v1/models/{model_id:path}", handle_model, methods=["GET"]),
        Route("/health", handle_health, methods=["GET"]),
        # Catch-all LAST: everything else under /v1/ goes to the real upstream.
        Route(
            "/v1/{path:path}",
            handle_v1_passthrough,
            methods=["GET", "POST", "PUT", "DELETE", "PATCH", "HEAD"],
        ),
    ]
    return Starlette(routes=routes, lifespan=lifespan)
