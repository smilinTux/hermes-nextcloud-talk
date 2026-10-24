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
    NEXTCLOUD_TALK_ALLOWED_USERS           Allowlist (gateway auth integration)
    NEXTCLOUD_TALK_ALLOW_ALL_USERS         Allow any user -- dev only
    NEXTCLOUD_TALK_HOME_CHANNEL            Default room token for cron delivery
    NEXTCLOUD_TALK_HOME_CHANNEL_NAME       Human label for the home channel
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

Active listening and the agent-to-agent loop guard only matter in a room
with more than one participant (DMs are always addressed to this account,
so neither applies there).
"""

import asyncio
import base64
import logging
import os
import re
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

try:
    import httpx
    HTTPX_AVAILABLE = True
except ImportError:
    HTTPX_AVAILABLE = False
    httpx = None  # type: ignore[assignment]

from gateway.config import Platform, PlatformConfig
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

from gateway.platforms.base import (
    BasePlatformAdapter,
    MessageEvent,
    MessageType,
    ProcessingOutcome,
    SendResult,
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
    message_parameters = message_parameters or {}
    for param in message_parameters.values():
        if not isinstance(param, dict):
            continue
        if param.get("type") in ("user", "call") and param.get("id") == own_user_id:
            return True

    text = text or ""
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
                await self._discover_rooms()
                await self._discover_invitations()
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
        if message.get("messageType") not in (None, "comment", "voice-message"):
            # Skip system messages (join/leave/call events etc).
            return

        actor = _extract_actor(message)
        if actor["id"] == self._username and actor.get("type") != "federated_users":
            return  # echo of our own send

        text = (message.get("message") or "").strip()
        if not text:
            return

        room = self._room_meta.get(token, {})
        room_type = room.get("type")
        display_name = room.get("displayName") or room.get("name")
        chat_type = _room_chat_type(room_type)
        mode = ROOM_MODE_FREE if chat_type == "dm" else _room_mode(
            token, display_name, self._room_modes
        )

        mentioned = _is_mention(
            text, message.get("messageParameters"), self._username, self._display_name
        )
        text = _resolve_mention_placeholders(text, message.get("messageParameters"))

        source = self.build_source(
            chat_id=token,
            chat_name=display_name,
            chat_type=chat_type,
            user_id=actor["id"],
            user_name=actor.get("display_name"),
            message_id=msg_id,
        )

        event = MessageEvent(
            text=text,
            message_type=MessageType.TEXT,
            source=source,
            message_id=msg_id,
            raw_message=message,
            timestamp=self._parse_timestamp(message.get("timestamp")),
        )

        from_agent = _is_agent_actor(actor, self._agent_users)
        if from_agent:
            self._agent_chain[token] = self._agent_chain.get(token, 0) + 1
        else:
            self._agent_chain[token] = 0

        if mode == ROOM_MODE_WATCH:
            self._watch_buffers.setdefault(token, []).append(event)
            return
        if mode == ROOM_MODE_MENTION and not mentioned:
            self._remember(token, actor, text)
            return
        if from_agent and chat_type != "dm":
            if not mentioned:
                # free means free for humans; another agent must still address us
                self._remember(token, actor, text)
                return
            if self._agent_chain[token] > self._agent_chain_limit:
                logger.info(
                    "[%s] room %s: %d agent messages in a row with no human in "
                    "between, ignoring %s until a human speaks",
                    self.name, token, self._agent_chain[token], actor.get("id"),
                )
                self._remember(token, actor, text)
                return

        backlog = self._room_context.pop(token, [])
        if backlog:
            event.text = _with_room_context(text, backlog)
        if chat_type != "dm" and (self._room_rules_all or token in self._room_modes):
            # Whoever creates a room writes its description; only rooms the
            # operator listed (or an explicit opt-in) may hand the agent rules.
            event.text = _with_room_rules(event.text, room.get("description"))
        await self.handle_message(event)

    def _remember(self, token: str, actor: Dict[str, Optional[str]], text: str) -> None:
        """Keep an unanswered message as context for the next reply in this room."""
        if self._context_messages <= 0:
            return
        if not self._sender_allowed(actor.get("id")):
            # Same filter as watch digests: an unlisted sender's text never
            # rides into a later turn as room context.
            return
        name = actor.get("display_name") or actor.get("id") or "someone"
        backlog = self._room_context.setdefault(token, [])
        backlog.append(f"{name}: {text}")
        self._room_context[token] = _trim_backlog(backlog, self._context_messages)

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

    def _sender_allowed(self, user_id: Optional[str]) -> bool:
        if self._allow_all_users:
            return True
        uid = (user_id or "").strip().lower()
        return bool(uid) and uid in self._allowed_users

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

        chunks = _chunk_message(content)
        last_id: Optional[str] = None
        for i, chunk in enumerate(chunks):
            body: Dict[str, Any] = {"message": chunk}
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
    chunks = _chunk_message(message)
    try:
        async with httpx.AsyncClient(base_url=server, headers=headers, timeout=15.0) as client:
            last_id = None
            for chunk in chunks:
                resp = await client.post(
                    f"/ocs/v2.php/apps/spreed/api/v1/chat/{chat_id}",
                    params={"format": "json"},
                    json={"message": chunk},
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
            '@"Display Name" syntax for names with spaces. There are no '
            "threads; replies are flat in the room timeline. Keep "
            "responses concise -- up to 32000 characters per message, "
            "longer replies are sent as multiple messages."
        ),
    )
