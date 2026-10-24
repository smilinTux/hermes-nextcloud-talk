"""Unit tests for the user-mode Nextcloud Talk adapter's pure logic (no network).

Network/long-poll behavior (connect/_discover_rooms/_poll_room/send) needs a
live Nextcloud instance or a much heavier httpx mock harness than is useful
here; those paths were exercised against a real Nextcloud Hub instance
during development instead. These tests cover every pure helper: auth
header construction, mention detection (rich + plain-text, including
federated actor ids), room-mode resolution (display-name override vs. env
map vs. default), DM classification, federation-invite allowlisting,
message chunking at the Talk 32000-char limit, the memory source-tag
format, the agent-to-agent loop guard, active-listening context, and
room-description rules -- plus end-to-end ``_on_message`` behavior tests
for the last three (no network: ``handle_message`` is a recording stub,
see ``tests/stubs/gateway/platforms/base.py``).
"""

import asyncio
import base64
import importlib
import os

import pytest

from unittest.mock import AsyncMock, MagicMock

import user.adapter as user_adapter
from user.adapter import (
    DEFAULT_AGENT_CHAIN_LIMIT,
    DEFAULT_LONG_POLL_TIMEOUT,
    DEFAULT_WORKING_REACTION_EMOJI,
    LONG_POLL_TIMEOUT_ENV,
    MAX_MESSAGE_LENGTH,
    ROOM_MODE_FREE,
    ROOM_MODE_MENTION,
    ROOM_MODE_WATCH,
    ROOM_TYPE_GROUP,
    ROOM_TYPE_NOTE_TO_SELF,
    ROOM_TYPE_ONE_TO_ONE,
    NextcloudTalkUserAdapter,
    ProcessingOutcome,
    _build_auth_header,
    _chunk_message,
    _extract_actor,
    _invite_accept_path,
    _is_agent_actor,
    _is_mention,
    _is_pending_invite,
    _is_skippable_room,
    _ocs_headers,
    _parse_agent_users,
    _parse_room_modes_env,
    _room_chat_type,
    _room_mode,
    _should_auto_accept_invite,
    _source_tag,
    _trim_backlog,
    _with_room_context,
    _with_room_rules,
    _working_reaction_emoji,
)
from gateway.config import PlatformConfig
from gateway.platforms.base import MessageEvent, MessageType


def _make_adapter(**extra) -> NextcloudTalkUserAdapter:
    config = PlatformConfig(
        extra={
            "server_url": "https://cloud.example.com",
            "username": "hermes-bot",
            "app_password": "s3cr3t",
            **extra,
        }
    )
    return NextcloudTalkUserAdapter(config)


# -- Auth ---------------------------------------------------------------


def test_build_auth_header_happy_path():
    headers = _build_auth_header("hermes-bot", "s3cr3t-app-pw")
    expected = base64.b64encode(b"hermes-bot:s3cr3t-app-pw").decode("ascii")
    assert headers == {"Authorization": f"Basic {expected}"}


@pytest.mark.parametrize("username,app_password", [("", "pw"), ("hermes-bot", ""), ("", "")])
def test_build_auth_header_missing_fields_returns_empty(username, app_password):
    assert _build_auth_header(username, app_password) == {}


def test_ocs_headers_include_marker_and_auth():
    headers = _ocs_headers("hermes-bot", "pw")
    assert headers["OCS-APIRequest"] == "true"
    assert headers["Accept"] == "application/json"
    assert "Authorization" in headers


def test_ocs_headers_without_credentials_still_sets_markers():
    headers = _ocs_headers("", "")
    assert headers["OCS-APIRequest"] == "true"
    assert "Authorization" not in headers


# -- Mention detection ----------------------------------------------------


def test_mention_rich_parameter_matches_own_user_id():
    params = {"mention-user1": {"type": "user", "id": "hermes-bot", "name": "Hermes"}}
    assert _is_mention("hey team", params, "hermes-bot", "Hermes") is True


def test_mention_rich_parameter_other_user_does_not_match():
    params = {"mention-user1": {"type": "user", "id": "alice", "name": "Alice"}}
    assert _is_mention("hey team", params, "hermes-bot", "Hermes") is False


def test_mention_plain_text_username():
    assert _is_mention("@hermes-bot can you check this", None, "hermes-bot", "Hermes") is True


def test_mention_plain_text_quoted_display_name():
    assert _is_mention('@"Hermes" status please', None, "hermes-bot", "Hermes") is True


def test_mention_plain_text_display_name_no_quotes():
    assert _is_mention("Hermes hello @Hermes", None, "hermes-bot", "Hermes") is True


def test_mention_no_match_in_ordinary_text():
    assert _is_mention("just chatting about dinner", None, "hermes-bot", "Hermes") is False


def test_mention_federated_actor_rich_param_still_matches_local_id():
    # A federated sender @-mentions the home server's local account; the
    # rich parameter id is still the LOCAL id on the home server, regardless
    # of the federated actor who sent the message.
    params = {"mention-user1": {"type": "user", "id": "hermes-bot"}}
    assert _is_mention("ping", params, "hermes-bot", "Hermes") is True


def test_mention_does_not_false_positive_on_substring():
    # "Hermesbot" must not match display name "Hermes" via a loose substring
    # check (no word boundary between "Hermes" and the following "bot").
    assert _is_mention("@Hermesbot is a word", None, "hermes-bot", "Hermes") is False


# -- Actor extraction (incl. federated) -----------------------------------


def test_extract_actor_local_user():
    msg = {"actorId": "alice", "actorDisplayName": "Alice", "actorType": "users"}
    assert _extract_actor(msg) == {
        "id": "alice",
        "display_name": "Alice",
        "type": "users",
    }


def test_extract_actor_federated_user_keeps_qualified_id():
    msg = {
        "actorId": "alice@remote.example",
        "actorDisplayName": "Alice (remote)",
        "actorType": "federated_users",
    }
    actor = _extract_actor(msg)
    assert actor["type"] == "federated_users"
    assert actor["id"] == "alice@remote.example"


# -- Room mode resolution ---------------------------------------------------


def test_room_mode_defaults_to_mention():
    assert _room_mode("tok1", "Ops Room", {}) == ROOM_MODE_MENTION


def test_room_mode_from_env_map():
    modes = {"tok1": ROOM_MODE_WATCH}
    assert _room_mode("tok1", "Ops Room", modes) == ROOM_MODE_WATCH


def test_room_mode_display_name_prefix_overrides_env_map():
    modes = {"tok1": ROOM_MODE_MENTION}
    assert _room_mode("tok1", "[watch] Ops Room", modes) == ROOM_MODE_WATCH


def test_room_mode_display_name_prefix_case_insensitive():
    modes = {"tok1": ROOM_MODE_MENTION}
    assert _room_mode("tok1", "[FREE] test room", modes) == ROOM_MODE_FREE


def test_room_mode_prefix_ignored_for_rooms_the_operator_did_not_list():
    # Anyone can name a room "[watch] ..." -- only listed rooms honour it.
    assert _room_mode("tok1", "[watch] Ops Room", {}) == ROOM_MODE_MENTION
    assert _room_mode("tok1", "[free] Ops Room", {"other": ROOM_MODE_FREE}) == ROOM_MODE_MENTION


def test_parse_room_modes_env_basic():
    parsed = _parse_room_modes_env("tok1=mention,tok2=watch,tok3=free")
    assert parsed == {"tok1": "mention", "tok2": "watch", "tok3": "free"}


def test_parse_room_modes_env_skips_malformed_and_unknown_mode():
    parsed = _parse_room_modes_env("tok1=bogus,justtoken,tok2=watch")
    assert parsed == {"tok2": "watch"}


def test_parse_room_modes_env_empty():
    assert _parse_room_modes_env("") == {}
    assert _parse_room_modes_env(None) == {}


# -- Room type / DM classification -----------------------------------------


def test_room_chat_type_one_to_one_is_dm():
    assert _room_chat_type(ROOM_TYPE_ONE_TO_ONE) == "dm"


def test_room_chat_type_group_is_group():
    assert _room_chat_type(ROOM_TYPE_GROUP) == "group"


def test_room_chat_type_unknown_defaults_group():
    assert _room_chat_type(None) == "group"


def test_note_to_self_is_skippable():
    assert _is_skippable_room(ROOM_TYPE_NOTE_TO_SELF) is True
    assert _is_skippable_room(ROOM_TYPE_GROUP) is False


# -- Federation invite allowlisting ----------------------------------------


def test_invite_auto_accept_bare_local_part_match():
    assert _should_auto_accept_invite("alice@remote.example", ["alice"]) is True


def test_invite_auto_accept_full_cloud_id_match():
    allow = ["alice@remote.example"]
    assert _should_auto_accept_invite("alice@remote.example", allow) is True


def test_invite_not_on_allowlist_is_not_auto_accepted():
    assert _should_auto_accept_invite("rando@elsewhere.example", ["alice"]) is False


def test_invite_empty_allowlist_never_auto_accepts():
    assert _should_auto_accept_invite("alice@remote.example", []) is False


def test_invite_empty_inviter_never_auto_accepts():
    assert _should_auto_accept_invite("", ["alice"]) is False


def test_invite_allowlist_matching_is_case_insensitive():
    assert _should_auto_accept_invite("Alice@Remote.Example", ["alice"]) is True


# -- Chunking ---------------------------------------------------------------


def test_chunk_message_under_limit_returns_single_chunk():
    assert _chunk_message("hello", limit=32000) == ["hello"]


def test_chunk_message_empty_returns_single_empty_chunk():
    assert _chunk_message("") == [""]


def test_chunk_message_splits_at_limit():
    content = "a" * 2500
    chunks = _chunk_message(content, limit=1000)
    assert len(chunks) == 3
    assert chunks[0] == "a" * 1000
    assert chunks[1] == "a" * 1000
    assert chunks[2] == "a" * 500
    assert "".join(chunks) == content


def test_default_max_message_length_matches_talk_limit():
    assert MAX_MESSAGE_LENGTH == 32000


# -- Memory source tagging --------------------------------------------------


def test_source_tag_strips_scheme():
    assert _source_tag("https://cloud.example.com", "abc123") == (
        "nextcloud-talk/cloud.example.com/abc123"
    )


def test_source_tag_strips_trailing_slash():
    assert _source_tag("https://cloud.example.com/", "abc123") == (
        "nextcloud-talk/cloud.example.com/abc123"
    )


# -- Federation invite accept endpoint ---------------------------------


def test_invite_accept_path_is_the_invitation_resource_itself():
    # Talk accepts with POST /federation/invitation/{id}; a trailing
    # "/accept" is not a route and leaves the invite pending forever.
    assert _invite_accept_path(2) == "/ocs/v2.php/apps/spreed/api/v1/federation/invitation/2"
    assert not _invite_accept_path(2).endswith("/accept")


# -- Federation invite state (re-accepting an already-accepted invite) --


def test_pending_invite_state_zero_is_pending():
    assert _is_pending_invite({"id": 2, "state": 0}) is True


def test_pending_invite_missing_state_defaults_to_pending():
    # Older Talk versions never returned a "state" key at all.
    assert _is_pending_invite({"id": 2}) is True


def test_pending_invite_already_accepted_is_not_pending():
    # Observed live: an already-accepted invite kept re-appearing in the
    # list, and re-POSTing accept on it 400'd every discovery cycle.
    assert _is_pending_invite({"id": 2, "state": 1}) is False


def test_pending_invite_rejected_is_not_pending():
    assert _is_pending_invite({"id": 2, "state": 2}) is False


# -- Federated actor id normalization (defensive "federated_users/" prefix strip) --


def test_extract_actor_strips_federated_users_prefix_if_present():
    msg = {
        "actorId": "federated_users/alice@remote.example",
        "actorDisplayName": "alice",
        "actorType": "federated_users",
    }
    actor = _extract_actor(msg)
    assert actor["id"] == "alice@remote.example"


def test_extract_actor_without_prefix_is_unchanged():
    msg = {
        "actorId": "alice@remote.example",
        "actorDisplayName": "alice",
        "actorType": "federated_users",
    }
    actor = _extract_actor(msg)
    assert actor["id"] == "alice@remote.example"


# -- Working reaction config resolution --------------------------------


def test_working_reaction_defaults_to_eyes_when_unset():
    assert _working_reaction_emoji(None) == DEFAULT_WORKING_REACTION_EMOJI
    assert _working_reaction_emoji(None) == "\U0001F440"


def test_working_reaction_empty_string_disables():
    assert _working_reaction_emoji("") == ""


def test_working_reaction_custom_value_is_stripped():
    assert _working_reaction_emoji("  \U0001F916  ") == "\U0001F916"


# -- Working reaction lifecycle hooks (on_processing_start/complete) --------


def test_on_processing_start_adds_configured_reaction():
    adapter = _make_adapter()
    adapter._add_reaction = AsyncMock(return_value=True)
    event = MessageEvent(
        text="hi", message_type=MessageType.TEXT,
        source=MagicMock(chat_id="tok1"), message_id="42",
    )

    asyncio.run(adapter.on_processing_start(event))

    adapter._add_reaction.assert_awaited_once_with("tok1", "42", DEFAULT_WORKING_REACTION_EMOJI)


def test_on_processing_complete_removes_configured_reaction():
    adapter = _make_adapter()
    adapter._remove_reaction = AsyncMock(return_value=True)
    event = MessageEvent(
        text="hi", message_type=MessageType.TEXT,
        source=MagicMock(chat_id="tok1"), message_id="42",
    )

    asyncio.run(adapter.on_processing_complete(event, ProcessingOutcome.SUCCESS))

    adapter._remove_reaction.assert_awaited_once_with("tok1", "42", DEFAULT_WORKING_REACTION_EMOJI)


def test_on_processing_start_noop_when_reaction_disabled():
    adapter = _make_adapter(working_reaction="")
    adapter._add_reaction = AsyncMock(return_value=True)
    event = MessageEvent(
        text="hi", message_type=MessageType.TEXT,
        source=MagicMock(chat_id="tok1"), message_id="42",
    )

    asyncio.run(adapter.on_processing_start(event))

    adapter._add_reaction.assert_not_awaited()


def test_on_processing_start_noop_without_message_id():
    adapter = _make_adapter()
    adapter._add_reaction = AsyncMock(return_value=True)
    event = MessageEvent(
        text="hi", message_type=MessageType.TEXT,
        source=MagicMock(chat_id="tok1"), message_id=None,
    )

    asyncio.run(adapter.on_processing_start(event))

    adapter._add_reaction.assert_not_awaited()


# -- Agent-to-agent loop guard: pure helpers --------------------------------


def test_parse_agent_users_splits_and_lowercases():
    assert _parse_agent_users("Ops-Bot, Research-Bot ") == {"ops-bot", "research-bot"}


def test_parse_agent_users_empty_is_empty_set():
    assert _parse_agent_users("") == frozenset()
    assert _parse_agent_users(None) == frozenset()


def test_is_agent_actor_bot_type_always_true():
    assert _is_agent_actor({"type": "bots", "id": "anything"}, frozenset()) is True


def test_is_agent_actor_bare_id_matches_listed_agent():
    assert _is_agent_actor({"type": "users", "id": "ops-bot"}, frozenset({"ops-bot"})) is True


def test_is_agent_actor_bare_id_matches_federated_qualified_form():
    agent_users = frozenset({"ops-bot"})
    actor = {"type": "federated_users", "id": "ops-bot@remote.example"}
    assert _is_agent_actor(actor, agent_users) is True


def test_is_agent_actor_unlisted_human_is_not_an_agent():
    assert _is_agent_actor({"type": "users", "id": "alice"}, frozenset({"ops-bot"})) is False


def test_is_agent_actor_empty_id_is_not_an_agent():
    assert _is_agent_actor({"type": "users", "id": ""}, frozenset({"ops-bot"})) is False


# -- Active listening / room context: pure helpers --------------------------


def test_with_room_context_no_backlog_is_passthrough():
    assert _with_room_context("hello", []) == "hello"


def test_with_room_context_prefixes_backlog_and_keeps_addressed_message():
    out = _with_room_context("what do you think?", ["alice: hi", "bob: anyone around?"])
    assert "2 line(s)" in out
    assert "alice: hi" in out
    assert "bob: anyone around?" in out
    assert out.endswith("[The message addressed to you]\nwhat do you think?")


def test_with_room_rules_no_description_is_passthrough():
    assert _with_room_rules("hello", None) == "hello"
    assert _with_room_rules("hello", "   ") == "hello"


def test_with_room_rules_prefixes_description():
    out = _with_room_rules("hello", "Keep replies under 3 sentences.")
    assert out.startswith("[Room rules")
    assert "Keep replies under 3 sentences." in out
    assert out.endswith("\n\nhello")


def test_trim_backlog_caps_by_message_count():
    backlog = [f"line {i}" for i in range(10)]
    trimmed = _trim_backlog(backlog, max_messages=3)
    assert trimmed == ["line 7", "line 8", "line 9"]


def test_trim_backlog_zero_max_clears_everything():
    assert _trim_backlog(["a", "b"], max_messages=0) == []


def test_trim_backlog_caps_by_total_chars():
    backlog = ["x" * 5000, "y" * 5000]
    trimmed = _trim_backlog(backlog, max_messages=10)
    assert trimmed == ["y" * 5000]


# -- _on_message end-to-end: agent loop guard, active listening, room rules -


def _talk_message(msg_id, actor_id, text, *, actor_type="users", display_name=None, params=None):
    return {
        "id": msg_id,
        "actorId": actor_id,
        "actorType": actor_type,
        "actorDisplayName": display_name or actor_id,
        "message": text,
        "messageType": "comment",
        "messageParameters": params or {},
        "timestamp": 1700000000,
    }


def _free_group_room(token, *, description=None):
    return {
        "token": token,
        "type": ROOM_TYPE_GROUP,
        "displayName": "[free] room",
        "description": description,
    }


def _run(coro):
    return asyncio.run(coro)


def test_agent_message_in_free_room_without_mention_is_not_forwarded():
    adapter = _make_adapter(agent_users="ops-bot", room_modes="tok1=free")
    adapter._room_meta["tok1"] = _free_group_room("tok1")

    _run(adapter._on_message("tok1", _talk_message("1", "ops-bot", "hey team, status?")))

    assert adapter.handled_events == []


def test_agent_message_in_free_room_with_mention_is_forwarded():
    adapter = _make_adapter(agent_users="ops-bot", room_modes="tok1=free")
    adapter._room_meta["tok1"] = _free_group_room("tok1")
    params = {"mention-user1": {"type": "user", "id": "hermes-bot", "name": "Hermes"}}

    _run(adapter._on_message("tok1", _talk_message("1", "ops-bot", "@hermes-bot status?", params=params)))

    assert len(adapter.handled_events) == 1


def test_agent_chain_limit_silences_agent_until_a_human_speaks():
    adapter = _make_adapter(agent_users="ops-bot", agent_chain_limit=2, room_modes="tok1=free")
    adapter._room_meta["tok1"] = _free_group_room("tok1")
    params = {"mention-user1": {"type": "user", "id": "hermes-bot", "name": "Hermes"}}

    for i in range(3):
        _run(adapter._on_message("tok1", _talk_message(str(i), "ops-bot", "@hermes-bot ping", params=params)))
    assert len(adapter.handled_events) == 2  # 3rd exceeds the limit (> 2), silenced

    # A human message resets the chain counter.
    _run(adapter._on_message("tok1", _talk_message("human-1", "alice", "@hermes-bot are you there?", params=params)))
    assert len(adapter.handled_events) == 3

    _run(adapter._on_message("tok1", _talk_message("4", "ops-bot", "@hermes-bot ping", params=params)))
    assert len(adapter.handled_events) == 4


def test_webhook_bot_actor_type_counts_as_agent_even_when_unlisted():
    adapter = _make_adapter(room_modes="tok1=free")  # no agent_users configured
    adapter._room_meta["tok1"] = _free_group_room("tok1")

    _run(adapter._on_message("tok1", _talk_message("1", "some-bot", "hey team", actor_type="bots")))

    assert adapter.handled_events == []


def test_mention_mode_remembers_unaddressed_messages_as_context():
    adapter = _make_adapter(room_modes="tok1=mention")
    adapter._room_meta["tok1"] = {"token": "tok1", "type": ROOM_TYPE_GROUP, "displayName": "team"}

    _run(adapter._on_message("tok1", _talk_message("1", "alice", "anyone around?")))
    assert adapter.handled_events == []  # not mentioned, remembered instead

    params = {"mention-user1": {"type": "user", "id": "hermes-bot", "name": "Hermes"}}
    _run(adapter._on_message("tok1", _talk_message("2", "bob", "@hermes-bot status?", params=params)))

    assert len(adapter.handled_events) == 1
    event = adapter.handled_events[0]
    assert "alice: anyone around?" in event.text
    assert event.text.endswith("[The message addressed to you]\n@hermes-bot status?")
    assert event.metadata == {}  # newer Hermes refuses non-WhatsApp metadata
    # The backlog is consumed, not replayed on the next addressed message.
    _run(adapter._on_message("tok1", _talk_message("3", "bob", "@hermes-bot again?", params=params)))
    assert "alice: anyone around?" not in adapter.handled_events[1].text


def test_context_messages_zero_disables_active_listening():
    adapter = _make_adapter(room_modes="tok1=mention", context_messages=0)
    adapter._room_meta["tok1"] = {"token": "tok1", "type": ROOM_TYPE_GROUP, "displayName": "team"}

    _run(adapter._on_message("tok1", _talk_message("1", "alice", "anyone around?")))

    params = {"mention-user1": {"type": "user", "id": "hermes-bot", "name": "Hermes"}}
    _run(adapter._on_message("tok1", _talk_message("2", "bob", "@hermes-bot status?", params=params)))

    assert len(adapter.handled_events) == 1
    assert "anyone around?" not in adapter.handled_events[0].text


def test_room_description_is_applied_as_rules_for_group_rooms():
    adapter = _make_adapter(room_modes="tok1=free")
    adapter._room_meta["tok1"] = _free_group_room("tok1", description="Be brief.")

    _run(adapter._on_message("tok1", _talk_message("1", "alice", "hello")))

    assert len(adapter.handled_events) == 1
    assert adapter.handled_events[0].text.startswith("[Room rules")
    assert "Be brief." in adapter.handled_events[0].text


def test_room_description_is_not_applied_in_dms():
    adapter = _make_adapter()
    adapter._room_meta["tok1"] = {
        "token": "tok1",
        "type": ROOM_TYPE_ONE_TO_ONE,
        "displayName": "alice",
        "description": "Be brief.",
    }

    _run(adapter._on_message("tok1", _talk_message("1", "alice", "hello")))

    assert len(adapter.handled_events) == 1
    assert adapter.handled_events[0].text == "hello"


# -- Long-poll timeout (module-level, read from env at import time) --------


def _reload_adapter_with_env(value):
    """Reload ``user.adapter`` with NEXTCLOUD_TALK_LONG_POLL_TIMEOUT set (or
    unset for ``value is None``), returning the resulting LONG_POLL_TIMEOUT.
    Always restores the prior env state and reloads back to it afterwards,
    so this module's own globals are unaffected for the rest of the suite.
    """
    had_prior = LONG_POLL_TIMEOUT_ENV in os.environ
    prior = os.environ.get(LONG_POLL_TIMEOUT_ENV)
    try:
        if value is None:
            os.environ.pop(LONG_POLL_TIMEOUT_ENV, None)
        else:
            os.environ[LONG_POLL_TIMEOUT_ENV] = value
        importlib.reload(user_adapter)
        return user_adapter.LONG_POLL_TIMEOUT
    finally:
        if had_prior:
            os.environ[LONG_POLL_TIMEOUT_ENV] = prior
        else:
            os.environ.pop(LONG_POLL_TIMEOUT_ENV, None)
        importlib.reload(user_adapter)


def test_long_poll_timeout_defaults_to_25_under_talks_own_30s_ceiling():
    assert DEFAULT_LONG_POLL_TIMEOUT == 25
    assert _reload_adapter_with_env(None) == 25


def test_long_poll_timeout_honors_env_override():
    assert _reload_adapter_with_env("12") == 12


def test_long_poll_timeout_can_be_restored_to_talks_own_ceiling():
    assert _reload_adapter_with_env("30") == 30


# -- v1.3.0: platform id, metadata, secrets, watch-digest allowlist -------


def test_platform_name_is_distinct_from_the_other_catalog_talk_plugin():
    assert user_adapter.PLATFORM_NAME == "nctalk"
    assert _make_adapter().platform.value == "nctalk"


def test_inbound_event_carries_no_adapter_metadata():
    adapter = _make_adapter(room_modes="tok1=free")
    adapter._room_meta["tok1"] = _free_group_room("tok1")

    _run(adapter._on_message("tok1", _talk_message("1", "alice", "hello")))

    assert len(adapter.handled_events) == 1
    assert adapter.handled_events[0].metadata == {}


def test_secret_falls_back_to_env_without_hermes_secret_scope(monkeypatch):
    monkeypatch.setattr(user_adapter, "_hermes_get_secret", None)
    monkeypatch.setenv("NEXTCLOUD_TALK_APP_PASSWORD", "from-env")
    assert user_adapter._secret("NEXTCLOUD_TALK_APP_PASSWORD") == "from-env"


def test_secret_reads_through_hermes_secret_scope_when_available(monkeypatch):
    monkeypatch.setattr(user_adapter, "_hermes_get_secret", lambda name, default=None: "scoped")
    monkeypatch.setenv("NEXTCLOUD_TALK_APP_PASSWORD", "from-env")
    assert user_adapter._secret("NEXTCLOUD_TALK_APP_PASSWORD") == "scoped"


def _watch_event(adapter, user_id, text):
    source = adapter.build_source(chat_id="tok1", chat_type="group", user_id=user_id, user_name=user_id)
    return MessageEvent(text=text, message_type=MessageType.TEXT, source=source)


def test_watch_digest_drops_unlisted_senders_and_is_not_internal():
    adapter = _make_adapter(allowed_users="alice")
    events = [_watch_event(adapter, "alice", "deploy at 5"), _watch_event(adapter, "mallory", "rm -rf /")]

    _run(adapter._dispatch_watch_digest("tok1", events))

    assert len(adapter.handled_events) == 1
    digest = adapter.handled_events[0]
    assert digest.internal is False
    assert "deploy at 5" in digest.text
    assert "rm -rf" not in digest.text
    assert digest.source.user_id == "alice"


def test_watch_digest_with_no_allowed_sender_is_not_dispatched():
    adapter = _make_adapter(allowed_users="alice")

    _run(adapter._dispatch_watch_digest("tok1", [_watch_event(adapter, "mallory", "hi")]))

    assert adapter.handled_events == []


def test_watch_digest_allow_all_users_keeps_every_sender():
    adapter = _make_adapter(allow_all_users="true")
    events = [_watch_event(adapter, "alice", "one"), _watch_event(adapter, "bob", "two")]

    _run(adapter._dispatch_watch_digest("tok1", events))

    assert "one" in adapter.handled_events[0].text and "two" in adapter.handled_events[0].text
