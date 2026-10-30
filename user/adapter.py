"""Nextcloud Talk real-user adapter (Hermes plugin, user mode).

The agent is a genuine Nextcloud account (an "app password" login), not a
webhook bot. It discovers every room the account is a member of, runs one
long-poll task per room against the Chat REST API, detects @mentions,
applies a per-room mode (mention / watch / free), and auto-accepts
federation invitations only from an allowlisted inviter.

This is the sibling of ``bot/adapter.py`` (the webhook-bot connector) in
this repo. The two do not share a transport -- a webhook receiver and a
long-polling REST client are different I/O models -- so they stay separate
adapter classes; see the repo README for how one plugin picks between them
at startup.

Configuration in config.yaml::

    platforms:
      nctalk:
        enabled: true
        extra:
          server_url: "https://cloud.example.com"
          username: "hermes-bot"
          app_password: "..."            # or NEXTCLOUD_TALK_APP_PASSWORD env var

Environment variables (env wins over config.yaml ``extra``):

    NEXTCLOUD_TALK_SERVER_URL              Base URL (required)
    NEXTCLOUD_TALK_APP_PASSWORD            App password, NOT the login password (required)
    NEXTCLOUD_TALK_USERNAME                Nextcloud user id (default: hermes-bot)
    NEXTCLOUD_TALK_DISPLAY_NAME             Display name for plain-text mentions (default: Hermes)
    NEXTCLOUD_TALK_ALLOWED_INVITERS        Federation inviters to auto-accept from
    NEXTCLOUD_TALK_ROOM_MODES              "token1=mention,token2=watch,token3=free"
    NEXTCLOUD_TALK_DM_MODE                 Mode for unlisted one-to-one rooms and for group rooms
                                             whose only participants are this account and one
                                             allowed user (default "free"; "mention" restores the
                                             old @mention requirement there)
    NEXTCLOUD_TALK_FEDERATED_MEDIA         "link" posts an expiring public link for files in federated
                                             rooms (Talk cannot share into them); default "off"
    NEXTCLOUD_TALK_PROFILE_ROLE            "off" stops reading the agent's role from its Nextcloud
                                             profile (role, headline, organisation, about); default on
    NEXTCLOUD_TALK_ALLOWED_USERS           Allowlist (gateway auth integration)
    NEXTCLOUD_TALK_ALLOW_ALL_USERS         Allow any user -- dev only
    NEXTCLOUD_TALK_HOME_CHANNEL            Default room token for cron delivery
    NEXTCLOUD_TALK_HOME_CHANNEL_NAME       Human label for the home channel
    NEXTCLOUD_TALK_HOME_CHANNEL_THREAD_ID  Thread id (root message id) inside the home channel for
                                             cron / notification delivery (Talk 22+ threads)
    NEXTCLOUD_TALK_THREAD_OWNERSHIP        "off" stops treating a thread started by another agent
                                             (NEXTCLOUD_TALK_AGENT_USERS) as that agent's, where
                                             this account then only answers when @mentioned
                                             (default on)
    NEXTCLOUD_TALK_DISCOVERY_INTERVAL_SECONDS  Re-discovery interval (default 30)
    NEXTCLOUD_TALK_MAX_ROOMS                Max concurrent per-room poll tasks (default 20)
    NEXTCLOUD_TALK_WORKING_REACTION        Reaction added while a turn is being processed and
                                             removed once the reply is sent (default: the eyes
                                             emoji; empty string disables the feature)
    NEXTCLOUD_TALK_AGENT_USERS             Other agents' Nextcloud user ids sharing this server
                                             ("ops-bot,research-bot"); in a "free" room they still need an
                                             explicit @mention of this account (loop guard)
    NEXTCLOUD_TALK_AGENT_CHAIN_LIMIT       Agent messages answered in a row, with no human
                                             message in between, before this account stops
                                             answering that agent until a human speaks (default 6)
    NEXTCLOUD_TALK_CONTEXT_MESSAGES        Unanswered room messages kept per room and handed
                                             over with the next message this account does
                                             answer, so "mention" mode still has context from
                                             what it skipped (default 40; 0 disables)
    NEXTCLOUD_TALK_LONG_POLL_TIMEOUT       Server-side long-poll hold time in seconds (default
                                             25; Talk's own ceiling is 30 -- lower this if
                                             something in front of Hermes has its own shorter
                                             read/proxy timeout, since a poll that doesn't return
                                             before that boundary is an error rather than a plain
                                             "nothing new")

Threads (Talk 22+, capability ``threads``): each thread gets its own Hermes
session, its own context backlog and its own agent-chain counter, and replies
go back into the thread they came from. Without the capability the adapter
behaves exactly as before threads existed.

Active listening and the agent-to-agent loop guard only matter in a room
with more than one participant (DMs are always addressed to this account,
so neither applies there). Unlisted DMs, and group rooms holding only this
account and one allowed user, run in NEXTCLOUD_TALK_DM_MODE (default free).
"""

import asyncio
import base64
import logging
import os
import re
import time
from datetime import datetime, timezone
import json
import mimetypes
import uuid
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import quote

try:
    import httpx
    HTTPX_AVAILABLE = True
except ImportError:
    HTTPX_AVAILABLE = False
    httpx = None  # type: ignore[assignment]

from gateway.config import Platform, PlatformConfig

try:  # package import (plugin and tests both load ``user`` as a package)
    from . import access, pollstate, textstyle
except ImportError:  # pragma: no cover - loaded as a loose module
    import access  # type: ignore
    import pollstate  # type: ignore
    import textstyle  # type: ignore
# Platform id. Not ``nextcloud_talk``: another author's Talk plugin in the
# Hermes catalog registers that id, and two plugins claiming one id silently
# replace each other. Env var names keep the NEXTCLOUD_TALK_ prefix.
PLATFORM_NAME = "nctalk"

try:  # Hermes resolves credentials per profile scope (multiplexed gateways)
    from agent.secret_scope import get_secret as _hermes_get_secret
except ImportError:  # pragma: no cover - older Hermes / test stubs
    _hermes_get_secret = None


def _secret(name: str, default: str = "") -> str:
    """Read a credential through Hermes' secret scope, falling back to the env."""
    if _hermes_get_secret is not None:
        val = _hermes_get_secret(name, default)
        return default if val is None else val
    return os.getenv(name, default)


# Matches tools.py (repeated here so each connector still deploys standalone).
CALL_TOOLS_PROMPT = (
    "For joining, leaving or checking voice calls, always use call_join / "
    "call_leave / call_status; never start skcall from the terminal. Leave their "
    "room argument empty to mean this chat."
)


def _call_tools_prompt() -> str:
    """The call-tools line when NEXTCLOUD_TALK_CALL_TOOLS is on, else ""."""
    on = _secret("NEXTCLOUD_TALK_CALL_TOOLS", "").strip().lower() in ("1", "on", "true", "yes")
    return CALL_TOOLS_PROMPT if on else ""


def _with_call_tools_prompt(prompt: Optional[str]) -> Optional[str]:
    """Append the call-tools line (when on) to a channel prompt."""
    line = _call_tools_prompt()
    if not line:
        return prompt
    return f"{prompt}\n\n{line}" if prompt else line

from gateway.platforms.base import (
    BasePlatformAdapter,
    MessageEvent,
    MessageType,
    ProcessingOutcome,
    SendResult,
    cache_audio_from_bytes,
    cache_document_from_bytes,
    cache_image_from_bytes,
    cache_video_from_bytes,
)

logger = logging.getLogger(__name__)

DEFAULT_USERNAME = "hermes-bot"
DEFAULT_DISPLAY_NAME = "Hermes"
MAX_MESSAGE_LENGTH = 32000  # Nextcloud Talk chat message cap
DEFAULT_DISCOVERY_INTERVAL = 30.0
DEFAULT_MAX_ROOMS = 20
LONG_POLL_TIMEOUT_ENV = "NEXTCLOUD_TALK_LONG_POLL_TIMEOUT"
# Server-side seconds Talk holds a long-poll request open before returning 304.
# Talk itself allows up to 30. Anything sitting in front of Hermes with its own
# proxy/gateway read timeout under ~30s (a reverse proxy, a load balancer, some
# PaaS platforms) turns every idle poll into an error at that boundary, so the
# default here is a few seconds under Talk's own ceiling; override with
# NEXTCLOUD_TALK_LONG_POLL_TIMEOUT if your front door's timeout is different
# (or set it back to 30 if nothing sits in front of Hermes).
DEFAULT_LONG_POLL_TIMEOUT = 25
ROOM_RULES_ALL_ENV = "NEXTCLOUD_TALK_ROOM_RULES_ALL_ROOMS"
BASELINE_LOOKUP_LIMIT = 1
DEDUP_MAX_SIZE = 2000
DEDUP_WINDOW_SECONDS = 600
RECONNECT_BACKOFF = [2, 5, 10, 30, 60]
WATCH_FLUSH_INTERVAL_SECONDS = 900  # review buffered "watch" rooms every 15 min

# Federation invitation states (Talk's federation API). Only ever act on a
# pending one -- an invite already accepted/rejected elsewhere can still
# appear in the list on some Talk versions, and re-POSTing accept on it
# always 400s.
INVITE_STATE_PENDING = 0

WORKING_REACTION_ENV = "NEXTCLOUD_TALK_WORKING_REACTION"
DEFAULT_WORKING_REACTION_EMOJI = "\U0001F440"  # eyes

# Nextcloud Talk room type constants
# https://nextcloud-talk.readthedocs.io/en/latest/constants/#conversation-types
ROOM_TYPE_ONE_TO_ONE = 1
ROOM_TYPE_GROUP = 2
ROOM_TYPE_PUBLIC = 3
ROOM_TYPE_CHANGELOG = 4
ROOM_TYPE_FORMER_ONE_TO_ONE = 5
ROOM_TYPE_NOTE_TO_SELF = 6

ROOM_MODE_MENTION = "mention"
ROOM_MODE_WATCH = "watch"
ROOM_MODE_FREE = "free"
VALID_ROOM_MODES = frozenset({ROOM_MODE_MENTION, ROOM_MODE_WATCH, ROOM_MODE_FREE})

# One-to-one rooms, and group rooms that are a private pair in practice
# (Talk types a conversation Chef made with just himself and Jarvis as a
# group, type 2), run in this mode unless NEXTCLOUD_TALK_ROOM_MODES lists them.
DM_MODE_ENV = "NEXTCLOUD_TALK_DM_MODE"
DEFAULT_DM_MODE = ROOM_MODE_FREE
# A group room's participant list is re-read this often, and at once when the
# room posts a participant-change system message.
PAIR_REFRESH_SECONDS = 300.0
PARTICIPANT_CHANGE_MESSAGES = frozenset({
    "user_added", "user_removed",
    "federated_user_added", "federated_user_removed",
    "group_added", "group_removed",
    "circle_added", "circle_removed",
    "guest_joined", "guest_left",
})

# Agent-to-agent loop guard. A "free" room replies to every human message,
# but two free agents replying to each other would never stop. So a message
# from another agent (NEXTCLOUD_TALK_AGENT_USERS, or any Talk webhook bot)
# still needs an explicit @mention of this account even in a free room, and
# after AGENT_CHAIN_LIMIT agent messages in a row with no human message in
# between, agent messages in that room are ignored until a human speaks again.
AGENT_USERS_ENV = "NEXTCLOUD_TALK_AGENT_USERS"
AGENT_CHAIN_LIMIT_ENV = "NEXTCLOUD_TALK_AGENT_CHAIN_LIMIT"
DEFAULT_AGENT_CHAIN_LIMIT = 6
ACTOR_TYPE_BOTS = "bots"  # Talk's actorType for a webhook-bot sender

# Active listening. A message this account does not answer (mention mode
# without a mention, or another agent not addressing it) is kept rather than
# dropped, and the backlog is handed over as context with the next message it
# does answer -- so a reply in mention mode is informed by what the room has
# been saying, not just by the one line that named it.
CONTEXT_MESSAGES_ENV = "NEXTCLOUD_TALK_CONTEXT_MESSAGES"
DEFAULT_CONTEXT_MESSAGES = 40  # per room; 0 turns active listening off
CONTEXT_MAX_CHARS = 8000       # oldest backlog lines are dropped past this

# Threads (Talk 22+, capability "threads"). The thread id IS the root
# message's id. One long-poll per room returns every message, thread replies
# included, each carrying ``threadId``; routing is client-side because Talk
# does not proxy a thread-filtered chat read for federated rooms.
THREADS_CAPABILITY = "threads"
THREADS_HINT = (
    "Rooms and direct messages can have threads: a titled side conversation under one "
    "message, with its own conversation for you. People post requests in the main "
    "timeline. When you take on a task, do the work for it inside a thread "
    "(talk_thread_start, when you have it, opens one titled after the task) instead of "
    "filling the main timeline. In a direct message, give each separate matter its own "
    "thread the same way; small talk and quick questions stay in the main timeline. "
    "Reply where you were addressed: an answer to a message in a thread goes into that "
    "thread on its own. When a task finishes, post a one-line result in the main "
    "timeline if the room would want to know, and close the thread (talk_thread_close) "
    "when the person says it is done. A thread another agent started is that agent's "
    "work: do not speak in it unless you are @mentioned there. In a group room where you "
    "answer without being @mentioned (you are its Chief of Staff), answer "
    "unaddressed questions meant for the team and stay silent on human-to-human "
    "chatter; a message @mentioning someone else never reaches you as a turn."
)
THREAD_OWNERSHIP_ENV = "NEXTCLOUD_TALK_THREAD_OWNERSHIP"
HOME_THREAD_ENV = "NEXTCLOUD_TALK_HOME_CHANNEL_THREAD_ID"
THREAD_LOOKUP_RETRY_SECONDS = 300.0  # an unknown thread root is re-read after this
THREAD_CACHE_MAX = 2000
THREAD_ROOT_MAX_CHARS = 1500
# Resume: a thread idle this long gets its recent history handed back to the
# model on the next turn, so a reset Hermes session still knows where it stood.
THREAD_RESUME_ENV = "NEXTCLOUD_TALK_THREAD_RESUME_HOURS"
DEFAULT_THREAD_RESUME_HOURS = 6.0
THREAD_RESUME_LINES = 12
THREAD_RESUME_MAX_CHARS = 3000
THREAD_HISTORY_SCAN = 100      # federated rooms: Talk does not proxy a thread-filtered read
NOTIFY_DEFAULT = 0             # Talk Participant::NOTIFY_* (thread follow levels)
NOTIFY_NEVER = 3               # "unfollow"; the agent uses it on its own side to mark a thread closed
CLOSED_MARK = "\u2705 "         # talk_thread_close prefixes a closed thread's title with this

# Polls (tools.py talk_poll_*). The agent gets a turn when its poll is answered:
# a native Talk poll after POLL_SETTLE seconds of quiet following a vote (Talk
# hides votes from the author until closed, so the adapter closes it to read
# them), a numbered reaction poll after a short settle. Any person's message in
# the thread while a poll is open is their own answer ("fill in") and closes it.
POLL_SETTLE_ENV = "NEXTCLOUD_TALK_POLL_SETTLE_SECONDS"
DEFAULT_POLL_SETTLE = 15.0
REACTION_POLL_SETTLE = 4.0
REACTION_POLL_MULTI_SETTLE = 30.0
POLLS_HINT = (
    "To let the person choose between 2 to 4 real directions (which approach, what next), use "
    "talk_poll_create with the top choices; it adds a 'something else, reply here' line, and you "
    "get a new turn with their answer. A yes/no question is not a poll: ask it in one line and "
    "they answer with a check mark or cross reaction, which reaches you as a turn."
)
# Reactions that give the agent a turn when a person puts them on one of its
# messages: yes/no answers to a question, and "done" on a thread's last message.
REACTION_TURNS_ENV = "NEXTCLOUD_TALK_REACTION_TURNS"
DEFAULT_REACTION_TURNS = "✅,❌"  # check mark, cross
YES_REACTIONS = frozenset({"✅", "\U0001F44D", "✔️", "✔"})
# Server capabilities: re-read daily, told to the model once per conversation.
CAPS_REFRESH_SECONDS = 24 * 3600.0
CAPS_NOTE_ENV = "NEXTCLOUD_TALK_CAPABILITY_NOTE"
CAPS_FEATURE_WORDS = (
    ("threads", "threads"),
    ("talk-polls", "polls"),
    ("scheduled-messages", "scheduled messages (talk_message_schedule)"),
    ("silent-send", "silent messages"),
    ("reactions", "reactions"),
    ("edit-messages", "message edits"),
)
CAPS_APP_WORDS = (
    ("deck", "Deck"), ("calendar", "Calendar"), ("tables", "Tables"), ("forms", "Forms"),
    ("polls", "Polls app"), ("richdocuments", "Office"), ("assistant", "Assistant"),
    ("cookbook", "Cookbook"),
)

_DISPLAY_MODE_PREFIX_RE = re.compile(r"^\s*\[(mention|watch|free)\]", re.IGNORECASE)


class _FatalRoomError(Exception):
    """Raised when a room's long-poll loop hit an unrecoverable error (403/404)."""


# ---------------------------------------------------------------------------
# Pure helpers (unit-testable without any network)
# ---------------------------------------------------------------------------


def _build_auth_header(username: str, app_password: str) -> Dict[str, str]:
    """Build a Basic-Auth Authorization header from a username + app password.

    Returns ``{}`` if either half is missing -- callers treat that as
    "not configured" rather than sending a malformed header.
    """
    username = (username or "").strip()
    app_password = (app_password or "").strip()
    if not username or not app_password:
        return {}
    token = base64.b64encode(f"{username}:{app_password}".encode("utf-8")).decode("ascii")
    return {"Authorization": f"Basic {token}"}


def _ocs_headers(username: str, app_password: str) -> Dict[str, str]:
    """Headers every OCS/Talk request needs: auth + the OCS marker + JSON."""
    headers = {
        "OCS-APIRequest": "true",
        "Accept": "application/json",
    }
    headers.update(_build_auth_header(username, app_password))
    return headers


def _parse_room_modes_env(raw: Optional[str]) -> Dict[str, str]:
    """Parse ``NEXTCLOUD_TALK_ROOM_MODES`` ("token1=mention,token2=watch").

    Unknown mode values and malformed pairs are skipped (logged by the
    caller), never raised -- a typo in one room's mode must not break
    config loading for every other room.
    """
    modes: Dict[str, str] = {}
    if not raw:
        return modes
    for pair in raw.split(","):
        pair = pair.strip()
        if not pair or "=" not in pair:
            continue
        token, _, mode = pair.partition("=")
        token = token.strip()
        mode = mode.strip().lower()
        if token and mode in VALID_ROOM_MODES:
            modes[token] = mode
    return modes


def _room_mode(token: str, display_name: Optional[str], modes_map: Dict[str, str]) -> str:
    """Resolve the effective mode for a room.

    Rooms not listed in the ``token=mode`` map are always ``mention``. For a
    listed room, a ``[watch]``/``[free]``/``[mention]`` prefix on its
    *display name* wins (an admin can flip it from inside Talk without
    touching ``.env``); otherwise the map's value applies.
    """
    if token not in modes_map:
        # Only rooms the operator listed in NEXTCLOUD_TALK_ROOM_MODES may be
        # re-moded from inside Talk; anyone can name a room "[watch] ...".
        return ROOM_MODE_MENTION
    if display_name:
        match = _DISPLAY_MODE_PREFIX_RE.match(display_name)
        if match:
            return match.group(1).lower()
    return modes_map.get(token, ROOM_MODE_MENTION)


def _is_pair_participants(
    participants: List[Dict[str, Any]],
    username: str,
    allowed_users: frozenset,
    agent_users: frozenset,
) -> bool:
    """True when the room holds exactly this account plus one allowed human.

    The human must be listed in NEXTCLOUD_TALK_ALLOWED_USERS (allow-all does
    not count) and must not be another agent, so the agent-to-agent loop
    guard never meets a pair room.
    """
    if len(participants) != 2:
        return False
    me = (username or "").lower()
    others = [
        p for p in participants
        if not (p.get("actorType") == "users" and (p.get("actorId") or "").lower() == me)
    ]
    if len(others) != 1:
        return False
    other = others[0]
    if other.get("actorType") not in ("users", "federated_users"):
        return False
    actor_id = other.get("actorId") or ""
    if actor_id.startswith("federated_users/"):
        actor_id = actor_id[len("federated_users/"):]
    actor_id = actor_id.lower()
    if not actor_id or actor_id not in allowed_users:
        return False
    return not _is_agent_actor({"id": actor_id, "type": other.get("actorType")}, agent_users)


def _parse_allowlist(raw: Optional[str]) -> frozenset:
    """Parse ``NEXTCLOUD_TALK_ALLOWED_USERS`` to a set of lowercase user ids."""
    return frozenset(s.strip().lower() for s in str(raw or "").split(",") if s.strip())


def _truthy(raw: Any) -> bool:
    return str(raw or "").strip().lower() in ("1", "true", "yes", "on")


def _parse_agent_users(raw: Optional[str]) -> frozenset:
    """Parse ``NEXTCLOUD_TALK_AGENT_USERS`` ("ops-bot,research-bot") to a set of lowercase ids."""
    return frozenset(s.strip().lower() for s in (raw or "").split(",") if s.strip())


def _is_agent_actor(actor: Dict[str, Optional[str]], agent_users: frozenset) -> bool:
    """True for a Talk webhook-bot sender, or an actor listed in ``agent_users``.

    A bare entry ("ops-bot") matches both the local user id and the same user
    id on any federated instance ("ops-bot@remote-instance"); an entry that
    already includes ``@`` matches only that exact qualified id.
    """
    if actor.get("type") == ACTOR_TYPE_BOTS:
        return True
    actor_id = (actor.get("id") or "").lower()
    if not actor_id:
        return False
    return actor_id in agent_users or actor_id.split("@", 1)[0] in agent_users


def _with_room_context(text: str, backlog: List[str]) -> str:
    """Prefix an addressed message with what the room said since the last reply."""
    if not backlog:
        return text
    return (
        f"[Room messages since you last replied, {len(backlog)} line(s). Context only: "
        "you were not addressed in these, so do not answer them one by one.]\n"
        + "\n".join(backlog)
        + "\n\n[The message addressed to you]\n"
        + text
    )


def _with_room_rules(text: str, description: Optional[str]) -> str:
    """Prefix an addressed message with the room's description, where a room keeps its rules.

    The description is read fresh on every discovery pass, so a rule edited
    in Talk itself reaches the agent on its next reply with no config or
    restart on the Hermes side.
    """
    description = (description or "").strip()
    if not description:
        return text
    return f"[Room rules, from the conversation description. Follow them.]\n{description}\n\n{text}"


def _message_thread_id(message: Dict[str, Any]) -> Optional[str]:
    """Thread id of a Talk chat message as a string, or None for the main timeline.

    Talk puts ``threadId`` on EVERY message (``topmostParentId ?: id``, see
    spreed ``Message::toArray``), thread or not, so ``threadId`` alone means
    nothing. Only ``isThread`` says the message belongs to a real thread: the
    root and every reply carry it (with ``threadTitle``), and ``threadId`` is
    then the root's id. Missing ``isThread``, or a junk id, is the main timeline.
    """
    if not message.get("isThread"):
        return None
    raw = message.get("threadId") or message.get("id")
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return None
    return str(value) if value > 0 else None


def _thread_id_int(raw: Any) -> Optional[int]:
    """A positive int thread id from a metadata/config value, else None."""
    try:
        value = int(str(raw).strip())
    except (TypeError, ValueError):
        return None
    return value if value > 0 else None


def _ctx_key(token: str, thread_id: Optional[str]):
    """Key for per-conversation state: the bare token for the main timeline
    (unchanged from before threads), ``(token, thread_id)`` inside a thread."""
    return (token, thread_id) if thread_id else token


def _with_thread_root(text: str, title: Optional[str], root_line: Optional[str]) -> str:
    """Prefix the first addressed message in a thread with what the thread is about."""
    if not title and not root_line:
        return text
    head = f'[You are in the Talk thread "{title}".' if title else "[You are in a Talk thread."
    head += " Your reply goes into this thread.]"
    if root_line:
        head += f"\n[Thread started with]\n{root_line[:THREAD_ROOT_MAX_CHARS]}"
    return f"{head}\n\n{text}"


def _idle_phrase(seconds: float) -> str:
    hours = seconds / 3600.0
    if hours >= 48:
        return f"{int(round(hours / 24))} days"
    if hours >= 1.5:
        return f"{int(round(hours))} hours"
    return "a while"


def _with_thread_resume(text: str, lines: List[str], idle_seconds: float) -> str:
    """Prefix a turn in a thread that sat idle with where the thread stood.

    The thread's Hermes session may have been reset while it was quiet; the
    recent lines bring it back, and the model opens with a short recap so the
    person sees where things stood before the new answer.
    """
    head = (f"[This thread was quiet for {_idle_phrase(idle_seconds)}. Start your reply with "
            "one short line recapping where it stood, then answer.]")
    if lines:
        head += "\n[Thread so far, oldest first]\n" + "\n".join(lines)
    return f"{head}\n\n{text}"


def _trim_backlog(backlog: List[str], max_messages: int) -> List[str]:
    """Cap a room's context backlog by message count, then by total size."""
    backlog = backlog[-max_messages:] if max_messages > 0 else []
    while backlog and sum(len(line) + 1 for line in backlog) > CONTEXT_MAX_CHARS:
        backlog = backlog[1:]
    return backlog


def _room_chat_type(room_type: Optional[int]) -> str:
    """Map a Talk room ``type`` to Hermes's generic ``chat_type``."""
    if room_type in (ROOM_TYPE_ONE_TO_ONE, ROOM_TYPE_FORMER_ONE_TO_ONE):
        return "dm"
    return "group"


def _is_skippable_room(room_type: Optional[int]) -> bool:
    """Note-to-self is not a conversation partner."""
    return room_type == ROOM_TYPE_NOTE_TO_SELF


def _resolve_mention_placeholders(text: str, parameters: Optional[Dict[str, Any]]) -> str:
    """Replace Talk's ``{mention-...}`` placeholders with ``@name``.

    Without this the model sees ``{mention-federated-user1}`` instead of
    ``@lumina`` and, under "reply only when @mentioned" room rules, decides it
    was not addressed and returns a silence marker.
    """
    if not parameters:
        return text

    def _sub(match: "re.Match[str]") -> str:
        param = parameters.get(match.group(1))
        if isinstance(param, dict) and param.get("name"):
            return f"@{param['name']}"
        return match.group(0)

    return re.sub(r"\{(mention-[a-zA-Z0-9_-]+)\}", _sub, text)


# -- Shared files (Talk "file" rich objects) ----------------------------------

# Hermes' document cap for chat uploads; Talk itself allows far larger shares.
MAX_INBOUND_FILE_BYTES = 20 * 1024 * 1024
# Small text files are inlined so the model reads them without a tool call.
MAX_INLINE_TEXT_BYTES = 100 * 1024
_TEXT_EXTENSIONS = frozenset({
    ".txt", ".md", ".csv", ".tsv", ".json", ".yaml", ".yml", ".xml", ".log",
    ".py", ".js", ".ts", ".sh", ".toml", ".ini", ".cfg", ".html", ".css", ".sql",
})
# Where outbound files are uploaded in the agent's own Files before sharing.
OUTBOUND_FOLDER = "Talk"
FEDERATED_MEDIA_ENV = "NEXTCLOUD_TALK_FEDERATED_MEDIA"
FEDERATED_LINK_DAYS = 7

# Team role from the account's Nextcloud profile (Settings > Personal info, or
# an admin in user management): edit an agent's job there, not on its host.
PROFILE_ROLE_ENV = "NEXTCLOUD_TALK_PROFILE_ROLE"
PROFILE_REFRESH_SECONDS = 300
_PROFILE_FIELDS = (
    ("role", "Role"),
    ("headline", "Headline"),
    ("organisation", "Organisation"),
    ("pronouns", "Pronouns"),
    ("biography", "About"),
)
MAX_PROFILE_PROMPT_CHARS = 4000


def _file_parameters(parameters: Optional[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """The ``file`` rich objects of a Talk message, in placeholder order."""
    if not parameters:
        return []
    return [
        p for key, p in sorted(parameters.items())
        if isinstance(p, dict) and p.get("type") == "file" and key.startswith("file")
    ]


def _describe_file(param: Dict[str, Any]) -> str:
    name = param.get("name") or "file"
    mime = param.get("mimetype") or "unknown type"
    return f"[file: {name} ({mime})]"


def _resolve_file_placeholders(text: str, parameters: Optional[Dict[str, Any]]) -> str:
    """Replace ``{file}`` placeholders with a readable ``[file: name (type)]``.

    A share with no caption arrives as the bare text ``{file}``; with a
    caption the text is the caption and the file is only in the parameters,
    so it is appended rather than lost.
    """
    files = _file_parameters(parameters)
    if not files:
        return text
    named = {key: p for key, p in (parameters or {}).items()
             if isinstance(p, dict) and p.get("type") == "file"}
    used = set()

    def _sub(match: "re.Match[str]") -> str:
        param = named.get(match.group(1))
        if param is None:
            return match.group(0)
        used.add(match.group(1))
        return _describe_file(param)

    out = re.sub(r"\{(file[a-zA-Z0-9_-]*)\}", _sub, text)
    extra = [_describe_file(p) for key, p in sorted(named.items()) if key not in used]
    return " ".join([out] + extra).strip() if extra else out


def _resolve_object_placeholders(text: str, parameters: Optional[Dict[str, Any]]) -> str:
    """A shared poll (or other rich object) arrives as the bare text ``{object}``."""
    obj = (parameters or {}).get("object")
    if not isinstance(obj, dict) or "{object}" not in text:
        return text
    if obj.get("type") == "talk-poll":
        label = f"[poll: {obj.get('name') or 'untitled'}]"
    else:
        label = f"[{obj.get('type') or 'object'}: {obj.get('name') or ''}]".replace(": ]", "]")
    return text.replace("{object}", label)


def _capability_note(features: List[str], apps: List[str], *, chat_type: str, federated: bool,
                     host_features: Optional[List[str]] = None) -> str:
    """One short block telling the model what this chat's servers can do."""
    feats = set(host_features if federated and host_features is not None else features)
    words = [w for key, w in CAPS_FEATURE_WORDS if key in feats]
    if federated and "scheduled messages (talk_message_schedule)" in words:
        words.remove("scheduled messages (talk_message_schedule)")  # the host refuses it (406)
    lines = []
    if words:
        lines.append("Talk here supports: " + ", ".join(words) + ".")
    if "talk-polls" in feats:
        if chat_type == "dm":
            lines.append("Talk has no polls in one-to-one chats, so talk_poll_create posts a "
                         "numbered reaction poll here.")
        elif federated:
            lines.append("This room is hosted on another server: a poll inside a thread becomes a "
                         "numbered reaction poll, and scheduled messages are not available.")
    if apps:
        lines.append("Your Nextcloud account also has: " + ", ".join(apps)
                     + ". For lookups (someone's status, a profile, unified search) use "
                       "nextcloud_ocs_call.")
    if not lines:
        return ""
    return "[Server capabilities, told once per conversation] " + " ".join(lines)


def _media_kind(mime: str, is_voice: bool) -> str:
    mime = (mime or "").lower()
    if is_voice:
        return "voice"
    for prefix in ("image", "audio", "video"):
        if mime.startswith(prefix + "/"):
            return prefix
    return "document"


def _profile_lines(profile: Dict[str, Any]) -> List[str]:
    return [
        f"{label}: {str(profile.get(key)).strip()}"
        for key, label in _PROFILE_FIELDS
        if str(profile.get(key) or "").strip()
    ]


def _build_role_prompt(
    own: Optional[Dict[str, Any]], teammates: Optional[Dict[str, Dict[str, Any]]] = None
) -> Optional[str]:
    """System-level note built from Nextcloud profiles: who you are on this team.

    Empty profiles add nothing, so an unconfigured account behaves as before.
    """
    parts: List[str] = []
    own_lines = _profile_lines(own or {})
    if own_lines:
        parts.append(
            "[Your role on this team, from your Nextcloud profile. The team edits it "
            "there; follow it alongside your own identity.]\n" + "\n".join(own_lines)
        )
    mates = []
    for uid, prof in sorted((teammates or {}).items()):
        lines = _profile_lines(prof)
        if lines:
            name = str(prof.get("displayname") or uid).strip()
            mates.append(f"- {name} ({uid}): " + "; ".join(lines))
    if mates:
        parts.append("[Your agent teammates and their roles, from their profiles]\n" + "\n".join(mates))
    if not parts:
        return None
    prompt = "\n\n".join(parts)
    return prompt[:MAX_PROFILE_PROMPT_CHARS]


def _strip_leading_self_mention(text: str, own_user_id: str, own_display_name: str) -> str:
    """Drop a leading ``@us`` so ``@jarvis /sethome`` reaches the core as ``/sethome``.

    In a mention-mode room a slash command only arrives if it @mentions us, and
    the core treats a message as a command only when it starts with ``/``.
    """
    names = {n for n in (own_user_id, own_display_name) if n}
    for name in sorted(names, key=len, reverse=True):
        m = re.match(r'@"?' + re.escape(name) + r'"?(?![\w.-])[\s,:]*', text, re.IGNORECASE)
        if m:
            return text[m.end():]
    return text


# Fenced blocks first (they may contain backticks), then inline code spans.
_RE_CODE = re.compile(r"```[\s\S]*?```|`[^`\n]*`")


def _is_mention(
    text: str,
    message_parameters: Optional[Dict[str, Any]],
    own_user_id: str,
    own_display_name: str,
) -> bool:
    """True if this message @mentions the bot account.

    Two independent signals, either is sufficient:
    1. Rich ``messageParameters`` entries of type ``user``/``call`` whose
       ``id`` matches ``own_user_id`` (handles federated actor ids too,
       since the id there is the full federated cloud id -- an exact
       plain-text match on ``own_user_id`` still works because Nextcloud
       always mentions *this* server's local account by its local id).
    2. Plain-text fallback: ``@<username>`` or Talk's quoted
       ``@"Display Name"`` syntax, case-insensitive, for clients that
       don't send rich parameters.
    """
    text = text or ""
    # A mention quoted inside code is an example, not an address. Lumina told
    # Dave to "use `@Jarvis /approve`" and Jarvis took it as a summons and
    # answered (Operators AI, 2026-10-25). Talk's server still turns that
    # quoted @jarvis into a rich {mention-userN} placeholder, so both signals
    # below look only at text outside code spans and fenced blocks.
    visible = _RE_CODE.sub(" ", text)

    message_parameters = message_parameters or {}
    for key, param in message_parameters.items():
        if not isinstance(param, dict):
            continue
        # Talk ids are case-insensitive: a federated sender's client can resolve
        # "@Jarvis" to id "Jarvis" for the local account "jarvis".
        if (param.get("type") in ("user", "call")
                and str(param.get("id") or "").casefold() == (own_user_id or "").casefold()):
            placeholder = "{" + str(key) + "}"
            if placeholder in text and placeholder not in visible:
                continue  # only ever quoted inside code
            return True

    text = visible
    own_user_id = (own_user_id or "").strip()
    own_display_name = (own_display_name or "").strip()
    if own_user_id and re.search(
        rf"@{re.escape(own_user_id)}\b", text, re.IGNORECASE
    ):
        return True
    if own_display_name and re.search(
        rf'@"{re.escape(own_display_name)}"', text, re.IGNORECASE
    ):
        return True
    if own_display_name and re.search(
        rf"@{re.escape(own_display_name)}\b", text, re.IGNORECASE
    ):
        return True
    return False


# Rich mention types that address someone (a person, guest, group or team);
# "call" (@all) addresses everyone, this account included, so it does not count.
_OTHER_MENTION_TYPES = frozenset({"user", "guest", "federated_user", "user-group", "group", "circle"})


def _mentions_someone_else(
    text: str,
    message_parameters: Optional[Dict[str, Any]],
    own_user_id: str,
    own_display_name: str,
    agent_users: frozenset,
) -> bool:
    """True if a message @mentions a participant other than this account.

    Rich ``messageParameters`` mentions count (outside code, like
    ``_is_mention``), plus a plain-text ``@<agent>`` for any other agent in
    ``agent_users``.
    """
    text = text or ""
    visible = _RE_CODE.sub(" ", text)
    own = (own_user_id or "").casefold()
    for key, param in (message_parameters or {}).items():
        if not isinstance(param, dict) or param.get("type") not in _OTHER_MENTION_TYPES:
            continue
        pid = str(param.get("id") or "").casefold()
        if not pid or pid == own or pid.split("@", 1)[0] == own:
            continue
        placeholder = "{" + str(key) + "}"
        if placeholder in text and placeholder not in visible:
            continue  # only quoted inside code
        return True
    for agent in agent_users:
        name = agent.split("@", 1)[0]
        if name and name != own and re.search(rf"(?<![\w.])@{re.escape(name)}\b", visible, re.IGNORECASE):
            return True
    return False


def _invite_accept_path(invite_id) -> str:
    """OCS path that accepts a federation invitation (POST)."""
    return f"/ocs/v2.php/apps/spreed/api/v1/federation/invitation/{invite_id}"


def _is_pending_invite(invite: Dict[str, Any]) -> bool:
    """True if a federation invitation is still pending (state == 0).

    The invitation list endpoint has been observed (against a live
    Nextcloud Hub instance) to keep returning an invite that was already
    accepted elsewhere, and a repeat POST accept on it always 400s. Only
    act on ``state == 0``. A missing ``state`` key (older Talk versions
    never returned the field) is treated as pending so nothing regresses
    there.
    """
    state = invite.get("state")
    return state is None or state == INVITE_STATE_PENDING


def _working_reaction_emoji(raw: Optional[str]) -> str:
    """Resolve the configured "processing" reaction from a raw env/config value.

    ``None`` (the setting left unset) falls back to the default eyes emoji.
    An explicit empty string disables the feature -- distinct from unset.
    """
    if raw is None:
        return DEFAULT_WORKING_REACTION_EMOJI
    return raw.strip()


def _should_auto_accept_invite(inviter_cloud_id: str, allowlist: List[str]) -> bool:
    """True only if the federation inviter is on the explicit allowlist.

    Matching is done on the local-part before ``@`` as well as the full
    cloud id, since a federated inviter's id commonly arrives formatted as
    ``user@remote-instance`` while the allowlist is typically expressed as
    the bare local username. Everything not on the allowlist is left
    pending, never auto-declined.
    """
    inviter_cloud_id = (inviter_cloud_id or "").strip().lower()
    if not inviter_cloud_id or not allowlist:
        return False
    local_part = inviter_cloud_id.split("@", 1)[0]
    normalized_allow = {entry.strip().lower() for entry in allowlist if entry.strip()}
    return inviter_cloud_id in normalized_allow or local_part in normalized_allow


def _extract_actor(message: Dict[str, Any]) -> Dict[str, Optional[str]]:
    """Pull a normalized actor id/display name out of a Talk chat message.

    Federated participants carry ``actorType == "federated_users"`` and an
    ``actorId`` qualified as ``user@remote-instance`` (confirmed against a
    live federated room) -- returned as the full cloud id so memory/
    attribution can tell local from federated senders. Some Talk surfaces
    (participant listings, not chat messages) spell the id
    ``federated_users/user@remote-instance``; strip that prefix defensively
    if it ever shows up here too, so downstream mention/allowlist matching
    against the bare cloud id keeps working either way.
    """
    actor_id = message.get("actorId")
    if isinstance(actor_id, str) and actor_id.startswith("federated_users/"):
        actor_id = actor_id[len("federated_users/"):]
    return {
        "id": actor_id,
        "display_name": message.get("actorDisplayName"),
        "type": message.get("actorType"),
    }


def _source_tag(instance_host: str, room_token: str) -> str:
    """Memory-source tag: ``nextcloud-talk/<instance-host>/<room-token>``."""
    instance_host = (instance_host or "").strip().rstrip("/")
    instance_host = re.sub(r"^https?://", "", instance_host)
    return f"nextcloud-talk/{instance_host}/{room_token}"


def _chunk_message(content: str, limit: int = MAX_MESSAGE_LENGTH) -> List[str]:
    """Split content into Talk-sized chunks without breaking mid-codepoint.

    Simple length-based split (Talk messages are plain text/limited
    markdown, no code-fence continuation semantics to preserve).
    """
    if not content:
        return [""]
    if len(content) <= limit:
        return [content]
    return [content[i : i + limit] for i in range(0, len(content), limit)]


def check_requirements() -> bool:
    """Minimal env check used by the plugin registry pre-flight."""
    if not HTTPX_AVAILABLE:
        return False
    server = os.getenv("NEXTCLOUD_TALK_SERVER_URL", "").strip()
    app_password = _secret("NEXTCLOUD_TALK_APP_PASSWORD", "").strip()
    return bool(server and app_password)


def validate_config(config) -> bool:
    extra = getattr(config, "extra", {}) or {}
    server = extra.get("server_url") or os.getenv("NEXTCLOUD_TALK_SERVER_URL", "")
    app_password = extra.get("app_password") or _secret("NEXTCLOUD_TALK_APP_PASSWORD", "")
    return bool(server and app_password)


def is_connected(config) -> bool:
    return validate_config(config)


# ---------------------------------------------------------------------------
# Adapter
# ---------------------------------------------------------------------------


class NextcloudTalkUserAdapter(BasePlatformAdapter):
    """Real-user Nextcloud Talk adapter: room discovery + per-room long-poll."""

    MAX_MESSAGE_LENGTH = MAX_MESSAGE_LENGTH

    def __init__(self, config: PlatformConfig):
        platform = Platform(PLATFORM_NAME)
        super().__init__(config=config, platform=platform)

        extra = config.extra or {}
        self._server: str = (
            extra.get("server_url") or os.getenv("NEXTCLOUD_TALK_SERVER_URL", "")
        ).rstrip("/")
        self._username: str = extra.get("username") or os.getenv(
            "NEXTCLOUD_TALK_USERNAME", DEFAULT_USERNAME
        )
        self._display_name: str = extra.get("display_name") or os.getenv(
            "NEXTCLOUD_TALK_DISPLAY_NAME", DEFAULT_DISPLAY_NAME
        )
        self._app_password: str = extra.get("app_password") or _secret("NEXTCLOUD_TALK_APP_PASSWORD", ""
        )
        self._allowed_inviters: List[str] = [
            s.strip()
            for s in (
                extra.get("allowed_inviters")
                or os.getenv("NEXTCLOUD_TALK_ALLOWED_INVITERS", "")
            ).split(",")
            if s.strip()
        ]
        self._room_modes: Dict[str, str] = _parse_room_modes_env(
            extra.get("room_modes") or os.getenv("NEXTCLOUD_TALK_ROOM_MODES", "")
        )
        self._agent_users: frozenset = _parse_agent_users(
            extra.get("agent_users") or os.getenv(AGENT_USERS_ENV, "")
        )
        self._agent_chain_limit: int = int(
            extra.get("agent_chain_limit")
            or os.getenv(AGENT_CHAIN_LIMIT_ENV, DEFAULT_AGENT_CHAIN_LIMIT)
        )
        self._context_messages: int = int(
            extra.get("context_messages")
            if extra.get("context_messages") is not None
            else os.getenv(CONTEXT_MESSAGES_ENV, DEFAULT_CONTEXT_MESSAGES)
        )
        self._room_rules_all: bool = _truthy(
            extra.get("room_rules_all_rooms") or os.getenv(ROOM_RULES_ALL_ENV, "")
        )
        # Read at adapter start, not import, so it is correct per profile.
        self._long_poll_timeout: int = int(
            extra.get("long_poll_timeout")
            or os.getenv(LONG_POLL_TIMEOUT_ENV, DEFAULT_LONG_POLL_TIMEOUT)
        )
        # room token -> unanswered "name: text" lines since the agent last replied
        self._room_context: Dict[str, List[str]] = {}
        # room token -> {message id: its line in _room_context}, so an edit or
        # deletion of a still-unanswered message updates the backlog in place
        self._room_context_ids: Dict[str, Dict[str, str]] = {}
        raw_role = extra.get("profile_role")
        if raw_role is None:
            raw_role = os.getenv(PROFILE_ROLE_ENV, "on")
        self._profile_role_enabled: bool = str(raw_role).strip().lower() not in ("0", "off", "false", "no")
        self._role_prompt: Optional[str] = None
        self._profile_fetched_at: float = 0.0
        # Talk cannot share a file into a federated conversation. "link" uploads
        # to the agent's own Files and posts an expiring public link instead;
        # off by default because a public link is readable by anyone who has it.
        self._federated_media: str = str(
            extra.get("federated_media") or os.getenv(FEDERATED_MEDIA_ENV, "off")
        ).strip().lower()
        # room token -> agent messages seen in a row since the last human message
        self._agent_chain: Dict[str, int] = {}
        self._discovery_interval: float = float(
            extra.get("discovery_interval_seconds")
            or os.getenv("NEXTCLOUD_TALK_DISCOVERY_INTERVAL_SECONDS", DEFAULT_DISCOVERY_INTERVAL)
        )
        self._max_rooms: int = int(
            extra.get("max_rooms") or os.getenv("NEXTCLOUD_TALK_MAX_ROOMS", DEFAULT_MAX_ROOMS)
        )
        _working_reaction_raw = extra.get("working_reaction")
        if _working_reaction_raw is None:
            _working_reaction_raw = os.environ.get(WORKING_REACTION_ENV)
        self._working_reaction: str = _working_reaction_emoji(_working_reaction_raw)

        self._http_client: Optional["httpx.AsyncClient"] = None
        self._discovery_task: Optional[asyncio.Task] = None
        self._watch_flush_task: Optional[asyncio.Task] = None
        self._room_tasks: Dict[str, asyncio.Task] = {}
        self._room_meta: Dict[str, Dict[str, Any]] = {}
        self._seen_messages: Dict[str, float] = {}
        self._watch_buffers: Dict[str, List[MessageEvent]] = {}
        # Watch digests are filtered against the same allowlist the gateway
        # applies, so an unlisted sender's text never rides in on a digest.
        self._allowed_users = _parse_allowlist(
            extra.get("allowed_users") or os.getenv("NEXTCLOUD_TALK_ALLOWED_USERS", "")
        )
        self._allow_all_users = _truthy(
            extra.get("allow_all_users") or os.getenv("NEXTCLOUD_TALK_ALLOW_ALL_USERS", "")
        )
        # A per-agent access file (written by Agent Seats) overrides the env
        # list when present, and is re-read when it changes.
        self._access = access.TalkAccess(
            extra.get("access_file") or os.getenv(access.ACCESS_FILE_ENV)
            or access.default_path(extra.get("username") or os.getenv("NEXTCLOUD_TALK_USERNAME", "")),
            env_users=self._allowed_users, env_allow_all=self._allow_all_users,
        )
        self._sync_gateway_allowlist()
        dm_mode = str(extra.get("dm_mode") or os.getenv(DM_MODE_ENV, "") or DEFAULT_DM_MODE)
        dm_mode = dm_mode.strip().lower()
        if dm_mode not in VALID_ROOM_MODES:
            logger.warning("[%s] %s=%r is not a room mode, using %s", PLATFORM_NAME,
                           DM_MODE_ENV, dm_mode, DEFAULT_DM_MODE)
            dm_mode = DEFAULT_DM_MODE
        self._dm_mode: str = dm_mode
        # token -> (is a pair room, when last read)
        self._pair_rooms: Dict[str, Tuple[bool, float]] = {}
        # Threads: off until the server's capabilities list "threads", so an
        # older Talk sees exactly the pre-thread behavior.
        self._threads_enabled: bool = False
        self._capabilities_checked: bool = False
        raw_ownership = extra.get("thread_ownership")
        if raw_ownership is None:
            raw_ownership = os.getenv(THREAD_OWNERSHIP_ENV, "on")
        self._thread_ownership: bool = str(raw_ownership).strip().lower() not in ("0", "off", "false", "no")
        # (token, thread id) -> {"actor": {...} or None, "title", "root", "at"}
        self._threads: Dict[Tuple[str, str], Dict[str, Any]] = {}
        # (token, thread id) pairs whose root was already handed to the model
        self._thread_root_given: set = set()
        # (token, thread id) -> timestamp of the last message seen in it (resume detection)
        self._thread_last_at: Dict[Tuple[str, str], float] = {}
        raw_resume = extra.get("thread_resume_hours")
        if raw_resume is None:
            raw_resume = os.getenv(THREAD_RESUME_ENV, str(DEFAULT_THREAD_RESUME_HOURS))
        try:
            self._thread_resume_s: float = max(0.0, float(raw_resume)) * 3600.0
        except (TypeError, ValueError):
            self._thread_resume_s = DEFAULT_THREAD_RESUME_HOURS * 3600.0
        # Polls: settle time after a vote, pending decide tasks (key -> task).
        raw_settle = extra.get("poll_settle_seconds")
        if raw_settle is None:
            raw_settle = os.getenv(POLL_SETTLE_ENV, str(DEFAULT_POLL_SETTLE))
        try:
            self._poll_settle: float = max(0.0, float(raw_settle))
        except (TypeError, ValueError):
            self._poll_settle = DEFAULT_POLL_SETTLE
        self._poll_tasks: Dict[str, asyncio.Task] = {}
        raw_turns = extra.get("reaction_turns")
        if raw_turns is None:
            raw_turns = os.getenv(REACTION_TURNS_ENV, DEFAULT_REACTION_TURNS)
        self._reaction_turns = frozenset(
            e.strip().replace("\ufe0f", "") for e in str(raw_turns).split(",") if e.strip())
        # Capabilities of our own server (features + apps), and per federated room
        # the host's Talk features; the note goes out once per conversation key.
        self._server_features: List[str] = []
        self._server_apps: List[str] = []
        self._caps_fetched_at: float = 0.0
        self._room_host_features: Dict[str, Tuple[float, List[str]]] = {}
        self._caps_note_given: set = set()
        raw_note = extra.get("capability_note")
        if raw_note is None:
            raw_note = os.getenv(CAPS_NOTE_ENV, "on")
        self._caps_note_on: bool = str(raw_note).strip().lower() not in ("0", "off", "false", "no")

    # -- Connection lifecycle -------------------------------------------

    async def connect(self, *, is_reconnect: bool = False) -> bool:
        if not HTTPX_AVAILABLE:
            logger.warning("[%s] httpx not installed", self.name)
            return False
        if not self._server or not self._app_password:
            logger.warning(
                "[%s] NEXTCLOUD_TALK_SERVER_URL / NEXTCLOUD_TALK_APP_PASSWORD not configured",
                self.name,
            )
            return False

        self._http_client = httpx.AsyncClient(
            base_url=self._server,
            headers=_ocs_headers(self._username, self._app_password),
            timeout=httpx.Timeout(connect=15.0, read=self._long_poll_timeout + 15.0, write=15.0, pool=15.0),
        )
        self._running = True
        self._discovery_task = asyncio.create_task(self._discovery_loop())
        self._watch_flush_task = asyncio.create_task(self._watch_flush_loop())
        self._mark_connected()
        logger.info("[%s] Connected as %s on %s", self.name, self._username, self._server)
        return True

    async def disconnect(self) -> None:
        self._running = False
        self._mark_disconnected()

        for task in list(self._poll_tasks.values()):
            task.cancel()
        self._poll_tasks.clear()
        for task in list(self._room_tasks.values()):
            task.cancel()
        for task in (self._discovery_task, self._watch_flush_task):
            if task:
                task.cancel()
        for task in [*self._room_tasks.values(), self._discovery_task, self._watch_flush_task]:
            if not task:
                continue
            try:
                await task
            except asyncio.CancelledError:
                pass
        self._room_tasks.clear()

        if self._http_client:
            await self._http_client.aclose()
            self._http_client = None
        logger.info("[%s] Disconnected", self.name)

    # -- Discovery (rooms + federation invitations) ----------------------

    async def _discovery_loop(self) -> None:
        backoff_idx = 0
        while self._running:
            try:
                if (not self._capabilities_checked
                        or time.time() - self._caps_fetched_at > CAPS_REFRESH_SECONDS):
                    await self._refresh_capabilities()
                await self._discover_rooms()
                await self._discover_invitations()
                await self._refresh_profile_role()
                backoff_idx = 0
            except asyncio.CancelledError:
                return
            except Exception as e:
                if not self._running:
                    return
                logger.warning("[%s] Discovery error: %s", self.name, e)
                backoff_idx = min(backoff_idx + 1, len(RECONNECT_BACKOFF) - 1)
            delay = self._discovery_interval if backoff_idx == 0 else RECONNECT_BACKOFF[backoff_idx]
            try:
                await asyncio.sleep(delay)
            except asyncio.CancelledError:
                return

    async def _refresh_capabilities(self) -> None:
        """Read Talk's feature list once; threads stay off unless it says ``threads``.

        A failed read leaves threads off and is retried on the next discovery
        pass, so a transient error never turns them on by guesswork.
        """
        if not self._http_client:
            return
        try:
            resp = await self._http_client.get("/ocs/v2.php/cloud/capabilities", params={"format": "json"})
            resp.raise_for_status()
            caps = resp.json().get("ocs", {}).get("data", {}).get("capabilities", {}) or {}
        except Exception as e:
            logger.warning("[%s] capabilities read failed (threads stay off for now): %s", self.name, e)
            return
        features = (caps.get("spreed") or {}).get("features") or []
        self._threads_enabled = THREADS_CAPABILITY in features
        self._capabilities_checked = True
        self._server_features = list(features)
        self._server_apps = [word for key, word in CAPS_APP_WORDS if caps.get(key)]
        self._caps_fetched_at = time.time()
        logger.info("[%s] Talk threads %s", self.name,
                    "enabled" if self._threads_enabled else "not supported by this server (off)")

    async def _refresh_profile_role(self, force: bool = False) -> None:
        """Re-read the role prompt from Nextcloud profiles every few minutes.

        Own profile plus the local accounts in NEXTCLOUD_TALK_AGENT_USERS.
        A federated teammate's profile lives on another server and is skipped.
        A failed read keeps the last good prompt.
        """
        if not self._profile_role_enabled or not self._http_client:
            return
        if not force and time.time() - self._profile_fetched_at < PROFILE_REFRESH_SECONDS:
            return
        own = await self._fetch_profile(self._username)
        if own is None:
            return
        teammates: Dict[str, Dict[str, Any]] = {}
        for uid in sorted(self._agent_users):
            if "@" in uid or uid == self._username.lower():
                continue
            prof = await self._fetch_profile(uid)
            if prof:
                teammates[uid] = prof
        prompt = _build_role_prompt(own, teammates)
        if prompt != self._role_prompt:
            logger.info("[%s] team role prompt %s (%d chars)", self.name,
                        "updated" if prompt else "cleared", len(prompt or ""))
        self._role_prompt = prompt
        self._profile_fetched_at = time.time()

    async def _fetch_profile(self, user_id: str) -> Optional[Dict[str, Any]]:
        try:
            resp = await self._http_client.get(
                f"/ocs/v2.php/cloud/users/{quote(user_id)}", params={"format": "json"})
        except Exception as e:
            logger.debug("[%s] profile %s: %s", self.name, user_id, e)
            return None
        if resp.status_code != 200:
            return None
        data = resp.json().get("ocs", {}).get("data")
        return data if isinstance(data, dict) else None

    def _apply_role_prompt(self, event: MessageEvent) -> None:
        """Ride the profile role and (when on) the call-tools line on channel_prompt."""
        extra = _with_call_tools_prompt(self._role_prompt)
        if not extra:
            return
        existing = getattr(event, "channel_prompt", None)
        event.channel_prompt = f"{existing}\n\n{extra}" if existing else extra

    async def _discover_rooms(self) -> None:
        resp = await self._http_client.get(
            "/ocs/v2.php/apps/spreed/api/v4/room", params={"format": "json"}
        )
        resp.raise_for_status()
        rooms = resp.json().get("ocs", {}).get("data", []) or []

        live_tokens = set()
        for room in rooms[: self._max_rooms]:
            token = room.get("token")
            if not token or _is_skippable_room(room.get("type")):
                continue
            live_tokens.add(token)
            self._room_meta[token] = room
            await self._refresh_pair_room(token)
            if token not in self._room_tasks or self._room_tasks[token].done():
                federated = " [federated via %s]" % room["remoteServer"] if room.get("remoteServer") else ""
                logger.info(
                    "[%s] Discovered room %s (%s)%s -- starting poll",
                    self.name, token, room.get("displayName") or room.get("name") or token, federated,
                )
                self._room_tasks[token] = asyncio.create_task(self._poll_room(token))

        for token in list(self._room_tasks):
            if token not in live_tokens:
                self._room_tasks.pop(token).cancel()
                self._room_meta.pop(token, None)
                self._pair_rooms.pop(token, None)

    async def _refresh_pair_room(self, token: str, force: bool = False) -> None:
        """Re-read whether a group room is just this account and one allowed human.

        Only local group rooms the operator did not list are checked; a listed
        room keeps its listed mode. Every participant counts (guest, group,
        federated user). A failed read fails closed: the room is treated as a
        group (mention) until a read succeeds.
        """
        room = self._room_meta.get(token) or {}
        if (
            room.get("type") != ROOM_TYPE_GROUP
            or room.get("remoteServer")
            or token in self._room_modes
            or self._http_client is None
        ):
            self._pair_rooms.pop(token, None)
            return
        cached = self._pair_rooms.get(token)
        if cached and not force and time.time() - cached[1] < PAIR_REFRESH_SECONDS:
            return
        try:
            resp = await self._http_client.get(
                f"/ocs/v2.php/apps/spreed/api/v4/room/{token}/participants",
                params={"format": "json"},
            )
            resp.raise_for_status()
            participants = resp.json().get("ocs", {}).get("data", []) or []
            is_pair = _is_pair_participants(
                participants, self._username, self._access.people(), self._agent_users
            )
        except Exception as e:
            logger.debug("[%s] participants of %s: %s", self.name, token, e)
            is_pair = False
        if cached is None or cached[0] != is_pair:
            logger.info(
                "[%s] room %s %s a private pair with one allowed user (%s mode)",
                self.name, token, "is" if is_pair else "is not",
                self._dm_mode if is_pair else ROOM_MODE_MENTION,
            )
        self._pair_rooms[token] = (is_pair, time.time())

    def _is_pair_room(self, token: str) -> bool:
        cached = self._pair_rooms.get(token)
        return bool(cached and cached[0])

    async def _discover_invitations(self) -> None:
        resp = await self._http_client.get(
            "/ocs/v2.php/apps/spreed/api/v1/federation/invitation",
            params={"format": "json"},
        )
        if resp.status_code == 404:
            # Federation app/endpoint not enabled yet -- not an error.
            return
        resp.raise_for_status()
        invitations = resp.json().get("ocs", {}).get("data", []) or []
        for invite in invitations:
            invite_id = invite.get("id")
            inviter = invite.get("inviterCloudId") or invite.get("inviterDisplayName", "")
            if invite_id is None:
                continue
            if not _is_pending_invite(invite):
                logger.debug(
                    "[%s] Federation invitation %s from %s is no longer pending (state=%s) -- skipping",
                    self.name, invite_id, inviter, invite.get("state"),
                )
                continue
            if _should_auto_accept_invite(inviter, self._allowed_inviters):
                try:
                    accept = await self._http_client.post(
                        _invite_accept_path(invite_id),
                        params={"format": "json"},
                    )
                    accept.raise_for_status()
                    logger.info(
                        "[%s] Auto-accepted federation invitation %s from %s",
                        self.name, invite_id, inviter,
                    )
                except Exception as e:
                    logger.warning(
                        "[%s] Failed to accept federation invitation %s: %s",
                        self.name, invite_id, e,
                    )
            else:
                logger.info(
                    "[%s] Federation invitation %s from %s left pending (not on allowlist)",
                    self.name, invite_id, inviter,
                )

    # -- Per-room long-poll -----------------------------------------------

    async def _poll_room(self, token: str) -> None:
        backoff_idx = 0
        try:
            last_given = await self._baseline_last_given(token)
        except asyncio.CancelledError:
            return
        except Exception as e:
            logger.warning("[%s] Baseline lookup failed for room %s: %s", self.name, token, e)
            last_given = 0

        while self._running:
            try:
                last_given = await self._long_poll_once(token, last_given)
                backoff_idx = 0
            except asyncio.CancelledError:
                return
            except _FatalRoomError:
                logger.warning("[%s] Room %s poll stopped (fatal)", self.name, token)
                return
            except Exception as e:
                if not self._running:
                    return
                logger.warning("[%s] Room %s poll error: %s", self.name, token, e)
                backoff_idx = min(backoff_idx + 1, len(RECONNECT_BACKOFF) - 1)
                try:
                    await asyncio.sleep(RECONNECT_BACKOFF[backoff_idx])
                except asyncio.CancelledError:
                    return

    async def _baseline_last_given(self, token: str) -> int:
        resp = await self._http_client.get(
            f"/ocs/v2.php/apps/spreed/api/v1/chat/{token}",
            params={"format": "json", "lookIntoFuture": 0, "limit": BASELINE_LOOKUP_LIMIT},
        )
        resp.raise_for_status()
        return int(resp.headers.get("X-Chat-Last-Given", 0) or 0)

    async def _long_poll_once(self, token: str, last_given: int) -> int:
        resp = await self._http_client.get(
            f"/ocs/v2.php/apps/spreed/api/v1/chat/{token}",
            params={
                "format": "json",
                "lookIntoFuture": 1,
                "lastKnownMessageId": last_given,
                "timeout": self._long_poll_timeout,
                "limit": 100,
            },
        )
        if resp.status_code in (403, 404):
            raise _FatalRoomError(f"HTTP {resp.status_code} for room {token}")
        if resp.status_code not in (200, 304):
            resp.raise_for_status()

        next_given = int(resp.headers.get("X-Chat-Last-Given", last_given) or last_given)
        if resp.status_code == 200:
            messages = resp.json().get("ocs", {}).get("data", []) or []
            for message in messages:
                await self._on_message(token, message)
        return next_given

    # -- Inbound message handling ------------------------------------------

    async def _on_message(self, token: str, message: Dict[str, Any]) -> None:
        msg_id = str(message.get("id", ""))
        if not msg_id or self._is_duplicate(msg_id):
            return
        if message.get("systemMessage") in ("message_edited", "message_deleted"):
            # Another participant (often an agent streaming through edits)
            # changed a message we may be holding as context: keep the backlog
            # current instead of handing over its first draft.
            self._apply_backlog_edit(token, message)
            return
        if message.get("systemMessage") in PARTICIPANT_CHANGE_MESSAGES:
            # Someone joined or left: a pair room with a third person in it
            # must drop back to mention before that person's first message.
            await self._refresh_pair_room(token, force=True)
        if message.get("systemMessage") in ("poll_voted", "poll_closed", "reaction"):
            # A vote on, or a reaction to, something we asked: may be an answer.
            await self._on_answer_signal(token, message)
            return
        if message.get("messageType") not in (None, "comment", "voice-message"):
            # Skip system messages (join/leave/call events etc).
            return

        actor = _extract_actor(message)
        # Inbound needs no per-room capability check: isThread comes from the
        # server hosting the room, so a federated host without threads never sets it.
        thread_id = _message_thread_id(message) if self._threads_enabled else None
        msg_at = self._message_time(message)
        prev_thread_at: Optional[float] = None
        if thread_id:
            prev_thread_at = self._thread_last_at.get((token, thread_id))
            self._note_thread_activity((token, thread_id), msg_at)
        if thread_id and thread_id == msg_id:
            # A thread root (ours included): remember who opened it and how.
            self._note_thread_root(token, thread_id, message, actor)
        if self._is_self(actor):
            self._note_own_poll_message(token, message)
            return  # echo of our own send
        if (self._access.source == "file" and not _is_agent_actor(actor, self._agent_users)
                and not self._sender_allowed(actor.get("id"), token)):
            # Not on the access file: no turn, no thread reopen, no poll answer,
            # no room context. Other agents pass here; the loop guard and the
            # gateway's list decide for them. Without a file the gateway alone
            # decides, as before (it also honors pairing and global grants).
            logger.debug("[%s] room %s: ignoring %s (not on the access list)",
                         self.name, token, actor.get("id"))
            return

        text = (message.get("message") or "").strip()
        if not text:
            return

        room = self._room_meta.get(token, {})
        room_type = room.get("type")
        display_name = room.get("displayName") or room.get("name")
        chat_type = _room_chat_type(room_type)
        if token not in self._room_modes and (chat_type == "dm" or self._is_pair_room(token)):
            mode = self._dm_mode
        else:
            mode = _room_mode(token, display_name, self._room_modes)
        # A listed "free" group room (not a DM or private pair): the agent is
        # the room's Chief of Staff and human chatter is not all addressed to it.
        chief_room = (mode == ROOM_MODE_FREE and chat_type != "dm" and not self._is_pair_room(token))

        # A person's message always re-reads the thread: one GET per human
        # line is cheap, and it catches a close the thread tools made even
        # seconds ago (live 2026-10-30: a reply 13s after a close kept a
        # 60s-cached "open" and the thread stayed closed).
        thread = (await self._thread_info(
            token, thread_id, max_age=None if _is_agent_actor(actor, self._agent_users) else 0.0)
            if thread_id else None)
        if thread is not None and self._thread_ownership and chat_type != "dm":
            owner = thread.get("actor")
            if owner and self._is_self(owner):
                # Our own thread: the people in it are talking to us.
                if mode == ROOM_MODE_MENTION:
                    mode = ROOM_MODE_FREE
            elif owner and _is_agent_actor(owner, self._agent_users):
                # Another agent's thread is that agent's work: only an
                # explicit @mention brings us in, whatever the room mode.
                mode = ROOM_MODE_MENTION

        mentioned = _is_mention(
            text, message.get("messageParameters"), self._username, self._display_name
        )
        text = _resolve_mention_placeholders(text, message.get("messageParameters"))
        text = _resolve_file_placeholders(text, message.get("messageParameters"))
        text = _resolve_object_placeholders(text, message.get("messageParameters"))
        shared_files = _file_parameters(message.get("messageParameters"))
        if mentioned:
            bare = _strip_leading_self_mention(text, self._username, self._display_name)
            if bare.startswith("/"):
                text = bare
        is_command = text.startswith("/")

        source = self.build_source(
            chat_id=token,
            chat_name=display_name,
            chat_type=chat_type,
            user_id=actor["id"],
            user_name=actor.get("display_name"),
            message_id=msg_id,
            thread_id=thread_id,
        )

        event = MessageEvent(
            text=text,
            message_type=MessageType.TEXT,
            source=source,
            message_id=msg_id,
            raw_message=message,
            timestamp=self._parse_timestamp(message.get("timestamp")),
        )

        # The loop-guard counter, the context backlog and the session are all
        # per conversation: the main timeline, or one thread.
        key = _ctx_key(token, thread_id)
        from_agent = _is_agent_actor(actor, self._agent_users)
        if from_agent:
            self._agent_chain[key] = self._agent_chain.get(key, 0) + 1
        else:
            self._agent_chain[key] = 0

        if mode == ROOM_MODE_WATCH:
            self._watch_buffers.setdefault(token, []).append(event)
            return
        if mode == ROOM_MODE_MENTION and not mentioned:
            self._remember(token, actor, text, msg_id, thread_id=thread_id)
            return
        if from_agent and chat_type != "dm":
            if not mentioned:
                # free means free for humans; another agent must still address us
                self._remember(token, actor, text, msg_id, thread_id=thread_id)
                return
            if self._agent_chain[key] > self._agent_chain_limit:
                logger.info(
                    "[%s] room %s%s: %d agent messages in a row with no human in "
                    "between, ignoring %s until a human speaks",
                    self.name, token, f" thread {thread_id}" if thread_id else "",
                    self._agent_chain[key], actor.get("id"),
                )
                self._remember(token, actor, text, msg_id, thread_id=thread_id)
                return
            # Another agent addressing us does not oblige an answer: closing a
            # bot-to-bot exchange by staying quiet is correct (room rule:
            # stop after N rounds). Without this the core treated Lumina's
            # deliberate silence marker as a failure and posted its
            # "model returned only a silence marker" warning into the room
            # (Operators AI, 2026-10-25). A human's mention still expects a reply.
            event.reply_expected = False
        elif chief_room and not mentioned and not is_command:
            if _mentions_someone_else(text, message.get("messageParameters"), self._username,
                                      self._display_name, self._agent_users):
                # "@jarvis do X" from a human is for Jarvis, not for us: keep
                # it as context, take no turn.
                self._remember(token, actor, text, msg_id, thread_id=thread_id)
                return
            # Unaddressed: may be for the team, may be human-to-human chatter.
            # The agent decides; staying silent must not post the core's
            # "model returned only a silence marker" warning (Operators AI,
            # four times 2026-10-24/25).
            event.reply_expected = False

        if shared_files:
            await self._attach_files(event, shared_files, message)

        self._apply_role_prompt(event)

        if is_command:
            # Slash commands (/sethome, /new ...) go to the core verbatim: any
            # prefix would stop it recognising the command. Backlog is kept
            # for the next normal reply.
            await self.handle_message(event)
            return
        backlog = self._room_context.pop(key, [])
        self._room_context_ids.pop(key, None)
        if backlog:
            event.text = _with_room_context(text, backlog)
        if thread is not None and key not in self._thread_root_given:
            # First turn of this thread's session: say what the thread is.
            self._thread_root_given.add(key)
            root = thread.get("root") if thread_id != msg_id else None
            if not thread.get("title") and message.get("threadTitle"):
                thread["title"] = message.get("threadTitle")
            event.text = _with_thread_root(event.text, thread.get("title"), root)
        if thread is not None and thread.get("closed") and not from_agent:
            # A person wrote in a thread we closed: it is live again.
            await self._set_thread_level(token, thread_id, NOTIFY_DEFAULT)
            thread["closed"] = False
            title = thread.get("title") or ""
            if title.startswith(CLOSED_MARK) and await self._rename_thread(
                    token, thread_id, title[len(CLOSED_MARK):]):
                thread["title"] = title[len(CLOSED_MARK):]
            event.text = ("[You had closed this thread; they wrote in it again, so it is open "
                          "again.]\n\n" + event.text)
        if thread is not None and thread_id != msg_id and self._thread_resume_s > 0:
            event.text = await self._with_resume_if_idle(
                token, thread_id, msg_id, msg_at, prev_thread_at, event.text)
        if chat_type != "dm" and (self._room_rules_all or token in self._room_modes):
            # Whoever creates a room writes its description; only rooms the
            # operator listed (or an explicit opt-in) may hand the agent rules.
            event.text = _with_room_rules(event.text, room.get("description"))
        if not from_agent:
            if thread_id or chat_type == "dm" or self._is_pair_room(token):
                # Main-timeline chatter in a group room is not an answer to a poll.
                event.text = await self._with_poll_fill_in(token, thread_id, event.text)
        event.text = await self._with_capability_note(token, thread_id, chat_type, room, event.text)
        await self.handle_message(event)

    async def _attach_files(
        self, event: MessageEvent, files: List[Dict[str, Any]], message: Dict[str, Any]
    ) -> None:
        """Download shared files into Hermes' media cache and attach them to *event*.

        Only runs for a message the agent is about to answer, never for the
        backlog of a listening room. A file the account cannot read (a share
        from a federated room has no path on this server) or one over the size
        cap becomes a notice instead, so the model knows something was sent.
        """
        is_voice = message.get("messageType") == "voice-message"
        notices: List[str] = []
        for param in files:
            name = param.get("name") or "file"
            mime = param.get("mimetype") or mimetypes.guess_type(name)[0] or "application/octet-stream"
            path = param.get("path")
            try:
                size = int(param.get("size") or 0)
            except (TypeError, ValueError):
                size = 0
            if not path:
                notices.append(f"{name}: not reachable from this account (shared from another server)")
                continue
            if size > MAX_INBOUND_FILE_BYTES:
                notices.append(f"{name}: {size // (1024 * 1024)} MB is over the 20 MB limit")
                continue
            data = await self._download_own_file(path)
            if data is None:
                notices.append(f"{name}: download failed")
                continue
            kind = _media_kind(mime, is_voice)
            ext = os.path.splitext(name)[1].lower() or (mimetypes.guess_extension(mime) or "")
            try:
                if kind == "image":
                    cached = cache_image_from_bytes(data, ext=ext or ".jpg")
                    event.message_type = MessageType.PHOTO
                elif kind in ("voice", "audio"):
                    cached = cache_audio_from_bytes(data, ext=ext or ".ogg")
                    event.message_type = MessageType.VOICE if kind == "voice" else MessageType.AUDIO
                elif kind == "video":
                    cached = cache_video_from_bytes(data, ext=ext or ".mp4")
                    event.message_type = MessageType.VIDEO
                else:
                    cached = cache_document_from_bytes(data, name)
                    event.message_type = MessageType.DOCUMENT
            except Exception as e:
                logger.warning("[%s] caching %s failed: %s", self.name, name, e)
                notices.append(f"{name}: could not be cached")
                continue
            inlined = False
            if kind == "document" and len(data) <= MAX_INLINE_TEXT_BYTES and (
                    ext in _TEXT_EXTENSIONS or mime.startswith("text/")):
                try:
                    safe = re.sub(r"[^\w.\- ]", "_", name)
                    event.text = f"[Content of {safe}]:\n{data.decode('utf-8')}\n\n{event.text}"
                    inlined = True
                except UnicodeDecodeError:
                    pass
            event.media_urls.append(cached)
            event.media_types.append(mime)
            if hasattr(event, "media_text_inlined"):
                event.media_text_inlined.append(inlined)
        if notices:
            block = "[Talk attachment notice]\n" + "\n".join(f"- {n}" for n in notices)
            event.text = f"{block}\n\n{event.text}"

    async def _download_own_file(self, path: str) -> Optional[bytes]:
        """GET a file from the agent's own Files over WebDAV (the share's path)."""
        if not self._http_client:
            return None
        url = f"/remote.php/dav/files/{quote(self._username)}/{quote(path.lstrip('/'))}"
        try:
            resp = await self._http_client.get(url)
        except Exception as e:
            logger.warning("[%s] WebDAV GET %s failed: %s", self.name, path, e)
            return None
        if resp.status_code != 200:
            logger.warning("[%s] WebDAV GET %s: HTTP %s", self.name, path, resp.status_code)
            return None
        if len(resp.content) > MAX_INBOUND_FILE_BYTES:
            return None
        return resp.content

    def _remember(
        self, token: str, actor: Dict[str, Optional[str]], text: str, msg_id: Optional[str] = None,
        *, thread_id: Optional[str] = None,
    ) -> None:
        """Keep an unanswered message as context for the next reply in this room.

        Main timeline and each thread keep separate backlogs. A thread root is
        a main-timeline message, so it lands in the main backlog; the thread's
        first turn gets it from the thread info instead.
        """
        if self._context_messages <= 0:
            return
        if not self._sender_allowed(actor.get("id")):
            # Same filter as watch digests: an unlisted sender's text never
            # rides into a later turn as room context.
            return
        name = actor.get("display_name") or actor.get("id") or "someone"
        key = _ctx_key(token, thread_id if thread_id and msg_id != thread_id else None)
        backlog = self._room_context.setdefault(key, [])
        line = f"{name}: {text}"
        backlog.append(line)
        self._room_context[key] = _trim_backlog(backlog, self._context_messages)
        if msg_id:
            ids = self._room_context_ids.setdefault(key, {})
            ids[msg_id] = line
            if len(ids) > 4 * max(self._context_messages, 1):
                kept = set(self._room_context[key])
                self._room_context_ids[key] = {k: v for k, v in ids.items() if v in kept}

    def _apply_backlog_edit(self, token: str, system_message: Dict[str, Any]) -> None:
        """Rewrite or drop the backlog line of an edited/deleted unanswered message."""
        parent = system_message.get("parent") or {}
        parent_id = str(parent.get("id", ""))
        # The line may sit in the main backlog or in any of the room's threads.
        key = next(
            (k for k, ids in self._room_context_ids.items()
             if (k == token or (isinstance(k, tuple) and k[0] == token)) and parent_id in ids),
            token,
        )
        ids = self._room_context_ids.get(key) or {}
        old_line = ids.get(parent_id)
        backlog = self._room_context.get(key) or []
        if not old_line or old_line not in backlog:
            return
        index = backlog.index(old_line)
        if system_message.get("systemMessage") == "message_deleted":
            del backlog[index]
            ids.pop(parent_id, None)
            return
        text = _resolve_mention_placeholders(
            (parent.get("message") or "").strip(), parent.get("messageParameters")
        )
        if not text:
            return
        name = old_line.split(": ", 1)[0]
        new_line = f"{name}: {text}"
        backlog[index] = new_line
        ids[parent_id] = new_line

    # -- Threads ---------------------------------------------------------------

    def _is_self(self, actor: Dict[str, Optional[str]]) -> bool:
        """True for this account's own local actor (a federated namesake is not us)."""
        return (
            (actor.get("id") or "").lower() == (self._username or "").lower()
            and actor.get("type") != "federated_users"
        )

    def _note_thread_root(
        self, token: str, thread_id: str, message: Dict[str, Any], actor: Dict[str, Optional[str]]
    ) -> None:
        text = _resolve_mention_placeholders(
            (message.get("message") or "").strip(), message.get("messageParameters"))
        name = actor.get("display_name") or actor.get("id") or "someone"
        self._store_thread(token, thread_id, {
            "actor": actor,
            "title": message.get("threadTitle") or None,
            "root": f"{name}: {text}" if text else None,
            "at": time.time(),
        })

    def _store_thread(self, token: str, thread_id: str, info: Dict[str, Any]) -> None:
        if len(self._threads) >= THREAD_CACHE_MAX:
            # Oldest first; a dropped thread is simply looked up again.
            for k in sorted(self._threads, key=lambda k: self._threads[k].get("at", 0))[: THREAD_CACHE_MAX // 4]:
                self._threads.pop(k, None)
        self._threads[(token, thread_id)] = info

    async def _thread_info(
        self, token: str, thread_id: str, max_age: Optional[float] = None,
    ) -> Dict[str, Any]:
        """Who opened a thread, its title and root line, from cache or the threads API.

        ``GET /chat/{token}/threads/{id}`` is proxied for federated rooms. A
        failed read gives an unknown owner (the thread then follows the room
        mode) and is retried after THREAD_LOOKUP_RETRY_SECONDS. ``max_age``
        re-reads a known thread older than that, so a close made by the
        thread tools (our follow level) is seen when a person writes again;
        a failed re-read keeps what was known.
        """
        cached = self._threads.get((token, thread_id))
        if cached:
            age = time.time() - cached.get("at", 0)
            if cached.get("actor"):
                if max_age is None or age < max_age:
                    return cached
            elif age < THREAD_LOOKUP_RETRY_SECONDS:
                return cached
        info: Dict[str, Any] = {"actor": None, "title": None, "root": None, "closed": False}
        if cached:
            info.update(cached)
        info["at"] = time.time()
        if self._http_client is not None:
            try:
                resp = await self._http_client.get(
                    f"/ocs/v2.php/apps/spreed/api/v1/chat/{token}/threads/{thread_id}",
                    params={"format": "json"},
                )
                resp.raise_for_status()
                data = resp.json().get("ocs", {}).get("data", {}) or {}
                first = data.get("first") or {}
                thread = data.get("thread") or {}
                # Only OUR level is visible (Talk keeps each person's follow level
                # private); "never" on our side is how the agent marks a thread closed.
                level = (data.get("attendee") or {}).get("notificationLevel")
                info["closed"] = level == NOTIFY_NEVER
                info["title"] = thread.get("title") or first.get("threadTitle") or None
                if first.get("actorId"):
                    actor = _extract_actor(first)
                    info["actor"] = actor
                    text = _resolve_mention_placeholders(
                        (first.get("message") or "").strip(), first.get("messageParameters"))
                    name = actor.get("display_name") or actor.get("id") or "someone"
                    info["root"] = f"{name}: {text}" if text else None
            except Exception as e:
                logger.debug("[%s] thread %s/%s lookup failed: %s", self.name, token, thread_id, e)
        self._store_thread(token, thread_id, info)
        return info

    @staticmethod
    def _message_time(message: Dict[str, Any]) -> float:
        try:
            return float(message.get("timestamp") or 0) or time.time()
        except (TypeError, ValueError):
            return time.time()

    def _note_thread_activity(self, key: Tuple[str, str], at: float) -> None:
        if len(self._thread_last_at) >= THREAD_CACHE_MAX and key not in self._thread_last_at:
            for k in sorted(self._thread_last_at, key=self._thread_last_at.get)[: THREAD_CACHE_MAX // 4]:
                self._thread_last_at.pop(k, None)
        self._thread_last_at[key] = max(at, self._thread_last_at.get(key, 0.0))

    async def _set_thread_level(self, token: str, thread_id: str, level: int) -> bool:
        """Set this account's own follow level on a thread (proxied for federated rooms)."""
        if self._http_client is None:
            return False
        try:
            resp = await self._http_client.post(
                f"/ocs/v2.php/apps/spreed/api/v1/chat/{token}/threads/{thread_id}/notify",
                params={"format": "json"}, json={"level": level},
            )
            resp.raise_for_status()
            return True
        except Exception as e:
            logger.debug("[%s] thread %s/%s level %s failed: %s", self.name, token, thread_id, level, e)
            return False

    async def _rename_thread(self, token: str, thread_id: str, title: str) -> bool:
        """Rename a thread (its starter or a moderator only; a refusal is fine)."""
        if self._http_client is None or not title:
            return False
        try:
            resp = await self._http_client.put(
                f"/ocs/v2.php/apps/spreed/api/v1/chat/{token}/threads/{thread_id}",
                params={"format": "json"}, json={"threadTitle": title},
            )
            resp.raise_for_status()
            return True
        except Exception as e:
            logger.debug("[%s] thread %s/%s rename failed: %s", self.name, token, thread_id, e)
            return False

    async def _thread_history(self, token: str, thread_id: str, before_id: str) -> List[Tuple[float, str]]:
        """Recent lines of one thread before *before_id*, oldest first, as (time, "- name: text").

        Local rooms ask Talk for the thread only. Talk does not proxy that filter
        for federated rooms (spreed ChatController FIXME), so there the recent
        room history is read and filtered here. Only lines from allowed senders
        and this account are kept, like the room-context backlog.
        """
        if self._http_client is None:
            return []
        federated = bool(self._room_meta.get(token, {}).get("remoteServer"))
        params: Dict[str, Any] = {
            "format": "json", "lookIntoFuture": 0, "setReadMarker": 0, "includeLastKnown": 0,
            "lastKnownMessageId": int(before_id),
            "limit": THREAD_HISTORY_SCAN if federated else THREAD_RESUME_LINES * 2,
        }
        if not federated:
            params["threadId"] = int(thread_id)
        try:
            resp = await self._http_client.get(f"/ocs/v2.php/apps/spreed/api/v1/chat/{token}", params=params)
            resp.raise_for_status()
            if resp.status_code != 200:
                return []  # 304: nothing before this message
            messages = resp.json().get("ocs", {}).get("data", []) or []
            if not isinstance(messages, list):
                return []
        except Exception as e:
            logger.debug("[%s] thread %s/%s history read failed: %s", self.name, token, thread_id, e)
            return []
        out: List[Tuple[float, str]] = []
        for m in messages:
            if _message_thread_id(m) != thread_id:
                continue
            if m.get("messageType") not in (None, "comment", "voice-message"):
                continue
            actor = _extract_actor(m)
            mine = self._is_self(actor)
            if not mine and not self._sender_allowed(actor.get("id")):
                continue
            text = _resolve_mention_placeholders((m.get("message") or "").strip(), m.get("messageParameters"))
            if not text:
                continue
            name = "you" if mine else (actor.get("display_name") or actor.get("id") or "someone")
            out.append((self._message_time(m), f"- {name}: {' '.join(text.split())[:600]}"))
        out.sort(key=lambda x: x[0])
        return out

    async def _with_resume_if_idle(
        self, token: str, thread_id: str, msg_id: str, msg_at: float,
        prev_at: Optional[float], text: str,
    ) -> str:
        """Add the thread's recent history and a recap ask when it sat idle past the resume gap.

        ``prev_at`` is the last message this process saw in the thread; after a
        restart it is unknown and the thread's own history answers it (one read).
        """
        history: Optional[List[Tuple[float, str]]] = None
        if prev_at is None:
            history = await self._thread_history(token, thread_id, msg_id)
            if history:
                prev_at = history[-1][0]
        if prev_at is None or msg_at - prev_at < self._thread_resume_s:
            return text
        if history is None:
            history = await self._thread_history(token, thread_id, msg_id)
        lines = [line for _, line in history][-THREAD_RESUME_LINES:]
        while lines and sum(len(line) + 1 for line in lines) > THREAD_RESUME_MAX_CHARS:
            lines = lines[1:]
        return _with_thread_resume(text, lines, msg_at - prev_at)

    def _thread_param(self, metadata: Optional[Dict[str, Any]]) -> Optional[int]:
        """``threadId`` for an outbound post, from Hermes' send metadata (None without threads)."""
        if not self._threads_enabled or not metadata:
            return None
        return _thread_id_int(metadata.get("thread_id"))

    @staticmethod
    def _parse_timestamp(unix_ts: Any) -> datetime:
        try:
            return datetime.fromtimestamp(int(unix_ts), tz=timezone.utc)
        except (TypeError, ValueError, OSError):
            return datetime.now(tz=timezone.utc)

    def _is_duplicate(self, msg_id: str) -> bool:
        now = time.time()
        if len(self._seen_messages) > DEDUP_MAX_SIZE:
            cutoff = now - DEDUP_WINDOW_SECONDS
            self._seen_messages = {k: v for k, v in self._seen_messages.items() if v > cutoff}
        if msg_id in self._seen_messages:
            return True
        self._seen_messages[msg_id] = now
        return False

    # -- Polls, answer reactions, capability note -----------------------------

    def _poll_store(self):
        return pollstate.state_path(self._server, self._username)

    def _is_person(self, actor: Dict[str, Optional[str]], room: Optional[str] = None) -> bool:
        """A human allowed to steer us: not us, not another agent, on the allowlist."""
        return (not self._is_self(actor) and not _is_agent_actor(actor, self._agent_users)
                and self._sender_allowed(actor.get("id"), room))

    def _note_own_poll_message(self, token: str, message: Dict[str, Any]) -> None:
        """Our own native poll's chat message: keep its id so the answer turn replies to it."""
        obj = (message.get("messageParameters") or {}).get("object") or {}
        if obj.get("type") != "talk-poll":
            return
        try:
            for rec in pollstate.find(self._poll_store(), room=token, kind="native",
                                      poll_id=str(obj.get("id"))):
                if not rec.get("message_id"):
                    pollstate.update(self._poll_store(), rec["key"], message_id=str(message.get("id")))
        except Exception as e:  # never cost a turn over bookkeeping
            logger.debug("[%s] poll message note failed: %s", self.name, e)

    def _schedule_poll_decide(self, key: str, coro_fn, delay: float) -> None:
        task = self._poll_tasks.get(key)
        if task and not task.done():
            return  # one pending check re-reads everything when it fires
        async def _run():
            try:
                await asyncio.sleep(delay)
                await coro_fn()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.warning("[%s] poll %s: %s", self.name, key, e)
            finally:
                self._poll_tasks.pop(key, None)
        self._poll_tasks[key] = asyncio.create_task(_run())

    async def _on_answer_signal(self, token: str, message: Dict[str, Any]) -> None:
        msg_id = str(message.get("id", ""))
        kind = message.get("systemMessage")
        params = message.get("messageParameters") or {}
        store = self._poll_store()
        if kind in ("poll_voted", "poll_closed"):
            pid = str((params.get("poll") or {}).get("id") or "")
            if not pid:
                return
            recs = pollstate.find(store, room=token, kind="native", poll_id=pid)
            if not recs:
                return
            actor = _extract_actor(message)
            if kind == "poll_closed" and self._is_self(actor):
                return  # we closed it (talk_poll_close or our own decide)
            rec = recs[0]
            delay = 0.0 if kind == "poll_closed" else self._poll_settle
            self._schedule_poll_decide(rec["key"], lambda: self._decide_native(token, rec["key"]), delay)
            return
        # A reaction. Our own (working indicator, poll keycaps) and other agents' never count.
        actor = _extract_actor(message)
        if not self._is_person(actor, token):
            return
        emoji = (message.get("message") or "").strip()
        parent = message.get("parent") or {}
        parent_id = str(parent.get("id") or "")
        if not parent_id:
            return
        if pollstate.keycap_index(emoji) is not None:
            recs = pollstate.find(store, room=token, kind="reaction", message_id=parent_id)
            if recs:
                rec = recs[0]
                delay = REACTION_POLL_MULTI_SETTLE if rec.get("multi") else REACTION_POLL_SETTLE
                self._schedule_poll_decide(
                    rec["key"], lambda: self._decide_reaction(token, rec["key"]), delay)
                return
        bare = emoji.replace("️", "")
        if bare not in self._reaction_turns:
            return
        if not self._is_self(_extract_actor(parent)):
            return  # a reaction on someone else's message is not an answer to us
        await self._dispatch_reaction_turn(token, message, parent, actor, emoji, msg_id)

    def _event_for(self, token: str, actor: Dict[str, Optional[str]], text: str,
                   message_id: Optional[str], thread_id: Optional[str]) -> MessageEvent:
        room = self._room_meta.get(token, {})
        source = self.build_source(
            chat_id=token,
            chat_name=room.get("displayName") or room.get("name"),
            chat_type=_room_chat_type(room.get("type")),
            user_id=actor.get("id"),
            user_name=actor.get("display_name"),
            message_id=message_id,
            thread_id=thread_id or None,
        )
        event = MessageEvent(text=text, message_type=MessageType.TEXT, source=source,
                             message_id=message_id, raw_message={},
                             timestamp=datetime.now(timezone.utc))
        self._apply_role_prompt(event)
        return event

    async def _dispatch_reaction_turn(self, token: str, message: Dict[str, Any], parent: Dict[str, Any],
                                      actor: Dict[str, Optional[str]], emoji: str, msg_id: str) -> None:
        thread_id = ((_message_thread_id(message) or _message_thread_id(parent))
                     if self._threads_enabled else None)
        snippet = " ".join(_resolve_mention_placeholders(
            str(parent.get("message") or ""), parent.get("messageParameters")).split())[:300]
        who = actor.get("display_name") or actor.get("id") or "They"
        yes = emoji.replace("️", "") in {e.replace("️", "") for e in YES_REACTIONS}
        if yes:
            meaning = ("If that message asked a yes/no question or for approval, this is YES: go "
                       "ahead now. If it did not ask anything"
                       + (" and this is a thread, it means this matter is done: close the thread "
                          "(talk_thread_close) with a one-line result." if thread_id else
                          ", treat it as an acknowledgement and reply only if something is due."))
        else:
            meaning = ("If that message asked a yes/no question or for approval, this is NO: do not "
                       "do it, and ask in one line what they want instead if that is unclear. "
                       "Otherwise they disagree with it: ask briefly what to change.")
        text = f"[Reaction] {who} reacted {emoji} to your message: \"{snippet}\". {meaning}"
        event = self._event_for(token, actor, text, str(parent.get("id") or msg_id), thread_id)
        await self.handle_message(event)

    async def _decide_native(self, token: str, key: str) -> None:
        store = self._poll_store()
        rec = pollstate.get(store, key)
        if not rec or rec.get("status") != "open" or self._http_client is None:
            return
        base = f"/ocs/v2.php/apps/spreed/api/v1/poll/{token}/{rec['poll_id']}"
        resp = await self._http_client.get(base, params={"format": "json"})
        data = resp.json().get("ocs", {}).get("data") if resp.status_code == 200 else None
        if not isinstance(data, dict):
            return
        if data.get("status") != 1:
            if int(data.get("numVoters") or 0) < int(rec.get("wait_for") or 1):
                return
            # Talk shows the author the votes only once the poll is closed.
            resp = await self._http_client.delete(base, params={"format": "json"})
            data = resp.json().get("ocs", {}).get("data") if resp.status_code == 200 else None
            if not isinstance(data, dict):
                return
        options = data.get("options") or rec.get("options") or []
        picks: Dict[str, List[str]] = {}
        first_actor: Optional[Dict[str, Optional[str]]] = None
        for d in data.get("details") or []:
            voter = {"id": d.get("actorId"), "display_name": d.get("actorDisplayName"),
                     "type": d.get("actorType")}
            if not self._is_person(voter, token):
                continue
            try:
                label = options[int(d.get("optionId"))]
            except (TypeError, ValueError, IndexError):
                continue
            picks.setdefault(label, []).append(voter.get("display_name") or voter.get("id"))
            first_actor = first_actor or voter
        if not picks or first_actor is None:
            pollstate.claim(store, key, "closed")
            return
        claimed = pollstate.claim(store, key, "decided", result=picks)
        if claimed:
            await self._dispatch_poll_answer(token, claimed, picks, first_actor)

    async def _decide_reaction(self, token: str, key: str) -> None:
        store = self._poll_store()
        rec = pollstate.get(store, key)
        if not rec or rec.get("status") != "open" or self._http_client is None:
            return
        resp = await self._http_client.get(
            f"/ocs/v2.php/apps/spreed/api/v1/reaction/{token}/{rec['message_id']}",
            params={"format": "json"})
        data = resp.json().get("ocs", {}).get("data") if resp.status_code == 200 else None
        if not isinstance(data, dict):
            return
        options = rec.get("options") or []
        picks: Dict[str, List[str]] = {}
        voters: Dict[str, Dict[str, Optional[str]]] = {}
        for emoji, people in data.items():
            idx = pollstate.keycap_index(emoji)
            if idx is None or idx >= len(options):
                continue
            for p in people or []:
                voter = {"id": p.get("actorId"), "display_name": p.get("actorDisplayName"),
                         "type": p.get("actorType")}
                if not self._is_person(voter, token):
                    continue
                picks.setdefault(options[idx], []).append(voter.get("display_name") or voter.get("id"))
                voters.setdefault(str(voter.get("id")), voter)
        if len(voters) < int(rec.get("wait_for") or 1):
            return
        claimed = pollstate.claim(store, key, "decided", result=picks)
        if not claimed:
            return
        await self._mark_reaction_poll(token, rec, picks)
        await self._dispatch_poll_answer(token, claimed, picks, next(iter(voters.values())))

    async def _mark_reaction_poll(self, token: str, rec: Dict[str, Any], picks: Dict[str, List[str]]) -> None:
        """Best effort: edit the poll message so it reads as answered."""
        if self._http_client is None or not rec.get("message_id"):
            return
        mark = " / ".join(picks) if picks else "answered in the thread"
        text = (f"\U0001F4CA **{rec.get('question')}**\n\n"
                f"✅ Answered: {mark}")
        try:
            await self._http_client.put(
                f"/ocs/v2.php/apps/spreed/api/v1/chat/{token}/{rec['message_id']}",
                params={"format": "json"}, json={"message": text})
        except Exception as e:
            logger.debug("[%s] reaction poll edit failed: %s", self.name, e)

    async def _dispatch_poll_answer(self, token: str, rec: Dict[str, Any], picks: Dict[str, List[str]],
                                    actor: Dict[str, Optional[str]]) -> None:
        who = actor.get("display_name") or actor.get("id") or "They"
        chosen = "; ".join(f"{label} ({', '.join(names)})" for label, names in picks.items())
        fill_in = any(label == pollstate.FILL_IN_LABEL for label in picks)
        text = (f"[Poll answer] Your poll \"{rec.get('question')}\" was answered: {chosen}. "
                f"Options were: {', '.join(rec.get('options') or [])}. ")
        if fill_in:
            text += ("They picked 'something else': if they have not written what they want in this "
                     "thread yet, ask for it in one short line.")
        else:
            text += f"{who}'s choice decides it: act on it now, in this thread."
        event = self._event_for(token, actor, text, rec.get("message_id"), rec.get("thread") or None)
        logger.info("[%s] poll %s answered in %s: %s", self.name, rec.get("key"), token, chosen)
        await self.handle_message(event)

    async def _with_poll_fill_in(self, token: str, thread_id: Optional[str], text: str) -> str:
        """A person writing in a thread (or main timeline) with our poll open: their own answer."""
        store = self._poll_store()
        try:
            recs = pollstate.find(store, room=token, thread=thread_id or "")
        except Exception as e:
            logger.debug("[%s] poll lookup failed: %s", self.name, e)
            return text
        if not recs:
            return text
        rec = pollstate.claim(store, recs[0]["key"], "answered")
        if not rec:
            return text
        task = self._poll_tasks.pop(rec["key"], None)
        if task:
            task.cancel()
        if self._http_client is not None:
            try:
                if rec.get("kind") == "native":
                    await self._http_client.delete(
                        f"/ocs/v2.php/apps/spreed/api/v1/poll/{token}/{rec['poll_id']}",
                        params={"format": "json"})
                else:
                    await self._mark_reaction_poll(token, rec, {})
            except Exception as e:
                logger.debug("[%s] poll close on fill-in failed: %s", self.name, e)
        return (f"[Your poll \"{rec.get('question')}\" was open here and they wrote this instead of "
                "picking an option: treat it as their answer if it is one (the poll is closed "
                "now).]\n\n" + text)

    async def _room_host_caps(self, token: str) -> Optional[List[str]]:
        cached = self._room_host_features.get(token)
        if cached and time.time() - cached[0] < CAPS_REFRESH_SECONDS:
            return cached[1]
        if self._http_client is None:
            return None
        try:
            resp = await self._http_client.get(
                f"/ocs/v2.php/apps/spreed/api/v4/room/{token}/capabilities", params={"format": "json"})
            data = resp.json().get("ocs", {}).get("data") if resp.status_code == 200 else None
        except Exception:
            return None
        if not isinstance(data, dict):
            return None
        feats = list(data.get("features") or [])
        self._room_host_features[token] = (time.time(), feats)
        return feats

    async def _with_capability_note(self, token: str, thread_id: Optional[str], chat_type: str,
                                    room: Dict[str, Any], text: str) -> str:
        if not self._caps_note_on or not self._server_features:
            return text
        key = _ctx_key(token, thread_id)
        if key in self._caps_note_given:
            return text
        federated = bool(room.get("remoteServer"))
        host = await self._room_host_caps(token) if federated else None
        note = _capability_note(self._server_features, self._server_apps, chat_type=chat_type,
                                federated=federated, host_features=host)
        self._caps_note_given.add(key)
        return f"{note}\n\n{text}" if note else text

    # -- Watch-mode digest --------------------------------------------------

    async def _watch_flush_loop(self) -> None:
        while self._running:
            try:
                await asyncio.sleep(WATCH_FLUSH_INTERVAL_SECONDS)
            except asyncio.CancelledError:
                return
            for token, events in list(self._watch_buffers.items()):
                if not events:
                    continue
                self._watch_buffers[token] = []
                await self._dispatch_watch_digest(token, events)

    def _sender_allowed(self, user_id: Optional[str], room: Optional[str] = None) -> bool:
        if self._access.reload():
            self._sync_gateway_allowlist()
        return self._access.allowed(user_id, room)

    def _sync_gateway_allowlist(self) -> None:
        """With an access file, hand the gateway the same list.

        The adapter gates at intake; the gateway's own check is the second
        line. It reads ``NEXTCLOUD_TALK_ALLOWED_USERS`` live (or, with no env
        list, this adapter's ``extra.allowed_users``), so both are updated.
        """
        if self._access.source != "file":
            return
        ids = ",".join(self._access.gateway_ids())
        os.environ["NEXTCLOUD_TALK_ALLOWED_USERS"] = ids
        os.environ["NEXTCLOUD_TALK_ALLOW_ALL_USERS"] = "false"
        extra = getattr(self.config, "extra", None)
        if isinstance(extra, dict):
            extra["allowed_users"] = ids
            extra["allow_all_users"] = False

    async def _dispatch_watch_digest(self, token: str, events: List[MessageEvent]) -> None:
        """Send one watch-mode digest as an ordinary inbound event.

        Not ``internal``: the gateway's allowlist and pause gate apply. Lines
        from senders outside the allowlist are dropped before the digest is
        built; the digest is attributed to the last allowed sender.
        """
        allowed = [e for e in events if self._sender_allowed(e.source.user_id)]
        if not allowed:
            return
        digest_text = "\n".join(f"- {e.source.user_name or e.source.user_id}: {e.text}" for e in allowed)
        last = allowed[-1].source
        room = self._room_meta.get(token, {})
        source = self.build_source(
            chat_id=token,
            chat_name=room.get("displayName") or room.get("name"),
            chat_type="group",
            user_id=last.user_id,
            user_name=last.user_name,
        )
        digest_event = MessageEvent(
            text=f"[watch-mode digest, {len(allowed)} message(s)]\n{digest_text}",
            message_type=MessageType.TEXT,
            source=source,
        )
        self._apply_role_prompt(digest_event)
        await self.handle_message(digest_event)

    # -- Outbound messaging --------------------------------------------------

    async def send(
        self,
        chat_id: str,
        content: str,
        reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> SendResult:
        if not self._http_client:
            return SendResult(success=False, error="HTTP client not initialized")

        chunks = _chunk_message(textstyle.undash(content))
        # Every chunk carries the thread: only the first has a replyTo (which
        # already lands in the thread), so without threadId chunks 2+ would
        # fall into the main timeline. Talk ignores threadId when replyTo is set.
        thread = self._thread_param(metadata)
        last_id: Optional[str] = None
        for i, chunk in enumerate(chunks):
            body: Dict[str, Any] = {"message": chunk}
            if thread:
                body["threadId"] = thread
            if reply_to and i == 0:
                try:
                    body["replyTo"] = int(reply_to)
                except (TypeError, ValueError):
                    pass
            try:
                resp = await self._http_client.post(
                    f"/ocs/v2.php/apps/spreed/api/v1/chat/{chat_id}",
                    params={"format": "json"},
                    json=body,
                )
            except Exception as e:
                return SendResult(success=False, error=str(e), retryable=True)

            if resp.status_code >= 300:
                return SendResult(
                    success=False,
                    error=f"HTTP {resp.status_code}: {resp.text[:200]}",
                )
            try:
                data = resp.json().get("ocs", {}).get("data", {})
                last_id = str(data.get("id", last_id))
            except Exception:
                pass

        return SendResult(success=True, message_id=last_id)

    async def edit_message(
        self, chat_id: str, message_id: str, content: str, *, finalize: bool = False
    ) -> SendResult:
        """Edit one of our own messages in place (Talk ``edit-messages``, Talk 18+).

        Implementing this is what turns on Hermes' tool-progress bubble and
        edit-based streaming for Talk: the runner drops every progress update
        on a platform whose adapter keeps the base no-op. Talk only lets you
        edit your own messages, for a limited time, and an edit cannot be
        split, so over-long content and refusals return success=False and the
        caller sends a fresh message instead.
        """
        if not self._http_client:
            return SendResult(success=False, error="HTTP client not initialized")
        if len(content) > MAX_MESSAGE_LENGTH:
            return SendResult(success=False, error="Too long to edit in place")
        try:
            resp = await self._http_client.put(
                f"/ocs/v2.php/apps/spreed/api/v1/chat/{chat_id}/{message_id}",
                params={"format": "json"},
                json={"message": textstyle.undash(content)},
            )
        except Exception as e:
            return SendResult(success=False, error=str(e), retryable=True)
        if resp.status_code >= 300:
            return SendResult(success=False, error=f"HTTP {resp.status_code}: {resp.text[:200]}")
        return SendResult(success=True, message_id=str(message_id))

    async def delete_message(self, chat_id: str, message_id: str) -> bool:
        """Delete one of our own messages (Talk ``delete-messages``; time-limited)."""
        if not self._http_client:
            return False
        try:
            resp = await self._http_client.delete(
                f"/ocs/v2.php/apps/spreed/api/v1/chat/{chat_id}/{message_id}",
                params={"format": "json"},
            )
        except Exception as e:
            logger.debug("[%s] delete %s/%s failed: %s", self.name, chat_id, message_id, e)
            return False
        return resp.status_code in (200, 202)

    # -- Outbound media: upload to our own Files, then share into the room --

    async def send_image(
        self, chat_id: str, image_url: str, caption: Optional[str] = None,
        reply_to: Optional[str] = None, metadata: Optional[Dict[str, Any]] = None,
    ) -> SendResult:
        data = await self._fetch_url(image_url)
        if data is None:
            # Could not fetch it: the base posts the URL as text, still useful.
            return await super().send_image(chat_id, image_url, caption, reply_to, metadata)
        name = os.path.basename(image_url.split("?", 1)[0]) or "image.png"
        return await self._share_bytes(chat_id, data, name, caption, reply_to, metadata=metadata)

    async def send_animation(
        self, chat_id: str, animation_url: str, caption: Optional[str] = None,
        reply_to: Optional[str] = None, metadata: Optional[Dict[str, Any]] = None,
    ) -> SendResult:
        return await self.send_image(chat_id, animation_url, caption, reply_to, metadata)

    async def send_image_file(
        self, chat_id: str, image_path: str, caption: Optional[str] = None,
        reply_to: Optional[str] = None, metadata: Optional[Dict[str, Any]] = None, **kwargs,
    ) -> SendResult:
        return await self._share_path(chat_id, image_path, caption, reply_to, metadata=metadata)

    async def send_video(
        self, chat_id: str, video_path: str, caption: Optional[str] = None,
        reply_to: Optional[str] = None, metadata: Optional[Dict[str, Any]] = None, **kwargs,
    ) -> SendResult:
        return await self._share_path(chat_id, video_path, caption, reply_to, metadata=metadata)

    async def send_document(
        self, chat_id: str, file_path: str, caption: Optional[str] = None,
        file_name: Optional[str] = None, reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None, **kwargs,
    ) -> SendResult:
        return await self._share_path(chat_id, file_path, caption, reply_to, name=file_name,
                                      metadata=metadata)

    async def send_voice(
        self, chat_id: str, audio_path: str, caption: Optional[str] = None,
        reply_to: Optional[str] = None, metadata: Optional[Dict[str, Any]] = None, **kwargs,
    ) -> SendResult:
        """A Talk voice message (inline player), not a plain audio attachment."""
        return await self._share_path(chat_id, audio_path, caption, reply_to, voice=True,
                                      metadata=metadata)

    async def _share_path(
        self, chat_id: str, path: str, caption: Optional[str], reply_to: Optional[str],
        *, name: Optional[str] = None, voice: bool = False,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> SendResult:
        if path.startswith("file://"):
            from urllib.request import url2pathname
            path = url2pathname(path[7:])
        try:
            with open(path, "rb") as fh:
                data = fh.read()
        except OSError as e:
            return SendResult(success=False, error=f"cannot read {path}: {e}")
        return await self._share_bytes(
            chat_id, data, name or os.path.basename(path), caption, reply_to, voice=voice,
            metadata=metadata)

    async def _share_bytes(
        self, chat_id: str, data: bytes, filename: str, caption: Optional[str],
        reply_to: Optional[str], *, voice: bool = False,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> SendResult:
        """Upload *data* to ``Talk/`` in the agent's Files and share it into the room.

        Talk shows a share to the conversation (shareType 10) as a file message
        with the caption as its text; ``talkMetaData.messageType`` makes it a
        voice message. Federated rooms cannot receive shares, see
        ``_federated_media``.
        """
        if not self._http_client:
            return SendResult(success=False, error="HTTP client not initialized")
        room = self._room_meta.get(chat_id) or {}
        federated = bool(room.get("remoteServer"))
        if federated and self._federated_media != "link":
            return SendResult(
                success=False,
                error="file sharing is not available in federated conversations "
                      f"(set {FEDERATED_MEDIA_ENV}=link to post an expiring link)",
            )
        safe = re.sub(r"[^\w.\- ]", "_", filename).strip() or "file"
        # A unique folder per upload, so the room sees the file's real name.
        folder = f"{OUTBOUND_FOLDER}/{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6]}"
        remote = f"{folder}/{safe}"
        dav = f"/remote.php/dav/files/{quote(self._username)}"
        try:
            await self._http_client.request("MKCOL", f"{dav}/{OUTBOUND_FOLDER}")  # 405 = exists
            await self._http_client.request("MKCOL", f"{dav}/{quote(folder)}")
            put = await self._http_client.put(f"{dav}/{quote(remote)}", content=data)
        except Exception as e:
            return SendResult(success=False, error=f"upload failed: {e}", retryable=True)
        if put.status_code not in (201, 204):
            return SendResult(success=False, error=f"upload failed: HTTP {put.status_code}")

        if federated:
            return await self._post_public_link(chat_id, remote, safe, caption, reply_to, metadata)

        meta: Dict[str, Any] = {}
        if caption:
            meta["caption"] = caption
        if reply_to:
            try:
                meta["replyTo"] = int(reply_to)
            except (TypeError, ValueError):
                pass
        if voice:
            meta["messageType"] = "voice-message"
        thread = self._thread_param(metadata)
        if thread:
            meta["threadId"] = thread
        form = {"shareType": "10", "shareWith": chat_id, "path": f"/{remote}"}
        if meta:
            form["talkMetaData"] = json.dumps(meta)
        try:
            resp = await self._http_client.post(
                "/ocs/v2.php/apps/files_sharing/api/v1/shares",
                params={"format": "json"}, data=form,
            )
        except Exception as e:
            return SendResult(success=False, error=f"share failed: {e}", retryable=True)
        if resp.status_code >= 300:
            return SendResult(success=False, error=f"share failed: HTTP {resp.status_code}: {resp.text[:200]}")
        return SendResult(success=True)

    async def _post_public_link(
        self, chat_id: str, remote: str, name: str, caption: Optional[str], reply_to: Optional[str],
        metadata: Optional[Dict[str, Any]] = None,
    ) -> SendResult:
        expire = time.strftime("%Y-%m-%d", time.gmtime(time.time() + FEDERATED_LINK_DAYS * 86400))
        try:
            resp = await self._http_client.post(
                "/ocs/v2.php/apps/files_sharing/api/v1/shares",
                params={"format": "json"},
                data={"shareType": "3", "path": f"/{remote}", "permissions": "1", "expireDate": expire},
            )
            url = resp.json().get("ocs", {}).get("data", {}).get("url") if resp.status_code < 300 else None
        except Exception as e:
            return SendResult(success=False, error=f"link share failed: {e}", retryable=True)
        if not url:
            return SendResult(success=False, error=f"link share failed: HTTP {resp.status_code}")
        text = f"{caption}\n" if caption else ""
        text += f"📎 {name} (link expires {expire}): {url}"
        return await self.send(chat_id, text, reply_to=reply_to, metadata=metadata)

    async def _fetch_url(self, url: str) -> Optional[bytes]:
        """Fetch an image URL (http/https/file) for re-upload; None on any failure."""
        if url.startswith("file://") or os.path.isabs(url):
            path = url[7:] if url.startswith("file://") else url
            try:
                with open(path, "rb") as fh:
                    return fh.read(MAX_INBOUND_FILE_BYTES + 1)[:MAX_INBOUND_FILE_BYTES]
            except OSError:
                return None
        if not url.startswith(("http://", "https://")):
            return None
        try:
            async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as client:
                resp = await client.get(url)
        except Exception:
            return None
        if resp.status_code != 200 or len(resp.content) > MAX_INBOUND_FILE_BYTES:
            return None
        return resp.content

    async def send_typing(self, chat_id: str, metadata=None) -> None:
        # Talk's typing signal requires the signaling backend, not the
        # plain chat REST surface this adapter uses. No-op.
        return

    # -- Working-indicator reaction (real-user POST/DELETE reaction API) ----

    async def _add_reaction(self, chat_id: str, message_id: str, emoji: str) -> bool:
        """Add *emoji* to the triggering message. Never raises -- a failed
        reaction must not break the turn it is only decorating."""
        if not emoji or not self._http_client:
            return False
        try:
            resp = await self._http_client.post(
                f"/ocs/v2.php/apps/spreed/api/v1/reaction/{chat_id}/{message_id}",
                params={"format": "json"},
                json={"reaction": emoji},
            )
            if resp.status_code not in (200, 201):
                logger.warning(
                    "[%s] Could not add working reaction %s to %s/%s: HTTP %s",
                    self.name, emoji, chat_id, message_id, resp.status_code,
                )
                return False
            return True
        except Exception as e:
            logger.warning(
                "[%s] Could not add working reaction %s to %s/%s: %s",
                self.name, emoji, chat_id, message_id, e,
            )
            return False

    async def _remove_reaction(self, chat_id: str, message_id: str, emoji: Optional[str] = None) -> bool:
        """Remove *emoji* (defaults to the configured working reaction) from a message."""
        emoji = emoji or self._working_reaction
        if not emoji or not self._http_client:
            return False
        try:
            resp = await self._http_client.request(
                "DELETE",
                f"/ocs/v2.php/apps/spreed/api/v1/reaction/{chat_id}/{message_id}",
                params={"format": "json"},
                json={"reaction": emoji},
            )
            if resp.status_code not in (200,):
                logger.warning(
                    "[%s] Could not remove working reaction %s from %s/%s: HTTP %s",
                    self.name, emoji, chat_id, message_id, resp.status_code,
                )
                return False
            return True
        except Exception as e:
            logger.warning(
                "[%s] Could not remove working reaction %s from %s/%s: %s",
                self.name, emoji, chat_id, message_id, e,
            )
            return False

    async def on_processing_start(self, event: MessageEvent) -> None:
        """Add the working-indicator reaction once the agent decides to answer.

        ``handle_message`` has already applied the room-mode/mention filter
        by the time this fires, so every call here corresponds to a real,
        about-to-be-processed turn -- never a message that was silently
        skipped (watch-mode buffering, an un-mentioned message in a
        mention-mode room, an echo of our own send).
        """
        if not self._working_reaction:
            return
        chat_id = getattr(event.source, "chat_id", None)
        message_id = event.message_id
        if not chat_id or not message_id:
            return
        await self._add_reaction(chat_id, message_id, self._working_reaction)

    async def on_processing_complete(self, event: MessageEvent, outcome: ProcessingOutcome) -> None:
        """Remove the working-indicator reaction once processing is done, any outcome."""
        if not self._working_reaction:
            return
        chat_id = getattr(event.source, "chat_id", None)
        message_id = event.message_id
        if not chat_id or not message_id:
            return
        await self._remove_reaction(chat_id, message_id, self._working_reaction)

    async def get_chat_info(self, chat_id: str) -> Dict[str, Any]:
        room = self._room_meta.get(chat_id, {})
        return {
            "name": room.get("displayName") or room.get("name") or chat_id,
            "type": _room_chat_type(room.get("type")),
            "chat_id": chat_id,
        }


# ---------------------------------------------------------------------------
# Standalone registration helpers (consumed by the repo root's dispatcher)
# ---------------------------------------------------------------------------


def _env_enablement() -> Optional[dict]:
    server = os.getenv("NEXTCLOUD_TALK_SERVER_URL", "").strip()
    app_password = _secret("NEXTCLOUD_TALK_APP_PASSWORD", "").strip()
    if not server or not app_password:
        return None
    seed: dict = {
        "server_url": server.rstrip("/"),
        "username": os.getenv("NEXTCLOUD_TALK_USERNAME", DEFAULT_USERNAME),
        "display_name": os.getenv("NEXTCLOUD_TALK_DISPLAY_NAME", DEFAULT_DISPLAY_NAME),
    }
    home = os.getenv("NEXTCLOUD_TALK_HOME_CHANNEL", "").strip()
    if home:
        seed["home_channel"] = {
            "chat_id": home,
            "name": os.getenv("NEXTCLOUD_TALK_HOME_CHANNEL_NAME", home),
        }
        home_thread = _thread_id_int(os.getenv(HOME_THREAD_ENV, ""))
        if home_thread:
            seed["home_channel"]["thread_id"] = str(home_thread)
    return seed


async def _standalone_send(
    pconfig,
    chat_id: str,
    message: str,
    *,
    thread_id: Optional[str] = None,
    media_files: Optional[List[str]] = None,
    force_document: bool = False,
) -> Dict[str, Any]:
    """Out-of-process send for cron / send_message_tool when no live adapter exists."""
    if not HTTPX_AVAILABLE:
        return {"error": "nctalk standalone send: httpx not installed"}

    extra = getattr(pconfig, "extra", {}) or {}
    server = (extra.get("server_url") or os.getenv("NEXTCLOUD_TALK_SERVER_URL", "")).rstrip("/")
    username = extra.get("username") or os.getenv("NEXTCLOUD_TALK_USERNAME", DEFAULT_USERNAME)
    app_password = extra.get("app_password") or _secret("NEXTCLOUD_TALK_APP_PASSWORD", "")
    if not server or not app_password:
        return {"error": "nctalk standalone send: not configured"}

    headers = _ocs_headers(username, app_password)
    chunks = _chunk_message(textstyle.undash(message))
    # Hermes resolves the target's thread (for cron: <HOME_CHANNEL>_THREAD_ID)
    # and hands it over here; every chunk goes into that thread.
    thread = _thread_id_int(thread_id)
    try:
        async with httpx.AsyncClient(base_url=server, headers=headers, timeout=15.0) as client:
            last_id = None
            for chunk in chunks:
                body: Dict[str, Any] = {"message": chunk}
                if thread:
                    body["threadId"] = thread
                resp = await client.post(
                    f"/ocs/v2.php/apps/spreed/api/v1/chat/{chat_id}",
                    params={"format": "json"},
                    json=body,
                )
                if resp.status_code >= 300:
                    return {"error": f"HTTP {resp.status_code}: {resp.text[:200]}"}
                try:
                    last_id = resp.json().get("ocs", {}).get("data", {}).get("id")
                except Exception:
                    pass
        return {"success": True, "platform": PLATFORM_NAME, "chat_id": chat_id, "message_id": last_id}
    except Exception as e:
        return {"error": f"nctalk standalone send failed: {e}"}


def register(ctx) -> None:
    """Standalone plugin entry point for user-mode-only deployments.

    The combined repo's root ``register(ctx)`` picks between this module
    and ``bot/adapter.py`` automatically; this function exists so the
    ``user/`` directory also works as a self-contained plugin on its own
    (copy it to ``~/.hermes/plugins/nctalk_user/`` with its own
    ``plugin.yaml`` if you only want the real-user connector).
    """
    ctx.register_platform(
        name=PLATFORM_NAME,
        label="Nextcloud Talk",
        adapter_factory=lambda cfg: NextcloudTalkUserAdapter(cfg),
        check_fn=check_requirements,
        validate_config=validate_config,
        is_connected=is_connected,
        required_env=["NEXTCLOUD_TALK_SERVER_URL", "NEXTCLOUD_TALK_APP_PASSWORD"],
        install_hint="pip install httpx   # already a Hermes dependency",
        env_enablement_fn=_env_enablement,
        cron_deliver_env_var="NEXTCLOUD_TALK_HOME_CHANNEL",
        standalone_sender_fn=_standalone_send,
        allowed_users_env="NEXTCLOUD_TALK_ALLOWED_USERS",
        allow_all_env="NEXTCLOUD_TALK_ALLOW_ALL_USERS",
        max_message_length=MAX_MESSAGE_LENGTH,
        emoji="☁",
        pii_safe=False,
        allow_update_command=True,
        platform_hint=(
            "You are on Nextcloud Talk. Messages support basic Markdown "
            "(bold/italic/inline code) and @mentions use Talk's "
            '@"Display Name" syntax for names with spaces. Keep '
            "responses concise -- up to 32000 characters per message, "
            "longer replies are sent as multiple messages. " + THREADS_HINT + " " + POLLS_HINT
        ),
    )
