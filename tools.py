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

Room map: ``NEXTCLOUD_TALK_CALL_ROOM_MAP=<chat room>:<estate>:<call room>,...``
sends a call request from one chat room to a call on another estate. A
federated room is a copy of a room hosted elsewhere, and the agent joins that
call natively on the host: ``m4jyfpxq:nam:og63pxfh`` makes "@lumina join call"
in nor's federated Operators AI (m4jyfpxq) start
``skcall-join-nam@og63pxfh.service`` (skcall's ``systemd/skcall-join-nam@.service``).

Thread tools (toolset ``nctalk-threads``, on by default in user mode, off with
``NEXTCLOUD_TALK_THREAD_TOOLS=off``): ``talk_thread_start`` opens a Talk
thread (Talk 22+, capability ``threads``) with a title and a first message and
returns its id; ``talk_thread_reply`` posts into an existing thread;
``talk_thread_list`` lists a room's open threads; ``talk_thread_close`` puts a
finished thread away (the agent's own follow level set to "never", a check mark
on the title). They work the same in group rooms, direct messages and federated
rooms; support is checked per room, on the server hosting it. They post as the
agent's own Nextcloud account (user mode credentials), never a bot.

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
ESTATE_RE = re.compile(r"[a-z0-9][a-z0-9-]{0,31}")
ROOM_MAP_ENV = "NEXTCLOUD_TALK_CALL_ROOM_MAP"
MAPPED_UNIT = "skcall-join-{estate}@{room}.service"
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
    # Hermes has no public plugin seam for the current turn's chat yet; this is
    # the accessor its own tools use. Guarded: without it the room is required.
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


OVERRIDE_ARG = "other_room"


def _resolve_room(args: dict) -> tuple[str, Optional[str], Optional[str]]:
    """(room, error, note) for a call tool.

    Invoked from a Talk room, the tool acts on THAT room. An explicit ``room``
    that names a different room is ignored (with a note saying so) unless the
    args also carry ``other_room: true``: models fill ``room`` from whatever
    token they can see (seen live: the home channel from the session context,
    so "@jarvis join call" in one room started the call in another). Outside
    Talk there is no current room, so ``room`` is required.
    """
    args = args or {}
    explicit = str(args.get("room") or "").strip()
    current = current_room()
    if current and explicit and explicit != current and not _truthy(args.get(OVERRIDE_ARG)):
        return current, None, (
            f"Used this chat's room {current}, not {explicit!r}: the request came from Talk room "
            f"{current}. To act on a different room, pass room plus {OVERRIDE_ARG}=true, and only "
            "when the user explicitly asked for that other room.")
    room = explicit or current
    if not room:
        return "", ("room is required: give the Nextcloud Talk room token "
                    "(the current chat is not a Talk room)"), None
    if not ROOM_RE.fullmatch(room):
        return "", f"invalid room token {room!r}: expected 4-32 lowercase letters/digits", None
    if current and room != current:
        return room, None, f"Acting on room {room}, not this chat ({current}), as requested."
    return room, None, None


def _with_note(data: dict, note: Optional[str]) -> dict:
    if note:
        data["room_note"] = note
    return data


def unit_for(room: str) -> str:
    return UNIT_TEMPLATE.format(room=room)


def room_map() -> dict:
    """{chat room: (estate, call room)} from NEXTCLOUD_TALK_CALL_ROOM_MAP; bad entries are skipped."""
    out = {}
    for entry in _env(ROOM_MAP_ENV, "").split(","):
        parts = [x.strip() for x in entry.split(":")]
        if (len(parts) == 3 and ROOM_RE.fullmatch(parts[0]) and ESTATE_RE.fullmatch(parts[1])
                and ROOM_RE.fullmatch(parts[2])):
            out[parts[0]] = (parts[1], parts[2])
    return out


def call_target(room: str) -> tuple[str, str, str]:
    """(call room, unit, estate) for a chat room; estate is "" when the room is not mapped."""
    mapped = room_map().get(room)
    if mapped:
        estate, call_room = mapped
        return call_room, MAPPED_UNIT.format(estate=estate, room=call_room), estate
    return room, unit_for(room), ""


def _target_info(room: str, call_room: str, unit: str, estate: str) -> dict:
    out = {"room": room, "unit": unit}
    if estate:
        out.update(call_room=call_room, estate=estate)
    return out


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


def unit_state(unit: str) -> dict:
    p = _systemctl("show", unit, "--property=ActiveState,SubState,Result,ExecMainStatus,"
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
    room, err, note = _resolve_room(args)
    if err:
        return _result(error=err)
    call_room, unit, estate = call_target(room)
    info = _target_info(room, call_room, unit, estate)
    state = unit_state(unit)
    if state.get("LoadState") == "not-found":
        return _result(error=f"{unit} is not installed on this host (skcall systemd template missing)")
    if _in_call(state):
        return _result(**_with_note({"status": "already in call", **info,
                                     "note": "You are already in this call. Do not start another one."},
                                    note))
    try:
        write_env_file(call_room, _truthy((args or {}).get("ring")), clean_greeting((args or {}).get("greeting")))
    except OSError as e:
        return _result(error=f"could not write call options: {e}")
    p = _systemctl("start", "--no-block", unit)
    if p.returncode != 0:
        return _result(error=f"systemctl start {unit} failed: {(p.stderr or p.stdout or '').strip()[:300]}")
    return _result(**_with_note({"status": "joining", **info,
                                 "note": ("Joining now in the background; it takes a few seconds. Do not "
                                          "run it again, do not poll, and do not start skcall any other "
                                          "way. A summary is posted to the room's chat when the call ends.")},
                                note))


def call_leave(args: dict, **_kw: Any) -> str:
    room, err, note = _resolve_room(args)
    if err:
        return _result(error=err)
    call_room, unit, estate = call_target(room)
    info = _target_info(room, call_room, unit, estate)
    state = unit_state(unit)
    try:
        env_file(call_room).unlink()
    except (FileNotFoundError, OSError):
        pass
    if not _in_call(state):
        return _result(**_with_note({"status": "not in call", **info}, note))
    p = _systemctl("stop", "--no-block", unit)
    if p.returncode != 0:
        return _result(error=f"systemctl stop {unit} failed: {(p.stderr or p.stdout or '').strip()[:300]}")
    return _result(**_with_note({"status": "leaving", **info}, note))


def _describe(room: str, state: dict, unit: str = "", call_room: str = "", estate: str = "") -> dict:
    unit = unit or unit_for(room)
    out = {**_target_info(room, call_room or room, unit, estate),
           "state": state.get("ActiveState", "unknown"), "sub_state": state.get("SubState", "")}
    if _in_call(state):
        out["status"] = "in call"
        if state.get("ActiveEnterTimestamp"):
            out["since"] = state["ActiveEnterTimestamp"]
    elif state.get("ActiveState") == "failed":
        out["status"] = "last join failed"
        out["result"] = state.get("Result", "")
        out["exit_status"] = state.get("ExecMainStatus", "")
        j = _run(["journalctl", "--user", "-u", unit, "-n", "6", "-o", "cat", "--no-pager"])
        tail = [ln for ln in (j.stdout or "").splitlines() if ln.strip()][-6:]
        if tail:
            out["last_log"] = tail
    else:
        out["status"] = "not in call"
        if state.get("ExecMainStatus") == EXIT_ALREADY_IN_CALL:
            out["last_exit"] = "already in call (another session held it)"
    return out


def call_status(args: dict, **_kw: Any) -> str:
    if str((args or {}).get("room") or "").strip() or current_room():
        room, err, note = _resolve_room(args)
        if err:
            return _result(error=err)
        call_room, unit, estate = call_target(room)
        return _result(**_with_note(_describe(room, unit_state(unit), unit, call_room, estate), note))
    p = _systemctl("list-units", "--all", "--no-legend", "--plain", "skcall-join@*.service",
                   "skcall-join-*@*.service")
    calls = []
    for line in (p.stdout or "").splitlines():
        parts = line.split()
        if not parts:
            continue
        m = re.fullmatch(r"skcall-join(?:-([a-z0-9][a-z0-9-]{0,31}))?@([a-z0-9]{4,32})\.service", parts[0])
        if m:
            estate, call_room = m.group(1) or "", m.group(2)
            calls.append(_describe(call_room, unit_state(parts[0]), parts[0], call_room, estate))
    return _result(calls=calls, in_call=[c["room"] for c in calls if c["status"] == "in call"])


# ----------------------------------------------------------------- schemas
_COMMON = (" Use these call tools for ANY request to join, leave or check a call "
           "(e.g. '@agent join call'). Never launch skcall, python or systemd-run "
           "through the terminal tool, and never run a call as a background process.")

_ROOM_HINT = (" From a Talk chat, leave room EMPTY: it means this chat, which is what "
              "'join the call' asks for. Never fill room from a home channel, a channel "
              "list or memory.")

_ROOM_PROP = {"type": "string",
              "description": ("Talk room token (4-32 lowercase letters/digits). Leave empty to mean "
                              "the current Talk chat (the normal case). Only set it for a different "
                              "room the user explicitly named, and then also set other_room=true; "
                              "without it, a different room is ignored and this chat is used.")}

_OTHER_ROOM_PROP = {"type": "boolean", "default": False,
                    "description": ("true only when the user explicitly asked to act on a "
                                    "different Talk room than this chat. Default false.")}

CALL_JOIN_SCHEMA = {
    "name": "call_join",
    "description": (
        "Join the Nextcloud Talk call of a room as yourself (voice, with your avatar). "
        "Returns immediately with 'joining' or 'already in call'; the call then runs on "
        "its own in the background, exactly one instance per room. Call it ONCE per "
        "request: do not retry, poll or relaunch it, and never start a second join."
        + _ROOM_HINT + _COMMON),
    "parameters": {
        "type": "object",
        "properties": {
            "room": _ROOM_PROP,
            OVERRIDE_ARG: _OTHER_ROOM_PROP,
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
                    "posted to the room's chat)." + _ROOM_HINT + _COMMON),
    "parameters": {
        "type": "object",
        "properties": {"room": _ROOM_PROP, OVERRIDE_ARG: _OTHER_ROOM_PROP},
    },
}

CALL_STATUS_SCHEMA = {
    "name": "call_status",
    "description": ("Check whether you are in a Nextcloud Talk call: for one room (default the "
                    "current Talk chat) or, with no room outside Talk, every call unit on this "
                    "host. Shows the last failure reason if a join failed." + _ROOM_HINT + _COMMON),
    "parameters": {
        "type": "object",
        "properties": {"room": _ROOM_PROP, OVERRIDE_ARG: _OTHER_ROOM_PROP},
    },
}

TOOLS = (
    ("call_join", CALL_JOIN_SCHEMA, call_join, "\U0001f4de"),     # telephone receiver
    ("call_leave", CALL_LEAVE_SCHEMA, call_leave, "\U0001f4f4"),  # mobile phone off
    ("call_status", CALL_STATUS_SCHEMA, call_status, "\U0001f4f6"),  # antenna bars
)


_TOOL_SEARCH_MODULE = "tools.tool_search"
_DIRECT_ATTR = "_DIRECT_SURFACE_TOOLSETS"


def pin_visible() -> bool:
    """Keep the ``nctalk-calls`` toolset out of Hermes tool_search deferral.

    Hermes progressive disclosure (``tools/tool_search.py``) defers EVERY plugin
    tool behind ``tool_search`` / ``tool_describe`` / ``tool_call`` unless its
    toolset is in ``_DIRECT_SURFACE_TOOLSETS``; ``register_tool`` has no
    per-tool flag and ``tools.tool_search.defer`` in config.yaml can only ADD
    tools to the deferred set, never pin one eager. Deferred, the call tools
    only appear as bare names in a 240-tool listing, and the model reaches for
    ``terminal`` instead (seen live: "@lumina join call" -> a hand-built venv
    and ``systemd-run ... skcall join``). Three schemas cost ~560 tokens.

    ``is_deferrable_tool_name`` reads the module global on every call, so
    rebinding it (frozenset, idempotent) is enough. Best effort: if the Hermes
    internals ever move, the tools still work, just deferred again.
    """
    try:
        import importlib
        ts = importlib.import_module(_TOOL_SEARCH_MODULE)
        current = getattr(ts, _DIRECT_ATTR)
        wanted = {TOOLSET, THREAD_TOOLSET}
        if not wanted <= set(current):
            setattr(ts, _DIRECT_ATTR, frozenset(current) | wanted)
        return True
    except Exception:
        import logging
        logging.getLogger(__name__).warning(
            "nctalk: could not pin %s visible; call tools stay behind tool_search", TOOLSET,
            exc_info=True)
        return False


# ----------------------------------------------------------------- threads
_THREAD_ROOM_HINT = (" From a Talk chat, leave room EMPTY: it means this chat. Never fill room "
                     "from a home channel, a channel list or memory.")
THREAD_TOOLSET = "nctalk-threads"
THREAD_ENABLE_ENV = "NEXTCLOUD_TALK_THREAD_TOOLS"
THREAD_TITLE_MAX = 200
THREAD_MESSAGE_MAX = 32000
CAPS_TTL_SECONDS = 600.0
_CHAT_PATH = "/ocs/v2.php/apps/spreed/api/v1/chat/{room}"
_THREAD_PATH = "/ocs/v2.php/apps/spreed/api/v1/chat/{room}/threads/{thread}"
_RECENT_PATH = "/ocs/v2.php/apps/spreed/api/v1/chat/{room}/threads/recent"
_ROOM_CAPS_PATH = "/ocs/v2.php/apps/spreed/api/v4/room/{room}/capabilities"
NOTIFY_DEFAULT = 0   # Talk Participant::NOTIFY_* thread follow levels
NOTIFY_NEVER = 3     # "unfollow"; on the agent's own side it marks a thread closed
CLOSED_MARK = "\u2705 "  # check mark prefix on a closed thread's title
THREAD_LIST_MAX = 50

# (server, checked at, has threads); swappable in tests together with _http_post/_http_get.
_caps_cache: dict = {}
_room_caps_cache: dict = {}


def _talk_credentials() -> tuple[str, str, str]:
    server = _env("NEXTCLOUD_TALK_SERVER_URL", "").strip().rstrip("/")
    user = _env("NEXTCLOUD_TALK_USERNAME", "hermes-bot").strip()
    password = _env("NEXTCLOUD_TALK_APP_PASSWORD", "").strip()
    return server, user, password


def thread_tools_enabled() -> bool:
    """On when user-mode credentials exist, unless NEXTCLOUD_TALK_THREAD_TOOLS=off."""
    if _env(THREAD_ENABLE_ENV, "on").strip().lower() in ("0", "off", "false", "no"):
        return False
    server, _user, password = _talk_credentials()
    return bool(server and password)


def _ocs(method: str, path: str, *, json_body: Optional[dict] = None,
         params: Optional[dict] = None) -> tuple[int, Any]:
    """One OCS request as the agent's account: (status, parsed ocs.data or text)."""
    import httpx  # Hermes dependency; imported lazily to keep this module stdlib-only at import

    server, user, password = _talk_credentials()
    headers = {"OCS-APIRequest": "true", "Accept": "application/json"}
    with httpx.Client(base_url=server, auth=(user, password), headers=headers, timeout=20.0) as client:
        resp = client.request(method, path, params={**(params or {}), "format": "json"}, json=json_body)
    try:
        data = resp.json().get("ocs", {}).get("data")
    except Exception:
        data = resp.text[:300]
    return resp.status_code, data


_request = _ocs  # swappable in tests


def server_has_threads() -> bool:
    import time

    server = _talk_credentials()[0]
    cached = _caps_cache.get(server)
    if cached and time.time() - cached[0] < CAPS_TTL_SECONDS:
        return cached[1]
    status, data = _request("GET", "/ocs/v2.php/cloud/capabilities")
    if status >= 300 or not isinstance(data, dict):
        return False  # not cached: retried on the next call
    features = ((data.get("capabilities") or {}).get("spreed") or {}).get("features") or []
    has = "threads" in features
    _caps_cache[server] = (time.time(), has)
    return has


def room_has_threads(room: str) -> bool:
    """Threads in this room: our server must have them, and for a federated room the
    HOST server too. ``/room/{token}/capabilities`` answers for the host (proxied) and
    for a local room it is our own list. Cached per room; a failed read is not cached."""
    import time

    if not server_has_threads():
        return False
    cached = _room_caps_cache.get(room)
    if cached and time.time() - cached[0] < CAPS_TTL_SECONDS:
        return cached[1]
    status, data = _request("GET", _ROOM_CAPS_PATH.format(room=room))
    if status >= 300 or not isinstance(data, dict):
        return False
    has = "threads" in (data.get("features") or [])
    _room_caps_cache[room] = (time.time(), has)
    return has


_NO_THREADS = "threads are not available in this room (needs Talk 22+ on the server hosting it)"


def _thread_state(room: str, thread: str) -> tuple[Optional[dict], Optional[str]]:
    """(thread info, error) from GET threads/{id}; info carries title and our follow level."""
    try:
        status, data = _request("GET", _THREAD_PATH.format(room=room, thread=thread))
    except Exception as e:
        return None, f"thread read failed: {e}"
    if status >= 300 or not isinstance(data, dict):
        return None, f"thread {thread} not found in this room (HTTP {status})"
    return data, None


def _is_closed(info: Optional[dict]) -> bool:
    return ((info or {}).get("attendee") or {}).get("notificationLevel") == NOTIFY_NEVER


def _set_level(room: str, thread: str, level: int) -> Optional[str]:
    try:
        status, data = _request("POST", _THREAD_PATH.format(room=room, thread=thread) + "/notify",
                                json_body={"level": level})
    except Exception as e:
        return f"follow level change failed: {e}"
    return None if status < 300 else f"HTTP {status}: {str(data)[:200]}"


def _rename(room: str, thread: str, title: str) -> Optional[str]:
    try:
        status, data = _request("PUT", _THREAD_PATH.format(room=room, thread=thread),
                                json_body={"threadTitle": title[:THREAD_TITLE_MAX]})
    except Exception as e:
        return f"rename failed: {e}"
    return None if status < 300 else f"HTTP {status}"


def current_thread() -> str:
    """The Talk thread this turn came from, or "" (main timeline / not Talk)."""
    if not current_room():
        return ""
    tid = _session("HERMES_SESSION_THREAD_ID").strip()
    return tid if tid.isdigit() and int(tid) > 0 else ""


def _one_line(text: Any, limit: int) -> str:
    return " ".join(str(text or "").split())[:limit]


def _post_chat(room: str, body: dict) -> tuple[Optional[str], Optional[str]]:
    """(message id, error) for a chat POST."""
    try:
        status, data = _request("POST", _CHAT_PATH.format(room=room), json_body=body)
    except Exception as e:
        return None, f"post failed: {e}"
    if status >= 300:
        return None, f"HTTP {status}: {str(data)[:300]}"
    mid = data.get("id") if isinstance(data, dict) else None
    return (str(mid) if mid else None), None


def talk_thread_start(args: dict, **_kw: Any) -> str:
    args = args or {}
    title = _one_line(args.get("title"), THREAD_TITLE_MAX)
    message = str(args.get("message") or "").strip()[:THREAD_MESSAGE_MAX]
    if not title:
        return _result(error="title is required: Talk refuses a thread without a title")
    if not message:
        return _result(error="message is required: it becomes the thread's first message")
    room, err, note = _resolve_room(args)
    if err:
        return _result(error=err)
    if not room_has_threads(room):
        return _result(error=_NO_THREADS)
    mid, err = _post_chat(room, {"message": message, "threadTitle": title})
    if err:
        return _result(error=f"could not start the thread: {err}")
    out = {"status": "created", "room": room, "title": title, "thread_id": mid,
           "note": ("Thread started. Post follow-ups into it with talk_thread_reply(thread_id). "
                    "Replies people make in it reach you as that thread's own conversation.")}
    if not mid:
        out["note"] = "Posted, but the server did not return the new message id, so the thread id is unknown."
    return _result(**_with_note(out, note))


def talk_thread_reply(args: dict, **_kw: Any) -> str:
    args = args or {}
    message = str(args.get("message") or "").strip()[:THREAD_MESSAGE_MAX]
    if not message:
        return _result(error="message is required")
    room, err, note = _resolve_room(args)
    if err:
        return _result(error=err)
    explicit = str(args.get("thread_id") or "").strip()
    thread = explicit or (current_thread() if room == current_room() else "")
    if not thread.isdigit() or int(thread) <= 0:
        return _result(error="thread_id is required: the id talk_thread_start returned (a message id)")
    if not room_has_threads(room):
        return _result(error=_NO_THREADS)
    reopened = False
    if room != current_room() or thread != current_thread():
        # Posting unprompted (not answering inside this thread): a closed thread stays
        # quiet unless reopen is asked for. A person writing in it reopens it on its own.
        info, _err = _thread_state(room, thread)
        if _is_closed(info):
            if not _truthy(args.get("reopen")):
                return _result(error=(f"thread {thread} is closed; pass reopen=true only if the person "
                                      "asked to pick it back up"))
            _set_level(room, thread, NOTIFY_DEFAULT)
            title = ((info or {}).get("thread") or {}).get("title") or ""
            if title.startswith(CLOSED_MARK):
                _rename(room, thread, title[len(CLOSED_MARK):])
            reopened = True
    mid, err = _post_chat(room, {"message": message, "threadId": int(thread)})
    if err:
        return _result(error=f"could not post into thread {thread}: {err}")
    out = {"status": "posted", "room": room, "thread_id": thread, "message_id": mid}
    if reopened:
        out["reopened"] = True
    return _result(**_with_note(out, note))


def talk_thread_list(args: dict, **_kw: Any) -> str:
    args = args or {}
    room, err, note = _resolve_room(args)
    if err:
        return _result(error=err)
    if not room_has_threads(room):
        return _result(error=_NO_THREADS)
    try:
        limit = max(1, min(THREAD_LIST_MAX, int(args.get("limit") or 20)))
    except (TypeError, ValueError):
        limit = 20
    try:
        status, data = _request("GET", _RECENT_PATH.format(room=room), params={"limit": limit})
    except Exception as e:
        return _result(error=f"could not list threads: {e}")
    if status >= 300 or not isinstance(data, list):
        return _result(error=f"could not list threads: HTTP {status}")
    include_closed = _truthy(args.get("include_closed"))
    threads = []
    for item in data:
        t = (item or {}).get("thread") or {}
        closed = _is_closed(item)
        if closed and not include_closed:
            continue
        threads.append({
            "thread_id": str(t.get("id")),
            "title": t.get("title") or "",
            "replies": t.get("numReplies") or 0,
            "last_activity": t.get("lastActivity"),
            "closed": closed,
        })
    return _result(**_with_note({"room": room, "threads": threads}, note))


def talk_thread_close(args: dict, **_kw: Any) -> str:
    args = args or {}
    room, err, note = _resolve_room(args)
    if err:
        return _result(error=err)
    explicit = str(args.get("thread_id") or "").strip()
    thread = explicit or (current_thread() if room == current_room() else "")
    if not thread.isdigit() or int(thread) <= 0:
        return _result(error="thread_id is required (empty means the thread this chat turn came from)")
    if not room_has_threads(room):
        return _result(error=_NO_THREADS)
    info, err = _thread_state(room, thread)
    if err:
        return _result(error=err)
    summary = str(args.get("summary") or "").strip()[:THREAD_MESSAGE_MAX]
    if summary:
        _mid, perr = _post_chat(room, {"message": summary, "threadId": int(thread)})
        if perr:
            return _result(error=f"could not post the closing line: {perr}")
    lerr = _set_level(room, thread, NOTIFY_NEVER)
    if lerr:
        return _result(error=f"could not close thread {thread}: {lerr}")
    title = ((info or {}).get("thread") or {}).get("title") or ""
    renamed = False
    if title and not title.startswith(CLOSED_MARK):
        # Only the thread's starter or a moderator may rename: in someone else's
        # thread in a group room this fails and the close still stands.
        renamed = _rename(room, thread, CLOSED_MARK + title) is None
    return _result(**_with_note({"status": "closed", "room": room, "thread_id": thread,
                                 "title_marked": renamed}, note))


TALK_THREAD_START_SCHEMA = {
    "name": "talk_thread_start",
    "description": (
        "Open a new Nextcloud Talk thread in this room or direct message: a titled side "
        "conversation for one task or matter, so the work does not fill the main timeline. "
        "Posts the first message as you and returns thread_id. Use it when you take on a "
        "task, or a new separate matter comes up in a direct message; inside an existing "
        "thread just reply normally." + _THREAD_ROOM_HINT),
    "parameters": {
        "type": "object",
        "properties": {
            "title": {"type": "string",
                      "description": "Short thread title naming the task (required, one line)."},
            "message": {"type": "string", "description": "The thread's first message (required)."},
            "room": _ROOM_PROP,
            OVERRIDE_ARG: _OTHER_ROOM_PROP,
        },
        "required": ["title", "message"],
    },
}

TALK_THREAD_REPLY_SCHEMA = {
    "name": "talk_thread_reply",
    "description": (
        "Post a message into an existing Nextcloud Talk thread, e.g. one you opened with "
        "talk_thread_start. Not needed to answer the message you are replying to: that "
        "answer already goes to the right place." + _THREAD_ROOM_HINT),
    "parameters": {
        "type": "object",
        "properties": {
            "thread_id": {"type": "string",
                          "description": ("Thread id (the thread's first message id, as returned by "
                                          "talk_thread_start). Empty means the thread this chat "
                                          "turn came from.")},
            "message": {"type": "string", "description": "Message text (required)."},
            "reopen": {"type": "boolean",
                       "description": ("Post into a thread you closed, reopening it. Only when the "
                                       "person asked to pick that matter back up.")},
            "room": _ROOM_PROP,
            OVERRIDE_ARG: _OTHER_ROOM_PROP,
        },
        "required": ["message"],
    },
}

TALK_THREAD_LIST_SCHEMA = {
    "name": "talk_thread_list",
    "description": (
        "List the open Nextcloud Talk threads in this room or direct message, most recently "
        "active first: thread_id, title, reply count, last activity (unix time). Use it when "
        "asked what is open or in progress here, or to find a matter's thread before "
        "posting into it." + _THREAD_ROOM_HINT),
    "parameters": {
        "type": "object",
        "properties": {
            "limit": {"type": "integer", "description": "How many threads (default 20, max 50)."},
            "include_closed": {"type": "boolean", "description": "Also list threads you closed."},
            "room": _ROOM_PROP,
            OVERRIDE_ARG: _OTHER_ROOM_PROP,
        },
    },
}

TALK_THREAD_CLOSE_SCHEMA = {
    "name": "talk_thread_close",
    "description": (
        "Close a Nextcloud Talk thread when its matter is finished or the person says it is "
        "done: optionally posts a one-line summary into it, marks its title with a check mark "
        "and stops you posting into it unprompted. Talk has no real close, and you cannot see "
        "whether the person unfollowed it, so this is how a thread is put away. If they write "
        "in it again it reopens by itself." + _THREAD_ROOM_HINT),
    "parameters": {
        "type": "object",
        "properties": {
            "thread_id": {"type": "string",
                          "description": "Thread id; empty means the thread this chat turn came from."},
            "summary": {"type": "string", "description": "Optional one-line result to post first."},
            "room": _ROOM_PROP,
            OVERRIDE_ARG: _OTHER_ROOM_PROP,
        },
    },
}

THREAD_TOOLS = (
    ("talk_thread_start", TALK_THREAD_START_SCHEMA, talk_thread_start, "🧵"),  # thread
    ("talk_thread_reply", TALK_THREAD_REPLY_SCHEMA, talk_thread_reply, "💬"),  # speech balloon
    ("talk_thread_list", TALK_THREAD_LIST_SCHEMA, talk_thread_list, "📋"),  # clipboard
    ("talk_thread_close", TALK_THREAD_CLOSE_SCHEMA, talk_thread_close, "✅"),  # check mark
)


def register_tools(ctx) -> None:
    """Register the call tools (``nctalk-calls``, gated on NEXTCLOUD_TALK_CALL_TOOLS) and the
    thread tools (``nctalk-threads``, on in user mode unless NEXTCLOUD_TALK_THREAD_TOOLS=off)."""
    for name, schema, handler, emoji in TOOLS:
        ctx.register_tool(name=name, toolset=TOOLSET, schema=schema, handler=handler,
                          description=schema["description"], emoji=emoji, check_fn=tools_enabled)
    for name, schema, handler, emoji in THREAD_TOOLS:
        ctx.register_tool(name=name, toolset=THREAD_TOOLSET, schema=schema, handler=handler,
                          description=schema["description"], emoji=emoji, check_fn=thread_tools_enabled)
    # Harmless when the tools are off: check_fn drops them before tool_search sees them.
    pin_visible()
