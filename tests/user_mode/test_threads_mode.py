"""v1.14.0: per-room threads mode (/threads on|off), main-timeline replies open threads."""

import asyncio
import json

import httpx

from gateway.config import PlatformConfig
from user import threadsmode
from user.adapter import (ROOM_TYPE_GROUP, ROOM_TYPE_ONE_TO_ONE, NextcloudTalkUserAdapter,
                          _fallback_title)


def _run(coro):
    return asyncio.run(coro)


def _adapter(tmp_path, monkeypatch, **extra):
    monkeypatch.setenv("NEXTCLOUD_TALK_STATE_DIR", str(tmp_path))
    extra.setdefault("allowed_users", "chef")
    extra.setdefault("working_reaction", "")
    a = NextcloudTalkUserAdapter(PlatformConfig(extra=dict(extra)))
    a._threads_enabled = True
    a._capabilities_checked = True
    a._room_meta["dm1"] = {"token": "dm1", "type": ROOM_TYPE_ONE_TO_ONE, "displayName": "chef"}
    return a


class Talk:
    def __init__(self, host_features=("threads",), fail_thread=False):
        self.posts = []
        self.host_features = list(host_features)
        self.fail_thread = fail_thread

    def __call__(self, request):
        path = request.url.path
        if request.method == "POST":
            body = json.loads(request.content)
            self.posts.append(body)
            if self.fail_thread and "threadTitle" in body:
                return httpx.Response(500, json={})
            return httpx.Response(201, json={"ocs": {"data": {"id": 500 + len(self.posts)}}})
        if path.endswith("/capabilities"):
            return httpx.Response(200, json={"ocs": {"data": {"features": self.host_features}}})
        if "/threads/" in path:
            tid = int(path.rsplit("/", 1)[1])
            return httpx.Response(200, json={"ocs": {"data": {
                "thread": {"id": tid, "title": "T"}, "attendee": {"notificationLevel": 0},
                "first": {"id": tid, "actorId": "hermes-bot", "actorType": "users", "message": "ans",
                          "messageType": "comment", "timestamp": 1, "isThread": True, "threadId": tid}}}})
        return httpx.Response(200, json={"ocs": {"data": []}})


def _wire(a, talk):
    a._http_client = httpx.AsyncClient(base_url="https://cloud.example.com",
                                       transport=httpx.MockTransport(talk))


def _msg(msg_id, actor, text, thread=None, actor_type="users"):
    m = {"id": msg_id, "actorId": actor, "actorType": actor_type, "actorDisplayName": actor,
         "message": text, "messageType": "comment", "messageParameters": {}, "timestamp": 1700000000,
         "threadId": msg_id}
    if thread:
        m.update(isThread=True, threadId=thread, threadTitle="T")
    return m


def _on(a, talk, token="dm1"):
    _wire(a, talk)
    _run(a._on_message(token, _msg(1, "chef", "/threads on")))


# -- toggle -------------------------------------------------------------------


def test_default_off_and_status(tmp_path, monkeypatch):
    a, talk = _adapter(tmp_path, monkeypatch), Talk()
    _wire(a, talk)
    _run(a._on_message("dm1", _msg(1, "chef", "/threads")))
    assert "off" in talk.posts[0]["message"] and a.handled_events == []


def test_toggle_persists_across_restart(tmp_path, monkeypatch):
    a, talk = _adapter(tmp_path, monkeypatch), Talk()
    _on(a, talk)
    assert "is on" in talk.posts[0]["message"]
    b = _adapter(tmp_path, monkeypatch)
    assert b._threads_mode_stored("dm1") and not b._threads_mode_stored("other")
    _wire(b, talk)
    _run(b._on_message("dm1", _msg(2, "chef", "/threads off")))
    assert not _adapter(tmp_path, monkeypatch)._threads_mode_stored("dm1")


def test_env_seed_and_stored_off_beats_it(tmp_path, monkeypatch):
    monkeypatch.setenv("NEXTCLOUD_TALK_THREADS_MODE_ROOMS", "dm1, g2")
    a, talk = _adapter(tmp_path, monkeypatch), Talk()
    assert a._threads_mode_stored("dm1") and a._threads_mode_stored("g2")
    _wire(a, talk)
    _run(a._on_message("dm1", _msg(1, "chef", "/threads off")))
    assert not a._threads_mode_stored("dm1") and a._threads_mode_stored("g2")


def test_state_file_is_per_account(tmp_path, monkeypatch):
    monkeypatch.setenv("NEXTCLOUD_TALK_STATE_DIR", str(tmp_path))
    assert (threadsmode.state_path("https://a", "x") != threadsmode.state_path("https://a", "y"))


def test_non_allowed_human_cannot_toggle(tmp_path, monkeypatch):
    a, talk = _adapter(tmp_path, monkeypatch), Talk()
    _wire(a, talk)
    _run(a._on_message("dm1", _msg(1, "mallory", "/threads on")))
    assert not a._threads_mode_stored("dm1") and a.handled_events == []
    assert "Only people allowed" in talk.posts[0]["message"]


def test_agent_cannot_toggle(tmp_path, monkeypatch):
    a, talk = _adapter(tmp_path, monkeypatch, allowed_users="chef,jarvis", agent_users="jarvis"), Talk()
    _wire(a, talk)
    _run(a._on_message("dm1", _msg(1, "jarvis", "/threads on")))
    assert not a._threads_mode_stored("dm1") and a.handled_events == [] and talk.posts == []


def test_threads_command_not_forwarded_to_core(tmp_path, monkeypatch):
    a, talk = _adapter(tmp_path, monkeypatch), Talk()
    _on(a, talk)
    assert a.handled_events == []
    _run(a._on_message("dm1", _msg(3, "chef", "/status")))
    assert len(a.handled_events) == 1


# -- behaviour ----------------------------------------------------------------


def _ask(a, text="Plan my taxes for this year please", msg_id=10):
    _run(a._on_message("dm1", _msg(msg_id, "chef", text)))
    ev = a.handled_events[-1]
    _run(a.on_processing_start(ev))  # what the core does before the model runs
    return ev


def test_on_reply_opens_thread_with_model_title(tmp_path, monkeypatch):
    a, talk = _adapter(tmp_path, monkeypatch), Talk()
    _on(a, talk)
    ev = _ask(a)
    assert "Thread title:" in ev.text
    n = len(talk.posts)
    res = _run(a.send("dm1", "Thread title: Tax plan 2026\nHere is the plan.", reply_to="10"))
    assert res.success and res.message_id == str(500 + n + 1)
    body = talk.posts[n]
    assert body["threadTitle"] == "Tax plan 2026" and body["message"] == "Here is the plan."
    assert "replyTo" not in body
    # later message of the same turn follows into the thread
    _run(a.send("dm1", "more", reply_to="10"))
    assert talk.posts[-1]["threadId"] == int(res.message_id) and "replyTo" not in talk.posts[-1]


def test_on_reply_fallback_title(tmp_path, monkeypatch):
    a, talk = _adapter(tmp_path, monkeypatch), Talk()
    _on(a, talk)
    _ask(a, "**Hey** @hermes-bot can you plan my taxes \u2014 for this year and next one please")
    _run(a.send("dm1", "Sure.", reply_to="10"))
    title = talk.posts[-1]["threadTitle"]
    assert title == "Hey can you plan my taxes, for this"
    assert "\u2014" not in title and "@" not in title
    assert _fallback_title("   ") == "New thread"


def test_followup_in_new_thread_routes_to_thread_session_with_seed(tmp_path, monkeypatch):
    a, talk = _adapter(tmp_path, monkeypatch), Talk()
    _on(a, talk)
    _ask(a, "taxes please")
    res = _run(a.send("dm1", "On it.", reply_to="10"))
    tid = int(res.message_id)
    _run(a._on_message("dm1", _msg(tid + 1, "chef", "and the car?", thread=tid)))
    ev = a.handled_events[-1]
    assert ev.source.thread_id == str(tid) and "chef: taxes please" in ev.text


def test_on_in_thread_reply_stays_in_thread(tmp_path, monkeypatch):
    a, talk = _adapter(tmp_path, monkeypatch), Talk()
    _on(a, talk)
    _run(a._on_message("dm1", _msg(21, "chef", "in thread", thread=20)))
    ev = a.handled_events[-1]
    assert "Threads mode" not in ev.text
    n = len(talk.posts)
    _run(a.send("dm1", "reply", reply_to="21", metadata={"thread_id": "20"}))
    assert "threadTitle" not in talk.posts[n] and talk.posts[n]["threadId"] == 20


def test_on_unrelated_sends_unchanged(tmp_path, monkeypatch):
    a, talk = _adapter(tmp_path, monkeypatch), Talk()
    _on(a, talk)
    n = len(talk.posts)
    _run(a.send("dm1", "cron result"))
    assert talk.posts[n] == {"message": "cron result"}
    _ask(a, "/status")
    assert "Threads mode" not in a.handled_events[-1].text


def test_off_is_unchanged(tmp_path, monkeypatch):
    a, talk = _adapter(tmp_path, monkeypatch), Talk()
    _wire(a, talk)
    ev = _ask(a)
    assert "Threads mode" not in ev.text and a._thread_turns == {}
    _run(a.send("dm1", "Thread title: x\nhi", reply_to="10"))
    assert talk.posts[-1] == {"message": "Thread title: x\nhi", "replyTo": 10}


def test_thread_refused_falls_back_to_main_timeline(tmp_path, monkeypatch):
    a, talk = _adapter(tmp_path, monkeypatch), Talk()
    _on(a, talk)
    _ask(a)
    talk.fail_thread = True
    res = _run(a.send("dm1", "hi", reply_to="10"))
    assert res.success and talk.posts[-1] == {"message": "hi", "replyTo": 10}


def test_federated_host_without_threads_behaves_off(tmp_path, monkeypatch):
    a, talk = _adapter(tmp_path, monkeypatch, room_modes="fed1=free"), Talk(host_features=["chat-v2"])
    a._room_meta["fed1"] = {"token": "fed1", "type": ROOM_TYPE_GROUP, "displayName": "r",
                            "remoteServer": "https://other.example.com"}
    _wire(a, talk)
    _run(a._on_message("fed1", _msg(1, "chef", "/threads on")))
    assert "does not support threads" in talk.posts[-1]["message"]
    _run(a._on_message("fed1", _msg(2, "chef", "hello there")))
    assert "Threads mode" not in a.handled_events[-1].text and a._thread_turns == {}


# -- review fixes ---------------------------------------------------------------


def _access_file(tmp_path, monkeypatch):
    f = tmp_path / "access.yaml"
    f.write_text("users: [chef]\nagents: [jarvis]\n")
    return str(f)


def test_agent_listed_only_in_access_file_cannot_toggle(tmp_path, monkeypatch):
    a, talk = _adapter(tmp_path, monkeypatch, access_file=_access_file(tmp_path, monkeypatch)), Talk()
    _wire(a, talk)
    _run(a._on_message("dm1", _msg(1, "jarvis", "/threads on")))
    assert not a._threads_mode_stored("dm1") and a.handled_events == [] and talk.posts == []
    _run(a._on_message("dm1", _msg(2, "chef", "/threads on")))
    assert a._threads_mode_stored("dm1")


def test_bot_actor_cannot_toggle(tmp_path, monkeypatch):
    a, talk = _adapter(tmp_path, monkeypatch, allowed_users="chef,helper"), Talk()
    _wire(a, talk)
    _run(a._on_message("dm1", _msg(1, "helper", "/threads on", actor_type="bots")))
    assert not a._threads_mode_stored("dm1")


def test_env_allowlisted_user_may_toggle_without_access_file(tmp_path, monkeypatch):
    a, talk = _adapter(tmp_path, monkeypatch, allowed_users="chef"), Talk()
    _wire(a, talk)
    _run(a._on_message("dm1", _msg(1, "chef", "/threads on")))
    assert a._threads_mode_stored("dm1") is True


def test_gateway_only_grant_does_not_configure_rooms(tmp_path, monkeypatch):
    """Only the plugin's own allowlist decides; the gateway's private auth is never asked."""
    a, talk = _adapter(tmp_path, monkeypatch, allowed_users="nobody"), Talk()

    class Runner:
        def _is_user_authorized_for_source(self, source):
            raise AssertionError("private gateway method called")
    a.gateway_runner = Runner()
    _wire(a, talk)
    _run(a._on_message("dm1", _msg(1, "paired", "/threads on")))
    assert not a._threads_mode_stored("dm1")


def test_no_allowlist_at_all_configures_nothing(tmp_path, monkeypatch):
    monkeypatch.delenv("NEXTCLOUD_TALK_ALLOWED_USERS", raising=False)
    monkeypatch.delenv("NEXTCLOUD_TALK_ALLOW_ALL_USERS", raising=False)
    a, talk = _adapter(tmp_path, monkeypatch, allowed_users=""), Talk()
    _wire(a, talk)
    _run(a._on_message("dm1", _msg(1, "chef", "/threads on")))
    assert not a._threads_mode_stored("dm1")


def test_busy_ack_does_not_become_the_root_and_title_never_leaks(tmp_path, monkeypatch):
    a, talk = _adapter(tmp_path, monkeypatch), Talk()
    _on(a, talk)
    _run(a._on_message("dm1", _msg(10, "chef", "taxes")))  # armed, not yet started
    n = len(talk.posts)
    _run(a.send("dm1", "Busy, I will get to this next.", reply_to="10"))
    assert talk.posts[n] == {"message": "Busy, I will get to this next.", "replyTo": 10}
    _run(a.on_processing_start(a.handled_events[-1]))
    res = _run(a.send("dm1", "Thread title: Taxes\nAnswer.", reply_to="10"))
    assert talk.posts[-1] == {"message": "Answer.", "threadTitle": "Taxes"}
    assert res.message_id == str(500 + len(talk.posts))


def test_interim_commentary_title_stripped_later_interim_stays_main(tmp_path, monkeypatch):
    a, talk = _adapter(tmp_path, monkeypatch), Talk()
    _on(a, talk)
    _ask(a)
    _run(a.send("dm1", "Thread title: Tax plan\nLet me look that up."))  # no reply_to
    root = talk.posts[-1]
    root_id = 500 + len(talk.posts)
    assert root["threadTitle"] == "Tax plan" and root["message"] == "Let me look that up."
    _run(a.send("dm1", "Checking the forms."))  # later interim: no turn identity, so as with mode off
    assert talk.posts[-1] == {"message": "Checking the forms."}
    _run(a.send("dm1", "Thread title: Tax plan\nFinal.", reply_to="10"))
    assert talk.posts[-1]["message"] == "Final." and "threadTitle" not in talk.posts[-1]


def test_title_only_reply_posts_nothing_and_is_kept_for_the_root(tmp_path, monkeypatch):
    a, talk = _adapter(tmp_path, monkeypatch), Talk()
    _on(a, talk)
    _ask(a)
    n = len(talk.posts)
    res = _run(a.send("dm1", "Thread title: Kept title", reply_to="10"))
    assert res.success and len(talk.posts) == n
    _run(a.send("dm1", "Body.", reply_to="10"))
    assert talk.posts[-1] == {"message": "Body.", "threadTitle": "Kept title"}


def test_edit_strips_title_line(tmp_path, monkeypatch):
    a = _adapter(tmp_path, monkeypatch)
    puts = []

    def handler(request):
        if request.method == "PUT":
            puts.append(json.loads(request.content))
            return httpx.Response(200, json={"ocs": {"data": {}}})
        return Talk()(request)
    _wire(a, handler)
    _run(a._on_message("dm1", _msg(1, "chef", "/threads on")))
    _ask(a)
    _run(a.edit_message("dm1", "77", "Thread title: T\nBody so far"))
    assert puts[-1]["message"] == "Body so far"
    assert not _run(a.edit_message("dm1", "77", "Thread title: T")).success


# -- round 2: attribution, overlap, completion --------------------------------


def test_cron_and_unattributed_sends_never_enter_a_thread(tmp_path, monkeypatch):
    a, talk = _adapter(tmp_path, monkeypatch), Talk()
    _on(a, talk)
    _ask(a)
    _run(a.send("dm1", "Thread title: Taxes\nAnswer.", reply_to="10"))
    _run(a.send("dm1", "CRON: disk 91% full", metadata={"job_id": "j1"}))
    assert talk.posts[-1] == {"message": "CRON: disk 91% full"}
    _run(a.send("dm1", "plain alert"))
    assert talk.posts[-1] == {"message": "plain alert"}
    # even a cron post that happens to start with a title line is untouched
    _run(a.send("dm1", "Thread title: x\nbody", metadata={"job_id": "j1"}))
    assert talk.posts[-1] == {"message": "Thread title: x\nbody"}


def test_overlapping_turns_each_get_their_own_thread_and_title(tmp_path, monkeypatch):
    a, talk = _adapter(tmp_path, monkeypatch), Talk()
    _on(a, talk)
    _ask(a, "taxes", msg_id=10)
    _ask(a, "car", msg_id=11)
    r1 = _run(a.send("dm1", "Thread title: Taxes\nT answer", reply_to="10"))
    # commentary of turn 2 with two live turns cannot be attributed: stripped, main timeline
    _run(a.send("dm1", "Thread title: Car\nChecking car."))
    assert talk.posts[-1] == {"message": "Checking car."}
    r2 = _run(a.send("dm1", "Thread title: Car\nC answer", reply_to="11"))
    assert talk.posts[-1] == {"message": "C answer", "threadTitle": "Car"}
    assert r1.message_id != r2.message_id
    _run(a.send("dm1", "T more", reply_to="10"))
    assert talk.posts[-1] == {"message": "T more", "threadId": int(r1.message_id)}


def test_completion_drops_the_turn_after_grace(tmp_path, monkeypatch):
    a, talk = _adapter(tmp_path, monkeypatch), Talk()
    _on(a, talk)
    ev = _ask(a)
    _run(a.send("dm1", "Thread title: T\nA", reply_to="10"))
    from gateway.platforms.base import ProcessingOutcome
    _run(a.on_processing_complete(ev, ProcessingOutcome.SUCCESS))
    _run(a.send("dm1", "late final", reply_to="10"))  # inside the grace: still the thread's
    assert "threadId" in talk.posts[-1]
    a._thread_turns[("dm1", "10")]["done_at"] -= 60
    _run(a.send("dm1", "after", reply_to="10"))
    assert talk.posts[-1] == {"message": "after", "replyTo": 10}
    assert a._thread_turns == {}


def test_ttl_backstop_drops_a_turn_that_never_completes(tmp_path, monkeypatch):
    a, talk = _adapter(tmp_path, monkeypatch), Talk()
    _on(a, talk)
    _ask(a)
    a._thread_turns[("dm1", "10")]["at"] -= 7200
    _run(a.send("dm1", "late", reply_to="10"))
    assert talk.posts[-1] == {"message": "late", "replyTo": 10} and a._thread_turns == {}


# -- round 3: redirect leaves no phantom turn; /threads off clears turns --------


def test_redirected_followup_is_not_a_running_turn(tmp_path, monkeypatch):
    from gateway.platforms.base import ProcessingOutcome
    a, talk = _adapter(tmp_path, monkeypatch), Talk()
    _on(a, talk)
    ev_a = _ask(a, "taxes", msg_id=10)                       # turn A runs
    _run(a._on_message("dm1", _msg(11, "chef", "also the car")))  # B folded into A: no hooks for B
    assert a.handled_events[-1].message_id == "11"
    # A's titled commentary: B never started, so A is the single running turn
    _run(a.send("dm1", "Thread title: Taxes\nLooking.", reply_to="10"))
    assert talk.posts[-1]["threadTitle"] == "Taxes"
    ev_a.reply_anchor_override = "11"
    _run(a.on_processing_complete(ev_a, ProcessingOutcome.SUCCESS))
    a._thread_turns[("dm1", "10")]["done_at"] -= 60
    a._thread_turns[("dm1", "11")]["done_at"] -= 60
    _run(a.send("dm1", "Thread title: Nightly report\nAll green."))
    assert talk.posts[-1] == {"message": "Thread title: Nightly report\nAll green."}  # no thread, as mode off
    assert not any(k[0] == "dm1" for k in a._thread_turns)


def test_never_started_turn_does_not_block_title_attribution(tmp_path, monkeypatch):
    a, talk = _adapter(tmp_path, monkeypatch), Talk()
    _on(a, talk)
    _ask(a, "one", msg_id=10)
    _run(a._on_message("dm1", _msg(11, "chef", "two")))  # armed, never started
    _run(a.send("dm1", "Thread title: One\nFirst."))
    assert talk.posts[-1] == {"message": "First.", "threadTitle": "One"}


def test_threads_off_clears_armed_turns(tmp_path, monkeypatch):
    a, talk = _adapter(tmp_path, monkeypatch), Talk()
    _on(a, talk)
    _ask(a)
    assert a._thread_turns
    _run(a._on_message("dm1", _msg(12, "chef", "/threads off")))
    assert a._thread_turns == {}
    _run(a.send("dm1", "Thread title: x\nhi", reply_to="10"))
    assert talk.posts[-1] == {"message": "Thread title: x\nhi", "replyTo": 10}
