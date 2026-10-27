"""Thread tools (tools.py, v1.9.0): talk_thread_start / talk_thread_reply with a
fake OCS transport. No network."""

import importlib.util
import json
import os

import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_spec = importlib.util.spec_from_file_location("nctalk_thread_tools", os.path.join(_ROOT, "tools.py"))
ct = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ct)


class FakeOCS:
    def __init__(self, threads=True, post_status=201, post_id=901):
        self.threads = threads
        self.post_status = post_status
        self.post_id = post_id
        self.calls = []

    def __call__(self, method, path, *, json_body=None):
        self.calls.append((method, path, json_body))
        if path == "/ocs/v2.php/cloud/capabilities":
            feats = ["chat-v2"] + (["threads"] if self.threads else [])
            return 200, {"capabilities": {"spreed": {"features": feats}}}
        if self.post_status >= 300:
            return self.post_status, "nope"
        return self.post_status, {"id": self.post_id} if self.post_id else {}

    def posts(self):
        return [c for c in self.calls if c[0] == "POST"]


@pytest.fixture
def ocs(monkeypatch):
    for k in ("HERMES_SESSION_PLATFORM", "HERMES_SESSION_CHAT_ID", "HERMES_SESSION_THREAD_ID",
              "NEXTCLOUD_TALK_THREAD_TOOLS"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("NEXTCLOUD_TALK_SERVER_URL", "https://cloud.example.com")
    monkeypatch.setenv("NEXTCLOUD_TALK_APP_PASSWORD", "x" * 12)  # filler, not a credential
    fake = FakeOCS()
    monkeypatch.setattr(ct, "_request", fake)
    ct._caps_cache.clear()
    return fake


def j(s):
    return json.loads(s)


def _in_talk(monkeypatch, room="xazze5d7", thread=None):
    monkeypatch.setenv("HERMES_SESSION_PLATFORM", "nctalk")
    monkeypatch.setenv("HERMES_SESSION_CHAT_ID", room)
    if thread:
        monkeypatch.setenv("HERMES_SESSION_THREAD_ID", thread)


def test_thread_tools_gate(monkeypatch, ocs):
    assert ct.thread_tools_enabled()
    monkeypatch.setenv("NEXTCLOUD_TALK_THREAD_TOOLS", "off")
    assert not ct.thread_tools_enabled()
    monkeypatch.delenv("NEXTCLOUD_TALK_THREAD_TOOLS")
    monkeypatch.delenv("NEXTCLOUD_TALK_APP_PASSWORD")
    assert not ct.thread_tools_enabled()  # bot mode / unconfigured


def test_start_creates_thread_in_current_room(monkeypatch, ocs):
    _in_talk(monkeypatch)
    r = j(ct.talk_thread_start({"title": "  Fix\nthe build ", "message": "On it."}))
    assert r["status"] == "created" and r["room"] == "xazze5d7" and r["thread_id"] == "901"
    assert r["title"] == "Fix the build"
    assert ocs.posts() == [("POST", "/ocs/v2.php/apps/spreed/api/v1/chat/xazze5d7",
                            {"message": "On it.", "threadTitle": "Fix the build"})]


def test_start_requires_title_and_message(monkeypatch, ocs):
    _in_talk(monkeypatch)
    assert "title is required" in j(ct.talk_thread_start({"message": "x"}))["error"]
    assert "message is required" in j(ct.talk_thread_start({"title": "x"}))["error"]
    assert ocs.posts() == []


def test_start_refuses_without_threads_capability(monkeypatch, ocs):
    _in_talk(monkeypatch)
    ocs.threads = False
    r = j(ct.talk_thread_start({"title": "t", "message": "m"}))
    assert "does not support threads" in r["error"] and ocs.posts() == []


def test_start_ignores_a_different_room_unless_asked(monkeypatch, ocs):
    _in_talk(monkeypatch)
    r = j(ct.talk_thread_start({"title": "t", "message": "m", "room": "otherroom1"}))
    assert r["room"] == "xazze5d7" and "room_note" in r
    r = j(ct.talk_thread_start({"title": "t", "message": "m", "room": "otherroom1", "other_room": True}))
    assert r["room"] == "otherroom1"


def test_start_outside_talk_needs_a_room(ocs):
    assert "room is required" in j(ct.talk_thread_start({"title": "t", "message": "m"}))["error"]


def test_start_reports_http_errors(monkeypatch, ocs):
    _in_talk(monkeypatch)
    ocs.post_status = 400
    assert "could not start the thread" in j(ct.talk_thread_start({"title": "t", "message": "m"}))["error"]


def test_reply_posts_into_given_thread(monkeypatch, ocs):
    _in_talk(monkeypatch)
    r = j(ct.talk_thread_reply({"thread_id": "901", "message": "progress"}))
    assert r["status"] == "posted" and r["thread_id"] == "901" and r["message_id"] == "901"
    assert ocs.posts()[-1][2] == {"message": "progress", "threadId": 901}


def test_reply_defaults_to_the_current_thread(monkeypatch, ocs):
    _in_talk(monkeypatch, thread="77")
    j(ct.talk_thread_reply({"message": "here"}))
    assert ocs.posts()[-1][2] == {"message": "here", "threadId": 77}


def test_reply_without_any_thread_is_an_error(monkeypatch, ocs):
    _in_talk(monkeypatch)
    assert "thread_id is required" in j(ct.talk_thread_reply({"message": "x"}))["error"]
    assert "thread_id is required" in j(ct.talk_thread_reply({"message": "x", "thread_id": "abc"}))["error"]


def test_capabilities_are_cached(monkeypatch, ocs):
    _in_talk(monkeypatch)
    ct.talk_thread_reply({"thread_id": "1", "message": "a"})
    ct.talk_thread_reply({"thread_id": "1", "message": "b"})
    assert sum(1 for c in ocs.calls if c[1] == "/ocs/v2.php/cloud/capabilities") == 1
