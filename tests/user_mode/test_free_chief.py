"""v1.9.1: a "free" group room is a Chief of Staff room, not "every line is for me".

A human message there that @mentions someone else is context only; an
unaddressed one is delivered with reply_expected=False (the agent may stay
silent without the core's silence-marker warning); one that mentions this
account still expects a reply. DMs and private pair rooms are unchanged.
"""

import asyncio

import pytest

from user.adapter import (
    ROOM_TYPE_GROUP,
    ROOM_TYPE_ONE_TO_ONE,
    NextcloudTalkUserAdapter,
    _mentions_someone_else,
)
from gateway.config import PlatformConfig

ME = {"mention-user1": {"type": "user", "id": "hermes-bot", "name": "Hermes"}}
JARVIS = {"mention-user1": {"type": "user", "id": "jarvis", "name": "Jarvis"}}


def _run(coro):
    return asyncio.run(coro)


def _adapter(room_type=ROOM_TYPE_GROUP, **extra):
    adapter = NextcloudTalkUserAdapter(PlatformConfig(extra={"room_modes": "tok1=free", **extra}))
    adapter._room_meta["tok1"] = {"token": "tok1", "type": room_type, "displayName": "Operators AI"}
    return adapter


def _msg(msg_id, actor, text, params=None):
    return {"id": msg_id, "actorId": actor, "actorType": "users", "actorDisplayName": actor,
            "message": text, "messageType": "comment", "messageParameters": params or {},
            "timestamp": 1700000000}


def test_human_mentioning_someone_else_is_context_only():
    adapter = _adapter(allowed_users="chef")
    _run(adapter._on_message("tok1", _msg(1, "chef", "{mention-user1} can you check the build?", JARVIS)))
    assert adapter.handled_events == []
    assert adapter._room_context["tok1"] == ["chef: @Jarvis can you check the build?"]


def test_plain_text_mention_of_another_agent_is_context_only():
    adapter = _adapter(agent_users="jarvis")
    _run(adapter._on_message("tok1", _msg(1, "chef", "@jarvis do X")))
    assert adapter.handled_events == []


def test_unaddressed_human_message_does_not_oblige_a_reply():
    adapter = _adapter()
    _run(adapter._on_message("tok1", _msg(1, "chef", "anyone know where the logs are?")))
    assert len(adapter.handled_events) == 1
    assert adapter.handled_events[0].reply_expected is False


def test_message_mentioning_this_account_still_expects_a_reply():
    adapter = _adapter()
    _run(adapter._on_message("tok1", _msg(1, "chef", "{mention-user1} status?", ME)))
    assert len(adapter.handled_events) == 1
    assert adapter.handled_events[0].reply_expected is None  # default: reply expected


def test_mentioning_both_counts_as_addressed_to_us():
    adapter = _adapter()
    params = {**ME, "mention-user2": {"type": "user", "id": "jarvis", "name": "Jarvis"}}
    _run(adapter._on_message("tok1", _msg(1, "chef", "{mention-user1} {mention-user2} sync up", params)))
    assert len(adapter.handled_events) == 1 and adapter.handled_events[0].reply_expected is None


def test_dm_is_unchanged():
    adapter = _adapter(room_type=ROOM_TYPE_ONE_TO_ONE)
    adapter._room_modes = {}  # unlisted DM: DM mode (free)
    _run(adapter._on_message("tok1", _msg(1, "chef", "{mention-user1} hi", JARVIS)))
    _run(adapter._on_message("tok1", _msg(2, "chef", "what's up?")))
    assert len(adapter.handled_events) == 2
    assert all(e.reply_expected is None for e in adapter.handled_events)


def test_private_pair_room_is_unchanged():
    adapter = _adapter(allowed_users="chef")
    adapter._room_modes = {}
    adapter._pair_rooms["tok1"] = (True, 9e18)
    _run(adapter._on_message("tok1", _msg(1, "chef", "what's up?")))
    assert adapter.handled_events[0].reply_expected is None


def test_mention_mode_room_is_unchanged():
    adapter = _adapter(room_modes="tok1=mention")
    _run(adapter._on_message("tok1", _msg(1, "chef", "{mention-user1} status?", ME)))
    assert adapter.handled_events[0].reply_expected is None


@pytest.mark.parametrize("text,params,agents,expected", [
    ("{mention-user1} hi", JARVIS, frozenset(), True),
    ("{mention-user1} hi", ME, frozenset(), False),
    ("`{mention-user1}` is how you call him", JARVIS, frozenset(), False),
    ("{mention-call} everyone", {"mention-call": {"type": "call", "id": "tok1", "name": "room"}},
     frozenset(), False),
    ("@jarvis go", None, frozenset({"jarvis"}), True),
    ("mail chef@example.com", None, frozenset({"example"}), False),
    ("@hermes-bot go", None, frozenset({"hermes-bot"}), False),
    ("{mention-guest1} hi", {"mention-guest1": {"type": "guest", "id": "guest/abc", "name": "G"}},
     frozenset(), True),
])
def test_mentions_someone_else(text, params, agents, expected):
    assert _mentions_someone_else(text, params, "hermes-bot", "Hermes", agents) is expected
