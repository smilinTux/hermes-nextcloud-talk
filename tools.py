"""Call tools: let the agent join, leave and check a Nextcloud Talk call.

Opt-in with ``NEXTCLOUD_TALK_CALL_TOOLS=on``. The tools never run a call
themselves: they drive one systemd --user template unit per room,
``skcall-join@<room>.service`` (from the skcall repo,
``systemd/skcall-join@.service``), so a call is never a process the agent
watches, restarts or duplicates. ``call_join`` returns at once.

Single instance, three layers: systemd runs each instance at most once, the
tools check ``is-active`` before starting, and skcall itself holds a per-
(agent, room) flock and refuses to join if the agent is already in the call
from another session (exit 3, "already in call").

Per-call options (ring, greeting) go in ``$XDG_RUNTIME_DIR/skcall/join-<room>.env``,
which the unit reads as an optional EnvironmentFile.

Kept import-light (stdlib only, no gateway imports at module level) so the
Hermes loader can pre-register these tools in CLI/TUI processes.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path
from typing import Any, Callable, List, Optional

TOOLSET = "nctalk-calls"
ENABLE_ENV = "NEXTCLOUD_TALK_CALL_TOOLS"
UNIT_TEMPLATE = os.getenv("NEXTCLOUD_TALK_CALL_UNIT", "skcall-join@{room}.service")
ROOM_RE = re.compile(r"[a-z0-9]{4,32}")
PLATFORM = "nctalk"
GREETING_MAX = 300
EXIT_ALREADY_IN_CALL = "3"

# Swappable in tests.
Runner = Callable[[List[str]], "subprocess.CompletedProcess[str]"]


def _default_run(cmd: List[str]) -> "subprocess.CompletedProcess[str]":
    return subprocess.run(cmd, capture_output=True, text=True, timeout=20, check=False)


_run: Runner = _default_run


def _env(name: str, default: str = "") -> str:
    try:  # Hermes resolves credentials/env per profile scope (multiplexed gateways)
        from agent.secret_scope import get_secret  # type: ignore
        val = get_secret(name, default)
        return default if val is None else val
    except Exception:
        return os.getenv(name, default)


def tools_enabled() -> bool:
    return _env(ENABLE_ENV, "").strip().lower() in ("1", "on", "true", "yes")


def _session(name: str) -> str:
    try:
        from gateway.session_context import get_session_env  # type: ignore
        return get_session_env(name, "") or ""
    except Exception:
        return os.getenv(name, "")


def current_room() -> str:
    """The Talk room this turn came from, or "" when not invoked from Talk."""
    platform = _session("HERMES_SESSION_PLATFORM").strip().lower()
    chat_id = _session("HERMES_SESSION_CHAT_ID").strip()
    if platform == PLATFORM and ROOM_RE.fullmatch(chat_id):
        return chat_id
    return ""


def _result(**data: Any) -> str:
    return json.dumps(data, ensure_ascii=False)


def _resolve_room(args: dict) -> tuple[str, Optional[str]]:
    """(room, error). An explicit room wins; otherwise the current Talk room."""
    room = str((args or {}).get("room") or "").strip()
    if not room:
        room = current_room()
        if not room:
            return "", ("room is required: give the Nextcloud Talk room token "
                        "(the current chat is not a Talk room)")
    if not ROOM_RE.fullmatch(room):
        return "", f"invalid room token {room!r}: expected 4-32 lowercase letters/digits"
    return room, None


def unit_for(room: str) -> str:
    return UNIT_TEMPLATE.format(room=room)


def runtime_dir() -> Path:
    base = os.getenv("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}"
    return Path(base) / "skcall"


def env_file(room: str) -> Path:
    return runtime_dir() / f"join-{room}.env"


def clean_greeting(text: Optional[str]) -> str:
    """One printable line, safe inside a double-quoted systemd EnvironmentFile value."""
    if not text:
        return ""
    text = "".join(ch if ch.isprintable() else " " for ch in str(text))
    text = re.sub(r'[\\"$`]', "", text)
    return " ".join(text.split())[:GREETING_MAX]


def write_env_file(room: str, ring: bool, greeting: str) -> None:
    path = env_file(room)
    if not ring and not greeting:
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        return
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    lines = []
    if ring:
        lines.append("SKCALL_RING=1")
    if greeting:
        lines.append(f'SKCALL_GREETING="{greeting}"')
    tmp = path.with_suffix(".tmp")
    tmp.write_text("\n".join(lines) + "\n")
    os.chmod(tmp, 0o600)
    tmp.replace(path)


def _systemctl(*args: str) -> "subprocess.CompletedProcess[str]":
    return _run(["systemctl", "--user", *args])


def unit_state(room: str) -> dict:
    p = _systemctl("show", unit_for(room), "--property=ActiveState,SubState,Result,ExecMainStatus,"
                   "ActiveEnterTimestamp,LoadState")
    props = {}
    for line in (p.stdout or "").splitlines():
        k, sep, v = line.partition("=")
        if sep:
            props[k] = v
    return props


def _in_call(state: dict) -> bool:
    return state.get("ActiveState") in ("active", "activating", "reloading")


def _truthy(v: Any) -> bool:
    if isinstance(v, bool):
        return v
    return str(v or "").strip().lower() in ("1", "true", "yes", "on")


# ----------------------------------------------------------------- handlers
def call_join(args: dict, **_kw: Any) -> str:
    room, err = _resolve_room(args)
    if err:
        return _result(error=err)
    unit = unit_for(room)
    state = unit_state(room)
    if state.get("LoadState") == "not-found":
        return _result(error=f"{unit} is not installed on this host (skcall systemd template missing)")
    if _in_call(state):
        return _result(status="already in call", room=room, unit=unit,
                       note="She is already in this call. Do not start another one.")
    try:
        write_env_file(room, _truthy((args or {}).get("ring")), clean_greeting((args or {}).get("greeting")))
    except OSError as e:
        return _result(error=f"could not write call options: {e}")
    p = _systemctl("start", "--no-block", unit)
    if p.returncode != 0:
        return _result(error=f"systemctl start {unit} failed: {(p.stderr or p.stdout or '').strip()[:300]}")
    return _result(status="joining", room=room, unit=unit,
                   note=("Joining now in the background; it takes a few seconds. Do not run it "
                         "again, do not poll, and do not start skcall any other way. A summary "
                         "is posted to the room's chat when the call ends."))


def call_leave(args: dict, **_kw: Any) -> str:
    room, err = _resolve_room(args)
    if err:
        return _result(error=err)
    unit = unit_for(room)
    state = unit_state(room)
    try:
        env_file(room).unlink()
    except (FileNotFoundError, OSError):
        pass
    if not _in_call(state):
        return _result(status="not in call", room=room, unit=unit)
    p = _systemctl("stop", "--no-block", unit)
    if p.returncode != 0:
        return _result(error=f"systemctl stop {unit} failed: {(p.stderr or p.stdout or '').strip()[:300]}")
    return _result(status="leaving", room=room, unit=unit)


def _describe(room: str, state: dict) -> dict:
    out = {"room": room, "unit": unit_for(room),
           "state": state.get("ActiveState", "unknown"), "sub_state": state.get("SubState", "")}
    if _in_call(state):
        out["status"] = "in call"
        if state.get("ActiveEnterTimestamp"):
            out["since"] = state["ActiveEnterTimestamp"]
    elif state.get("ActiveState") == "failed":
        out["status"] = "last join failed"
        out["result"] = state.get("Result", "")
        out["exit_status"] = state.get("ExecMainStatus", "")
        j = _run(["journalctl", "--user", "-u", unit_for(room), "-n", "6", "-o", "cat", "--no-pager"])
        tail = [ln for ln in (j.stdout or "").splitlines() if ln.strip()][-6:]
        if tail:
            out["last_log"] = tail
    else:
        out["status"] = "not in call"
        if state.get("ExecMainStatus") == EXIT_ALREADY_IN_CALL:
            out["last_exit"] = "already in call (another session held it)"
    return out


def call_status(args: dict, **_kw: Any) -> str:
    room = str((args or {}).get("room") or "").strip() or current_room()
    if room:
        if not ROOM_RE.fullmatch(room):
            return _result(error=f"invalid room token {room!r}: expected 4-32 lowercase letters/digits")
        return _result(**_describe(room, unit_state(room)))
    p = _systemctl("list-units", "--all", "--no-legend", "--plain", "skcall-join@*.service")
    calls = []
    for line in (p.stdout or "").splitlines():
        parts = line.split()
        if not parts:
            continue
        m = re.fullmatch(r"skcall-join@([a-z0-9]{4,32})\.service", parts[0])
        if m:
            calls.append(_describe(m.group(1), unit_state(m.group(1))))
    return _result(calls=calls, in_call=[c["room"] for c in calls if c["status"] == "in call"])


# ----------------------------------------------------------------- schemas
_COMMON = (" Use these call tools for ANY request to join, leave or check a call "
           "(e.g. '@lumina join call'). Never launch skcall, python or systemd-run "
           "through the terminal tool, and never run a call as a background process.")

CALL_JOIN_SCHEMA = {
    "name": "call_join",
    "description": (
        "Join the Nextcloud Talk call of a room as yourself (voice, with your avatar). "
        "Returns immediately with 'joining' or 'already in call'; the call then runs on "
        "its own in the background, exactly one instance per room. Call it ONCE per "
        "request: do not retry, poll or relaunch it, and never start a second join. "
        "When invoked from a Talk chat, room defaults to that chat's room." + _COMMON),
    "parameters": {
        "type": "object",
        "properties": {
            "room": {"type": "string",
                     "description": "Talk room token (4-32 lowercase letters/digits). "
                                    "Omit to use the current Talk chat's room."},
            "ring": {"type": "boolean", "default": False,
                     "description": "Ring the room's members (non-silent join). Default false: "
                                    "only ring when explicitly asked."},
            "greeting": {"type": "string",
                         "description": "Optional short line to say when the first person joins."},
        },
    },
}

CALL_LEAVE_SCHEMA = {
    "name": "call_leave",
    "description": ("Leave the Nextcloud Talk call of a room (clean leave; a summary is "
                    "posted to the room's chat). Room defaults to the current Talk chat." + _COMMON),
    "parameters": {
        "type": "object",
        "properties": {
            "room": {"type": "string",
                     "description": "Talk room token. Omit to use the current Talk chat's room."},
        },
    },
}

CALL_STATUS_SCHEMA = {
    "name": "call_status",
    "description": ("Check whether you are in a Nextcloud Talk call: for one room (default the "
                    "current Talk chat) or, with no room outside Talk, every call unit on this "
                    "host. Shows the last failure reason if a join failed." + _COMMON),
    "parameters": {
        "type": "object",
        "properties": {
            "room": {"type": "string", "description": "Talk room token (optional)."},
        },
    },
}

TOOLS = (
    ("call_join", CALL_JOIN_SCHEMA, call_join, "\U0001f4de"),     # telephone receiver
    ("call_leave", CALL_LEAVE_SCHEMA, call_leave, "\U0001f4f4"),  # mobile phone off
    ("call_status", CALL_STATUS_SCHEMA, call_status, "\U0001f4f6"),  # antenna bars
)


def register_tools(ctx) -> None:
    """Register the call tools in the ``nctalk-calls`` toolset, gated on NEXTCLOUD_TALK_CALL_TOOLS."""
    for name, schema, handler, emoji in TOOLS:
        ctx.register_tool(name=name, toolset=TOOLSET, schema=schema, handler=handler,
                          description=schema["description"], emoji=emoji, check_fn=tools_enabled)
