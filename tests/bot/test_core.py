"""TDD unit tests for core.py -- pure logic, no network, no gateway import."""
import json
import os
import sys
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.dirname(os.path.dirname(_HERE))
sys.path.insert(0, os.path.join(_REPO_ROOT, "bot"))

import core  # noqa: E402


# ---------------------------------------------------------------------------
# Signature
# ---------------------------------------------------------------------------

def test_compute_signature_matches_known_vector():
    # Hand-computed: hmac_sha256(key="s3cr3t", msg="RANDOM123" + b"body")
    import hashlib
    import hmac as _hmac

    secret, random, body = "s3cr3t", "RANDOM123", b"body"
    expected = _hmac.new(secret.encode(), (random + "body").encode(), hashlib.sha256).hexdigest()
    assert core.compute_signature(secret, random, body) == expected


def test_verify_signature_accepts_correct_signature():
    secret, random, body = "topsecret", "r" * 64, b'{"a":1}'
    sig = core.compute_signature(secret, random, body)
    assert core.verify_signature(secret, random, body, sig) is True


def test_verify_signature_rejects_wrong_secret():
    body = b'{"a":1}'
    sig = core.compute_signature("right-secret", "r" * 64, body)
    assert core.verify_signature("wrong-secret", "r" * 64, body, sig) is False


def test_verify_signature_rejects_tampered_body():
    secret, random = "topsecret", "r" * 64
    sig = core.compute_signature(secret, random, b'{"a":1}')
    assert core.verify_signature(secret, random, b'{"a":2}', sig) is False


def test_verify_signature_rejects_missing_pieces():
    assert core.verify_signature("", "r", b"x", "sig") is False
    assert core.verify_signature("secret", "", b"x", "sig") is False
    assert core.verify_signature("secret", "r", b"x", "") is False


def test_verify_signature_is_case_insensitive_on_hex():
    secret, random, body = "s", "r" * 64, b"x"
    sig = core.compute_signature(secret, random, body).upper()
    assert core.verify_signature(secret, random, body, sig) is True


def test_new_random_is_64_chars_and_varies():
    a, b = core.new_random(), core.new_random()
    assert len(a) == 64
    assert a != b


# ---------------------------------------------------------------------------
# Webhook payload parsing
# ---------------------------------------------------------------------------

def _create_payload(message="hi", token="abc123", msg_id="1567", actor_id="users/ada"):
    return {
        "type": "Create",
        "actor": {"type": "Person", "id": actor_id, "name": "Ada"},
        "object": {
            "type": "Note",
            "id": msg_id,
            "name": "message",
            "content": json.dumps({"message": message, "parameters": {}}),
            "mediaType": "text/markdown",
        },
        "target": {"type": "Collection", "id": token, "name": "World"},
    }


def test_parse_webhook_body_extracts_core_fields():
    parsed = core.parse_webhook_body(_create_payload())
    assert parsed is not None
    assert parsed.event_type == "Create"
    assert parsed.room_token == "abc123"
    assert parsed.room_name == "World"
    assert parsed.actor_id == "users/ada"
    assert parsed.actor_name == "Ada"
    assert parsed.message_id == "1567"
    assert parsed.text == "hi"


def test_parse_webhook_body_extracts_mention_parameters():
    payload = _create_payload(message="hi {mention-call1} !")
    payload["object"]["content"] = json.dumps({
        "message": "hi {mention-call1} !",
        "parameters": {"mention-call1": {"type": "call", "id": "n3xtc10ud", "name": "world"}},
    })
    parsed = core.parse_webhook_body(payload)
    assert parsed.parameters["mention-call1"]["name"] == "world"


def test_parse_webhook_body_returns_none_for_malformed():
    assert core.parse_webhook_body({}) is None
    assert core.parse_webhook_body({"type": "Create"}) is None  # no target
    assert core.parse_webhook_body("not a dict") is None


def test_parse_webhook_body_handles_non_json_content_gracefully():
    payload = _create_payload()
    payload["object"]["content"] = "plain text, not json"
    parsed = core.parse_webhook_body(payload)
    assert parsed.text == "plain text, not json"


def test_resolve_mentions_substitutes_placeholder_with_name():
    text = "hi {mention-call1} !"
    params = {"mention-call1": {"type": "call", "id": "n3xtc10ud", "name": "world"}}
    assert core.resolve_mentions(text, params) == "hi @world !"


def test_resolve_mentions_leaves_unknown_placeholder_untouched():
    assert core.resolve_mentions("hi {unknown-1}", {}) == "hi {unknown-1}"


def test_resolve_mentions_noop_without_parameters():
    assert core.resolve_mentions("plain text", {}) == "plain text"


# ---------------------------------------------------------------------------
# Mention detection
# ---------------------------------------------------------------------------

def test_is_mentioned_true_for_plain_at_text():
    assert core.is_mentioned("@Jarvis ping", {}) is True


def test_is_mentioned_true_case_insensitive_no_at():
    assert core.is_mentioned("hey jarvis, you there?", {}) is True


def test_is_mentioned_false_for_substring_inside_another_word():
    assert core.is_mentioned("jarvisson said hi", {}) is False


def test_is_mentioned_false_when_not_present():
    assert core.is_mentioned("just a regular message", {}) is False


def test_is_mentioned_true_via_structured_mention_parameter():
    params = {"mention-user1": {"type": "user", "id": "bots/jarvis", "name": "Jarvis"}}
    assert core.is_mentioned("hi there", params) is True


def test_is_mentioned_respects_custom_triggers():
    assert core.is_mentioned("@Nova help", {}, triggers=["nova"]) is True
    assert core.is_mentioned("@Nova help", {}, triggers=["jarvis"]) is False


def test_is_mentioned_false_with_empty_triggers():
    assert core.is_mentioned("@Jarvis ping", {}, triggers=[]) is False


# ---------------------------------------------------------------------------
# Quiet-reply sentinel
# ---------------------------------------------------------------------------

def test_is_quiet_reply_true_for_exact_sentinel():
    assert core.is_quiet_reply("NO_REPLY") is True


def test_is_quiet_reply_true_for_sentinel_with_whitespace_and_punctuation():
    assert core.is_quiet_reply("  no_reply.  ") is True
    assert core.is_quiet_reply("\nNO_REPLY\n") is True


def test_is_quiet_reply_true_for_empty_or_none():
    assert core.is_quiet_reply("") is True
    assert core.is_quiet_reply("   ") is True
    assert core.is_quiet_reply(None) is True


def test_is_quiet_reply_false_for_real_content():
    assert core.is_quiet_reply("Here's the answer: 42") is False
    assert core.is_quiet_reply("NO_REPLY, but also this") is False


# ---------------------------------------------------------------------------
# Dedupe cache
# ---------------------------------------------------------------------------

def test_dedupe_cache_first_seen_is_not_a_duplicate():
    cache = core.DedupeCache(ttl_seconds=60)
    assert cache.seen_before("msg-1") is False


def test_dedupe_cache_second_seen_within_ttl_is_a_duplicate():
    cache = core.DedupeCache(ttl_seconds=60)
    cache.seen_before("msg-1", now=1000.0)
    assert cache.seen_before("msg-1", now=1010.0) is True


def test_dedupe_cache_after_ttl_expiry_is_not_a_duplicate():
    cache = core.DedupeCache(ttl_seconds=60)
    cache.seen_before("msg-1", now=1000.0)
    assert cache.seen_before("msg-1", now=1100.0) is False


def test_dedupe_cache_empty_id_never_flagged():
    cache = core.DedupeCache()
    assert cache.seen_before("") is False
    assert cache.seen_before("") is False


def test_dedupe_cache_prunes_when_over_max_size():
    cache = core.DedupeCache(ttl_seconds=1, max_size=5)
    for i in range(10):
        cache.seen_before(f"msg-{i}", now=1000.0 + i)
    # Oldest entries (now far outside the 1s TTL relative to the last insert)
    # should have been pruned away, keeping the cache bounded.
    assert len(cache._seen) <= 5


# ---------------------------------------------------------------------------
# Room mode resolution
# ---------------------------------------------------------------------------

def test_resolve_room_mode_defaults_to_mention_when_unconfigured():
    assert core.resolve_room_mode({}, "unknown-room")["mode"] == "mention"


def test_resolve_room_mode_honors_configured_watch_mode():
    cfg = {"room-1": {"mode": "watch", "watch_interval_seconds": 120}}
    resolved = core.resolve_room_mode(cfg, "room-1")
    assert resolved["mode"] == "watch"
    assert resolved["watch_interval_seconds"] == 120


def test_resolve_room_mode_honors_free_mode():
    cfg = {"room-1": {"mode": "free"}}
    assert core.resolve_room_mode(cfg, "room-1")["mode"] == "free"


def test_resolve_room_mode_falls_back_on_invalid_mode():
    cfg = {"room-1": {"mode": "bogus"}}
    assert core.resolve_room_mode(cfg, "room-1")["mode"] == "mention"


def test_resolve_room_mode_default_watch_interval():
    cfg = {"room-1": {"mode": "watch"}}
    assert core.resolve_room_mode(cfg, "room-1")["watch_interval_seconds"] == 300


# ---------------------------------------------------------------------------
# Room buffer
# ---------------------------------------------------------------------------

def test_room_buffer_render_context_excludes_last_by_default():
    buf = core.RoomBuffer()
    buf.add("Chef", "first message", now=1.0)
    buf.add("Chef", "second message", now=2.0)
    ctx = buf.render_context()
    assert "first message" in ctx
    assert "second message" not in ctx


def test_room_buffer_render_context_can_include_last():
    buf = core.RoomBuffer()
    buf.add("Chef", "only message", now=1.0)
    ctx = buf.render_context(exclude_last=False)
    assert "only message" in ctx


def test_room_buffer_trims_by_max_messages():
    buf = core.RoomBuffer(max_messages=3)
    for i in range(10):
        buf.add("Chef", f"msg {i}", now=float(i))
    assert len(buf._messages) == 3
    assert buf._messages[-1].text == "msg 9"


def test_room_buffer_trims_by_age():
    buf = core.RoomBuffer(max_messages=100, max_age_seconds=10)
    buf.add("Chef", "old", now=0.0)
    buf.add("Chef", "new", now=100.0)
    assert len(buf._messages) == 1
    assert buf._messages[0].text == "new"


def test_room_buffer_watch_unseen_tracking():
    buf = core.RoomBuffer()
    buf.add("Chef", "m1", now=1.0)
    buf.add("Casey", "m2", now=2.0)
    unseen = buf.unseen_for_watch()
    assert len(unseen) == 2
    buf.mark_watch_seen()
    assert buf.unseen_for_watch() == []
    buf.add("Chef", "m3", now=3.0)
    unseen_again = buf.unseen_for_watch()
    assert len(unseen_again) == 1
    assert unseen_again[0].text == "m3"


# ---------------------------------------------------------------------------
# Watch-mode rate limiter
# ---------------------------------------------------------------------------

def test_watch_rate_limiter_allows_up_to_the_cap():
    limiter = core.WatchRateLimiter(max_per_hour=3)
    now = 1000.0
    assert limiter.allow("room-1", now=now) is True
    assert limiter.allow("room-1", now=now + 1) is True
    assert limiter.allow("room-1", now=now + 2) is True
    assert limiter.allow("room-1", now=now + 3) is False


def test_watch_rate_limiter_resets_after_window():
    limiter = core.WatchRateLimiter(max_per_hour=1)
    now = 1000.0
    assert limiter.allow("room-1", now=now) is True
    assert limiter.allow("room-1", now=now + 10) is False
    assert limiter.allow("room-1", now=now + 3700) is True


def test_watch_rate_limiter_is_per_room():
    limiter = core.WatchRateLimiter(max_per_hour=1)
    now = 1000.0
    assert limiter.allow("room-1", now=now) is True
    assert limiter.allow("room-2", now=now) is True


# -- sender allowlist ---------------------------------------------------


def test_parse_allowlist_lowercases_and_skips_blanks():
    assert core.parse_allowlist(" Alice, ,BOB ") == frozenset({"alice", "bob"})


def test_sender_allowed_matches_case_insensitively():
    assert core.sender_allowed("ALICE", frozenset({"alice"}), False)


def test_sender_allowed_rejects_unlisted_and_empty_ids():
    allow = frozenset({"alice"})
    assert not core.sender_allowed("mallory", allow, False)
    assert not core.sender_allowed("", allow, False)
    assert not core.sender_allowed(None, frozenset(), False)


def test_sender_allowed_allow_all_overrides_the_list():
    assert core.sender_allowed("anyone", frozenset(), True)


# -- v1.9.0: threads (Talk 22.0.10+ puts object.threadId on webhook payloads) --

def test_parse_webhook_body_reads_thread_id():
    payload = _create_payload()
    payload["object"]["threadId"] = 1500
    assert core.parse_webhook_body(payload).thread_id == "1500"


def test_parse_webhook_body_without_or_zero_thread_is_main_timeline():
    assert core.parse_webhook_body(_create_payload()).thread_id is None
    payload = _create_payload()
    payload["object"]["threadId"] = 0
    assert core.parse_webhook_body(payload).thread_id is None
