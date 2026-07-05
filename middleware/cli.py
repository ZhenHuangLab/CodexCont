"""codexcont: interactive installer + service manager for the CodexCont proxy.

    codexcont                 interactive menu (install / start / stop / logs / ...)
    codexcont install [-y]    guided setup: install deps, write config.toml
    codexcont start [-f]      start the proxy (background by default; -f = foreground)
    codexcont stop            stop a background proxy started by this tool
    codexcont restart [-f]    stop, then start
    codexcont status          show whether the proxy is running
    codexcont logs [-f] [-n N]   show/follow the background log file
    codexcont wire-codex      point Codex CLI's built-in provider at this proxy
                              (sets top-level `openai_base_url` in ~/.codex/config.toml;
                              does NOT touch `model_provider`, so history stays visible)
    codexcont unwire-codex    remove the `openai_base_url` line added above

Only CodexCont itself (dependencies, config.toml, the server process) is managed
non-interactively. Editing *other* tools' configs (`wire-codex` aside) is
intentionally left to INSTALL-GUIDE-AGENT/AGENT.md, since that involves
per-agent nuance and backups this script keeps deliberately out of scope.
"""

from __future__ import annotations

import argparse
import getpass
import json
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from .config import load_config
from . import paths
from .paths import ENV_CONFIG

CODEX_CONFIG = Path.home() / ".codex" / "config.toml"
WIRE_MARKER = "# codexcont-managed: openai_base_url (added by `codexcont wire-codex`)"
SERVER_CMD = [sys.executable, "-m", "middleware.server"]


def _config_path() -> Path:
    return paths.config_path()


def _state_dir() -> Path:
    return paths.state_dir()


def _pid_file() -> Path:
    return _state_dir() / "codexcont.pid"


def _log_file() -> Path:
    return _state_dir() / "codexcont.log"


def _env_file() -> Path:
    return _state_dir() / "env.json"


# --- tiny interactive helpers ------------------------------------------------


def _ask(prompt: str, default: str = "") -> str:
    suffix = f" [{default}]" if default else ""
    raw = input(f"{prompt}{suffix}: ").strip()
    return raw or default


def _ask_yes_no(prompt: str, *, default: bool) -> bool:
    hint = "Y/n" if default else "y/N"
    raw = input(f"{prompt} [{hint}]: ").strip().lower()
    if not raw:
        return default
    return raw in ("y", "yes")


def _ask_choice(prompt: str, choices: list[str], default: str) -> str:
    for i, c in enumerate(choices, 1):
        marker = "  <- default" if c == default else ""
        print(f"  {i}) {c}{marker}")
    raw = input(f"{prompt} [{default}]: ").strip()
    if not raw:
        return default
    if raw.isdigit() and 1 <= int(raw) <= len(choices):
        return choices[int(raw) - 1]
    return raw if raw in choices else default


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _backup_file(path: Path, name: str) -> Path:
    dest_dir = paths.backup_dir() / time.strftime("%Y%m%d-%H%M%S")
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / name
    dest.write_bytes(path.read_bytes())
    return dest


# --- config.toml text surgery (keeps comments; no TOML writer dependency) ---


def _set_toml_scalar(
    text: str, section: str, key: str, value: Any, *, quote: bool = True
) -> str:
    """Set `key = value` inside `[section]` of a TOML document, preserving
    every other line (including comments) verbatim."""
    val_str = json.dumps(value) if quote else str(value)
    sec_re = re.compile(
        rf"(^\[{re.escape(section)}\]\s*\n)(.*?)(?=^\[|\Z)", re.S | re.M
    )

    def repl(m: "re.Match[str]") -> str:
        header, body = m.group(1), m.group(2)
        key_re = re.compile(rf'^({re.escape(key)}\s*=\s*)(".*?"|\S+)(.*)$', re.M)
        if key_re.search(body):
            body = key_re.sub(
                lambda km: km.group(1) + val_str + km.group(3), body, count=1
            )
        else:
            body = body.rstrip("\n") + f"\n{key} = {val_str}\n"
        return header + body

    new_text, n = sec_re.subn(repl, text, count=1)
    if n == 0:
        new_text = text.rstrip("\n") + f"\n\n[{section}]\n{key} = {val_str}\n"
    return new_text


def _render_summary(text: str) -> str:
    """[server]/[upstream]/[auth] blocks with secret values masked."""
    secret_keys = {"access_token", "chatgpt_account_id"}

    def mask(line: str) -> str:
        m = re.match(r'^(\s*(\w+)\s*=\s*")([^"]*)(".*)$', line)
        if m and m.group(2) in secret_keys and m.group(3):
            return f"{m.group(1)}{'*' * min(len(m.group(3)), 8)}{m.group(4)}"
        return line

    out: list[str] = []
    for section in ("server", "upstream", "auth"):
        m = re.search(rf"^\[{section}\]\s*\n(.*?)(?=^\[|\Z)", text, re.S | re.M)
        if not m:
            continue
        out.append(f"[{section}]")
        out.extend(mask(ln) for ln in m.group(1).splitlines() if ln.strip())
        out.append("")
    return "\n".join(out).rstrip()


# --- process management ------------------------------------------------------


def _load_cfg():
    return load_config(_config_path())


def _client_host(host: str) -> str:
    """127.0.0.1 is what a local client should dial even if the server binds
    a wildcard address like 0.0.0.0."""
    return "127.0.0.1" if host in ("0.0.0.0", "::", "") else host


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        try:
            out = subprocess.run(
                ["tasklist", "/FI", f"PID eq {pid}"],
                capture_output=True,
                text=True,
                timeout=5,
            )
            return str(pid) in out.stdout
        except Exception:
            return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _read_pid() -> int | None:
    try:
        return int(_pid_file().read_text().strip())
    except (FileNotFoundError, ValueError):
        return None


def running_pid() -> int | None:
    pid = _read_pid()
    if pid and _pid_alive(pid):
        return pid
    return None


def _port_open(host: str, port: int, timeout: float = 0.5) -> bool:
    try:
        with socket.create_connection((_client_host(host), port), timeout=timeout):
            return True
    except OSError:
        return False


def _spawn_env() -> dict[str, str]:
    env = os.environ.copy()
    env[ENV_CONFIG] = str(_config_path())
    env_file = _env_file()
    if env_file.exists():
        try:
            extra = json.loads(env_file.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            extra = {}
        env.update({str(k): str(v) for k, v in extra.items() if v})
    return env


def _print_tail(path: Path, n: int) -> None:
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except FileNotFoundError:
        return
    for line in lines[-n:]:
        print(line)


def _follow(path: Path) -> None:
    print(f"--- following {path} (Ctrl+C to stop) ---")
    with path.open("r", encoding="utf-8", errors="replace") as fh:
        fh.seek(0, os.SEEK_END)
        try:
            while True:
                line = fh.readline()
                if line:
                    print(line, end="")
                else:
                    time.sleep(0.3)
        except KeyboardInterrupt:
            print()


# --- commands -----------------------------------------------------------------


def cmd_start(args: argparse.Namespace) -> int:
    cfg_path = _config_path()
    if not cfg_path.exists():
        print(f"{cfg_path} not found -- run `codexcont install` first.")
        return 1

    cfg = _load_cfg()
    host = _client_host(cfg.server.host)

    pid = running_pid()
    if pid:
        print(f"Already running (pid {pid}, http://{host}:{cfg.server.port}).")
        print("Use `codexcont restart` to restart it.")
        return 0

    if _port_open(cfg.server.host, cfg.server.port):
        print(
            f"Warning: {host}:{cfg.server.port} is already in use by some other process "
            f"(not one this tool started). Stop it first, or change [server].port in "
            f"{cfg_path}."
        )
        return 1

    env = _spawn_env()
    state = _state_dir()
    log_file = _log_file()
    pid_file = _pid_file()

    if getattr(args, "foreground", False):
        print(
            f"Starting CodexCont in the foreground on http://{host}:{cfg.server.port} (Ctrl+C to stop)..."
        )
        os.chdir(state)
        os.execve(sys.executable, SERVER_CMD, env)
        raise RuntimeError("unreachable")  # os.execve never returns on success

    state.mkdir(parents=True, exist_ok=True)
    with open(log_file, "a", encoding="utf-8") as fh:
        fh.write(f"\n=== codexcont start: {_now()} ===\n")
        fh.flush()
        kwargs: dict[str, Any] = dict(
            cwd=str(state), stdout=fh, stderr=subprocess.STDOUT, env=env
        )
        if os.name == "nt":
            kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
        else:
            kwargs["start_new_session"] = True
        proc = subprocess.Popen(SERVER_CMD, **kwargs)

    pid_file.write_text(str(proc.pid))
    print(f"Starting CodexCont in the background (pid {proc.pid})...")
    for _ in range(20):
        if _port_open(cfg.server.host, cfg.server.port):
            print(f"Up: http://{host}:{cfg.server.port}   (logs: `codexcont logs -f`)")
            return 0
        if not _pid_alive(proc.pid):
            print("The process exited immediately -- check `codexcont logs`.")
            return 1
        time.sleep(0.25)
    print(
        "Didn't confirm the port came up yet -- check `codexcont logs` / `codexcont status`."
    )
    return 0


def cmd_stop(_args: argparse.Namespace) -> int:
    pid = running_pid()
    pid_file = _pid_file()
    if not pid:
        stale = _read_pid()
        if stale:
            print(f"Not running (stale pid file for {stale}; removing it).")
            pid_file.unlink(missing_ok=True)
        else:
            print("Not running.")
        return 0

    print(f"Stopping CodexCont (pid {pid})...")
    if os.name == "nt":
        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True)
    else:
        os.kill(pid, signal.SIGTERM)
        for _ in range(30):
            if not _pid_alive(pid):
                break
            time.sleep(0.2)
        else:
            print("Still alive after 6s; sending SIGKILL...")
            os.kill(pid, signal.SIGKILL)
    pid_file.unlink(missing_ok=True)
    print("Stopped.")
    return 0


def cmd_restart(args: argparse.Namespace) -> int:
    cmd_stop(args)
    time.sleep(0.3)
    return cmd_start(args)


def cmd_status(_args: argparse.Namespace) -> int:
    cfg = _load_cfg()
    host = _client_host(cfg.server.host)
    pid = running_pid()
    cfg_path = _config_path()
    pid_file = _pid_file()
    env_file = _env_file()
    log_file = _log_file()
    if pid:
        print(f"\u25cf running   pid={pid}   http://{host}:{cfg.server.port}")
        reachable = _port_open(cfg.server.host, cfg.server.port)
        print(
            f"  port check: {'reachable' if reachable else 'NOT reachable yet (still starting up?)'}"
        )
    else:
        stale = _read_pid()
        if stale:
            print(f"\u25cb not running (stale pid file for {stale}; cleaning up)")
            pid_file.unlink(missing_ok=True)
        elif not cfg_path.exists():
            print("\u25cb not running  (not installed -- run `codexcont install`)")
        else:
            print("\u25cb not running")
    if env_file.exists():
        print(f"  outbound proxy: configured ({env_file})")
    if log_file.exists():
        print(f"  log file: {log_file}")
        print("  last lines:")
        _print_tail(log_file, 5)
    return 0


def cmd_logs(args: argparse.Namespace) -> int:
    log_file = _log_file()
    if not log_file.exists():
        print(
            f"No log file yet at {log_file} -- has the server been started in the background?"
        )
        return 1
    if args.follow:
        _follow(log_file)
    else:
        _print_tail(log_file, args.lines)
    return 0


def _wizard_config(existing_cfg: bool, args: argparse.Namespace) -> str | None:
    cfg_path = _config_path()
    if existing_cfg:
        if args.yes:
            print(
                f"Keeping the existing {cfg_path} unchanged (non-interactive install)."
            )
            return None
        if not _ask_yes_no(
            f"{cfg_path.name} already exists. Reconfigure it now?", default=False
        ):
            print(f"Keeping the existing {cfg_path} unchanged.")
            return None
        text = cfg_path.read_text(encoding="utf-8")
    else:
        text = paths.read_example_config()
        if text is None:
            print("Cannot find config.example.toml.")
            return None

    if args.yes:
        return (
            text  # fresh install, non-interactive: accept the example defaults verbatim
        )

    print("\n-- Auth mode ([auth].mode) --")
    print(
        "  passthrough              forward only the agent's own auth (default, safest)"
    )
    print("  inject                   always use the token entered below")
    print(
        "  passthrough_then_inject  use the agent's auth if present, else the token below"
    )
    mode = _ask_choice(
        "Choose a mode",
        ["passthrough", "inject", "passthrough_then_inject"],
        "passthrough",
    )
    text = _set_toml_scalar(text, "auth", "mode", mode)

    if mode in ("inject", "passthrough_then_inject"):
        token = getpass.getpass(
            "Access token (Authorization: Bearer <token>; hidden, blank to skip): "
        )
        text = _set_toml_scalar(text, "auth", "access_token", token)
        account = _ask("chatgpt-account-id (ChatGPT Codex backend only; blank to omit)")
        text = _set_toml_scalar(text, "auth", "chatgpt_account_id", account)

    print("\n-- Upstream --")
    upstream_choice = _ask_choice("Upstream endpoint", ["chatgpt", "custom"], "chatgpt")
    if upstream_choice == "custom":
        url = _ask(
            "Custom upstream Responses URL", "https://api.openai.com/v1/responses"
        )
        text = _set_toml_scalar(text, "upstream", "url", url)
        text = _set_toml_scalar(text, "upstream", "mode", "fixed")
    else:
        text = _set_toml_scalar(
            text, "upstream", "url", "https://chatgpt.com/backend-api/codex/responses"
        )
        text = _set_toml_scalar(text, "upstream", "mode", "header")

    print("\n-- Server --")
    port = _ask("Local port", "8787")
    text = _set_toml_scalar(text, "server", "port", port, quote=False)

    print(
        "\n-- Outbound proxy (only if your network needs one to reach OpenAI, e.g. Clash/mihomo) --"
    )
    proxy = _ask("Proxy URL for CodexCont's own outbound requests (blank = none)", "")
    state = _state_dir()
    env_file = _env_file()
    state.mkdir(parents=True, exist_ok=True)
    if proxy:
        env_file.write_text(
            json.dumps(
                {
                    "http_proxy": proxy,
                    "https_proxy": proxy,
                    "HTTP_PROXY": proxy,
                    "HTTPS_PROXY": proxy,
                },
                indent=2,
            )
        )
        print(
            f"Saved to {env_file} (applied automatically whenever `codexcont start` runs)."
        )
    elif env_file.exists() and _ask_yes_no(
        "Remove the previously saved outbound proxy setting?", default=False
    ):
        env_file.unlink()

    print("\n-- Summary (secrets masked) --\n")
    print(_render_summary(text))
    print()
    if not _ask_yes_no(f"Write this to {cfg_path}?", default=True):
        print("Aborted; config.toml left unchanged.")
        return None
    return text


def cmd_install(args: argparse.Namespace) -> int:
    cfg_path = _config_path()
    print("=== CodexCont installer ===")
    if paths.is_dev_checkout():
        print(f"Mode: development checkout ({paths.PACKAGE_ROOT})")
    else:
        print("Mode: installed package")
    print(f"Config: {cfg_path}\n")

    if sys.version_info < (3, 12):
        print(f"Python 3.12+ is required (found {sys.version.split()[0]}). Aborting.")
        return 1

    has_uv = shutil.which("uv") is not None
    existing_cfg = cfg_path.exists()
    dev = paths.is_dev_checkout()

    print("This will:")
    if dev:
        print(
            f"  - install dependencies via {'`uv sync`' if has_uv else 'pip (current interpreter)'}"
        )
    else:
        print("  - skip dependency install (already provided by uvx/pip)")
    print(f"  - {'optionally update' if existing_cfg else 'create'} {cfg_path}")
    print(
        "\nThis does NOT touch any other tool's config. Use `codexcont wire-codex` for the"
    )
    print(
        "one-line Codex CLI hookup, or hand INSTALL-GUIDE-AGENT/AGENT.md to your coding"
    )
    print("agent for a fully guided, backed-up wiring of any agent (Codex, Pi, ...).\n")

    if not args.yes and not _ask_yes_no("Proceed?", default=True):
        print("Aborted.")
        return 1

    if dev:
        if has_uv:
            rc = subprocess.run(["uv", "sync"], cwd=str(paths.PACKAGE_ROOT)).returncode
        else:
            rc = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "pip",
                    "install",
                    "httpx>=0.27",
                    "starlette>=0.37",
                    "uvicorn>=0.30",
                ],
                cwd=str(paths.PACKAGE_ROOT),
            ).returncode
        if rc != 0:
            print("Dependency install failed; see the output above.")
            return rc
    else:
        print("Dependencies already installed; skipping.")
        rc = 0

    new_text = _wizard_config(existing_cfg, args)
    if new_text is not None:
        cfg_path.parent.mkdir(parents=True, exist_ok=True)
        cfg_path.write_text(new_text, encoding="utf-8")
        print(f"Wrote {cfg_path}")

    print("\nInstall complete.")
    if not cfg_path.exists():
        print("No config.toml was written; re-run `codexcont install` to create one.")
        return 0

    if args.yes or _ask_yes_no("Start CodexCont now in the background?", default=True):
        rc = cmd_start(argparse.Namespace(foreground=False))
    else:
        print(
            "Start it later with `codexcont start` (or just `codexcont` for the menu)."
        )
        rc = 0

    print(
        "\nNext step -- point a coding agent at the proxy (nothing else was changed yet):"
    )
    if CODEX_CONFIG.exists():
        print(
            "  - Codex CLI detected: run `codexcont wire-codex` to point its built-in"
        )
        print("    `openai` provider at this proxy. A backup is made first, and")
        print("    `codexcont unwire-codex` reverts it.")
    else:
        print(
            "  - Codex CLI: once it's installed, run `codexcont wire-codex` to point it"
        )
        print("    at this proxy (`codexcont unwire-codex` reverts it).")
    print(
        "  - Any other coding agent, or to review the change before applying it: hand"
    )
    print(
        "    INSTALL-GUIDE-AGENT/AGENT.md to your coding agent for a guided, backed-up wiring."
    )
    return rc


# --- Codex CLI wiring (opt-in, explicit; openai_base_url only) --------------


def _wire_codex_text(text: str, base_url: str) -> tuple[str, bool]:
    """Insert/replace the top-level `openai_base_url` line. Returns
    (new_text, replaced_existing_line)."""
    new_line = f'openai_base_url = "{base_url}"'
    lines = text.splitlines()
    for i, ln in enumerate(lines):
        if re.match(r"^\s*openai_base_url\s*=", ln):
            if i > 0 and lines[i - 1].strip() == WIRE_MARKER:
                lines[i] = new_line
            else:
                lines[i : i + 1] = [WIRE_MARKER, new_line]
            trail = "\n" if text.endswith("\n") else ""
            return "\n".join(lines) + trail, True
    lines = [WIRE_MARKER, new_line] + lines
    trail = "\n" if text.endswith("\n") else ""
    return "\n".join(lines) + trail, False


def _unwire_codex_text(text: str) -> tuple[str, bool]:
    lines = text.splitlines()
    out: list[str] = []
    removed = False
    i = 0
    while i < len(lines):
        if re.match(r"^\s*openai_base_url\s*=", lines[i]):
            if out and out[-1].strip() == WIRE_MARKER:
                out.pop()
            removed = True
            i += 1
            continue
        out.append(lines[i])
        i += 1
    trail = "\n" if text.endswith("\n") else ""
    return "\n".join(out) + trail, removed


def cmd_wire_codex(args: argparse.Namespace) -> int:
    if not CODEX_CONFIG.exists():
        print(
            f"{CODEX_CONFIG} not found -- is the Codex CLI installed/initialized? Nothing to do."
        )
        return 1

    cfg = _load_cfg()
    base_url = f"http://{_client_host(cfg.server.host)}:{cfg.server.port}/v1"
    text = CODEX_CONFIG.read_text(encoding="utf-8")
    new_text, replaced = _wire_codex_text(text, base_url)
    if new_text == text:
        print(f"{CODEX_CONFIG} already points at {base_url}. Nothing to change.")
        return 0

    print(
        f"\nThis will {'update' if replaced else 'add'} one top-level line in {CODEX_CONFIG}:\n"
    )
    print(f'    openai_base_url = "{base_url}"\n')
    print(
        "This overrides only the built-in `openai` provider's base URL -- `model_provider`"
    )
    print("is left untouched, so Codex's session history stays visible under the same")
    print("provider (see openai/codex#12031). A full backup is made first.\n")
    if not args.yes and not _ask_yes_no("Proceed?", default=True):
        print("Aborted.")
        return 1

    backup = _backup_file(CODEX_CONFIG, "codex.config.toml")
    CODEX_CONFIG.write_text(new_text, encoding="utf-8")
    print(f"Backed up the original to {backup}")
    print(f"Wrote {CODEX_CONFIG}")
    print(
        "\nCodex only re-reads config.toml on a full restart -- fully quit and relaunch"
    )
    print("Codex (closing the chat window/tab is not enough) before testing.")
    return 0


def cmd_unwire_codex(args: argparse.Namespace) -> int:
    if not CODEX_CONFIG.exists():
        print(f"{CODEX_CONFIG} not found. Nothing to do.")
        return 0
    text = CODEX_CONFIG.read_text(encoding="utf-8")
    new_text, removed = _unwire_codex_text(text)
    if not removed:
        print(
            f"No top-level `openai_base_url` found in {CODEX_CONFIG}. Nothing to remove."
        )
        return 0

    print(
        f"This will remove the `openai_base_url` line from {CODEX_CONFIG} (backup made first)."
    )
    if not args.yes and not _ask_yes_no("Proceed?", default=True):
        print("Aborted.")
        return 1

    backup = _backup_file(CODEX_CONFIG, "codex.config.toml")
    CODEX_CONFIG.write_text(new_text, encoding="utf-8")
    print(f"Backed up the original to {backup}")
    print(f"Removed it from {CODEX_CONFIG}. Fully quit and relaunch Codex.")
    return 0


# --- interactive menu ---------------------------------------------------------


def _menu() -> int:
    while True:
        pid = running_pid()
        print("\n===================================")
        print(" CodexCont")
        print("===================================")
        if not _config_path().exists():
            print(" status: not installed")
        elif pid:
            cfg = _load_cfg()
            host = _client_host(cfg.server.host)
            print(f" status: RUNNING  (pid {pid}, http://{host}:{cfg.server.port})")
        else:
            print(" status: stopped")
        print()
        print(" 1) Install / reconfigure")
        print(" 2) Start (background)")
        print(" 3) Start (foreground -- Ctrl+C to stop; exits this menu)")
        print(" 4) Stop")
        print(" 5) Restart")
        print(" 6) Show recent logs")
        print(" 7) Follow logs (Ctrl+C to stop following)")
        print(" 8) Status")
        print(" 9) Wire Codex CLI at this proxy (sets openai_base_url)")
        print(" 10) Unwire Codex CLI")
        print(" 0) Exit")
        choice = input("\n> ").strip()
        no_confirm = argparse.Namespace(yes=False)
        if choice == "1":
            cmd_install(argparse.Namespace(yes=False))
        elif choice == "2":
            cmd_start(argparse.Namespace(foreground=False))
        elif choice == "3":
            cmd_start(argparse.Namespace(foreground=True))  # execve: does not return
        elif choice == "4":
            cmd_stop(argparse.Namespace())
        elif choice == "5":
            cmd_restart(argparse.Namespace(foreground=False))
        elif choice == "6":
            cmd_logs(argparse.Namespace(follow=False, lines=40))
        elif choice == "7":
            cmd_logs(argparse.Namespace(follow=True, lines=40))
        elif choice == "8":
            cmd_status(argparse.Namespace())
        elif choice == "9":
            cmd_wire_codex(no_confirm)
        elif choice == "10":
            cmd_unwire_codex(no_confirm)
        elif choice in ("0", "q", "quit", "exit"):
            return 0
        else:
            print("Unknown choice.")


# --- argparse wiring -----------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="codexcont", description="CodexCont installer + service manager."
    )
    sub = p.add_subparsers(dest="command")

    p_install = sub.add_parser(
        "install", help="guided setup: dependencies + config.toml"
    )
    p_install.add_argument(
        "-y", "--yes", action="store_true", help="non-interactive: accept defaults"
    )

    p_start = sub.add_parser("start", help="start the proxy (background by default)")
    p_start.add_argument(
        "-f",
        "--foreground",
        action="store_true",
        help="run attached, not in the background",
    )

    sub.add_parser("stop", help="stop the background proxy")

    p_restart = sub.add_parser("restart", help="stop then start the proxy")
    p_restart.add_argument("-f", "--foreground", action="store_true")

    sub.add_parser("status", help="show whether the proxy is running")

    p_logs = sub.add_parser("logs", help="show/follow the background log file")
    p_logs.add_argument("-f", "--follow", action="store_true")
    p_logs.add_argument("-n", "--lines", type=int, default=40)

    p_wire = sub.add_parser(
        "wire-codex",
        help="point Codex CLI's built-in provider at this proxy (openai_base_url)",
    )
    p_wire.add_argument("-y", "--yes", action="store_true")

    p_unwire = sub.add_parser(
        "unwire-codex", help="remove the openai_base_url line added by wire-codex"
    )
    p_unwire.add_argument("-y", "--yes", action="store_true")

    sub.add_parser("menu", help="interactive menu (default when no command is given)")
    return p


_COMMANDS = {
    "install": cmd_install,
    "start": cmd_start,
    "stop": cmd_stop,
    "restart": cmd_restart,
    "status": cmd_status,
    "logs": cmd_logs,
    "wire-codex": cmd_wire_codex,
    "unwire-codex": cmd_unwire_codex,
}


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if not args.command or args.command == "menu":
            return _menu()
        return _COMMANDS[args.command](args)
    except KeyboardInterrupt:
        print("\nAborted.")
        return 130
    except EOFError:
        print(
            "\nNo input available (not an interactive terminal). Pass -y/--yes for non-interactive use."
        )
        return 1


if __name__ == "__main__":
    sys.exit(main())
