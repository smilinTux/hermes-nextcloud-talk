"""v1.11.0 poll tools and server tools (tools.py) with a fake OCS transport. No network.

Shapes follow live Talk 24.0.6 answers (nor and nam, 2026-10-30): a one-to-one
room refuses polls, a federated room drops threadId, the author sees only
numVoters until a poll is closed."""

import importlib.util
import json
import os
import re

import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_spec = importlib.util.spec_from_file_location("nctalk_poll_tools", os.path.join(_ROOT, "tools.py"))
ct = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ct)
ps = ct._pollstate()

ONE, GROUP = 1, 2
KEY1, KEY2, KEY3 = ps.KEYCAPS[:3]


class FakeOCS:
    def __init__(self):
        self.rooms = {}       # token -> {"type": int, "remoteServer": str}
        self.features = ["threads", "talk-polls", "scheduled-messages", "reactions"]
        self.calls = []
        self.next_msg = 500
        self.next_poll = 40
        self.polls = {}       # poll id -> dict
        self.reactions = {}   # message id -> {emoji: [actors]}

    def __call__(self, method, path, *, json_body=None, params=None):
        self.calls.append((method, path, json_body))
        if path == "/ocs/v2.php/cloud/capabilities":
            return 200, {"capabilities": {"spreed": {"features": self.features}}}
        m = re.match(r"/ocs/v2.php/apps/spreed/api/v4/room/([a-z0-9]+)(/capabilities)?$", path)
        if m:
            if m.group(2):
                return 200, {"features": self.features}
            return 200, self.rooms.get(m.group(1), {"type": GROUP})
        m = re.match(r"/ocs/v2.php/apps/spreed/api/v1/poll/([a-z0-9]+)(?:/(\d+))?$", path)
        if m:
            room, pid = m.group(1), m.group(2)
            if method == "POST" and not pid:
                if self.rooms.get(room, {}).get("type") == ONE:
                    return 400, {"error": "room"}
                self.next_poll += 1
                p = dict(json_body, id=self.next_poll, status=0, numVoters=0, votes=[])
                self.polls[str(self.next_poll)] = p
                return 201, p
            p = self.polls[pid]
            if method == "DELETE":
                p["status"] = 1
            return 200, p
        m = re.match(r"/ocs/v2.php/apps/spreed/api/v1/reaction/[a-z0-9]+/(\d+)$", path)
        if m:
            if method == "POST":
                self.reactions.setdefault(m.group(1), {}).setdefault(json_body["reaction"], []).append(
                    {"actorType": "users", "actorId": "hermes-bot", "actorDisplayName": "Hermes"})
                return 201, {}
            return 200, self.reactions.get(m.group(1), {})
        if path.endswith("/schedule"):
            if method == "GET":
                return 200, [{"id": "77", "sendAt": 1900000000, "threadId": 0, "silent": False,
                              "message": "later"}]
            return 201, dict(json_body, id="146488433511579649")
        if "/schedule/" in path:
            return 200, {}
        m = re.match(r".*/chat/[a-z0-9]+/threads/(\d+)$", path)
        if m:
            return 200, {"thread": {"id": int(m.group(1)), "title": "T"}, "attendee": {"notificationLevel": 0}}
        if method == "POST" and "/chat/" in path:
            self.next_msg += 1
            return 201, {"id": self.next_msg}
        return 404, {}

    def posts(self, frag):
        return [c for c in self.calls if c[0] == "POST" and frag in c[1]]


@pytest.fixture
def ocs(monkeypatch):
    for k in ("HERMES_SESSION_PLATFORM", "HERMES_SESSION_CHAT_ID", "HERMES_SESSION_THREAD_ID"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("NEXTCLOUD_TALK_SERVER_URL", "https://cloud.example.com")
    monkeypatch.setenv("NEXTCLOUD_TALK_USERNAME", "hermes-bot")
    monkeypatch.setenv("NEXTCLOUD_TALK_APP_PASSWORD", "x" * 12)  # filler, not a credential
    fake = FakeOCS()
    monkeypatch.setattr(ct, "_request", fake)
    ct._caps_cache.clear()
    ct._room_caps_cache.clear()
    ct._room_info_cache.clear()
    return fake


def _in_talk(monkeypatch, room, thread=""):
    monkeypatch.setenv("HERMES_SESSION_PLATFORM", "nctalk")
    monkeypatch.setenv("HERMES_SESSION_CHAT_ID", room)
    if thread:
        monkeypatch.setenv("HERMES_SESSION_THREAD_ID", thread)


def _call(fn, **args):
    return json.loads(fn(args))


# -- create --------------------------------------------------------------------


def test_group_thread_gets_native_poll_with_fill_in(ocs, monkeypatch):
    _in_talk(monkeypatch, "grp1", "12245")
    out = _call(ct.talk_poll_create, question="Which way?", options=["A", "B", "C"])
    assert out["kind"] == "native" and out["thread_id"] == "12245"
    body = ocs.posts("/poll/grp1")[0][2]
    assert body["options"] == ["A", "B", "C", ps.FILL_IN_LABEL]
    assert body["threadId"] == 12245 and body["maxVotes"] == 1 and body["resultMode"] == 0
    rec = ps.find(ct._poll_store(), room="grp1", thread="12245")[0]
    assert rec["kind"] == "native" and rec["poll_id"] == out["poll_id"]


def test_multi_choice_allows_any_number_of_votes(ocs, monkeypatch):
    _in_talk(monkeypatch, "grp1")
    _call(ct.talk_poll_create, question="Which?", options=["A", "B"], multi=True, fill_in=False)
    body = ocs.posts("/poll/grp1")[0][2]
    assert body["maxVotes"] == 0 and body["options"] == ["A", "B"]


def test_dm_gets_reaction_poll_with_keycaps_preplaced(ocs, monkeypatch):
    ocs.rooms["dmx1"] = {"type": ONE}
    _in_talk(monkeypatch, "dmx1", "10")
    out = _call(ct.talk_poll_create, question="Next?", options=["Fix it", "Ship it", "Park it"])
    assert out["kind"] == "reaction" and "one-to-one" in out["note"]
    assert not ocs.posts("/poll/")  # Talk would refuse it: never even tried
    post = ocs.posts("/chat/dmx1")[0][2]
    assert post["threadId"] == 10
    assert f"{KEY1} Fix it" in post["message"] and "Something else" in post["message"]
    assert [c[2]["reaction"] for c in ocs.posts("/reaction/")] == [KEY1, KEY2, KEY3]


def test_federated_thread_gets_reaction_poll_but_main_timeline_native(ocs, monkeypatch):
    ocs.rooms["fed1"] = {"type": GROUP, "remoteServer": "https://other.example.com"}
    _in_talk(monkeypatch, "fed1", "5196")
    assert _call(ct.talk_poll_create, question="Q", options=["a", "b"])["kind"] == "reaction"
    monkeypatch.delenv("HERMES_SESSION_THREAD_ID")
    assert _call(ct.talk_poll_create, question="Q2", options=["a", "b"])["kind"] == "native"


def test_one_open_poll_per_thread(ocs, monkeypatch):
    _in_talk(monkeypatch, "grp1", "7")
    _call(ct.talk_poll_create, question="Q", options=["a", "b"])
    out = _call(ct.talk_poll_create, question="Q again", options=["a", "b"])
    assert "already an open poll" in out["error"]


def test_needs_two_real_options_and_drops_other(ocs, monkeypatch):
    _in_talk(monkeypatch, "grp1")
    assert "2 to" in _call(ct.talk_poll_create, question="Q", options=["only", "Other"])["error"]
    assert "at most" in _call(ct.talk_poll_create, question="Q", options=list("abcdefg"))["error"]
    assert "question" in _call(ct.talk_poll_create, options=["a", "b"])["error"]


# -- status / close ---------------------------------------------------------------


def test_status_native_open_explains_hidden_votes_and_close_reveals(ocs, monkeypatch):
    _in_talk(monkeypatch, "grp1", "7")
    out = _call(ct.talk_poll_create, question="Q", options=["a", "b"])
    p = ocs.polls[out["poll_id"]]
    p["numVoters"] = 1
    st = _call(ct.talk_poll_status)
    assert st["voters"] == 1 and not st["closed"] and "hides" in st["note"]
    p.update(votes={"option-1": 1}, details=[{"actorType": "users", "actorId": "chef",
                                              "actorDisplayName": "Chef", "optionId": 1}])
    closed = _call(ct.talk_poll_close)
    assert closed["closed"] and closed["who"] == {"b": ["Chef"]} and closed["counts"] == {"b": 1}
    assert ps.find(ct._poll_store(), room="grp1") == []  # nothing open any more


def test_status_reaction_poll_ignores_our_own_keycaps(ocs, monkeypatch):
    ocs.rooms["dmx1"] = {"type": ONE}
    _in_talk(monkeypatch, "dmx1")
    out = _call(ct.talk_poll_create, question="Q", options=["a", "b"])
    ocs.reactions[out["message_id"]][KEY2].append(
        {"actorType": "users", "actorId": "chef", "actorDisplayName": "Chef"})
    st = _call(ct.talk_poll_status)
    assert st["votes"] == {"b": ["Chef"]}


def test_status_without_open_poll_says_so(ocs, monkeypatch):
    _in_talk(monkeypatch, "grp1", "7")
    assert "no open poll" in _call(ct.talk_poll_status)["error"]


# -- scheduled messages ------------------------------------------------------------


def test_parse_when():
    now = 1_800_000_000
    assert ct.parse_when("in 2 hours", now) == now + 7200
    assert ct.parse_when("90m", now) == now + 5400
    assert ct.parse_when("1d", now) == now + 86400
    assert ct.parse_when("2026-11-02T09:00:00+00:00", now) == 1793610000
    assert ct.parse_when("whenever", now) is None


def test_schedule_into_current_thread(ocs, monkeypatch):
    _in_talk(monkeypatch, "grp1", "12245")
    out = _call(ct.talk_message_schedule, message="Check the oven", when="in 2 hours", silent=True)
    assert out["status"] == "scheduled" and out["thread_id"] == "12245"
    body = ocs.posts("/schedule")[0][2]
    assert body["threadId"] == 12245 and body["silent"] is True and body["message"] == "Check the oven"


def test_schedule_refuses_federated_past_and_missing_feature(ocs, monkeypatch):
    _in_talk(monkeypatch, "grp1")
    assert "at least a minute" in _call(ct.talk_message_schedule, message="x", when="2001-01-01T00:00")["error"]
    ocs.rooms["fed1"] = {"type": GROUP, "remoteServer": "https://other.example.com"}
    out = _call(ct.talk_message_schedule, message="x", when="1h", room="fed1", other_room=True)
    assert "federated" in out["error"]
    ocs.features = ["threads"]
    ct._caps_cache.clear()
    assert "no scheduled" in _call(ct.talk_message_schedule, message="x", when="1h")["error"]


def test_schedule_list_and_cancel(ocs, monkeypatch):
    _in_talk(monkeypatch, "grp1")
    assert _call(ct.talk_message_schedule, action="list")["scheduled"][0]["id"] == "77"
    assert _call(ct.talk_message_schedule, action="cancel", scheduled_id="77")["status"] == "cancelled"
    assert ocs.calls[-1][0] == "DELETE" and ocs.calls[-1][1].endswith("/schedule/77")


# -- OCS escape hatch ----------------------------------------------------------------


@pytest.mark.parametrize("method,path,ok", [
    ("GET", "/ocs/v2.php/apps/user_status/api/v1/statuses/chef", True),
    ("GET", "/ocs/v2.php/cloud/users/chef", True),
    ("GET", "/ocs/v2.php/search/providers/files/search", True),
    ("GET", "/ocs/v2.php/apps/spreed/api/v4/room/abcd1234/participants", True),
    ("POST", "/ocs/v2.php/apps/spreed/api/v1/reaction/abcd1234/55", True),
    ("GET", "/ocs/v2.php/cloud/users", False),                      # user listing (admin)
    ("PUT", "/ocs/v2.php/cloud/users/chef", False),                 # edit a user
    ("POST", "/ocs/v2.php/cloud/users/chef/disable", False),
    ("POST", "/ocs/v2.php/apps/files_sharing/api/v1/shares", False),
    ("GET", "/ocs/v2.php/apps/spreed/api/v4/room/abcd1234/../../x", False),
    ("GET", "/ocs/v2.php/cloud/capabilities?x=1", False),
    ("POST", "/ocs/v2.php/apps/spreed/api/v1/chat/abcd1234", False),  # plain posting: use the reply
])
def test_ocs_allowlist(method, path, ok):
    assert ct.ocs_allowed(method, path) is ok


def test_ocs_call_refuses_and_passes_through(ocs):
    out = _call(ct.nextcloud_ocs_call, method="GET", path="/ocs/v2.php/cloud/users")
    assert "not on the allowlist" in out["error"]
    out = _call(ct.nextcloud_ocs_call, path="/ocs/v2.php/cloud/capabilities")
    assert out["status"] == 200 and "capabilities" in out["data"]
    assert "URL" in _call(ct.nextcloud_ocs_call, path="https://evil.example/ocs/v2.php/cloud/user")["error"]


def test_silent_flag_on_thread_reply(ocs, monkeypatch):
    _in_talk(monkeypatch, "grp1", "12")
    _call(ct.talk_thread_reply, message="quiet progress", silent=True)
    body = [c for c in ocs.calls if c[0] == "POST" and c[1].endswith("/chat/grp1")][0][2]
    assert body == {"message": "quiet progress", "threadId": 12, "silent": True}
