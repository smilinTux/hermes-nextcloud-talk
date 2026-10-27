"""Unified Hermes plugin entry point for the Nextcloud Talk connectors.

Two independent connectors live in this repo under one platform name
(``nctalk``):

- ``bot/``  -- a webhook bot (Nextcloud Talk Bot API, HMAC-SHA256 signed
  requests both directions). No Nextcloud user account; authorization is
  Nextcloud-side room enablement (``occ talk:bot:setup``) plus the shared
  secret.
- ``user/`` -- a genuine Nextcloud user account (an app password), long-
  polling the Chat REST API the way a human client would.

They were built independently and are not merged into one adapter class: a
webhook receiver and a long-polling REST client are different I/O models,
and forcing a shared base class across them would make both harder to
read. What they DO share is called out below and in the README: the
per-room mode vocabulary (mention / watch / free), the 32000-char Talk
message limit and chunking approach, the working-reaction UX (an emoji
added to the triggering message while a turn is in flight, since Talk has
no bot/typing indicator), and the ``NEXTCLOUD_TALK_ALLOWED_USERS`` /
``NEXTCLOUD_TALK_ALLOW_ALL_USERS`` / ``NEXTCLOUD_TALK_HOME_CHANNEL`` env
vars, which both modes honour identically.

Mode selection happens once, by which credential is configured:

- ``NEXTCLOUD_TALK_APP_PASSWORD`` set (and no secret) -> the user connector.
- ``NEXTCLOUD_TALK_SECRET`` set (and no app password) -> the bot connector.
- Both set -> ``NEXTCLOUD_TALK_MODE`` (``bot`` or ``user``) breaks the tie;
  bot wins if that is also unset, for backward compatibility with
  deployments that only ever used the bot connector.
- Neither set -> the platform reports not-configured (``check_fn`` returns
  False) and Hermes does not attempt to connect it.

Each mode also works as a fully standalone plugin (see ``bot/__init__.py``
and ``user/__init__.py``) if you only want one of the two and would rather
not deploy the other's files at all.
"""

import os
from typing import Any, Dict, Optional

from .bot import adapter as _bot
from .user import adapter as _user


def _mode(config) -> Optional[str]:
    override = os.getenv("NEXTCLOUD_TALK_MODE", "").strip().lower()
    if override in ("bot", "user"):
        return override

    extra = getattr(config, "extra", {}) or {} if config is not None else {}
    has_user = bool(
        extra.get("app_password") or _user._secret("NEXTCLOUD_TALK_APP_PASSWORD", "").strip()
    )
    has_bot = bool(extra.get("secret") or _user._secret("NEXTCLOUD_TALK_SECRET", "").strip())

    if has_user and not has_bot:
        return "user"
    if has_bot:
        return "bot"  # covers has_bot-only and the both-configured tie-break
    return None


def check_requirements() -> bool:
    return _bot.check_requirements() or _user.check_requirements()


def validate_config(config) -> bool:
    mode = _mode(config)
    if mode == "user":
        return _user.validate_config(config)
    if mode == "bot":
        return _bot.validate_config(config)
    return False


def adapter_factory(config):
    if _mode(config) == "user":
        return _user.NextcloudTalkUserAdapter(config)
    return _bot.NextcloudTalkAdapter(config)


def env_enablement() -> Optional[Dict[str, Any]]:
    mode = _mode(None)
    if mode == "user":
        return _user._env_enablement()
    if mode == "bot":
        return _bot._env_enablement()
    return None


async def standalone_send(pconfig, chat_id, message, **kwargs):
    if _mode(pconfig) == "user":
        return await _user._standalone_send(pconfig, chat_id, message, **kwargs)
    return {
        "error": (
            "nctalk standalone send: the bot connector has no "
            "standalone sender (it only replies from a live webhook turn). "
            "Configure NEXTCLOUD_TALK_APP_PASSWORD for the user connector, "
            "or send from the live gateway adapter instead."
        )
    }


def register(ctx) -> None:
    """Plugin entry point -- called once by the Hermes plugin loader at startup."""
    # Opt-in call tools (call_join / call_leave / call_status), gated by
    # NEXTCLOUD_TALK_CALL_TOOLS=on through each tool's check_fn. Registered
    # first and independently: a failure here must never cost the platform.
    try:
        from .tools import register_tools
        register_tools(ctx)
    except Exception:  # pragma: no cover - defensive
        import logging
        logging.getLogger(__name__).warning("nctalk: call tools not registered", exc_info=True)
    ctx.register_platform(
        name=_user.PLATFORM_NAME,
        label="Nextcloud Talk",
        adapter_factory=adapter_factory,
        check_fn=check_requirements,
        validate_config=validate_config,
        required_env=[],  # either credential set satisfies check_fn; see plugin.yaml
        install_hint=(
            "pip install aiohttp httpx   # aiohttp for bot mode, httpx for "
            "user mode; both are already Hermes dependencies"
        ),
        env_enablement_fn=env_enablement,
        cron_deliver_env_var="NEXTCLOUD_TALK_HOME_CHANNEL",
        standalone_sender_fn=standalone_send,
        allowed_users_env="NEXTCLOUD_TALK_ALLOWED_USERS",
        allow_all_env="NEXTCLOUD_TALK_ALLOW_ALL_USERS",
        max_message_length=32000,
        emoji="☁",
        pii_safe=False,
        allow_update_command=True,
        platform_hint=(
            "You are chatting via Nextcloud Talk. Markdown is supported. "
            "Messages over 32000 characters are split automatically. In a "
            "'watch' mode room (bot mode only) with nothing worth adding, "
            "reply with exactly NO_REPLY and nothing else. " + _user.THREADS_HINT
        ),
    )
