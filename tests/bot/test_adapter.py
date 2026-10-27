"""Integration-ish tests for adapter.py's HTTP handler + send(), using
aiohttp's own test_utils so the signature verification and routing run for
real (no gateway process, no network) -- only ``handle_message`` and the
outbound HTTP POST are test doubles (the stub BasePlatformAdapter records
handled events; aiohttp.test_utils.TestServer stands in for Nextcloud when
exercising send()).
"""
import asyncio
import itertools
import json
import os
import sys

import pytest
import pytest_asyncio
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

HERE = os.path.dirname(os.path.abspath(__file__))
TESTS_DIR = os.path.dirname(HERE)
REPO_ROOT = os.path.dirname(TESTS_DIR)
sys.path.insert(0, os.path.join(TESTS_DIR, "stubs"))
sys.path.insert(0, os.path.join(REPO_ROOT, "bot"))

import core  # noqa: E402
from adapter import MAX_WEBHOOK_BODY_BYTES, NextcloudTalkAdapter  # noqa: E402
from gateway.config import PlatformConfig  # noqa: E402
from gateway.platforms.base import MessageEvent, MessageType, ProcessingOutcome, SessionSource  # noqa: E402

SECRET = "x" * 64

# A fresh 64-char nonce per webhook post by default, mirroring how Nextcloud
# signs each real delivery attempt (including retries) with its own random --
# so tests that post more than once don't accidentally collide with the new
# nonce-replay cache in adapter.py's _handle_webhook. Tests exercising that
# cache directly pass an explicit, deliberately-reused ``random=``.
_NONCE_COUNTER = itertools.count(1)


def _fresh_random() -> str:
    return f"{next(_NONCE_COUNTER):064d}"


def make_config(**extra_overrides):
    extra = {
        "secret": SECRET,
        "base_url": "http://talk.invalid",
        "host": "127.0.0.1",
        "port": 0,  # overridden per-test via direct handler calls, not a real bind
    }
    extra.update(extra_overrides)
    return PlatformConfig(enabled=True, extra=extra)


def sign(body: bytes, random: str = None) -> dict:
    random = random if random is not None else _fresh_random()
    return {
        "X-Nextcloud-Talk-Random": random,
        "X-Nextcloud-Talk-Signature": core.compute_signature(SECRET, random, body),
    }


def create_payload(message="hi", token="room1", msg_id="1", actor_name="Chef"):
    return {
        "type": "Create",
        "actor": {"type": "Person", "id": f"users/{actor_name.lower()}", "name": actor_name},
        "object": {
            "type": "Note",
            "id": msg_id,
            "name": "message",
            "content": json.dumps({"message": message, "parameters": {}}),
        },
        "target": {"type": "Collection", "id": token, "name": "Room"},
    }


@pytest_asyncio.fixture
def adapter():
    return NextcloudTalkAdapter(make_config())


async def _post_webhook(adapter_app_client, payload, *, random: str = None):
    body = json.dumps(payload).encode("utf-8")
    headers = sign(body, random=random)
    return await adapter_app_client.post("/webhooks/nextcloud-talk", data=body, headers=headers)


@pytest_asyncio.fixture
async def client(adapter):
    # client_max_size matches production's connect() exactly (see adapter.py),
    # so the body-size-cap tests exercise the real admission boundary.
    app = web.Application(client_max_size=MAX_WEBHOOK_BODY_BYTES)
    app.router.add_post(adapter._path, adapter._handle_webhook)
    server = TestServer(app)
    client = TestClient(server)
    await client.start_server()
    yield client, adapter
    await client.close()


@pytest.mark.asyncio
async def test_webhook_rejects_bad_signature(client):
    c, adapter = client
    body = json.dumps(create_payload()).encode("utf-8")
    resp = await c.post(
        "/webhooks/nextcloud-talk",
        data=body,
        headers={"X-Nextcloud-Talk-Random": "r" * 64, "X-Nextcloud-Talk-Signature": "deadbeef"},
    )
    assert resp.status == 401
    assert adapter.handled_events == []


@pytest.mark.asyncio
async def test_webhook_mention_mode_ignores_unmentioned_message(client):
    c, adapter = client
    resp = await _post_webhook(c, create_payload(message="just chatting"))
    assert resp.status == 200
    await asyncio.sleep(0)
    assert adapter.handled_events == []
    # but it's still buffered for later context
    assert "just chatting" in adapter._buffers["room1"].render_context(exclude_last=False)


@pytest.mark.asyncio
async def test_webhook_mention_mode_forwards_on_mention(client):
    c, adapter = client
    resp = await _post_webhook(c, create_payload(message="@Jarvis ping", msg_id="1"))
    assert resp.status == 200
    await asyncio.sleep(0)
    assert len(adapter.handled_events) == 1
    assert "ping" in adapter.handled_events[0].text


@pytest.mark.asyncio
async def test_webhook_includes_prior_context_on_mention(client):
    c, adapter = client
    await _post_webhook(c, create_payload(message="my favorite color is blue", msg_id="1"))
    await _post_webhook(c, create_payload(message="@Jarvis what did I just say", msg_id="2"))
    await asyncio.sleep(0)
    assert len(adapter.handled_events) == 1
    assert "favorite color is blue" in adapter.handled_events[0].text
    assert "what did I just say" in adapter.handled_events[0].text


@pytest.mark.asyncio
async def test_webhook_dedupes_retried_message_id(client):
    # Each post gets its own fresh nonce (the default), modeling a real Talk
    # retry: a fresh, re-signed delivery attempt carrying the same message id.
    # Dedup here is the message-id layer (_dedupe), not the nonce-replay cache.
    c, adapter = client
    payload = create_payload(message="@Jarvis ping", msg_id="dup-1")
    await _post_webhook(c, payload)
    await _post_webhook(c, payload)
    await asyncio.sleep(0)
    assert len(adapter.handled_events) == 1


# ---------------------------------------------------------------------------
# Inbound hardening: nonce replay + body-size cap
# (ported in concept from Cliff3c/hermes-nextcloud-talk, MIT; see adapter.py)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_webhook_rejects_exact_replay_same_nonce_same_body(client):
    c, adapter = client
    payload = create_payload(message="@Jarvis ping", msg_id="replay-1")
    nonce = "same-nonce-reused-for-a-byte-for-byte-replay-attempt-000000000"
    first = await _post_webhook(c, payload, random=nonce)
    second = await _post_webhook(c, payload, random=nonce)
    await asyncio.sleep(0)

    assert first.status == 200
    assert second.status == 409
    assert len(adapter.handled_events) == 1


@pytest.mark.asyncio
async def test_webhook_rejects_reused_nonce_even_with_a_different_body(client):
    # The nonce cache keys on the random header alone, independent of the
    # message id or content -- a captured nonce can't be reused to smuggle
    # a different signed payload through either.
    c, adapter = client
    nonce = "reused-nonce-with-a-different-message-the-second-time-00000000"
    first = await _post_webhook(c, create_payload(message="@Jarvis first", msg_id="a"), random=nonce)
    second = await _post_webhook(c, create_payload(message="@Jarvis second", msg_id="b"), random=nonce)
    await asyncio.sleep(0)

    assert first.status == 200
    assert second.status == 409
    assert len(adapter.handled_events) == 1


@pytest.mark.asyncio
async def test_webhook_accepts_same_payload_with_distinct_nonces():
    # Sanity check for the fix above: two genuinely distinct deliveries (their
    # own nonce each, as a real Talk retry would look) are NOT nonce-replay
    # rejected -- only exact nonce reuse is. (Message-id dedup still applies;
    # see test_webhook_dedupes_retried_message_id.)
    adapter = NextcloudTalkAdapter(make_config(rooms={"room1": {"mode": "free"}}))
    app = web.Application(client_max_size=MAX_WEBHOOK_BODY_BYTES)
    app.router.add_post(adapter._path, adapter._handle_webhook)
    async with TestClient(TestServer(app)) as c:
        first = await _post_webhook(c, create_payload(message="hi one", msg_id="x"))
        second = await _post_webhook(c, create_payload(message="hi two", msg_id="y"))
    assert first.status == 200
    assert second.status == 200


@pytest.mark.asyncio
async def test_webhook_rejects_oversized_body(client):
    c, adapter = client
    oversized = b"a" * (MAX_WEBHOOK_BODY_BYTES + 1)
    resp = await c.post(
        "/webhooks/nextcloud-talk",
        data=oversized,
        headers=sign(oversized),
    )
    assert resp.status == 413
    assert adapter.handled_events == []


@pytest.mark.asyncio
async def test_webhook_free_mode_forwards_every_message():
    adapter = NextcloudTalkAdapter(
        make_config(rooms={"room1": {"mode": "free"}})
    )
    app = web.Application()
    app.router.add_post(adapter._path, adapter._handle_webhook)
    async with TestClient(TestServer(app)) as c:
        await _post_webhook(c, create_payload(message="no mention needed", msg_id="1"))
        await asyncio.sleep(0)
    assert len(adapter.handled_events) == 1


@pytest.mark.asyncio
async def test_webhook_watch_mode_never_forwards_synchronously():
    adapter = NextcloudTalkAdapter(
        make_config(rooms={"room1": {"mode": "watch", "watch_interval_seconds": 9999}})
    )
    app = web.Application()
    app.router.add_post(adapter._path, adapter._handle_webhook)
    async with TestClient(TestServer(app)) as c:
        await _post_webhook(c, create_payload(message="@Jarvis anyone around?", msg_id="1"))
        await asyncio.sleep(0)
    assert adapter.handled_events == []
    assert len(adapter._buffers["room1"].unseen_for_watch()) == 1


@pytest.mark.asyncio
async def test_non_create_event_acknowledged_and_ignored(client):
    c, adapter = client
    payload = create_payload()
    payload["type"] = "Like"
    resp = await _post_webhook(c, payload)
    assert resp.status == 200
    assert adapter.handled_events == []


# ---------------------------------------------------------------------------
# send() — signing, chunking, quiet-sentinel suppression
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_send_suppresses_quiet_sentinel_without_any_http_call():
    calls = []

    async def fake_handler(request):
        calls.append(await request.read())
        return web.json_response({"ocs": {"meta": {"status": "ok"}}})

    app = web.Application()
    app.router.add_post("/ocs/v2.php/apps/spreed/api/v1/bot/{token}/message", fake_handler)
    server = TestServer(app)
    await server.start_server()
    try:
        adapter = NextcloudTalkAdapter(make_config(base_url=str(server.make_url("")).rstrip("/")))
        from aiohttp import ClientSession

        adapter._http = ClientSession()
        result = await adapter.send("room1", "NO_REPLY")
        assert result.success is True
        assert calls == []
        await adapter._http.close()
    finally:
        await server.close()


@pytest.mark.asyncio
async def test_send_posts_signed_request_and_chunks_long_messages():
    calls = []

    async def fake_handler(request):
        body = await request.read()
        calls.append(
            {
                "body": body,
                "random": request.headers.get("X-Nextcloud-Talk-Bot-Random"),
                "signature": request.headers.get("X-Nextcloud-Talk-Bot-Signature"),
                "ocs_header": request.headers.get("OCS-APIRequest"),
            }
        )
        return web.json_response({"ocs": {"meta": {"status": "ok"}}})

    app = web.Application()
    app.router.add_post("/ocs/v2.php/apps/spreed/api/v1/bot/{token}/message", fake_handler)
    server = TestServer(app)
    await server.start_server()
    try:
        adapter = NextcloudTalkAdapter(make_config(base_url=str(server.make_url("")).rstrip("/")))
        from aiohttp import ClientSession

        adapter._http = ClientSession()
        long_content = "x" * 70000  # forces 3 chunks at 32000 max_length
        result = await adapter.send("room1", long_content)
        assert result.success is True
        assert len(calls) == 3
        for call in calls:
            assert call["ocs_header"] == "true"
            body_obj = json.loads(call["body"])
            # Signature is over random + the MESSAGE TEXT, not the full JSON
            # body -- confirmed against a live Nextcloud instance's
            # spreed/openapi-bots.json after the full-body variant produced
            # a live 401 (see adapter.py _post_message comment).
            expected_sig = core.compute_signature(
                SECRET, call["random"], body_obj["message"].encode("utf-8")
            )
            assert call["signature"] == expected_sig
        await adapter._http.close()
    finally:
        await server.close()


@pytest.mark.asyncio
async def test_send_reports_failure_on_http_error():
    async def fail_handler(request):
        await request.read()
        return web.Response(status=500, text="boom")

    app = web.Application()
    app.router.add_post("/ocs/v2.php/apps/spreed/api/v1/bot/{token}/message", fail_handler)
    server = TestServer(app)
    await server.start_server()
    try:
        adapter = NextcloudTalkAdapter(make_config(base_url=str(server.make_url("")).rstrip("/")))
        from aiohttp import ClientSession

        adapter._http = ClientSession()
        result = await adapter.send("room1", "hello")
        assert result.success is False
        assert "500" in result.error
        await adapter._http.close()
    finally:
        await server.close()


# ---------------------------------------------------------------------------
# Watch-mode tick behaviour
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_watch_tick_dispatches_when_interval_elapsed_and_buffers_exist():
    adapter = NextcloudTalkAdapter(
        make_config(
            rooms={"room1": {"mode": "watch", "watch_interval_seconds": 0}},
            allowed_users="users/chef",
        )
    )
    adapter._buffers["room1"] = core.RoomBuffer()
    adapter._buffers["room1"].add("Chef", "anyone around?", actor_id="users/chef", now=0.0)
    await adapter._watch_tick()
    await asyncio.sleep(0)
    assert len(adapter.handled_events) == 1
    assert "anyone around?" in adapter.handled_events[0].text


@pytest.mark.asyncio
async def test_watch_tick_skips_when_no_unseen_messages():
    adapter = NextcloudTalkAdapter(
        make_config(rooms={"room1": {"mode": "watch", "watch_interval_seconds": 0}})
    )
    adapter._buffers["room1"] = core.RoomBuffer()
    adapter._buffers["room1"].add("Chef", "msg", now=0.0)
    adapter._buffers["room1"].mark_watch_seen()
    await adapter._watch_tick()
    assert adapter.handled_events == []


@pytest.mark.asyncio
async def test_watch_tick_respects_rate_limit():
    adapter = NextcloudTalkAdapter(
        make_config(rooms={"room1": {"mode": "watch", "watch_interval_seconds": 0}})
    )
    adapter._rate_limiter = core.WatchRateLimiter(max_per_hour=0)
    adapter._buffers["room1"] = core.RoomBuffer()
    adapter._buffers["room1"].add("Chef", "msg", now=0.0)
    await adapter._watch_tick()
    assert adapter.handled_events == []


# ---------------------------------------------------------------------------
# Working-reaction visible signal (👀 -> ✅/⚠️) -- Talk bots cannot show a
# typing indicator, so on_processing_start/on_processing_complete (the
# latter inherited from BasePlatformAdapter, driven by _OK_EMOJI/_FAIL_EMOJI)
# drive a reaction instead. See PLAN.md section on working-signal reactions.
# ---------------------------------------------------------------------------


def _reaction_app(calls):
    async def post_handler(request):
        body = json.loads(await request.read())
        calls.append(
            {
                "method": "POST",
                "token": request.match_info["token"],
                "message_id": request.match_info["message_id"],
                "reaction": body.get("reaction"),
                "random": request.headers.get("X-Nextcloud-Talk-Bot-Random"),
                "signature": request.headers.get("X-Nextcloud-Talk-Bot-Signature"),
                "ocs_header": request.headers.get("OCS-APIRequest"),
            }
        )
        return web.json_response({"ocs": {"meta": {"status": "ok"}}}, status=201)

    async def delete_handler(request):
        calls.append(
            {
                "method": "DELETE",
                "token": request.match_info["token"],
                "message_id": request.match_info["message_id"],
                "reaction": request.query.get("reaction"),
                "random": request.headers.get("X-Nextcloud-Talk-Bot-Random"),
                "signature": request.headers.get("X-Nextcloud-Talk-Bot-Signature"),
                "ocs_header": request.headers.get("OCS-APIRequest"),
            }
        )
        return web.json_response({"ocs": {"meta": {"status": "ok"}}})

    app = web.Application()
    app.router.add_post(
        "/ocs/v2.php/apps/spreed/api/v1/bot/{token}/reaction/{message_id}", post_handler
    )
    app.router.add_delete(
        "/ocs/v2.php/apps/spreed/api/v1/bot/{token}/reaction/{message_id}", delete_handler
    )
    return app


async def _start_reaction_server(calls):
    server = TestServer(_reaction_app(calls))
    await server.start_server()
    return server


def _make_event(chat_id="room1", message_id="42", text="hi"):
    return MessageEvent(
        text=text,
        message_type=MessageType.TEXT,
        source=SessionSource(platform=None, chat_id=chat_id, chat_type="group"),
        message_id=message_id,
    )


@pytest.mark.asyncio
async def test_on_processing_start_adds_default_working_reaction():
    calls = []
    server = await _start_reaction_server(calls)
    try:
        adapter = NextcloudTalkAdapter(make_config(base_url=str(server.make_url("")).rstrip("/")))
        from aiohttp import ClientSession

        adapter._http = ClientSession()
        event = _make_event()
        await adapter.on_processing_start(event)
        assert len(calls) == 1
        call = calls[0]
        assert call["method"] == "POST"
        assert call["token"] == "room1"
        assert call["message_id"] == "42"
        assert call["reaction"] == "\U0001F440"  # 👀
        assert call["ocs_header"] == "true"
        expected_sig = core.compute_signature(SECRET, call["random"], "\U0001F440".encode("utf-8"))
        assert call["signature"] == expected_sig
        await adapter._http.close()
    finally:
        await server.close()


@pytest.mark.asyncio
async def test_on_processing_start_honors_custom_working_reaction():
    calls = []
    server = await _start_reaction_server(calls)
    try:
        adapter = NextcloudTalkAdapter(
            make_config(base_url=str(server.make_url("")).rstrip("/"), working_reaction="\U0001F440\U0001F525")
        )
        from aiohttp import ClientSession

        adapter._http = ClientSession()
        await adapter.on_processing_start(_make_event())
        assert calls[0]["reaction"] == "\U0001F440\U0001F525"
        await adapter._http.close()
    finally:
        await server.close()


@pytest.mark.asyncio
async def test_on_processing_start_noop_when_working_reaction_disabled():
    calls = []
    server = await _start_reaction_server(calls)
    try:
        adapter = NextcloudTalkAdapter(
            make_config(base_url=str(server.make_url("")).rstrip("/"), working_reaction="")
        )
        from aiohttp import ClientSession

        adapter._http = ClientSession()
        await adapter.on_processing_start(_make_event())
        assert calls == []
        await adapter._http.close()
    finally:
        await server.close()


@pytest.mark.asyncio
async def test_on_processing_start_noop_without_message_id():
    calls = []
    server = await _start_reaction_server(calls)
    try:
        adapter = NextcloudTalkAdapter(make_config(base_url=str(server.make_url("")).rstrip("/")))
        from aiohttp import ClientSession

        adapter._http = ClientSession()
        await adapter.on_processing_start(_make_event(message_id=None))
        assert calls == []
        await adapter._http.close()
    finally:
        await server.close()


@pytest.mark.asyncio
async def test_on_processing_complete_success_removes_working_reaction_without_done_reaction():
    calls = []
    server = await _start_reaction_server(calls)
    try:
        adapter = NextcloudTalkAdapter(make_config(base_url=str(server.make_url("")).rstrip("/")))
        from aiohttp import ClientSession

        adapter._http = ClientSession()
        await adapter.on_processing_complete(_make_event(), ProcessingOutcome.SUCCESS)
        assert len(calls) == 1
        assert calls[0]["method"] == "DELETE"
        assert calls[0]["reaction"] == "\U0001F440"
        expected_sig = core.compute_signature(SECRET, calls[0]["random"], "\U0001F440".encode("utf-8"))
        assert calls[0]["signature"] == expected_sig
        await adapter._http.close()
    finally:
        await server.close()


@pytest.mark.asyncio
async def test_on_processing_complete_success_adds_configured_done_reaction():
    calls = []
    server = await _start_reaction_server(calls)
    try:
        adapter = NextcloudTalkAdapter(
            make_config(base_url=str(server.make_url("")).rstrip("/"), done_reaction="✅")
        )
        from aiohttp import ClientSession

        adapter._http = ClientSession()
        await adapter.on_processing_complete(_make_event(), ProcessingOutcome.SUCCESS)
        assert [c["method"] for c in calls] == ["DELETE", "POST"]
        assert calls[1]["reaction"] == "✅"  # ✅
        await adapter._http.close()
    finally:
        await server.close()


@pytest.mark.asyncio
async def test_on_processing_complete_failure_swaps_working_reaction_for_warning():
    calls = []
    server = await _start_reaction_server(calls)
    try:
        adapter = NextcloudTalkAdapter(make_config(base_url=str(server.make_url("")).rstrip("/")))
        from aiohttp import ClientSession

        adapter._http = ClientSession()
        await adapter.on_processing_complete(_make_event(), ProcessingOutcome.FAILURE)
        assert [c["method"] for c in calls] == ["DELETE", "POST"]
        assert calls[1]["reaction"] == "⚠️"  # ⚠️
        await adapter._http.close()
    finally:
        await server.close()


@pytest.mark.asyncio
async def test_on_processing_complete_cancelled_removes_without_adding():
    calls = []
    server = await _start_reaction_server(calls)
    try:
        adapter = NextcloudTalkAdapter(
            make_config(base_url=str(server.make_url("")).rstrip("/"), done_reaction="✅")
        )
        from aiohttp import ClientSession

        adapter._http = ClientSession()
        await adapter.on_processing_complete(_make_event(), ProcessingOutcome.CANCELLED)
        assert [c["method"] for c in calls] == ["DELETE"]
        await adapter._http.close()
    finally:
        await server.close()


@pytest.mark.asyncio
async def test_on_processing_complete_noop_when_working_reaction_disabled():
    calls = []
    server = await _start_reaction_server(calls)
    try:
        adapter = NextcloudTalkAdapter(
            make_config(base_url=str(server.make_url("")).rstrip("/"), working_reaction="", done_reaction="✅")
        )
        from aiohttp import ClientSession

        adapter._http = ClientSession()
        await adapter.on_processing_complete(_make_event(), ProcessingOutcome.SUCCESS)
        assert calls == []
        await adapter._http.close()
    finally:
        await server.close()


@pytest.mark.asyncio
async def test_reaction_http_error_is_logged_and_swallowed_not_raised():
    async def fail_handler(request):
        await request.read()
        return web.Response(status=500, text="boom")

    app = web.Application()
    app.router.add_post(
        "/ocs/v2.php/apps/spreed/api/v1/bot/{token}/reaction/{message_id}", fail_handler
    )
    app.router.add_delete(
        "/ocs/v2.php/apps/spreed/api/v1/bot/{token}/reaction/{message_id}", fail_handler
    )
    server = TestServer(app)
    await server.start_server()
    try:
        adapter = NextcloudTalkAdapter(make_config(base_url=str(server.make_url("")).rstrip("/")))
        from aiohttp import ClientSession

        adapter._http = ClientSession()
        # Must not raise even though the remote side 500s.
        await adapter.on_processing_start(_make_event())
        await adapter.on_processing_complete(_make_event(), ProcessingOutcome.SUCCESS)
        await adapter._http.close()
    finally:
        await server.close()


@pytest.mark.asyncio
async def test_reaction_network_exception_is_logged_and_swallowed_not_raised():
    adapter = NextcloudTalkAdapter(make_config(base_url="http://127.0.0.1:1"))  # nothing listening
    from aiohttp import ClientSession

    adapter._http = ClientSession()
    await adapter.on_processing_start(_make_event())
    await adapter.on_processing_complete(_make_event(), ProcessingOutcome.FAILURE)
    await adapter._http.close()


# ---------------------------------------------------------------------------
# env_enablement_fn: reaction env var tri-state (unset = default, "" = off)
# ---------------------------------------------------------------------------


def test_env_enablement_leaves_working_reaction_unseeded_when_unset(monkeypatch):
    monkeypatch.setenv("NEXTCLOUD_TALK_SECRET", SECRET)
    monkeypatch.setenv("NEXTCLOUD_TALK_BASE_URL", "http://talk.invalid")
    monkeypatch.delenv("NEXTCLOUD_TALK_WORKING_REACTION", raising=False)
    import adapter as adapter_module

    seed = adapter_module._env_enablement()
    assert "working_reaction" not in seed


def test_env_enablement_seeds_empty_string_to_disable_working_reaction(monkeypatch):
    monkeypatch.setenv("NEXTCLOUD_TALK_SECRET", SECRET)
    monkeypatch.setenv("NEXTCLOUD_TALK_BASE_URL", "http://talk.invalid")
    monkeypatch.setenv("NEXTCLOUD_TALK_WORKING_REACTION", "")
    import adapter as adapter_module

    seed = adapter_module._env_enablement()
    assert seed["working_reaction"] == ""


def test_env_enablement_seeds_custom_working_reaction(monkeypatch):
    monkeypatch.setenv("NEXTCLOUD_TALK_SECRET", SECRET)
    monkeypatch.setenv("NEXTCLOUD_TALK_BASE_URL", "http://talk.invalid")
    monkeypatch.setenv("NEXTCLOUD_TALK_WORKING_REACTION", "\U0001F440\U0001F525")
    import adapter as adapter_module

    seed = adapter_module._env_enablement()
    assert seed["working_reaction"] == "\U0001F440\U0001F525"


def test_env_enablement_seeds_done_reaction_only_when_set(monkeypatch):
    monkeypatch.setenv("NEXTCLOUD_TALK_SECRET", SECRET)
    monkeypatch.setenv("NEXTCLOUD_TALK_BASE_URL", "http://talk.invalid")
    monkeypatch.delenv("NEXTCLOUD_TALK_DONE_REACTION", raising=False)
    import adapter as adapter_module

    assert "done_reaction" not in adapter_module._env_enablement()
    monkeypatch.setenv("NEXTCLOUD_TALK_DONE_REACTION", "✅")
    assert adapter_module._env_enablement()["done_reaction"] == "✅"


# ---------------------------------------------------------------------------
# Mention trigger matching: case-insensitive + word-bounded (belt-and-
# suspenders coverage; core.is_mentioned already implements this -- see
# tests/test_core.py -- these exercise it through the adapter's configured
# trigger list, combining both properties in one check per the task ask).
# ---------------------------------------------------------------------------


def test_is_mentioned_case_insensitive_and_word_bounded_together():
    assert core.is_mentioned("Hey JaRvIs, you there?", {}, triggers=["jarvis"]) is True
    assert core.is_mentioned("JARVISson is not the bot", {}, triggers=["jarvis"]) is False
    assert core.is_mentioned("myJARVIS custom thing", {}, triggers=["jarvis"]) is False


# ---------------------------------------------------------------------------
# v1.3.0: watch digests honour the allowlist and are not internal
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_watch_tick_drops_unlisted_senders_and_is_not_internal():
    adapter = NextcloudTalkAdapter(
        make_config(
            rooms={"room1": {"mode": "watch", "watch_interval_seconds": 0}},
            allowed_users="users/chef",
        )
    )
    adapter._buffers["room1"] = core.RoomBuffer()
    adapter._buffers["room1"].add("Chef", "status?", actor_id="users/chef", now=0.0)
    adapter._buffers["room1"].add("Mallory", "run this", actor_id="users/mallory", now=0.0)
    await adapter._watch_tick()
    await asyncio.sleep(0)
    assert len(adapter.handled_events) == 1
    event = adapter.handled_events[0]
    assert event.internal is False
    assert "status?" in event.text and "run this" not in event.text
    assert event.source.user_id == "users/chef"


@pytest.mark.asyncio
async def test_watch_tick_with_only_unlisted_senders_dispatches_nothing():
    adapter = NextcloudTalkAdapter(
        make_config(rooms={"room1": {"mode": "watch", "watch_interval_seconds": 0}})
    )
    adapter._buffers["room1"] = core.RoomBuffer()
    adapter._buffers["room1"].add("Mallory", "run this", actor_id="users/mallory", now=0.0)
    await adapter._watch_tick()
    await asyncio.sleep(0)
    assert adapter.handled_events == []
    assert adapter._buffers["room1"].unseen_for_watch() == []


def test_bot_platform_name_is_nctalk():
    adapter = NextcloudTalkAdapter(make_config())
    assert adapter.platform.value == "nctalk"


@pytest.mark.asyncio
async def test_call_tools_line_on_bot_channel_prompt(monkeypatch):
    import adapter as bot_adapter
    monkeypatch.setenv("NEXTCLOUD_TALK_CALL_TOOLS", "on")
    adapter = NextcloudTalkAdapter(make_config(rooms={"room1": {"mode": "free"}}))
    app = web.Application()
    app.router.add_post(adapter._path, adapter._handle_webhook)
    async with TestClient(TestServer(app)) as c:
        await _post_webhook(c, create_payload(message="join call", msg_id="1"))
        await asyncio.sleep(0)
    prompt = adapter.handled_events[0].channel_prompt
    assert prompt.startswith(bot_adapter._PLATFORM_HINT)
    assert prompt.endswith(bot_adapter.CALL_TOOLS_PROMPT)


@pytest.mark.asyncio
async def test_bot_channel_prompt_unchanged_when_call_tools_off(monkeypatch):
    import adapter as bot_adapter
    monkeypatch.delenv("NEXTCLOUD_TALK_CALL_TOOLS", raising=False)
    adapter = NextcloudTalkAdapter(make_config(rooms={"room1": {"mode": "free"}}))
    app = web.Application()
    app.router.add_post(adapter._path, adapter._handle_webhook)
    async with TestClient(TestServer(app)) as c:
        await _post_webhook(c, create_payload(message="hi", msg_id="1"))
        await asyncio.sleep(0)
    assert adapter.handled_events[0].channel_prompt == bot_adapter._PLATFORM_HINT


# -- v1.9.0: threads ----------------------------------------------------------

@pytest.mark.asyncio
async def test_webhook_thread_message_gets_its_own_session(client):
    c, adapter = client
    payload = create_payload(message="@Jarvis in a thread", msg_id="7")
    payload["object"]["threadId"] = 5
    await _post_webhook(c, payload)
    await _post_webhook(c, create_payload(message="@Jarvis top level", msg_id="8"))
    await asyncio.sleep(0)
    assert [e.source.thread_id for e in adapter.handled_events] == ["5", None]


@pytest.mark.asyncio
async def test_send_in_thread_puts_thread_id_on_every_chunk():
    calls = []

    async def fake_handler(request):
        calls.append(json.loads(await request.read()))
        return web.json_response({"ocs": {"meta": {"status": "ok"}}})

    app = web.Application()
    app.router.add_post("/ocs/v2.php/apps/spreed/api/v1/bot/{token}/message", fake_handler)
    server = TestServer(app)
    await server.start_server()
    try:
        adapter = NextcloudTalkAdapter(make_config(base_url=str(server.make_url("")).rstrip("/")))
        from aiohttp import ClientSession

        adapter._http = ClientSession()
        result = await adapter.send("room1", "x" * 40000, reply_to="7", metadata={"thread_id": "5"})
        assert result.success is True
        assert len(calls) == 2 and all(b["threadId"] == 5 for b in calls)
        assert calls[0]["replyTo"] == 7 and "replyTo" not in calls[1]
        calls.clear()
        await adapter.send("room1", "plain")
        assert "threadId" not in calls[0]
        await adapter._http.close()
    finally:
        await server.close()
