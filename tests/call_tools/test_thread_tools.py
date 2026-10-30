"""Thread tools (tools.py, v1.9.0): talk_thread_start / talk_thread_reply with a
fake OCS transport. No network."""

import importlib.util
import json
import os
import re

import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_spec = importlib.util.spec_from_file_location("nctalk_thread_tools", os.path.join(_ROOT, "tools.py"))
ct = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ct)


class FakeOCS:
    """Talk OCS for the thread tools: server caps, per-room (host) caps, thread info,
    recent threads, follow level and rename. ``room_threads`` overrides the host
    answer per room (a federated host without threads)."""

    def __init__(self, threads=True, post_status=201, post_id=901):
        self.threads = threads
        self.room_threads = {}
        self.post_status = post_status
        self.post_id = post_id
        self.levels = {}   # thread id -> our follow level
        self.titles = {}   # thread id -> title
        self.rename_status = 200
        self.calls = []

    def __call__(self, method, path, *, json_body=None, params=None):
        self.calls.append((method, path, json_body))
        if path == "/ocs/v2.php/cloud/capabilities":
            feats = ["chat-v2"] + (["threads"] if self.threads else [])
            return 200, {"capabilities": {"spreed": {"features": feats}}}
        m = re.match(r"/ocs/v2.php/apps/spreed/api/v4/room/([a-z0-9]+)/capabilities$", path)
        if m:
            has = self.room_threads.get(m.group(1), self.threads)
            return 200, {"features": ["chat-v2"] + (["threads"] if has else [])}
        if path.endswith("/threads/recent"):
            self.recent_params = params
            return 200, [self._info(t) for t in sorted(self.titles, reverse=True)]
        m = re.match(r".*/chat/[a-z0-9]+/threads/(\d+)(/notify)?$", path)
        if m:
            tid = m.group(1)
            if m.group(2):
                self.levels[tid] = json_body["level"]
                return 200, self._info(tid)
            if method == "PUT":
                if self.rename_status >= 300:
                    return self.rename_status, {"error": "permission"}
                self.titles[tid] = json_body["threadTitle"]
                return 200, self._info(tid)
            if tid not in self.titles:
                return 404, {"error": "thread"}
            return 200, self._info(tid)
        if self.post_status >= 300:
            return self.post_status, "nope"
        return self.post_status, {"id": self.post_id} if self.post_id else {}

    def _info(self, tid):
        return {"thread": {"id": int(tid), "title": self.titles.get(tid, ""), "numReplies": 2,
                           "lastActivity": 1700000000},
                "attendee": {"notificationLevel": self.levels.get(tid, 0)}}

    def posts(self):
        return [c for c in self.calls if c[0] == "POST" and "/threads/" not in c[1]]


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
    ct._room_caps_cache.clear()
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
    assert "threads are not available" in r["error"] and ocs.posts() == []


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


# -- v1.10.0: per-room (federated host) detection, list, close, reopen ----------


def test_federated_host_without_threads_is_refused(monkeypatch, ocs):
    _in_talk(monkeypatch, room="fedroom1")
    ocs.room_threads["fedroom1"] = False
    r = j(ct.talk_thread_start({"title": "t", "message": "m"}))
    assert "threads are not available" in r["error"] and ocs.posts() == []


def test_room_capabilities_are_cached_per_room(monkeypatch, ocs):
    _in_talk(monkeypatch)
    ct.talk_thread_start({"title": "t", "message": "a"})
    ct.talk_thread_start({"title": "t", "message": "b"})
    path = "/ocs/v2.php/apps/spreed/api/v4/room/xazze5d7/capabilities"
    assert sum(1 for c in ocs.calls if c[1] == path) == 1


def test_list_skips_closed_threads_unless_asked(monkeypatch, ocs):
    _in_talk(monkeypatch)
    ocs.titles = {"10": "Taxes", "11": "\u2705 Car"}
    ocs.levels = {"11": 3}
    r = j(ct.talk_thread_list({"limit": 500}))
    assert [t["thread_id"] for t in r["threads"]] == ["10"] and ocs.recent_params == {"limit": 50}
    r = j(ct.talk_thread_list({"include_closed": True}))
    assert [(t["thread_id"], t["closed"]) for t in r["threads"]] == [("11", True), ("10", False)]


def test_close_current_thread_marks_title_and_level(monkeypatch, ocs):
    _in_talk(monkeypatch, thread="10")
    ocs.titles = {"10": "Taxes"}
    r = j(ct.talk_thread_close({"summary": "Filed, receipt in Docs."}))
    assert r["status"] == "closed" and r["thread_id"] == "10" and r["title_marked"]
    assert ocs.levels["10"] == 3 and ocs.titles["10"] == "\u2705 Taxes"
    assert ocs.posts()[-1][2] == {"message": "Filed, receipt in Docs.", "threadId": 10}


def test_close_keeps_working_when_rename_is_refused(monkeypatch, ocs):
    _in_talk(monkeypatch)
    ocs.titles = {"12": "Someone else's"}
    ocs.rename_status = 403
    r = j(ct.talk_thread_close({"thread_id": "12"}))
    assert r["status"] == "closed" and r["title_marked"] is False and ocs.levels["12"] == 3


def test_close_unknown_thread_is_an_error(monkeypatch, ocs):
    _in_talk(monkeypatch)
    assert "not found" in j(ct.talk_thread_close({"thread_id": "99"}))["error"]


def test_unprompted_reply_into_closed_thread_is_refused(monkeypatch, ocs):
    _in_talk(monkeypatch)  # main timeline: posting into thread 10 is unprompted
    ocs.titles = {"10": "\u2705 Taxes"}
    ocs.levels = {"10": 3}
    r = j(ct.talk_thread_reply({"thread_id": "10", "message": "news"}))
    assert "is closed" in r["error"] and ocs.posts() == []
    r = j(ct.talk_thread_reply({"thread_id": "10", "message": "news", "reopen": True}))
    assert r["status"] == "posted" and r["reopened"] is True
    assert ocs.levels["10"] == 0 and ocs.titles["10"] == "Taxes"


def test_reply_inside_the_current_thread_skips_the_closed_check(monkeypatch, ocs):
    _in_talk(monkeypatch, thread="10")
    ocs.levels = {"10": 3}
    r = j(ct.talk_thread_reply({"message": "answer"}))
    assert r["status"] == "posted"
    assert not any("/threads/10" == c[1][-11:] for c in ocs.calls if c[0] == "GET")
