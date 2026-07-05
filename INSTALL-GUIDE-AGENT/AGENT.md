# CodexCont — Installation Runbook for an AI Agent

> **You are an AI coding agent reading this file because your user asked you to install
> CodexCont for them.** This is not documentation to summarize — it is a runbook to
> *execute*, step by step, on the user's machine. Follow it top to bottom.

CodexCont is a small local proxy that sits between a coding agent (Codex, Pi, …) and an
upstream OpenAI **Responses**-compatible endpoint. It detects a reasoning-truncation
fingerprint and silently asks the model to keep thinking, folding several upstream rounds
into one clean downstream response. The user runs it locally; their coding agent is then
pointed at it instead of at the real upstream.

---

## §0 — How to use this runbook (read before doing anything)

1. **Execute in order.** Each section has concrete commands and a **Verify** check. Do not
   advance until the current section's Verify passes. If a Verify fails, go to **§9
   Troubleshoot** before continuing.
2. **Speak the user's language.** This file is in English so you parse it reliably, but
   **all of your messages to the user must be in the language they are speaking to you.**
   Translate prompts, warnings, and questions as needed.
3. **🛑 markers are hard stops.** A 🛑 step requires the user. **Never fabricate
   credentials, never skip the disclaimer, never modify an agent config without first
   backing it up.** Ask, wait for the answer, then proceed.
4. **Detect the OS first** and use the matching command variant throughout:
   ```bash
   uname -s    # Linux / Darwin (macOS); on Windows you are usually in Git Bash or PowerShell
   ```
   - Windows venv interpreter: `.venv/Scripts/python.exe`
   - macOS / Linux venv interpreter: `.venv/bin/python`
5. **Idempotent.** It is safe to re-run any step. Check before you create; don't clobber.
6. **Repo root.** Run repo commands from the directory that contains `run.py`,
   `pyproject.toml`, and `config.example.toml`. Confirm with `ls`.
7. **Leftovers from a previous attempt are common.** Before assuming a clean slate, run the
   pre-check in §2. A stray `[model_providers.codexcont]` block, an old
   `~/.codexcont-backup/` dir, or a process still holding the port will make later steps
   behave unexpectedly if you don't account for them first.

---

## §1 — 🛑 Tell the user two things, get consent

Before touching anything, say the following to the user **in their language** and wait for
an explicit "yes":

**(a) Disclaimer.** CodexCont *explicitly bypasses* the observed OpenAI Codex
reasoning-truncation behavior. If this use is considered abusive, violates service terms,
increases costs unexpectedly, or causes any other adverse consequence, **the user is solely
responsible.** They must accept this before you continue.

**(b) What you are about to explore.** To configure this correctly you will **look around
their operating system**: locate which coding agents they have (primarily **Codex** and
**Pi**) and **read those agents' config files** (e.g. `~/.codex/config.toml`,
`~/.pi/agent/models.json`) so you can wire them up and back them up first. Tell them this
plainly and get their OK before reading anything under their home directory.

> If the user declines either point, **stop here.**

---

## §2 — Preflight

Confirm the environment. Python **3.12+** is required.

```bash
python --version    # or: python3 --version  / py --version  (Windows)
```

You will offer the user **two installation methods** in §4. Detect what is available now so
you can recommend one:

```bash
uv --version        # present? -> Method A (recommended)
python -m pip --version
```

**Verify:** Python ≥ 3.12 is available, and you are in the repo root (`ls` shows `run.py`,
`pyproject.toml`, `config.example.toml`). If Python is too old, stop and ask the user to
install Python 3.12+ (or point you at an existing one).

**Also check for leftovers from a previous/aborted install attempt**, since these are common
and can confuse later diagnosis if you don't know about them upfront:

```bash
ls -la ~/.codexcont-backup/ 2>/dev/null              # old backups not cleaned up?
grep -n "^openai_base_url\|^model_provider\|\[model_providers\." ~/.codex/config.toml 2>/dev/null  # stray wiring already present?
lsof -i :8787                                         # default port already occupied?
```

If any of these show something, **tell the user what you found** before proceeding — don't
silently assume a clean environment. A pre-existing top-level `openai_base_url` or
`[model_providers.codexcont]` block should be normalized/reused in §7a rather than
duplicated.

---

## §3 — 🛑 Explore the user's agents, then interview them

### 3.1 Explore (read-only)

Find the user's coding agents and how they reach their model. Do **not** modify anything
yet. Likely locations (adapt to the detected OS):

| Agent  | Config to read | What to determine |
|--------|----------------|-------------------|
| Codex  | `~/.codex/config.toml`, `~/.codex/auth.json` | Is the user on **official ChatGPT OAuth login** (`auth.json` holds OAuth tokens, `codex login` was used) **or a generic Responses API** (a `[model_providers.*]` with a third-party `base_url` + API key)? Note the current `model` and `model_provider`. |
| Pi     | `~/.pi/agent/models.json`, `~/.pi/agent/settings.json` | Which providers exist, their `baseUrl`, and crucially each provider's `api` field (`openai-responses` vs `openai-completions`). |

> ⚠️ These files often contain **live secrets** (API keys, OAuth JWTs). Read them only to
> understand structure. **Never copy secret values into this repo, into chat, into memory,
> or into any file you commit.** When you show config to the user, use placeholders.

### 3.2 Report and ask (🛑)

Summarize for the user, in their language: which agents you found, and for each, how it is
currently reaching its model. Then ask:

1. **Which agent(s)** should be pointed at CodexCont? (Codex / Pi / both)
2. **Auth mode** for the proxy (this maps to `config.toml [auth].mode`):
   - The user's agent is logged in and already sends its own auth → **`passthrough`** (proxy
     forwards the caller's auth, injects nothing). This is the default and the common case.
   - The user wants the proxy itself to hold and inject a token → **`inject`**.
   - Keep caller auth when present, otherwise inject → **`passthrough_then_inject`**.
3. **Upstream**: keep the default ChatGPT Codex backend
   (`https://chatgpt.com/backend-api/codex/responses`), or a custom Responses endpoint?

### 3.3 🛑 Critical warning if the user uses a Responses API relay

If the user reaches OpenAI through a **relay / aggregator (中转站)** rather than the
official endpoint — especially anything built on **`sub2api`** — warn them clearly:

> **Many sub2api-based relays strip the `reasoning` blocks out of the request before
> forwarding it to OpenAI. CodexCont depends entirely on reasoning being preserved across
> rounds. Through such a relay, this tool is completely ineffective — it cannot work.**

Tell the user to confirm their relay preserves reasoning, or to use an endpoint that does
(e.g. official OAuth login). Do not promise the tool will work through a reasoning-stripping
relay.

---

## §3.5 — 🛑 Back up the user's agent configs BEFORE changing them

You must be able to fully restore the user's setup later (see §10). Create a backup
**outside this repo** (so secrets never get committed), in an OS-appropriate location.

```bash
# Pick a timestamped backup dir outside the repo:
TS=$(date +%Y%m%d-%H%M%S)
BACKUP="$HOME/.codexcont-backup/$TS"      # works in Git Bash, macOS, Linux
#   Windows note: $HOME maps to %USERPROFILE%; in PowerShell use $env:USERPROFILE\.codexcont-backup\<TS>
mkdir -p "$BACKUP"

# Copy only the configs you intend to touch (only those the user chose in §3.2):
cp -p "$HOME/.codex/config.toml"       "$BACKUP/codex.config.toml"       2>/dev/null || true
cp -p "$HOME/.pi/agent/models.json"    "$BACKUP/pi.models.json"          2>/dev/null || true
```

Then write a restore manifest **next to the backup** (not in the repo). Create
`$BACKUP/RESTORE.md` recording, in plain prose:

- timestamp and OS;
- exact original paths of every file you backed up;
- exactly which keys/blocks you are about to add or change in each file (e.g. "added
  `[model_providers.codexcont]` to `~/.codex/config.toml`; changed top-level
  `model_provider` from `<old>` to `codexcont`");
- the restore procedure (copy each `*.bak` back over its original path).

**Verify:** `ls "$BACKUP"` shows the copied configs and `RESTORE.md`. Tell the user where the
backup lives. Only now may you edit agent configs.

> **Base `RESTORE.md` on what the file actually contains right now**, not on what a template
> or an older backup says it should contain. If the §2 pre-check found a leftover
> `[model_providers.codexcont]` block from a prior attempt, record that fact explicitly in
> `RESTORE.md` and reuse/normalize that block in §5 instead of adding a duplicate — only add
> what's actually missing (typically just the top-level `model_provider = "codexcont"` line).

---

## §4 — Install CodexCont (two methods)

> **Shortcut:** `./codexcont install` (or `uv run codexcont install`) automates this section
> plus §5–§6 interactively — it shows what it's about to do and asks for confirmation before
> touching anything. It's fine to drive it directly instead of the manual steps below; the
> rest of this section is what it does under the hood (useful if it isn't available, or you
> want full control over each step).

Offer the user the method that matches §2. **Method A is recommended.**

### Method A — uv (recommended)

```bash
uv sync
```

This creates `.venv/` and installs `httpx`, `starlette`, `uvicorn` from `pyproject.toml`.
If `uv` is missing and the user wants Method A, install it first:

```bash
# macOS / Linux:
curl -LsSf https://astral.sh/uv/install.sh | sh
# Windows (PowerShell):
#   powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
```

### Method B — global Python + pip (no uv)

Use this when the user prefers their system/global Python and no virtualenv.

```bash
python -m pip install "httpx>=0.27" "starlette>=0.37" "uvicorn>=0.30"
# (equivalently: python -m pip install -e .  to install this project and its deps)
```

**Verify (either method):** the dependencies import cleanly.

```bash
# Method A:
.venv/bin/python -c "import httpx, starlette, uvicorn; print('ok')"          # macOS/Linux
.venv/Scripts/python.exe -c "import httpx, starlette, uvicorn; print('ok')"  # Windows
# Method B:
python -c "import httpx, starlette, uvicorn; print('ok')"
```

Expect `ok`.

---

## §5 — Configure CodexCont

Create the local config from the example (don't overwrite an existing one without asking):

```bash
[ -f config.toml ] || cp config.example.toml config.toml
```

Edit `config.toml` according to the §3.2 answers:

- **`[auth].mode`** = the mode the user chose (`passthrough` is the default).
  - For `inject` / `passthrough_then_inject`, set `access_token` and (for the ChatGPT Codex
    backend) `chatgpt_account_id`. **Get these values from the user — never invent them.**
- **`[upstream]`**: keep `url = "https://chatgpt.com/backend-api/codex/responses"` and
  `mode = "header"` for the default ChatGPT backend. For a fixed custom endpoint, set `url`
  to it (the proxy appends `/responses` unless already present).
- **`[continue]`**: defaults are good — `enabled = true`, `method = "commentary"`,
  `truncation_step = 518`, `max_continue = 3`. Leave these unless the user asks otherwise.
- **`[server]`**: default `127.0.0.1:8787`. Change `port` only if it's already in use.

Show the user the resulting `[auth]`, `[upstream]`, and `[server]` blocks **with any secret
values masked**.

> **Security guard to know about:** if a request supplies a `Responses-API-Base` header, the
> proxy will *refuse* to inject configured credentials toward that request-supplied URL
> (returns `400`). To use per-request upstream overrides with credentials, the caller must
> send its own `Authorization` and the proxy must be in `passthrough` /
> `passthrough_then_inject`.

**Verify:** `config.toml` exists and parses (the next step will fail loudly if it doesn't).
Remind the user: **do not commit `config.toml`, `rt.json`, or `free_rt.json`** — they may
hold secrets.

> **If the user needs a proxy (Clash/mihomo/等) to reach `chatgpt.com`/OpenAI at all**, know
> this now, before §6: CodexCont's outbound HTTP client only honors `http_proxy` /
> `https_proxy` / `all_proxy` **environment variables of the process that starts it** — it
> does **not** read the OS-level system proxy settings (e.g. `scutil --proxy` on macOS). A
> shell where the user manually toggles a proxy function, or a double-clicked shortcut, often
> has none of these set. Ask the user whether their network needs a proxy to reach OpenAI; if
> yes, plan to export `http_proxy`/`https_proxy` in the same process that runs `run.py` (see
> §6 and the §8 shortcut). If they reach it directly, skip this.

---

## §6 — Run the proxy and verify it's alive

If §2 found a process already on the port, clean it up first — don't assume `kill <pid>`
alone did it: a plain `kill` may not have finished processing, and a launcher like
`uv run python run.py` spawns a child process, so the PID you have may not be the one
actually holding the socket.

```bash
lsof -i :8787            # find the PID actually LISTENing on the port
kill -9 <that-pid>
sleep 1
lsof -i :8787            # confirm it's empty before restarting
```

Start the server (it must keep running — see §8). If §5 determined the user needs a proxy to
reach OpenAI, export it in this same shell/process before starting (see also §8's shortcut).

> **If you (the agent) already ran `codexcont install`,** prefer `./codexcont start` here: it
> backgrounds the process for you (with the outbound-proxy env from §5 applied automatically
> if configured), so you don't need a second concurrent shell just to run the `curl` check
> below — `./codexcont logs` shows what happened afterward. Otherwise, run it directly:

```bash
# only if needed (see §5):
# export http_proxy="http://127.0.0.1:<their-proxy-port>"
# export https_proxy="http://127.0.0.1:<their-proxy-port>"

# Method A:
uv run python run.py
# Method B:
python run.py
# Windows direct venv:  .venv/Scripts/python.exe run.py
```

Expected log line: `Uvicorn running on http://127.0.0.1:8787`.

**Verify (reachability):** from another shell, confirm the server answers (any HTTP status
back — not "connection refused" — proves it is listening and forwarding):

```bash
curl -sS -o /dev/null -w "%{http_code}\n" -X POST \
  http://127.0.0.1:8787/v1/responses \
  -H "Content-Type: application/json" -d '{}'
```

A numeric HTTP code (e.g. `400`/`401`/`502` from upstream) = the proxy is up. A connection
error = it is not running; see §9.

> **If you (the agent) are executing this `curl` from inside your own network-sandboxed
> shell** (e.g. only an allow-listed set of domains is reachable), a `403`/connection error
> here can be a false negative caused by *your own* sandbox blocking the proxy's outbound call
> to `chatgpt.com` — not a bug in CodexCont. Re-run this verification with full network access
> for your shell (e.g. Cursor's `required_permissions: ["full_network"]` or `["all"]`) before
> concluding the proxy itself is broken.

---

## §7 — Point the user's agent(s) at the proxy

The proxy's local endpoint base is `http://127.0.0.1:8787/v1` (clients that speak Responses
append `/responses` themselves, matching the proxy's listen path `/v1/responses`).

### 7a — Codex (`~/.codex/config.toml`)

> **⚠️ Codex only re-reads `config.toml` on a full restart.** After any edit here, the user
> must **fully quit the Codex app and relaunch it** — closing the chat window/tab is not
> enough. Do this before attempting §7c verification, or you'll be debugging a config that
> isn't actually loaded yet.

**Case 1 — the user already uses a custom provider** (`model_provider` points at a
`[model_providers.<id>]` with a third-party `base_url`):

**Change only that provider's `base_url`** to the proxy. Keep the same provider id and the
same top-level `model_provider` — history stays visible.

```toml
[model_providers.<their_existing_id>]
# name / wire_api / etc. unchanged:
base_url = "http://127.0.0.1:8787/v1"   # was: https://<their-upstream>/v1
```

That provider used to reach its upstream directly, so now point the **proxy** at that
original upstream so it forwards there. In `config.toml`:

```toml
[upstream]
url  = "<their old base_url>/responses"   # e.g. https://aihubmix.com/v1/responses
mode = "fixed"
```

Record the original `base_url` in `RESTORE.md` (§3.5).

**Case 2 — the user is on official ChatGPT OAuth login (built-in provider), preferred method:**

Set the **top-level** `openai_base_url` key — it overrides only the built-in `openai`
provider's base URL, in place. Do **not** touch `model_provider` or add a
`[model_providers.*]` block: the provider id stays `openai`, so **session history, remote
compaction, and remote-control all keep working exactly as before.**

```toml
# ~/.codex/config.toml — at the TOP LEVEL of the file, before any [section]:
openai_base_url = "http://127.0.0.1:8787/v1"
```

That's the entire change for this case — no `wire_api`, no new provider, no `model_provider`
switch. Keep the proxy's own `[upstream]` on the default ChatGPT Codex backend; Codex's OAuth
auth is forwarded by proxy `passthrough`. `codexcont wire-codex` automates exactly this edit
(with an automatic backup); `codexcont unwire-codex` removes it later.

`openai_base_url` was added upstream in `openai/codex` PR #12031 (merged 2026-03-14),
replacing the older, now-deprecated `OPENAI_BASE_URL` env var. It requires a reasonably
current Codex CLI and **only takes effect in the user-level `~/.codex/config.toml`** — Codex's
own docs state it is ignored inside a project-local `.codex/config.toml`. If §7c verification
shows Codex never reaching the proxy despite this key being set correctly, the CLI is
probably too old; fall back to the **Case 2 (legacy)** method below.

**Case 2 (legacy) — only if `openai_base_url` has no effect (old Codex CLI):**

Define a new provider and switch to it:

```toml
[model_providers.codexcont]
name = "CodexCont"
base_url = "http://127.0.0.1:8787/v1"
wire_api = "responses"          # REQUIRED — Codex only supports the Responses API

# then, at the TOP LEVEL of the file:
model_provider = "codexcont"
model = "gpt-5.5"               # or whatever model the user runs
```

🛑 **Before making this switch, explicitly tell the user it will hide their existing Codex
conversation history.** Past sessions are grouped per provider, so they stop appearing under
a different one — they are **hidden, not deleted** (restoring the original provider in §10
brings the history view back). Proceed only with their OK. Keep the proxy `[upstream]` on the
default ChatGPT Codex backend.

**Common notes (all cases):**

- These keys must live in the **user-level** `~/.codex/config.toml`, not a project-local one.
- In proxy `passthrough` mode, Codex's own login (OAuth `auth.json`, or its API key) is
  forwarded upstream unchanged — don't put a token in the provider block for the OAuth case.
- A relay that needs its key check loosened may require `requires_openai_auth = true` in its
  provider block; not needed for the default ChatGPT backend via passthrough.

### 7b — Pi (`~/.pi/agent/models.json`)

Add (or adjust) a provider whose `baseUrl` is the proxy and whose `api` is
**`openai-responses`**. `openai-completions` will *not* be folded by the proxy and gains
nothing.

```jsonc
{
  "providers": {
    "codexcont": {
      "baseUrl": "http://127.0.0.1:8787/v1",
      "api": "openai-responses",          // REQUIRED for this tool to do anything
      "apiKey": "<the user's upstream token — ask; do not invent>",
      "headers": {
        // optional, only if retargeting a custom upstream per-request:
        // "Responses-API-Base": "https://<upstream-base>/v1",
        // "chatgpt-account-id": "<account id>"
      },
      "models": [
        { "id": "gpt-5.5", "name": "GPT-5.5", "reasoning": true,
          "input": ["text", "image"], "contextWindow": 271000, "maxTokens": 128000 }
      ]
    }
  }
}
```

Then have the user select this provider/model inside Pi.

### 7c — Verify end-to-end (the real test)

A reachable port is not proof it works. Run a **real prompt** through the wired agent and
confirm the proxy actually folded a round:

- Watch the proxy's stdout (log level `info`) while the agent answers a non-trivial,
  reasoning-heavy prompt — or, if it's running via `codexcont start`, `./codexcont logs -f`.
- The final response's metadata should include **`metadata.proxy_rounds`** (per-round
  reasoning token counts and detected tier `n`) when continuation fired. For deeper proof,
  set `[log].dump_rounds_dir` in `config.toml` and inspect the per-round SSE dumps.

If the agent works but `proxy_rounds` never appears, continuation simply wasn't triggered
for that prompt (it only fires on the truncation fingerprint) — that's fine. If the agent
errors, see §9.

**A `GET /v1/models` 404 is no longer expected.** Some clients (Codex included) periodically
poll `GET /v1/models` to list models; CodexCont now answers it with a minimal placeholder
list (see `[models]` in `config.toml`) so this is a real `200`. A `404` there today would mean
an old CodexCont checkout — update it. Either way, judge real functionality only by
`POST /v1/responses` status codes and the `middleware.proxy:` round logs described above.

> **🛑 If the client reports a `404` on `/v1/responses` but the proxy's own log shows no
> record of that request at all**, do not assume CodexCont is broken — the most likely cause
> is a **local system proxy tool (Clash/mihomo/Surge, etc.) intercepting loopback traffic**
> meant for `127.0.0.1`. Many such tools add `127.0.0.1`/`localhost` to the OS proxy's
> "bypass/exceptions" list, but **many client apps (especially Rust/Node/Electron/Tauri —
> plausibly including Codex) don't honor that OS-level bypass list** and instead send every
> request through the configured proxy port. If that proxy's rules have no
> `DIRECT` rule for `127.0.0.0/8`, it forwards the "unrecognized" local request to a remote
> node, which can't reach the user's own machine and returns a 404.
>
> **To confirm:** manually `curl` the exact same URL (as in §6). If `curl` gets a normal
> numeric status *and it's logged by the proxy*, but the real client's request never appears
> in the proxy log, this is almost certainly the cause — do not start debugging or changing
> CodexCont's code/config for it.
>
> **Fix (offer the user a choice, don't silently edit their proxy tool's config):**
> 1. In the proxy tool's bypass/exceptions list, ensure `127.0.0.1, localhost` (and ideally
>    private ranges) are present.
> 2. Or, at the top of its `rules:`, add:
>    ```yaml
>    rules:
>      - IP-CIDR,127.0.0.0/8,DIRECT
>      - IP-CIDR,::1/128,DIRECT
>    ```
> 3. Or, without touching the proxy tool, set env-level `NO_PROXY` and have the user **fully
>    quit and relaunch** the client app (not just close the window — a new process must
>    inherit the new env):
>    ```bash
>    launchctl setenv NO_PROXY "127.0.0.1,localhost,::1"
>    launchctl setenv no_proxy "127.0.0.1,localhost,::1"
>    ```

---

## §8 — Keep it running + optional shortcut

**Tell the user clearly:** CodexCont must **stay running** the whole time they use their
agent through it. If it stops, the agent loses its upstream and will error until the proxy is
started again.

**Recommended: the bundled CLI.** `./codexcont start` (or `uv run codexcont start`) launches
CodexCont detached in the background and returns immediately; `./codexcont logs -f` tails its
output; `./codexcont stop` stops it; `./codexcont status` reports whether it's up. This
already satisfies "stays running without holding a terminal open" on macOS/Linux/Windows
alike — prefer it over a hand-rolled shortcut unless the user specifically wants CodexCont to
**auto-start at login/boot**, or wants the outbound-proxy env vars from §5 handled for them
(the CLI's `install` wizard asks about this and applies it on every `start` automatically).

If the user wants an auto-start-at-login shortcut, **ask** first, then create one appropriate
to the OS. Examples — adapt paths to the real repo location:

- **Windows** — a `start-codexcont.bat` on the Desktop, or in `shell:startup` to run at login:
  ```bat
  @echo off
  cd /d "C:\path\to\CodexCont"
  codexcont.bat start
  ```
- **macOS** — a `start-codexcont.command` (then `chmod +x` it) on the Desktop, or a
  `~/Library/LaunchAgents/*.plist` to run at login:
  ```bash
  #!/bin/bash
  cd "/path/to/CodexCont" && ./codexcont start
  ```
- **Linux** — a `~/.local/share/applications/codexcont.desktop`, or a systemd **user** unit to
  run at login:
  ```ini
  [Desktop Entry]
  Type=Application
  Name=CodexCont
  Exec=/bin/bash -lc 'cd /path/to/CodexCont && ./codexcont start'
  Terminal=false
  ```

All three call the CLI's background mode, so the shortcut itself returns right away; use
`codexcont logs -f` / `codexcont status` / `codexcont stop` afterward. Record any shortcut you
create in the `RESTORE.md` manifest from §3.5 so it can be removed on uninstall.

---

## §9 — Troubleshoot (symptom → cause → fix)

| Symptom | Likely cause | Fix |
|---|---|---|
| `curl` to `/v1/responses` → connection refused | Proxy not running / wrong port | Start it (§6); confirm `[server].port`; check nothing else owns 8787. |
| Client repeatedly logs `GET /v1/responses` → `405`, proxy log shows `Unsupported upgrade request` / `No supported WebSocket library detected` | Client (Codex >= ~0.140) tries a WebSocket upgrade at `ws(s)://.../v1/responses` before HTTP; an old CodexCont checkout only implements the POST route, and uvicorn has no `websockets`/`wsproto` installed | Update CodexCont (adds a WebSocket route) and re-run `uv sync` (pulls in `uvicorn[standard]`/`websockets`), then restart the proxy. `[server].enable_websocket = false` forces HTTP-only as a last-resort workaround. |
| Agent: 404 / "unknown endpoint" / empty stream | Client not using the Responses wire protocol | Codex: `wire_api = "responses"`. Pi: `"api": "openai-responses"`. |
| Agent: `401` | Upstream auth missing/invalid | In `passthrough`, the agent's own login must be valid. In `inject`, set a correct `access_token` (+ `chatgpt_account_id`) in `config.toml`. |
| Proxy returns `400` on per-request override | Credential-leak guard: a `Responses-API-Base` was sent while config would inject creds | Use `passthrough`/`passthrough_then_inject` and let the caller send its own `Authorization`. |
| Tool seems to do nothing; no `proxy_rounds` ever | (a) Reasoning stripped by a sub2api relay (§3.3); (b) reasoning disabled; (c) prompt simply didn't truncate | Use an upstream that preserves reasoning; ensure reasoning isn't disabled; this is expected when no truncation occurs. |
| `python: command not found` / wrong version | Python < 3.12 or not on PATH | Install/point at Python 3.12+. |
| `uv: command not found` | uv not installed | Install uv (§4) or use Method B. |
| Port already in use | Another process on 8787 | `lsof -i :8787` for the real listening PID, `kill -9` it, `sleep 1`, confirm empty, then change `[server].port` only if it must coexist with something else. |
| Higher first-token latency on final answer | Expected — final text is buffered until the round proves it wasn't truncated | Not a bug; document to the user. |
| Your own `curl` verification (§6) gets `403`/refused, but only when *you* (the agent) run it | Your shell is itself network-sandboxed and blocks the proxy's outbound call to `chatgpt.com` | Re-run the verification with full/unrestricted network access for your own shell; a numeric status code then confirms the proxy is fine. |
| Real client gets `404` on `/v1/responses`, but the proxy's own log has **no record** of that request | A local system proxy tool (Clash/mihomo/Surge) is intercepting loopback traffic instead of routing it `DIRECT`, and returning its own 404 | See the boxed guidance in §7c: add a `127.0.0.0/8 DIRECT` rule / bypass entry in the proxy tool, or set `NO_PROXY` + fully relaunch the client. Do not touch CodexCont. |
| Client periodically logs `GET /v1/models` → 404 | Old CodexCont checkout — current versions answer this with a placeholder 200 list | Update CodexCont; judge health via `POST /v1/responses` either way (§7c). |
| `openai_base_url` set correctly but Codex still hits the real API | Codex CLI predates PR #12031, or the key was set in a project-local `.codex/config.toml` (ignored there) | Move the key to the user-level `~/.codex/config.toml`, or upgrade Codex; else use the §7a Case 2 (legacy) `[model_providers.codexcont]` method. |
| CodexCont can't reach `chatgpt.com`/OpenAI at all (proxy up, but every upstream call fails/times out) | The user's network needs a proxy, but the process running `run.py` has no `http_proxy`/`https_proxy` env set (CodexCont doesn't read macOS system proxy settings) | Export `http_proxy`/`https_proxy` in the same shell/script that starts `run.py` (see §5/§6/§8). |
| Config edits to `~/.codex/config.toml` don't seem to take effect | Codex only reads config at startup | Have the user fully quit and relaunch Codex (not just close the window). |

---

## §10 — Uninstall / restore

When the user asks to remove CodexCont:

1. **Stop the proxy** (`codexcont stop`, or kill the `run.py` process / close its terminal).
2. **Undo each agent's wiring**, per what was actually done in §7:
   - Codex via §7a Case 2 (`openai_base_url`): run `codexcont unwire-codex`.
   - Any other edit (Case 1, Case 2 legacy, or Pi): restore from the backup made in §3.5 —
     open `$BACKUP/RESTORE.md`, then copy each backed-up file back over its original path.
     For example:
     ```bash
     cp -p "$BACKUP/codex.config.toml" "$HOME/.codex/config.toml"
     cp -p "$BACKUP/pi.models.json"    "$HOME/.pi/agent/models.json"
     ```
     (If you instead only added a provider block, you may surgically remove just that block —
     but restoring the backup is the safe default.)
3. **Remove the shortcut** you created in §8, if any.
4. **Optionally remove the project artifacts**: `.venv/`, `.codexcont/`, and `config.toml`
   (or, for Method B, `python -m pip uninstall httpx starlette uvicorn` only if the user
   wants those gone).
5. Confirm to the user, in their language, that their original agent configuration is
   restored, and verify their agent works against its original upstream again.
