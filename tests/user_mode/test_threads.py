"""v1.9.0: Talk threads in user mode (receive, send, ownership, chain, capability gate).

No network: ``handle_message`` is the recording stub from
``tests/stubs/gateway/platforms/base.py`` and HTTP goes through
``httpx.MockTransport``.
"""

import asyncio
import json
from urllib.parse import parse_qs

import httpx
import pytest

import user.adapter as user_adapter
from user.adapter import (
    MAX_MESSAGE_LENGTH,
    ROOM_TYPE_GROUP,
    NextcloudTalkUserAdapter,
    _ctx_key,
    _message_thread_id,
)
from gateway.config import PlatformConfig

# Not a credential: a filler value for the adapter's required app password.
FAKE_APP_PASSWORD = "x" * 12

MENTION = {"mention-user1": {"type": "user", "id": "hermes-bot", "name": "Hermes"}}


def _run(coro):
    return asyncio.run(coro)


def _adapter(threads=True, **extra) -> NextcloudTalkUserAdapter:
    # No credentials needed: nothing here connects, and the account id is the
    # adapter's default ("hermes-bot").
    adapter = NextcloudTalkUserAdapter(PlatformConfig(extra=dict(extra)))
    adapter._threads_enabled = threads
    adapter._capabilities_checked = True
    return adapter


def _room(adapter, token="tok1", mode_name="team"):
    adapter._room_meta[token] = {"token": token, "type": ROOM_TYPE_GROUP, "displayName": mode_name}


def _msg(msg_id, actor_id, text, *, thread=None, root=False, title=None, params=None, actor_type="users"):
    m = {
        "id": int(msg_id),
        "actorId": actor_id,
        "actorType": actor_type,
        "actorDisplayName": actor_id,
        "message": text,
        "messageType": "comment",
        "messageParameters": params or {},
        "timestamp": 1700000000,
    }
    if root:
        m["isThread"] = True
        m["threadId"] = int(msg_id)
        m["threadTitle"] = title or "task"
    elif thread is not None:
        m["threadId"] = int(thread)
    return m


def _recording_client(adapter, responder=None):
    calls = []

    def handler(request):
        calls.append(request)
        if responder is not None:
            resp = responder(request)
            if resp is not None:
                return resp
        if request.url.path.endswith("/shares"):
            return httpx.Response(200, json={"ocs": {"data": {"id": 90}}})
        if request.method == "MKCOL":
            return httpx.Response(405)
        if request.method == "PUT":
            return httpx.Response(201)
        return httpx.Response(201, json={"ocs": {"data": {"id": 777}}})

    adapter._http_client = httpx.AsyncClient(base_url="https://cloud.example.com",
                                             transport=httpx.MockTransport(handler))
    return calls


# -- pure helpers ---------------------------------------------------------------


@pytest.mark.parametrize("message,expected", [
    ({"id": 5}, None),
    ({"id": 5, "threadId": 0}, None),
    ({"id": 5, "threadId": None}, None),
    ({"id": 5, "threadId": "junk"}, None),
    ({"id": 9, "threadId": 5}, "5"),
    ({"id": 5, "isThread": True, "threadId": 5}, "5"),
    ({"id": 5, "isThread": True}, "5"),
])
def test_message_thread_id(message, expected):
    assert _message_thread_id(message) == expected


def test_ctx_key_keeps_bare_token_for_main_timeline():
    assert _ctx_key("tok1", None) == "tok1"
    assert _ctx_key("tok1", "5") == ("tok1", "5")


# -- receive --------------------------------------------------------------------


def test_main_timeline_message_has_no_thread():
    adapter = _adapter(room_modes="tok1=free")
    _room(adapter)
    _run(adapter._on_message("tok1", _msg(1, "alice", "hello")))
    assert adapter.handled_events[0].source.thread_id is None


def test_thread_reply_gets_its_own_session_per_thread():
    adapter = _adapter(room_modes="tok1=free")
    _room(adapter)
    _run(adapter._on_message("tok1", _msg(1, "alice", "top level")))
    _run(adapter._on_message("tok1", _msg(11, "alice", "in thread A", thread=10)))
    _run(adapter._on_message("tok1", _msg(21, "alice", "in thread B", thread=20)))
    threads = [e.source.thread_id for e in adapter.handled_events]
    assert threads == [None, "10", "20"]
    # Hermes keys sessions on (platform, chat, thread): three distinct sessions.
    keys = {(e.source.chat_id, e.source.thread_id) for e in adapter.handled_events}
    assert len(keys) == 3


def test_thread_root_is_routed_into_its_own_thread():
    adapter = _adapter(room_modes="tok1=free")
    _room(adapter)
    _run(adapter._on_message("tok1", _msg(30, "alice", "@hermes-bot please do X", root=True,
                                          title="Do X", params=MENTION)))
    event = adapter.handled_events[0]
    assert event.source.thread_id == "30"
    assert 'Talk thread "Do X"' in event.text


def test_capability_absent_ignores_thread_fields():
    adapter = _adapter(threads=False, room_modes="tok1=free")
    _room(adapter)
    _run(adapter._on_message("tok1", _msg(11, "alice", "in thread", thread=10)))
    event = adapter.handled_events[0]
    assert event.source.thread_id is None
    assert event.text == "in thread"
    assert adapter._threads == {}


def test_capabilities_are_read_from_the_server():
    adapter = _adapter(threads=False)
    adapter._capabilities_checked = False

    def caps(request):
        if request.url.path == "/ocs/v2.php/cloud/capabilities":
            return httpx.Response(200, json={"ocs": {"data": {"capabilities": {
                "spreed": {"features": ["chat-v2", "threads"]}}}}})
        return None

    _recording_client(adapter, caps)
    _run(adapter._refresh_capabilities())
    assert adapter._threads_enabled is True and adapter._capabilities_checked is True


def test_capabilities_without_threads_or_failing_keep_threads_off():
    adapter = _adapter(threads=False)
    adapter._capabilities_checked = False
    _recording_client(adapter, lambda r: httpx.Response(200, json={"ocs": {"data": {"capabilities": {
        "spreed": {"features": ["chat-v2"]}}}}}))
    _run(adapter._refresh_capabilities())
    assert adapter._threads_enabled is False and adapter._capabilities_checked is True

    adapter2 = _adapter(threads=False)
    adapter2._capabilities_checked = False
    _recording_client(adapter2, lambda r: httpx.Response(500))
    _run(adapter2._refresh_capabilities())
    assert adapter2._threads_enabled is False and adapter2._capabilities_checked is False


# -- context backlog per (room, thread) ----------------------------------------


def test_thread_and_main_backlogs_do_not_bleed():
    adapter = _adapter(room_modes="tok1=mention", allowed_users="alice,bob")
    _room(adapter)
    _run(adapter._on_message("tok1", _msg(1, "alice", "main chatter")))
    _run(adapter._on_message("tok1", _msg(11, "bob", "thread chatter", thread=10)))

    _run(adapter._on_message("tok1", _msg(12, "alice", "@hermes-bot in thread?", thread=10, params=MENTION)))
    thread_event = adapter.handled_events[-1]
    assert "thread chatter" in thread_event.text and "main chatter" not in thread_event.text

    _run(adapter._on_message("tok1", _msg(2, "alice", "@hermes-bot main?", params=MENTION)))
    main_event = adapter.handled_events[-1]
    assert "main chatter" in main_event.text and "thread chatter" not in main_event.text


def test_first_turn_in_a_thread_gets_the_root_then_only_replies():
    adapter = _adapter(room_modes="tok1=mention", allowed_users="alice,bob")
    _room(adapter)
    _run(adapter._on_message("tok1", _msg(10, "alice", "Plan the release", root=True, title="Release")))
    # The root is a main-timeline message: it is main context, not thread backlog.
    assert adapter._room_context["tok1"] == ["alice: Plan the release"]
    _run(adapter._on_message("tok1", _msg(11, "bob", "@hermes-bot thoughts?", thread=10, params=MENTION)))
    first = adapter.handled_events[-1].text
    assert 'Talk thread "Release"' in first and "alice: Plan the release" in first
    _run(adapter._on_message("tok1", _msg(12, "bob", "@hermes-bot and now?", thread=10, params=MENTION)))
    assert "Plan the release" not in adapter.handled_events[-1].text


def test_unknown_thread_root_is_looked_up_once():
    adapter = _adapter(room_modes="tok1=free")
    _room(adapter)
    lookups = []

    def info(request):
        if "/threads/" in request.url.path:
            lookups.append(request.url.path)
            return httpx.Response(200, json={"ocs": {"data": {
                "thread": {"id": 10, "title": "Old task"},
                "first": {"id": 10, "actorId": "alice", "actorType": "users",
                          "actorDisplayName": "Alice", "message": "the original ask"}}}})
        return None

    _recording_client(adapter, info)
    _run(adapter._on_message("tok1", _msg(11, "alice", "update?", thread=10)))
    _run(adapter._on_message("tok1", _msg(12, "alice", "more?", thread=10)))
    assert lookups == ["/ocs/v2.php/apps/spreed/api/v1/chat/tok1/threads/10"]
    assert "Alice: the original ask" in adapter.handled_events[0].text


def test_backlog_edit_inside_a_thread_is_applied():
    adapter = _adapter(room_modes="tok1=mention", allowed_users="alice")
    _room(adapter)
    _run(adapter._on_message("tok1", _msg(11, "alice", "first draft", thread=10)))
    edit = {"id": 99, "systemMessage": "message_edited", "messageType": "system",
            "parent": {"id": 11, "message": "final text", "threadId": 10}}
    _run(adapter._on_message("tok1", edit))
    assert adapter._room_context[("tok1", "10")] == ["alice: final text"]


# -- ownership ------------------------------------------------------------------


def _owned_thread(adapter, owner, thread=10, actor_type="users"):
    _run(adapter._on_message("tok1", _msg(thread, owner, "starting a task", root=True, actor_type=actor_type)))
    adapter.handled_events.clear()


def test_thread_started_by_another_agent_needs_a_mention_even_in_a_free_room():
    adapter = _adapter(room_modes="tok1=free", agent_users="ops-bot", allowed_users="alice")
    _room(adapter)
    _owned_thread(adapter, "ops-bot")
    _run(adapter._on_message("tok1", _msg(11, "alice", "how is it going?", thread=10)))
    assert adapter.handled_events == []
    _run(adapter._on_message("tok1", _msg(12, "alice", "@hermes-bot can you help?", thread=10, params=MENTION)))
    assert len(adapter.handled_events) == 1
    # The main timeline of the same free room is unaffected.
    _run(adapter._on_message("tok1", _msg(2, "alice", "hello")))
    assert len(adapter.handled_events) == 2


def test_thread_started_by_another_agent_overrides_watch_mode_too():
    adapter = _adapter(room_modes="tok1=watch", agent_users="ops-bot", allowed_users="alice")
    _room(adapter)
    _owned_thread(adapter, "ops-bot")
    _run(adapter._on_message("tok1", _msg(11, "alice", "chatter", thread=10)))
    assert adapter._watch_buffers.get("tok1", []) == []


def test_thread_started_by_a_human_follows_the_room_mode():
    adapter = _adapter(room_modes="tok1=free", agent_users="ops-bot")
    _room(adapter)
    _owned_thread(adapter, "alice")
    _run(adapter._on_message("tok1", _msg(11, "alice", "go on", thread=10)))
    assert len(adapter.handled_events) == 1


def test_own_thread_answers_its_people_without_a_mention():
    adapter = _adapter(room_modes="tok1=mention")
    _room(adapter)
    # Echo of a thread this account opened (talk_thread_start): not answered,
    # but remembered as ours.
    _run(adapter._on_message("tok1", _msg(10, "hermes-bot", "I will take this", root=True)))
    assert adapter.handled_events == []
    _run(adapter._on_message("tok1", _msg(11, "alice", "thanks, also check Y", thread=10)))
    assert len(adapter.handled_events) == 1
    # Other agents still need to address us in our own thread (loop guard).
    adapter2 = _adapter(room_modes="tok1=mention", agent_users="ops-bot")
    _room(adapter2)
    _run(adapter2._on_message("tok1", _msg(10, "hermes-bot", "mine", root=True)))
    _run(adapter2._on_message("tok1", _msg(11, "ops-bot", "fyi", thread=10)))
    assert adapter2.handled_events == []


def test_thread_ownership_can_be_turned_off():
    adapter = _adapter(room_modes="tok1=free", agent_users="ops-bot", thread_ownership="off")
    _room(adapter)
    _owned_thread(adapter, "ops-bot")
    _run(adapter._on_message("tok1", _msg(11, "alice", "how is it going?", thread=10)))
    assert len(adapter.handled_events) == 1


def test_thread_ownership_env_default_on(monkeypatch):
    monkeypatch.delenv("NEXTCLOUD_TALK_THREAD_OWNERSHIP", raising=False)
    assert _adapter()._thread_ownership is True
    monkeypatch.setenv("NEXTCLOUD_TALK_THREAD_OWNERSHIP", "off")
    assert _adapter()._thread_ownership is False


def test_ownership_from_thread_lookup_when_root_was_not_seen():
    adapter = _adapter(room_modes="tok1=free", agent_users="ops-bot")
    _room(adapter)
    _recording_client(adapter, lambda r: httpx.Response(200, json={"ocs": {"data": {
        "thread": {"id": 10, "title": "ops"},
        "first": {"id": 10, "actorId": "ops-bot", "actorType": "users", "message": "x"}}}})
        if "/threads/" in r.url.path else None)
    _run(adapter._on_message("tok1", _msg(11, "alice", "status?", thread=10)))
    assert adapter.handled_events == []


# -- hop cap per (room, thread) -------------------------------------------------


def test_agent_chain_counter_is_per_thread():
    adapter = _adapter(room_modes="tok1=free", agent_users="ops-bot", agent_chain_limit=2)
    _room(adapter)
    for i in range(3):
        _run(adapter._on_message("tok1", _msg(100 + i, "ops-bot", "@hermes-bot ping", thread=10, params=MENTION)))
    assert len(adapter.handled_events) == 2  # third in thread 10 exceeds the limit
    # Another thread and the main timeline keep their own counters.
    _run(adapter._on_message("tok1", _msg(200, "ops-bot", "@hermes-bot ping", thread=20, params=MENTION)))
    _run(adapter._on_message("tok1", _msg(300, "ops-bot", "@hermes-bot ping", params=MENTION)))
    assert len(adapter.handled_events) == 4
    # A human in thread 10 resets only thread 10.
    _run(adapter._on_message("tok1", _msg(101 + 10, "alice", "carry on", thread=10)))
    _run(adapter._on_message("tok1", _msg(400, "ops-bot", "@hermes-bot ping", thread=10, params=MENTION)))
    assert [e.source.thread_id for e in adapter.handled_events][-1] == "10"
    assert adapter._agent_chain[("tok1", "10")] == 1


# -- send -------------------------------------------------------------------------


def test_every_chunk_carries_the_thread_and_only_the_first_replies():
    adapter = _adapter()
    _room(adapter)
    calls = _recording_client(adapter)
    content = "x" * (MAX_MESSAGE_LENGTH * 2 + 5)
    result = _run(adapter.send("tok1", content, reply_to="11", metadata={"thread_id": "10"}))
    assert result.success
    bodies = [json.loads(c.content) for c in calls]
    assert len(bodies) == 3
    assert all(b["threadId"] == 10 for b in bodies)
    assert bodies[0]["replyTo"] == 11 and "replyTo" not in bodies[1] and "replyTo" not in bodies[2]


def test_send_without_thread_or_capability_has_no_thread_id():
    adapter = _adapter()
    _room(adapter)
    calls = _recording_client(adapter)
    _run(adapter.send("tok1", "hi"))
    assert "threadId" not in json.loads(calls[0].content)

    old = _adapter(threads=False)
    _room(old)
    old_calls = _recording_client(old)
    _run(old.send("tok1", "hi", metadata={"thread_id": "10"}))
    assert json.loads(old_calls[0].content) == {"message": "hi"}


def test_file_share_in_a_thread_sets_thread_id_in_talk_metadata(tmp_path):
    f = tmp_path / "report.pdf"
    f.write_bytes(b"%PDF")
    adapter = _adapter()
    _room(adapter)
    calls = _recording_client(adapter)
    assert _run(adapter.send_document("tok1", str(f), caption="done", metadata={"thread_id": "10"})).success
    form = parse_qs(calls[-1].content.decode())
    assert json.loads(form["talkMetaData"][0]) == {"caption": "done", "threadId": 10}


def test_voice_and_image_shares_keep_the_thread(tmp_path):
    f = tmp_path / "a.ogg"
    f.write_bytes(b"OggS")
    adapter = _adapter()
    _room(adapter)
    calls = _recording_client(adapter)
    assert _run(adapter.send_voice("tok1", str(f), metadata={"thread_id": "10"})).success
    meta = json.loads(parse_qs(calls[-1].content.decode())["talkMetaData"][0])
    assert meta == {"messageType": "voice-message", "threadId": 10}


def test_federated_link_fallback_posts_into_the_thread(tmp_path):
    f = tmp_path / "a.png"
    f.write_bytes(b"\x89PNG")
    adapter = _adapter(federated_media="link")
    _room(adapter)
    adapter._room_meta["tok1"]["remoteServer"] = "https://other.example"

    def link(request):
        if request.url.path.endswith("/shares"):
            return httpx.Response(200, json={"ocs": {"data": {"url": "https://x/s/abc"}}})
        return None

    calls = _recording_client(adapter, link)
    assert _run(adapter.send_image_file("tok1", str(f), metadata={"thread_id": "10"})).success
    assert json.loads(calls[-1].content)["threadId"] == 10


# -- standalone / cron ---------------------------------------------------------


def _patch_standalone_client(monkeypatch):
    calls = []
    real = httpx.AsyncClient

    def handler(request):
        calls.append(request)
        return httpx.Response(201, json={"ocs": {"data": {"id": 555}}})

    def factory(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return real(*args, **kwargs)

    monkeypatch.setattr(user_adapter.httpx, "AsyncClient", factory)
    return calls


def _standalone_config(monkeypatch):
    monkeypatch.setenv("NEXTCLOUD_TALK_SERVER_URL", "https://cloud.example.com")
    monkeypatch.setenv("NEXTCLOUD_TALK_APP_PASSWORD", FAKE_APP_PASSWORD)
    return PlatformConfig(extra={})


def test_standalone_send_honours_thread_id_on_every_chunk(monkeypatch):
    calls = _patch_standalone_client(monkeypatch)
    pconfig = _standalone_config(monkeypatch)
    content = "y" * (MAX_MESSAGE_LENGTH + 10)
    result = _run(user_adapter._standalone_send(pconfig, "tok1", content, thread_id="10"))
    assert result["success"] and result["message_id"] == 555
    bodies = [json.loads(c.content) for c in calls]
    assert len(bodies) == 2 and all(b["threadId"] == 10 for b in bodies)


def test_standalone_send_without_thread_is_unchanged(monkeypatch):
    calls = _patch_standalone_client(monkeypatch)
    pconfig = _standalone_config(monkeypatch)
    _run(user_adapter._standalone_send(pconfig, "tok1", "hello"))
    assert json.loads(calls[0].content) == {"message": "hello"}


def test_home_channel_thread_id_is_seeded_for_cron_delivery(monkeypatch):
    monkeypatch.setenv("NEXTCLOUD_TALK_SERVER_URL", "https://cloud.example.com")
    monkeypatch.setenv("NEXTCLOUD_TALK_APP_PASSWORD", FAKE_APP_PASSWORD)
    monkeypatch.setenv("NEXTCLOUD_TALK_HOME_CHANNEL", "tok1")
    monkeypatch.setenv("NEXTCLOUD_TALK_HOME_CHANNEL_THREAD_ID", "42")
    seed = user_adapter._env_enablement()
    assert seed["home_channel"] == {"chat_id": "tok1", "name": "tok1", "thread_id": "42"}
    monkeypatch.setenv("NEXTCLOUD_TALK_HOME_CHANNEL_THREAD_ID", "")
    assert "thread_id" not in user_adapter._env_enablement()["home_channel"]


def test_platform_hint_describes_threads():
    assert "There are no threads" not in open(user_adapter.__file__).read()
    hint = user_adapter.THREADS_HINT
    assert "thread" in hint and "@mentioned" in hint
    assert "\u2014" not in hint and "\u2013" not in hint
