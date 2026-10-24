"""Pure, dependency-free logic for the Nextcloud Talk Hermes adapter.

Deliberately has zero imports from aiohttp or the hermes-agent gateway so it
can be unit-tested in complete isolation (no running gateway, no network).
``adapter.py`` (the actual BasePlatformAdapter subclass) imports only from
this module for anything security- or correctness-sensitive: signature
verification, mention detection, payload parsing, dedupe, room-mode
resolution, and the room-buffer / quiet-reply sentinel used by watch mode.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import re
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

# ---------------------------------------------------------------------------
# HMAC signature (Nextcloud Talk bot scheme: HMAC-SHA256 over RANDOM + BODY)
# ---------------------------------------------------------------------------


def compute_signature(secret: str, random: str, body: bytes) -> str:
    """Return the hex HMAC-SHA256 digest of ``random + body`` under ``secret``.

    Used both to verify Nextcloud's inbound webhook signature and to sign
    our own outbound bot replies -- the scheme is identical in both
    directions (docs: nextcloud-talk.readthedocs.io/en/latest/bots/).
    """
    mac = hmac.new(secret.encode("utf-8"), random.encode("utf-8") + body, hashlib.sha256)
    return mac.hexdigest()


def verify_signature(secret: str, random: str, body: bytes, signature: str) -> bool:
    """Constant-time check of an inbound webhook signature."""
    if not secret or not random or not signature:
        return False
    expected = compute_signature(secret, random, body)
    return hmac.compare_digest(expected, signature.lower())


def new_random(n: int = 64) -> str:
    """64-char random string for signing our own outbound requests."""
    return uuid.uuid4().hex + uuid.uuid4().hex[: n - 32]


# ---------------------------------------------------------------------------
# Webhook payload parsing
# ---------------------------------------------------------------------------


@dataclass
class ParsedMessage:
    event_type: str
    room_token: str
    room_name: str
    actor_type: str
    actor_id: str
    actor_name: str
    message_id: str
    text: str = ""
    parameters: Dict[str, Any] = field(default_factory=dict)


def parse_webhook_body(data: Dict[str, Any]) -> Optional[ParsedMessage]:
    """Parse one Nextcloud Talk bot webhook JSON body.

    Returns ``None`` if the body is missing required fields (malformed /
    not a message we understand). ``event_type`` is the raw Activity
    Streams ``type`` field ("Create", "Like", "Undo", "Join", "Leave", ...);
    callers decide what to do with non-"Create" events.
    """
    if not isinstance(data, dict):
        return None
    event_type = data.get("type")
    target = data.get("object") or {}
    actor = data.get("actor") or {}
    room = data.get("target") or {}
    room_token = room.get("id")
    if not event_type or not room_token:
        return None

    text = ""
    parameters: Dict[str, Any] = {}
    content_raw = target.get("content")
    if isinstance(content_raw, str) and content_raw:
        try:
            content = json.loads(content_raw)
            text = str(content.get("message", "") or "")
            params = content.get("parameters")
            if isinstance(params, dict):
                parameters = params
        except (ValueError, TypeError):
            text = content_raw

    return ParsedMessage(
        event_type=str(event_type),
        room_token=str(room_token),
        room_name=str(room.get("name") or room_token),
        actor_type=str(actor.get("type") or ""),
        actor_id=str(actor.get("id") or ""),
        actor_name=str(actor.get("name") or actor.get("id") or "unknown"),
        message_id=str(target.get("id") or ""),
        text=text,
        parameters=parameters,
    )


def resolve_mentions(text: str, parameters: Dict[str, Any]) -> str:
    """Replace ``{mention-xxx}`` placeholders with a readable ``@Name``.

    Nextcloud substitutes mention/call/file placeholders with a parameters
    map keyed by the same placeholder name. Unknown placeholders are left
    as-is rather than dropped, so nothing silently disappears.
    """
    if not parameters:
        return text

    def _sub(match: "re.Match[str]") -> str:
        key = match.group(1)
        param = parameters.get(key)
        if isinstance(param, dict) and param.get("name"):
            return f"@{param['name']}"
        return match.group(0)

    return re.sub(r"\{([a-zA-Z0-9_-]+)\}", _sub, text)


# ---------------------------------------------------------------------------
# Mention ("is this message directed at the bot?") detection
# ---------------------------------------------------------------------------

DEFAULT_MENTION_TRIGGERS = ("jarvis",)


def is_mentioned(
    resolved_text: str,
    parameters: Dict[str, Any],
    triggers: "tuple[str, ...] | list[str]" = DEFAULT_MENTION_TRIGGERS,
) -> bool:
    """True if *resolved_text* (or a structured mention parameter) targets the bot.

    Nextcloud Talk's docs do not confirm a structured mention type for bots
    (only user/call/federated_user mentions are documented), so this checks
    BOTH:
      1. Any mention-style parameter whose resolved ``name`` case-insensitively
         matches a trigger (covers a future/undocumented bot-mention type).
      2. A plain word-boundary match of a trigger in the resolved text
         (covers the common case: the user just types "@Jarvis ..." as
         literal text because Talk's compose box cannot target a bot).
    """
    triggers_lower = {t.lower() for t in triggers if t}
    if not triggers_lower:
        return False

    for param in (parameters or {}).values():
        if isinstance(param, dict):
            name = str(param.get("name", "")).lower()
            if name in triggers_lower:
                return True

    lowered = resolved_text.lower()
    for trig in triggers_lower:
        if re.search(r"(?<![\w@])@?" + re.escape(trig) + r"\b", lowered):
            return True
    return False


# ---------------------------------------------------------------------------
# Quiet-reply sentinel (watch mode: "say nothing" without special-casing the
# agent loop -- the adapter's send() intercepts this exact token).
# ---------------------------------------------------------------------------

QUIET_SENTINEL = "NO_REPLY"


def is_quiet_reply(content: Optional[str]) -> bool:
    """True if *content* is the agent's "nothing worth saying" sentinel.

    Matches the sentinel alone (optionally with surrounding whitespace) so a
    model that wraps it in a stray newline or trailing punctuation still
    gets silenced; anything with additional real content is treated as a
    genuine reply.
    """
    if content is None:
        return True
    stripped = content.strip().strip(".! ")
    return stripped == "" or stripped.upper() == QUIET_SENTINEL


# ---------------------------------------------------------------------------
# Dedupe cache (webhook retries / at-least-once delivery)
# ---------------------------------------------------------------------------


class DedupeCache:
    """TTL-bounded set of seen message ids, mirroring wecom_callback's pattern."""

    def __init__(self, ttl_seconds: float = 300.0, max_size: int = 2000) -> None:
        self._ttl = ttl_seconds
        self._max_size = max_size
        self._seen: Dict[str, float] = {}

    def seen_before(self, message_id: str, *, now: Optional[float] = None) -> bool:
        """Record *message_id*; return True if it was already seen within the TTL."""
        if not message_id:
            return False
        now = time.time() if now is None else now
        last = self._seen.get(message_id)
        if last is not None and now - last < self._ttl:
            return True
        self._seen[message_id] = now
        if len(self._seen) > self._max_size:
            cutoff = now - self._ttl
            self._seen = {k: v for k, v in self._seen.items() if v > cutoff}
        return False


# ---------------------------------------------------------------------------
# Per-room mode resolution
# ---------------------------------------------------------------------------

VALID_MODES = ("mention", "watch", "free")
DEFAULT_MODE = "mention"
DEFAULT_WATCH_INTERVAL_SECONDS = 300


def resolve_room_mode(rooms_cfg: Dict[str, Any], room_token: str) -> Dict[str, Any]:
    """Resolve the configured mode (+ options) for *room_token*.

    Rooms not present in config default to "mention" -- the safest option
    (Nextcloud-side ``talk:bot:setup`` is what actually grants the bot
    visibility into a room; this config only chooses its behaviour once
    enabled there).
    """
    entry = (rooms_cfg or {}).get(room_token) or {}
    mode = str(entry.get("mode", DEFAULT_MODE)).lower()
    if mode not in VALID_MODES:
        mode = DEFAULT_MODE
    return {
        "mode": mode,
        "watch_interval_seconds": int(
            entry.get("watch_interval_seconds", DEFAULT_WATCH_INTERVAL_SECONDS)
        ),
    }


# ---------------------------------------------------------------------------
# Per-room rolling context buffer
# ---------------------------------------------------------------------------


@dataclass
class BufferedMessage:
    ts: float
    actor_name: str
    text: str
    watch_seen: bool = False
    actor_id: str = ""


class RoomBuffer:
    """Rolling window of recent messages in one Talk room.

    Bots cannot fetch Talk history, so this is the ONLY source of
    "what was said before" context -- built entirely from messages received
    over the webhook while the bot has been enabled in this room.
    """

    def __init__(self, max_messages: int = 40, max_age_seconds: float = 4 * 3600) -> None:
        self._max_messages = max_messages
        self._max_age = max_age_seconds
        self._messages: List[BufferedMessage] = []

    def add(
        self, actor_name: str, text: str, *, actor_id: str = "", now: Optional[float] = None
    ) -> None:
        now = time.time() if now is None else now
        self._messages.append(
            BufferedMessage(ts=now, actor_name=actor_name, text=text, actor_id=actor_id)
        )
        self._trim(now)

    def _trim(self, now: float) -> None:
        cutoff = now - self._max_age
        self._messages = [m for m in self._messages if m.ts >= cutoff]
        if len(self._messages) > self._max_messages:
            self._messages = self._messages[-self._max_messages :]

    def render_context(self, *, exclude_last: bool = True) -> str:
        """Render all-but-the-most-recent buffered message as context lines."""
        msgs = self._messages[:-1] if exclude_last and self._messages else self._messages
        return "\n".join(f"{m.actor_name}: {m.text}" for m in msgs)

    def unseen_for_watch(self) -> List[BufferedMessage]:
        return [m for m in self._messages if not m.watch_seen]

    def mark_watch_seen(self) -> None:
        for m in self._messages:
            m.watch_seen = True


# ---------------------------------------------------------------------------
# Watch-mode rate limiting
# ---------------------------------------------------------------------------


class WatchRateLimiter:
    """Hard cap on watch-mode dispatches per room, independent of the poll interval.

    The poll interval already bounds dispatch frequency in the common case;
    this is a defensive second layer in case of a misconfigured short
    interval or a bug that re-fires the loop.
    """

    def __init__(self, max_per_hour: int = 12) -> None:
        self._max_per_hour = max_per_hour
        self._timestamps: Dict[str, List[float]] = {}

    def allow(self, room_token: str, *, now: Optional[float] = None) -> bool:
        now = time.time() if now is None else now
        window_start = now - 3600
        hist = [t for t in self._timestamps.get(room_token, []) if t >= window_start]
        if len(hist) >= self._max_per_hour:
            self._timestamps[room_token] = hist
            return False
        hist.append(now)
        self._timestamps[room_token] = hist
        return True


# ---------------------------------------------------------------------------
# Sender allowlist (watch-mode digests)
# ---------------------------------------------------------------------------


def parse_allowlist(raw: Optional[str]) -> frozenset:
    """Parse a comma-separated user-id allowlist to lowercase ids."""
    return frozenset(s.strip().lower() for s in str(raw or "").split(",") if s.strip())


def sender_allowed(actor_id: Optional[str], allowlist: frozenset, allow_all: bool) -> bool:
    """True when ``allow_all`` is set or ``actor_id`` is on the allowlist."""
    if allow_all:
        return True
    uid = (actor_id or "").strip().lower()
    return bool(uid) and uid in allowlist
