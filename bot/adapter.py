"""Nextcloud Talk bot adapter for the Hermes gateway (webhook bot mode).

Deployed as a drop-in plugin: ``~/.hermes/plugins/nctalk_bot/``
(``__init__.py`` + this file + ``core.py``, copied verbatim from this repo),
or installed in one piece via the combined repo root (see the top-level
README). It follows the checklist in Hermes's own
``website/docs/developer-guide/adding-platform-adapters.md``, modelled closely
on ``gateway/platforms/wecom_callback.py`` (callback/webhook pattern: receive
+ immediately ack, reply later via a proactive signed POST).

Security model: the ONLY gate on who can reach this bot is (a) which Talk
rooms a moderator has run ``occ talk:bot:setup`` in (Nextcloud-side), and
(b) the HMAC secret shared between Nextcloud and this adapter. There is no
per-user allowlist here (mirrors how Platform.WEBHOOK is already treated in
``gateway/run.py::_is_user_authorized`` -- HMAC auth IS the authorization).

Three per-room modes (config.yaml ``nctalk.extra.rooms.<token>.mode``):
  - "mention" (default): reply only when @Jarvis-style text is detected.
  - "watch": buffer messages; a periodic background task decides whether to
    say anything (summary / unanswered question / action items), rate-limited.
  - "free": reply to every message (intended for a Jarvis-only room).
All three modes feed every inbound message into a per-room rolling context
buffer (``nctalk_core.RoomBuffer``) -- bots cannot fetch Talk history,
so this buffer is the only memory of "what was said before" the bot was
@mentioned.

Inbound delivery hardening (explicit body-size cap, a nonce-replay cache
ahead of payload parsing) was ported, in concept and independently
reimplemented against this file's own ``DedupeCache``/constant-naming
conventions, from the webhook admission design in
`Cliff3c/hermes-nextcloud-talk <https://github.com/Cliff3c/hermes-nextcloud-talk>`_
(MIT License, Copyright (c) 2026 Cliff3c) -- see THIRD_PARTY.md and the
CHANGELOG entry for this release. One piece of that design does not apply
here and was deliberately left out rather than added as dead code: Cliff3c's
adapter parses the outbound OCS *send* response (JSON or, on Nextcloud 32,
XML) to recover a message id, so it hardens that XML parse against DTDs.
This adapter's own ``_post_message`` never parses the send response body at
all -- it only checks the HTTP status and keeps the raw text -- so there is
no XML parser anywhere in this file's request path to harden.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import socket as _socket
from typing import Any, Dict, List, Optional

try:
    from aiohttp import web, ClientSession, ClientTimeout

    AIOHTTP_AVAILABLE = True
except ImportError:  # pragma: no cover
    web = None  # type: ignore[assignment]
    ClientSession = None  # type: ignore[assignment]
    ClientTimeout = None  # type: ignore[assignment]
    AIOHTTP_AVAILABLE = False

from gateway.config import Platform, PlatformConfig
from gateway.platforms.base import BasePlatformAdapter, MessageEvent, MessageType, SendResult
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


try:  # drop-in plugin layout: core.py is this file's sibling in the
    # package (~/.hermes/plugins/nctalk_bot/{__init__,adapter,core}.py)
    from .core import (
        DedupeCache,
        RoomBuffer,
        WatchRateLimiter,
        parse_allowlist,
        sender_allowed,
        compute_signature,
        is_mentioned,
        is_quiet_reply,
        new_random,
        parse_webhook_body,
        resolve_mentions,
        resolve_room_mode,
        verify_signature,
    )
except ImportError:  # pragma: no cover - local dev/test fallback (flat sys.path)
    from core import (
        DedupeCache,
        RoomBuffer,
        WatchRateLimiter,
        parse_allowlist,
        sender_allowed,
        compute_signature,
        is_mentioned,
        is_quiet_reply,
        new_random,
        parse_webhook_body,
        resolve_mentions,
        resolve_room_mode,
        verify_signature,
    )

logger = logging.getLogger(__name__)

DEFAULT_HOST = "127.0.0.1"  # inert by default; set to a private/VPN interface IP via env
DEFAULT_PORT = 18797
DEFAULT_PATH = "/webhooks/nextcloud-talk"
MAX_MESSAGE_LENGTH = 32000
DEDUPE_TTL_SECONDS = 300
WATCH_POLL_INTERVAL_SECONDS = 15  # background task granularity; per-room interval governs cadence

# Inbound webhook hardening. Named/explicit versions of two admission checks
# -- a body-size ceiling and a nonce-replay cache ahead of payload parsing --
# whose shape is ported from Cliff3c/hermes-nextcloud-talk (MIT); see the
# module docstring above and THIRD_PARTY.md.
MAX_WEBHOOK_BODY_BYTES = 1024 * 1024  # 1 MiB; matches aiohttp's own default, made explicit
NONCE_REPLAY_TTL_SECONDS = 3600  # a signed X-Nextcloud-Talk-Random must never repeat

DEFAULT_WORKING_REACTION = "\U0001F440"  # 👀 -- visible "I'm on it" signal; Talk bots have no typing indicator
ERROR_REACTION = "⚠️"  # ⚠️ -- swapped in for 👀 on a failed turn; not configurable (always this)

_PLATFORM_HINT = (
    "You are chatting via a Nextcloud Talk bot. Messages may include "
    "multiple participants; your own prior replies are not distinguished from "
    "other messages in the room transcript. Markdown is supported. Messages over "
    "32,000 characters are split automatically. If you were invoked in 'watch' "
    "mode and have nothing worth adding, reply with exactly NO_REPLY and nothing "
    "else -- it will be silently discarded rather than posted."
)


def check_nctalk_requirements() -> bool:
    return AIOHTTP_AVAILABLE


class NextcloudTalkAdapter(BasePlatformAdapter):
    def __init__(self, config: PlatformConfig):
        # Platform("nctalk") rather than a Platform.NEXTCLOUD_TALK
        # attribute: upstream's enum has no such built-in member, and
        # Platform._missing_() synthesizes one for any plugin platform name
        # (see register() below -- this is the drop-in plugin path, no
        # core-tree edit to gateway/config.py).
        super().__init__(config, Platform(PLATFORM_NAME))
        extra = config.extra or {}
        self._host = str(extra.get("host") or DEFAULT_HOST)
        self._port = int(extra.get("port") or DEFAULT_PORT)
        self._path = str(extra.get("path") or DEFAULT_PATH)
        self._secret = str(extra.get("secret") or "")
        self._base_url = str(extra.get("base_url") or "").rstrip("/")
        self._rooms_cfg: Dict[str, Any] = extra.get("rooms") or {}
        self._allowed_users = parse_allowlist(
            extra.get("allowed_users") or os.getenv("NEXTCLOUD_TALK_ALLOWED_USERS", "")
        )
        self._allow_all_users = str(
            extra.get("allow_all_users") or os.getenv("NEXTCLOUD_TALK_ALLOW_ALL_USERS", "")
        ).strip().lower() in ("1", "true", "yes", "on")
        self._triggers = extra.get("mention_triggers") or ["jarvis"]
        self._context_max_messages = int(extra.get("context_window_messages", 40))
        self._context_max_age = float(extra.get("context_window_minutes", 240)) * 60

        # Visible working signal (Talk bots cannot show a typing indicator): a reaction
        # on the triggering message, added at turn start and swapped out at turn end.
        # ``extra.get(key, default)`` -- not ``or default`` -- so an explicitly-empty
        # env value (NEXTCLOUD_TALK_WORKING_REACTION="") disables it, distinct from the
        # key being absent entirely (which keeps the 👀 default). See _env_enablement().
        self._working_reaction = str(extra.get("working_reaction", DEFAULT_WORKING_REACTION))
        self._done_reaction = str(extra.get("done_reaction", "") or "")
        # Wired for BasePlatformAdapter.on_processing_complete's default reaction-ack
        # hook (Discord 👀/✅/❌ pattern) -- it calls self._add_reaction/_remove_reaction
        # below via these two class-level knobs; we only override on_processing_start.
        self._OK_EMOJI = self._done_reaction or None
        self._FAIL_EMOJI = ERROR_REACTION

        self._runner: Optional["web.AppRunner"] = None
        self._site = None
        self._app: Optional["web.Application"] = None
        self._http: Optional["ClientSession"] = None
        self._dedupe = DedupeCache(ttl_seconds=DEDUPE_TTL_SECONDS)
        # Separate from _dedupe above: _dedupe keys on the Talk *message id*
        # (parsed out of the JSON body, so it only ever runs once the body is
        # known-good JSON); this one keys on the raw inbound random nonce and
        # runs immediately after signature verification, before any parsing,
        # so a byte-for-byte replay of a previously-accepted signed request
        # (not just a duplicate Talk message) is rejected outright.
        self._seen_nonces = DedupeCache(ttl_seconds=NONCE_REPLAY_TTL_SECONDS)
        self._buffers: Dict[str, RoomBuffer] = {}
        self._watch_last_dispatch: Dict[str, float] = {}
        self._rate_limiter = WatchRateLimiter(max_per_hour=12)
        self._watch_task: Optional[asyncio.Task] = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def connect(self, *, is_reconnect: bool = False) -> bool:
        # is_reconnect: added to BasePlatformAdapter.connect()'s signature in a
        # later upstream release than this adapter originally targeted; this
        # webhook adapter has no reconnect-specific behavior (no persistent
        # outbound connection to resume), so the flag is accepted and ignored.
        if not check_nctalk_requirements():
            logger.warning("[NextcloudTalk] aiohttp not installed")
            return False
        if not self._secret:
            logger.error("[NextcloudTalk] NEXTCLOUD_TALK_SECRET not configured")
            return False
        if not self._base_url:
            logger.error("[NextcloudTalk] NEXTCLOUD_TALK_BASE_URL not configured")
            return False

        try:
            with _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM) as sock:
                sock.settimeout(1)
                sock.connect((self._host, self._port))
            logger.error("[NextcloudTalk] Port %s:%d already in use", self._host, self._port)
            return False
        except (ConnectionRefusedError, OSError):
            pass

        try:
            self._http = ClientSession(timeout=ClientTimeout(total=20.0))
            self._app = web.Application(client_max_size=MAX_WEBHOOK_BODY_BYTES)
            self._app.router.add_get(self._path + "/health", self._handle_health)
            self._app.router.add_post(self._path, self._handle_webhook)
            self._runner = web.AppRunner(self._app)
            await self._runner.setup()
            self._site = web.TCPSite(self._runner, self._host, self._port)
            await self._site.start()
            self._watch_task = asyncio.create_task(self._watch_loop())
            self._mark_connected()
            logger.info(
                "[NextcloudTalk] Listening on %s:%d%s (rooms configured: %d)",
                self._host, self._port, self._path, len(self._rooms_cfg),
            )
            return True
        except Exception:
            await self._cleanup()
            logger.exception("[NextcloudTalk] Failed to start")
            return False

    async def disconnect(self) -> None:
        self._running = False
        if self._watch_task:
            self._watch_task.cancel()
            try:
                await self._watch_task
            except asyncio.CancelledError:
                pass
            self._watch_task = None
        await self._cleanup()
        self._mark_disconnected()
        logger.info("[NextcloudTalk] Disconnected")

    async def _cleanup(self) -> None:
        self._site = None
        if self._runner:
            await self._runner.cleanup()
            self._runner = None
        self._app = None
        if self._http:
            await self._http.close()
            self._http = None

    async def get_chat_info(self, chat_id: str) -> Dict[str, Any]:
        return {"name": chat_id, "type": "group"}

    # ------------------------------------------------------------------
    # Visible working signal: reaction ack (Talk bots have no typing
    # indicator). on_processing_start adds the working reaction (default
    # 👀) the instant the gateway accepts a turn; on_processing_complete
    # (inherited from BasePlatformAdapter, driven by _OK_EMOJI/_FAIL_EMOJI
    # above) removes it and swaps in done/error once the turn finishes.
    # See PLAN.md "Visible working signal (reaction ack)".
    # ------------------------------------------------------------------

    def _reactions_enabled(self) -> bool:
        return bool(self._working_reaction)

    async def on_processing_start(self, event: "MessageEvent") -> None:
        if not self._reactions_enabled():
            return
        chat_id = getattr(getattr(event, "source", None), "chat_id", None)
        message_id = getattr(event, "message_id", None)
        if not chat_id or not message_id:
            return  # e.g. watch-mode's synthetic review event has no single triggering message
        await self._add_reaction(chat_id, message_id, self._working_reaction)

    async def _add_reaction(self, chat_id: str, message_id: str, emoji: str) -> None:
        """POST *emoji* as the bot's reaction to *message_id* in *chat_id*.

        Never raises -- a reaction failure must never block the reply (see
        module docstring / PLAN.md). Errors are logged and swallowed.
        """
        if not emoji or not self._http:
            return
        try:
            result = await self._post_reaction(chat_id, message_id, emoji)
            if not result.success:
                logger.warning(
                    "[NextcloudTalk] add reaction %r to %s/%s failed: %s",
                    emoji, chat_id, message_id, result.error,
                )
        except Exception:
            logger.warning(
                "[NextcloudTalk] add reaction %r to %s/%s raised", emoji, chat_id, message_id,
                exc_info=True,
            )

    async def _remove_reaction(self, chat_id: str, message_id: str) -> None:
        """DELETE the bot's own working reaction from *message_id*.

        Only one reaction is ever tracked per message (the configured
        working reaction), so no emoji argument is needed here -- matches
        BasePlatformAdapter.on_processing_complete's call signature. Never
        raises; same rationale as _add_reaction.
        """
        emoji = self._working_reaction
        if not emoji or not self._http:
            return
        try:
            result = await self._delete_reaction(chat_id, message_id, emoji)
            if not result.success:
                logger.warning(
                    "[NextcloudTalk] remove reaction %r from %s/%s failed: %s",
                    emoji, chat_id, message_id, result.error,
                )
        except Exception:
            logger.warning(
                "[NextcloudTalk] remove reaction %r from %s/%s raised", emoji, chat_id, message_id,
                exc_info=True,
            )

    async def _post_reaction(self, room_token: str, message_id: str, reaction: str) -> SendResult:
        random = new_random()
        # Per custom_apps/spreed/openapi-bots.json's bot-react operation: the signature
        # is HMAC-SHA256(secret, RANDOM + the reaction string itself) -- same "sign over
        # the text field, not the JSON body" pattern as outbound messages (_post_message).
        signature = compute_signature(self._secret, random, reaction.encode("utf-8"))
        url = f"{self._base_url}/ocs/v2.php/apps/spreed/api/v1/bot/{room_token}/reaction/{message_id}"
        headers = {
            "Content-Type": "application/json",
            "OCS-APIRequest": "true",
            "X-Nextcloud-Talk-Bot-Random": random,
            "X-Nextcloud-Talk-Bot-Signature": signature,
        }
        body = json.dumps({"reaction": reaction}).encode("utf-8")
        try:
            resp = await self._http.post(url, data=body, headers=headers)
            async with resp:
                text = await resp.text()
                if resp.status >= 400:
                    return SendResult(success=False, error=f"HTTP {resp.status}: {text[:300]}")
                return SendResult(success=True, raw_response=text)
        except Exception as exc:
            return SendResult(success=False, error=str(exc))

    async def _delete_reaction(self, room_token: str, message_id: str, reaction: str) -> SendResult:
        random = new_random()
        signature = compute_signature(self._secret, random, reaction.encode("utf-8"))
        url = f"{self._base_url}/ocs/v2.php/apps/spreed/api/v1/bot/{room_token}/reaction/{message_id}"
        headers = {
            "OCS-APIRequest": "true",
            "X-Nextcloud-Talk-Bot-Random": random,
            "X-Nextcloud-Talk-Bot-Signature": signature,
        }
        try:
            # DELETE's "reaction" is a query parameter, not a body (bot-delete-reaction
            # in openapi-bots.json) -- the POST/DELETE request shapes differ, the
            # signature scheme (random + reaction text) does not.
            resp = await self._http.delete(url, params={"reaction": reaction}, headers=headers)
            async with resp:
                text = await resp.text()
                if resp.status >= 400:
                    return SendResult(success=False, error=f"HTTP {resp.status}: {text[:300]}")
                return SendResult(success=True, raw_response=text)
        except Exception as exc:
            return SendResult(success=False, error=str(exc))

    # ------------------------------------------------------------------
    # Inbound: HTTP webhook
    # ------------------------------------------------------------------

    async def _handle_health(self, request: "web.Request") -> "web.Response":
        return web.json_response({"status": "ok", "platform": PLATFORM_NAME})

    async def _handle_webhook(self, request: "web.Request") -> "web.Response":
        body = await request.read()
        random = request.headers.get("X-Nextcloud-Talk-Random", "")
        signature = request.headers.get("X-Nextcloud-Talk-Signature", "")

        if not verify_signature(self._secret, random, body, signature):
            logger.warning("[NextcloudTalk] Rejected webhook: bad signature")
            return web.Response(status=401, text="invalid signature")

        if self._seen_nonces.seen_before(random):
            logger.warning("[NextcloudTalk] Rejected webhook: replayed random nonce")
            return web.Response(status=409, text="replayed request")

        try:
            import json

            data = json.loads(body.decode("utf-8"))
        except Exception:
            return web.Response(status=400, text="invalid json")

        parsed = parse_webhook_body(data)
        if parsed is None:
            return web.Response(status=400, text="invalid payload")

        if parsed.event_type != "Create":
            # Reaction / Join / Leave / Undo events: acknowledge, take no action.
            return web.Response(text="success", content_type="text/plain")

        if self._dedupe.seen_before(parsed.message_id):
            return web.Response(text="success", content_type="text/plain")

        resolved_text = resolve_mentions(parsed.text, parsed.parameters)
        buf = self._buffers.setdefault(
            parsed.room_token,
            RoomBuffer(
                max_messages=self._context_max_messages,
                max_age_seconds=self._context_max_age,
            ),
        )
        buf.add(parsed.actor_name, resolved_text, actor_id=parsed.actor_id)

        mode_cfg = resolve_room_mode(self._rooms_cfg, parsed.room_token)
        mode = mode_cfg["mode"]

        should_forward = False
        if mode == "free":
            should_forward = True
        elif mode == "mention":
            should_forward = is_mentioned(resolved_text, parsed.parameters, self._triggers)
        # mode == "watch": never forward synchronously; the watch loop handles it.

        if should_forward:
            context_block = buf.render_context(exclude_last=True)
            text = (
                f"[Recent messages in this room]\n{context_block}\n\n[New message]\n"
                f"{parsed.actor_name}: {resolved_text}"
                if context_block
                else f"{parsed.actor_name}: {resolved_text}"
            )
            source = self.build_source(
                chat_id=parsed.room_token,
                chat_name=parsed.room_name,
                chat_type="group",
                user_id=parsed.actor_id,
                user_name=parsed.actor_name,
            )
            event = MessageEvent(
                text=text,
                message_type=MessageType.TEXT,
                source=source,
                message_id=parsed.message_id,
                channel_prompt=_with_call_tools_prompt(_PLATFORM_HINT),
            )
            task = asyncio.create_task(self.handle_message(event))
            self._background_tasks.add(task)
            task.add_done_callback(self._background_tasks.discard)

        return web.Response(text="success", content_type="text/plain")

    # ------------------------------------------------------------------
    # Watch mode: periodic background review per room
    # ------------------------------------------------------------------

    async def _watch_loop(self) -> None:
        while True:
            await asyncio.sleep(WATCH_POLL_INTERVAL_SECONDS)
            try:
                await self._watch_tick()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("[NextcloudTalk] watch loop tick failed")

    async def _watch_tick(self) -> None:
        import time

        now = time.time()
        for room_token, buf in list(self._buffers.items()):
            mode_cfg = resolve_room_mode(self._rooms_cfg, room_token)
            if mode_cfg["mode"] != "watch":
                continue
            unseen = [
                m for m in buf.unseen_for_watch()
                if sender_allowed(m.actor_id, self._allowed_users, self._allow_all_users)
            ]
            if not unseen:
                buf.mark_watch_seen()  # nothing from an allowed sender to review
                continue
            last = self._watch_last_dispatch.get(room_token, 0.0)
            if now - last < mode_cfg["watch_interval_seconds"]:
                continue
            if not self._rate_limiter.allow(room_token, now=now):
                logger.warning(
                    "[NextcloudTalk] watch mode rate-limited for room %s", room_token,
                )
                continue

            lines = "\n".join(f"{m.actor_name}: {m.text}" for m in unseen)
            watch_prompt = (
                _PLATFORM_HINT
                + "\n\nYou are reviewing new messages in this room since your last look. "
                "Only post if genuinely useful (answer an unanswered question, flag an "
                "action item, or summarize if asked). Stay quiet otherwise -- reply with "
                "exactly NO_REPLY if nothing is worth adding."
            )
            source = self.build_source(
                chat_id=room_token,
                chat_name=room_token,
                chat_type="group",
                user_id=unseen[-1].actor_id or None,
                user_name=unseen[-1].actor_name,
            )
            event = MessageEvent(
                text=f"[New messages since last review]\n{lines}",
                message_type=MessageType.TEXT,
                source=source,
                channel_prompt=_with_call_tools_prompt(watch_prompt),
            )
            buf.mark_watch_seen()
            self._watch_last_dispatch[room_token] = now
            task = asyncio.create_task(self.handle_message(event))
            self._background_tasks.add(task)
            task.add_done_callback(self._background_tasks.discard)

    # ------------------------------------------------------------------
    # Outbound: signed proactive POST
    # ------------------------------------------------------------------

    async def send(
        self,
        chat_id: str,
        content: str,
        reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> SendResult:
        if is_quiet_reply(content):
            logger.debug("[NextcloudTalk] watch mode: suppressing quiet reply for %s", chat_id)
            return SendResult(success=True, message_id=None)

        chunks = self.truncate_message(content, max_length=MAX_MESSAGE_LENGTH)
        last_result: Optional[SendResult] = None
        for i, chunk in enumerate(chunks):
            body_obj: Dict[str, Any] = {"message": chunk}
            if reply_to and i == 0:
                try:
                    body_obj["replyTo"] = int(reply_to)
                except (TypeError, ValueError):
                    pass
            last_result = await self._post_message(chat_id, body_obj)
            if not last_result.success:
                return last_result
        return last_result or SendResult(success=True)

    async def _post_message(self, room_token: str, body_obj: Dict[str, Any]) -> SendResult:
        import json

        body = json.dumps(body_obj).encode("utf-8")
        random = new_random()
        # Confirmed against a live Nextcloud Talk 24.0.5 instance's
        # custom_apps/spreed/openapi-bots.json: the outbound bot signature
        # is HMAC-SHA256(secret, random + MESSAGE TEXT) -- NOT random + the
        # full JSON body (that's the INBOUND webhook scheme; the two
        # directions differ). Signing over the full body here produced a
        # silent 401 with an empty OCS error message during live testing.
        signature = compute_signature(self._secret, random, body_obj["message"].encode("utf-8"))
        url = f"{self._base_url}/ocs/v2.php/apps/spreed/api/v1/bot/{room_token}/message"
        headers = {
            "Content-Type": "application/json",
            "OCS-APIRequest": "true",
            "X-Nextcloud-Talk-Bot-Random": random,
            "X-Nextcloud-Talk-Bot-Signature": signature,
        }
        try:
            resp = await self._http.post(url, data=body, headers=headers)
            async with resp:
                text = await resp.text()
                if resp.status >= 400:
                    return SendResult(
                        success=False,
                        error=f"HTTP {resp.status}: {text[:300]}",
                        retryable=resp.status in (429, 502, 503, 504),
                    )
                return SendResult(success=True, raw_response=text)
        except Exception as exc:
            return SendResult(success=False, error=str(exc), retryable=True)


# ----------------------------------------------------------------------------
# Plugin registration (ctx.register_platform) -- see
# website/docs/developer-guide/adding-platform-adapters.md in the Hermes
# checkout this is deployed into.
# ----------------------------------------------------------------------------


def check_requirements() -> bool:
    return check_nctalk_requirements()


def validate_config(config) -> bool:
    extra = getattr(config, "extra", {}) or {}
    secret = _secret("NEXTCLOUD_TALK_SECRET") or extra.get("secret")
    base_url = os.getenv("NEXTCLOUD_TALK_BASE_URL") or extra.get("base_url")
    return bool(secret and base_url)


def _env_enablement() -> Optional[dict]:
    """Seed PlatformConfig.extra from env vars so the platform auto-enables
    without a config.yaml edit (mirrors how the adapter already read these
    vars directly in __init__ before the plugin env_enablement_fn hook
    existed -- see PLAN.md section 4 for the env var semantics)."""
    secret = _secret("NEXTCLOUD_TALK_SECRET", "").strip()
    base_url = os.getenv("NEXTCLOUD_TALK_BASE_URL", "").strip()
    if not (secret and base_url):
        return None
    seed: Dict[str, Any] = {"secret": secret, "base_url": base_url}
    host = os.getenv("NEXTCLOUD_TALK_HOST", "").strip()
    if host:
        seed["host"] = host
    port = os.getenv("NEXTCLOUD_TALK_PORT", "").strip()
    if port:
        try:
            seed["port"] = int(port)
        except ValueError:
            logger.warning("[NextcloudTalk] NEXTCLOUD_TALK_PORT=%r is not an int, ignoring", port)
    path = os.getenv("NEXTCLOUD_TALK_PATH", "").strip()
    if path:
        seed["path"] = path
    rooms_json = os.getenv("NEXTCLOUD_TALK_ROOMS_JSON", "").strip()
    if rooms_json:
        try:
            seed["rooms"] = json.loads(rooms_json)
        except json.JSONDecodeError:
            logger.warning("[NextcloudTalk] NEXTCLOUD_TALK_ROOMS_JSON is not valid JSON, ignoring")
    triggers = os.getenv("NEXTCLOUD_TALK_MENTION_TRIGGERS", "").strip()
    if triggers:
        seed["mention_triggers"] = [t.strip() for t in triggers.split(",") if t.strip()]
    # Presence check, not truthiness: NEXTCLOUD_TALK_WORKING_REACTION="" must seed
    # "" (explicitly disables the reaction, per adapter.py's __init__ comment) --
    # that's different from the var being unset at all (keeps the 👀 default).
    if "NEXTCLOUD_TALK_WORKING_REACTION" in os.environ:
        seed["working_reaction"] = os.environ["NEXTCLOUD_TALK_WORKING_REACTION"].strip()
    done_reaction = os.getenv("NEXTCLOUD_TALK_DONE_REACTION", "").strip()
    if done_reaction:
        seed["done_reaction"] = done_reaction
    return seed


def register(ctx) -> None:
    """Plugin entry point -- called by the Hermes plugin system at startup."""
    ctx.register_platform(
        name=PLATFORM_NAME,
        label="Nextcloud Talk (webhook bot)",
        adapter_factory=lambda cfg: NextcloudTalkAdapter(cfg),
        check_fn=check_requirements,
        validate_config=validate_config,
        required_env=["NEXTCLOUD_TALK_SECRET", "NEXTCLOUD_TALK_BASE_URL"],
        install_hint="pip install aiohttp   # already a Hermes dependency",
        env_enablement_fn=_env_enablement,
        # Per this adapter's documented security model (module docstring, PLAN.md
        # section "Security model"): HMAC + per-room bot enablement on the
        # Nextcloud side are the authorization boundary, not a Hermes-level
        # per-sender allowlist -- deliberately NOT trusted_inbound (that flag
        # waives allowlists AND pairing for every chat platform's own senders,
        # which is too broad here: it's a chat platform with human senders,
        # just not ones Hermes itself should allowlist by user id). Set
        # NEXTCLOUD_TALK_ALLOW_ALL_USERS=true to match that model; leaving it
        # unset defaults to deny-all, same as every other platform.
        allowed_users_env="NEXTCLOUD_TALK_ALLOWED_USERS",
        allow_all_env="NEXTCLOUD_TALK_ALLOW_ALL_USERS",
        max_message_length=MAX_MESSAGE_LENGTH,
        emoji="☁",  # ☁
        pii_safe=False,
        platform_hint=_PLATFORM_HINT,
    )
