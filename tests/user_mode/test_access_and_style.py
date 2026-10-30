"""v1.12.0: the per-agent access file (who may steer the agent: messages, poll
votes, answer reactions), and the outbound filter that strips em/en dash
punctuation from everything the agent posts. No network."""

import os
import time

import httpx

from user import access, pollstate, textstyle
from user.adapter import NextcloudTalkUserAdapter
from gateway.config import PlatformConfig

import importlib.util as _ilu
import sys as _sys

# Reuse the poll suite's Talk fake and message builders (importlib import mode
# does not put this directory on sys.path).
_spec = _ilu.spec_from_file_location(
    "nctalk_test_polls_adapter", os.path.join(os.path.dirname(__file__), "test_polls_adapter.py"))
_polls = _ilu.module_from_spec(_spec)
_sys.modules[_spec.name] = _polls
_spec.loader.exec_module(_polls)
KEY1, Talk, _native, _poll_voted = _polls.KEY1, _polls.Talk, _polls._native, _polls._poll_voted
_reaction, _reaction_poll, _rooms = _polls._reaction, _polls._reaction_poll, _polls._rooms
_run, _say, _store, _wire = _polls._run, _polls._say, _polls._store, _polls._wire

FED_CASEY = "mrarch@skhub.nativeassetmanagement.com"


def _access_file(tmp_path, body):
    p = tmp_path / "talk-access.yaml"
    p.write_text(body)
    return p


def _adapter(tmp_path, monkeypatch, body=None, **extra):
    # The adapter writes the gateway's env list when a file is present; let
    # monkeypatch restore it after the test.
    monkeypatch.setenv("NEXTCLOUD_TALK_ALLOWED_USERS", "")
    monkeypatch.setenv("NEXTCLOUD_TALK_ALLOW_ALL_USERS", "")
    if body is not None:
        monkeypatch.setenv(access.ACCESS_FILE_ENV, str(_access_file(tmp_path, body)))
    extra.setdefault("poll_settle_seconds", "0")
    a = NextcloudTalkUserAdapter(PlatformConfig(extra=dict(extra)))
    a._threads_enabled = True
    a._capabilities_checked = True
    return a


FILE = f"""
users: [chef, {FED_CASEY}]
agents: [jarvis@skhub.nativeassetmanagement.com]
rooms:
  side1:
    users: [guest1]
  only1:
    users: [solo]
    only: true
"""


# -- access file -------------------------------------------------------------------


def test_file_overrides_allow_all_env(tmp_path, monkeypatch):
    a = _adapter(tmp_path, monkeypatch, FILE, allow_all_users="true")
    assert a._access.source == "file"
    assert a._sender_allowed("chef") and a._sender_allowed("CHEF")
    assert a._sender_allowed(FED_CASEY) and a._sender_allowed("federated_users/" + FED_CASEY)
    assert not a._sender_allowed("mallory")


def test_room_rules_add_or_replace(tmp_path, monkeypatch):
    a = _adapter(tmp_path, monkeypatch, FILE)
    assert a._sender_allowed("guest1", "side1") and not a._sender_allowed("guest1", "other")
    assert a._sender_allowed("solo", "only1") and not a._sender_allowed("chef", "only1")
    assert a._sender_allowed("jarvis@skhub.nativeassetmanagement.com", "only1")  # agents always listed


def test_gateway_list_follows_the_file(tmp_path, monkeypatch):
    a = _adapter(tmp_path, monkeypatch, FILE)
    ids = os.environ["NEXTCLOUD_TALK_ALLOWED_USERS"].split(",")
    assert {"chef", FED_CASEY, "jarvis@skhub.nativeassetmanagement.com", "guest1", "solo"} <= set(ids)
    assert os.environ["NEXTCLOUD_TALK_ALLOW_ALL_USERS"] == "false"
    assert a.config.extra["allowed_users"] == os.environ["NEXTCLOUD_TALK_ALLOWED_USERS"]


def test_file_reload_on_change(tmp_path, monkeypatch):
    monkeypatch.setattr(access, "RECHECK_SECONDS", 0.0)
    a = _adapter(tmp_path, monkeypatch, "users: [chef]\n")
    assert not a._sender_allowed("caseyK")
    p = tmp_path / "talk-access.yaml"
    p.write_text("users: [chef, caseyK]\n")
    os.utime(p, (time.time() + 5, time.time() + 5))
    assert a._sender_allowed("caseyk")
    assert "caseyK" in os.environ["NEXTCLOUD_TALK_ALLOWED_USERS"].split(",")


def test_broken_file_keeps_last_good(tmp_path, monkeypatch):
    monkeypatch.setattr(access, "RECHECK_SECONDS", 0.0)
    a = _adapter(tmp_path, monkeypatch, "users: [chef]\n")
    p = tmp_path / "talk-access.yaml"
    p.write_text("users: [chef\n  bad: : :")
    os.utime(p, (time.time() + 5, time.time() + 5))
    assert a._sender_allowed("chef") and not a._sender_allowed("mallory")


def test_no_file_keeps_env_behavior(tmp_path, monkeypatch):
    a = _adapter(tmp_path, monkeypatch, None, allowed_users="chef")
    assert a._access.source == "env"
    assert a._sender_allowed("chef") and not a._sender_allowed("mallory")
    b = _adapter(tmp_path, monkeypatch, None, allow_all_users="true")
    assert b._sender_allowed("anyone")


def test_broken_file_with_no_history_falls_back_to_env(tmp_path, monkeypatch):
    a = _adapter(tmp_path, monkeypatch, "- not\n- a mapping\n", allowed_users="chef")
    assert a._access.source == "env"
    assert a._sender_allowed("chef") and not a._sender_allowed("mallory")


# -- intake gate -------------------------------------------------------------------


def test_unlisted_message_takes_no_turn_and_does_not_answer_a_poll(tmp_path, monkeypatch):
    a, talk = _adapter(tmp_path, monkeypatch, FILE), Talk()
    _rooms(a)
    _wire(a, talk)
    _native(a, talk, room="dmx1", thread="12245")
    _run(a, _say(500, "my own idea", thread="12245", actor="mallory"), room="dmx1")
    assert a.handled_events == []
    assert pollstate.get(_store(a), "native:dmx1:4")["status"] == "open"
    _run(a, _say(501, "my own idea", thread="12245", actor="chef"), room="dmx1")
    assert len(a.handled_events) == 1


def test_unlisted_vote_does_not_wake_the_agent(tmp_path, monkeypatch):
    a, talk = _adapter(tmp_path, monkeypatch, FILE), Talk()
    _rooms(a)
    _wire(a, talk)
    _native(a, talk, voter="mallory")
    _run(a, _poll_voted(12249, "4"))
    assert a.handled_events == []


def test_federated_casey_vote_counts(tmp_path, monkeypatch):
    a, talk = _adapter(tmp_path, monkeypatch, FILE), Talk()
    _rooms(a)
    _wire(a, talk)
    _native(a, talk, voter=FED_CASEY)
    _run(a, _poll_voted(12249, "4"))
    assert len(a.handled_events) == 1


def test_unlisted_keycap_reaction_does_not_decide(tmp_path, monkeypatch):
    a, talk = _adapter(tmp_path, monkeypatch, FILE), Talk()
    _rooms(a)
    _wire(a, talk)
    _reaction_poll(a)
    talk.reactions["900"] = {KEY1: [{"actorId": "mallory", "actorType": "users",
                                     "actorDisplayName": "Mallory"}]}
    _run(a, _reaction(901, KEY1, "900", actor="mallory", thread="10"), room="dmx1")
    assert a.handled_events == []


def test_unlisted_check_mark_is_not_an_answer(tmp_path, monkeypatch):
    a, talk = _adapter(tmp_path, monkeypatch, FILE), Talk()
    _rooms(a)
    _wire(a, talk)
    _run(a, _reaction(902, "✅", "800", actor="mallory"))
    assert a.handled_events == []


# -- dash filter ---------------------------------------------------------------------


def test_undash_rules():
    u = textstyle.undash
    assert u("It works — mostly.") == "It works, mostly."
    assert u("It works—mostly.") == "It works, mostly."
    assert u("pages 5–10 and 5 – 10") == "pages 5-10 and 5-10"
    assert u("Mon–Fri") == "Mon-Fri"
    assert u("— first\n– second") == "- first\n- second"
    assert u("Here is the plan —\nstep one") == "Here is the plan:\nstep one"
    assert u("done —.") == "done."
    assert u("a - b stays") == "a - b stays"
    assert u("no dashes at all") == "no dashes at all"


def test_undash_leaves_code_and_urls_alone():
    u = textstyle.undash
    assert u("run `a—b` now — ok") == "run `a—b` now, ok"
    assert u("```\nx — y\n```\nthen — go") == "```\nx — y\n```\nthen, go"
    assert u("see https://x.example/a–b — there") == "see https://x.example/a–b, there"


def test_undash_body_fields():
    body = textstyle.undash_body({"message": "a — b", "threadTitle": "x—y", "replyTo": 3,
                                  "options": ["one — two", 5], "question": "q – r"})
    assert body == {"message": "a, b", "threadTitle": "x, y", "replyTo": 3,
                    "options": ["one, two", 5], "question": "q, r"}


def test_send_and_edit_strip_dashes(tmp_path, monkeypatch):
    a = _adapter(tmp_path, monkeypatch, None, allowed_users="chef")
    sent = []

    def handler(request):
        import json as _j
        sent.append(_j.loads(request.content or b"{}").get("message"))
        return httpx.Response(201, json={"ocs": {"data": {"id": 7}}})

    a._http_client = httpx.AsyncClient(base_url="https://cloud.example.com",
                                       transport=httpx.MockTransport(handler))
    import asyncio
    asyncio.run(a.send("room1", "Done — shipped 1–2 fixes"))
    asyncio.run(a.edit_message("room1", "7", "Edited — again"))
    assert sent == ["Done, shipped 1-2 fixes", "Edited, again"]


def test_mini_yaml_matches_the_documented_shapes():
    text = """
# comment
users:
  - dknestri        # Chef
  - mrarch@skhub.nativeassetmanagement.com
agents: [jarvis@skhub.nativeassetmanagement.com, lumina@x.example]
rooms:
  abc1:
    users: [carol]
    only: true
  abc2: {users: [dave, erin], only: false}
"""
    data = access._mini_yaml(text)
    assert data == {"users": ["dknestri", "mrarch@skhub.nativeassetmanagement.com"],
                    "agents": ["jarvis@skhub.nativeassetmanagement.com", "lumina@x.example"],
                    "rooms": {"abc1": {"users": ["carol"], "only": True},
                              "abc2": {"users": ["dave", "erin"], "only": False}}}
    try:
        import yaml
    except ImportError:
        return
    assert yaml.safe_load(text) == data


def test_without_pyyaml_the_file_still_loads(tmp_path, monkeypatch):
    import builtins
    real_import = builtins.__import__

    def no_yaml(name, *args, **kwargs):
        if name == "yaml":
            raise ImportError("no yaml")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", no_yaml)
    a = _adapter(tmp_path, monkeypatch, FILE)
    assert a._access.source == "file"
    assert a._sender_allowed("guest1", "side1") and not a._sender_allowed("chef", "only1")
