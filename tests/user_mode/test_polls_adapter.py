"""v1.11.0 adapter: the agent wakes when its poll is answered (vote, keycap reaction,
or a reply in the thread), check mark / cross reactions on its messages are turns,
and the server capability note goes out once per conversation. No network.

Message shapes are copied from live Talk 24.0.6 (2026-10-30): poll_voted is an
anonymous system message in the main timeline; a reaction is a "reaction" system
message whose text is the emoji, with the reacted message as ``parent``."""

import asyncio
import json as _j
import re

import httpx

from user import pollstate
from user.adapter import (ROOM_TYPE_GROUP, ROOM_TYPE_ONE_TO_ONE, NextcloudTalkUserAdapter,
                          _capability_note, _resolve_object_placeholders)
from gateway.config import PlatformConfig

KEY1, KEY2 = pollstate.KEYCAPS[:2]


def _adapter(**extra) -> NextcloudTalkUserAdapter:
    extra.setdefault("allowed_users", "chef")
    extra.setdefault("poll_settle_seconds", "0")
    a = NextcloudTalkUserAdapter(PlatformConfig(extra=dict(extra)))
    a._threads_enabled = True
    a._capabilities_checked = True
    return a


class Talk:
    def __init__(self):
        self.polls = {}       # id -> poll dict
        self.reactions = {}   # message id -> {emoji: [actor]}
        self.features = ["threads", "talk-polls"]
        self.calls = []

    def __call__(self, request):
        self.calls.append(request)
        path, method = request.url.path, request.method
        m = re.search(r"/poll/[a-z0-9]+/(\d+)$", path)
        if m:
            p = self.polls[m.group(1)]
            if method == "DELETE":
                p["status"] = 1
            return httpx.Response(200, json={"ocs": {"data": p}})
        m = re.search(r"/reaction/[a-z0-9]+/(\d+)$", path)
        if m:
            return httpx.Response(200, json={"ocs": {"data": self.reactions.get(m.group(1), {})}})
        if path.endswith("/capabilities"):
            return httpx.Response(200, json={"ocs": {"data": {"features": self.features}}})
        if "/threads/" in path:
            tid = int(path.rsplit("/", 1)[1])
            return httpx.Response(200, json={"ocs": {"data": {
                "thread": {"id": tid, "title": "Plan"}, "attendee": {"notificationLevel": 0},
                "first": {"id": tid, "actorId": "chef", "actorType": "users", "message": "plan",
                          "isThread": True, "threadId": tid}}}})
        return httpx.Response(200, json={"ocs": {"data": {}}})

    def did(self, method, frag):
        return [r for r in self.calls if r.method == method and frag in r.url.path]


def _wire(a, talk):
    a._http_client = httpx.AsyncClient(base_url="https://cloud.example.com",
                                       transport=httpx.MockTransport(talk))


def _rooms(a):
    a._room_meta["grp1"] = {"token": "grp1", "type": ROOM_TYPE_GROUP, "displayName": "Team"}
    a._room_meta["dmx1"] = {"token": "dmx1", "type": ROOM_TYPE_ONE_TO_ONE, "displayName": "chef"}


def _store(a):
    return pollstate.state_path(a._server, a._username)


def _native(a, talk, *, room="grp1", thread="12245", pid="4", wait_for=1, voters=1, picked=1,
            voter="chef"):
    talk.polls[pid] = {"id": int(pid), "question": "Which direction?", "status": 0,
                       "options": ["A", "B", pollstate.FILL_IN_LABEL], "numVoters": voters, "votes": [],
                       "details": [{"actorType": "users", "actorId": voter, "actorDisplayName": voter.title(),
                                    "optionId": picked}]}
    return pollstate.add(_store(a), {"key": f"native:{room}:{pid}", "kind": "native", "room": room,
                                     "thread": thread, "poll_id": pid, "question": "Which direction?",
                                     "options": ["A", "B", pollstate.FILL_IN_LABEL], "wait_for": wait_for})


def _reaction_poll(a, *, room="dmx1", thread="10", mid="900", multi=False):
    return pollstate.add(_store(a), {"key": f"reaction:{room}:{mid}", "kind": "reaction", "room": room,
                                     "thread": thread, "message_id": mid, "question": "Next?",
                                     "options": ["Fix", "Ship"], "multi": multi, "wait_for": 1})


def _sys(msg_id, kind, *, actor="chef", actor_type="users", text="", params=None, parent=None,
         thread=None):
    m = {"id": msg_id, "actorId": actor, "actorType": actor_type, "actorDisplayName": actor.title(),
         "message": text, "messageType": "system", "systemMessage": kind,
         "messageParameters": params or {}, "timestamp": 1700000000, "threadId": msg_id}
    if parent:
        m["parent"] = parent
    if thread:
        m.update(isThread=True, threadId=thread)
    return m


def _poll_voted(msg_id, pid):
    # Live shape: anonymous ("guests"/"system"), main timeline, poll in the parameters.
    return _sys(msg_id, "poll_voted", actor="system", actor_type="guests",
                text="Someone voted on the poll {poll}",
                params={"poll": {"type": "talk-poll", "id": pid, "name": "Which direction?"}})


def _reaction(msg_id, emoji, parent_id, *, actor="chef", parent_actor="hermes-bot", thread=None,
              parent_text="Shall I deploy it now?"):
    parent = {"id": parent_id, "actorId": parent_actor, "actorType": "users", "message": parent_text,
              "messageParameters": {}}
    if thread:
        parent.update(isThread=True, threadId=thread)
    return _sys(msg_id, "reaction", actor=actor, text=emoji, parent=parent, thread=thread)


def _say(msg_id, text, *, thread=None, actor="chef"):
    m = {"id": msg_id, "actorId": actor, "actorType": "users", "actorDisplayName": actor.title(),
         "message": text, "messageType": "comment", "messageParameters": {}, "timestamp": 1700000000,
         "threadId": msg_id}
    if thread:
        m.update(isThread=True, threadId=thread, threadTitle="Plan")
    return m


def _run(a, *msgs, room="grp1"):
    async def go():
        for m in msgs:
            await a._on_message(room, m)
            pending = list(a._poll_tasks.values())
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)
    asyncio.run(go())


# -- native poll answered by a vote ---------------------------------------------


def test_vote_closes_poll_and_wakes_agent_in_its_thread():
    a, talk = _adapter(), Talk()
    _rooms(a)
    _wire(a, talk)
    _native(a, talk)
    _run(a, _poll_voted(12249, "4"))
    assert talk.did("DELETE", "/poll/grp1/4")  # closed to read who voted what
    ev = a.handled_events[0]
    assert ev.source.thread_id == "12245" and ev.source.user_id == "chef"
    assert ev.text.startswith("[Poll answer]") and "B (Chef)" in ev.text and "act on it now" in ev.text
    assert pollstate.get(_store(a), "native:grp1:4")["status"] == "decided"


def test_vote_for_an_unknown_poll_is_ignored():
    a, talk = _adapter(), Talk()
    _rooms(a)
    _wire(a, talk)
    _run(a, _poll_voted(12249, "99"))
    assert a.handled_events == [] and talk.calls == []


def test_waits_for_enough_voters_without_closing():
    a, talk = _adapter(), Talk()
    _rooms(a)
    _wire(a, talk)
    _native(a, talk, wait_for=2, voters=1)
    _run(a, _poll_voted(12249, "4"))
    assert a.handled_events == [] and not talk.did("DELETE", "/poll/")


def test_vote_by_someone_not_allowed_closes_without_a_turn():
    a, talk = _adapter(), Talk()
    _rooms(a)
    _wire(a, talk)
    _native(a, talk, voter="mallory")
    _run(a, _poll_voted(12249, "4"))
    assert a.handled_events == []


def test_our_own_close_message_is_not_an_answer():
    a, talk = _adapter(), Talk()
    _rooms(a)
    _wire(a, talk)
    _native(a, talk)
    closed = _sys(12251, "poll_closed", actor="hermes-bot",
                  params={"poll": {"type": "talk-poll", "id": "4", "name": "q"}})
    _run(a, closed)
    assert a.handled_events == []


def test_fill_in_option_asks_for_their_words():
    a, talk = _adapter(), Talk()
    _rooms(a)
    _wire(a, talk)
    _native(a, talk, picked=2)
    _run(a, _poll_voted(12249, "4"))
    assert "something else" in a.handled_events[0].text


def test_one_turn_per_poll_even_with_repeat_votes():
    a, talk = _adapter(), Talk()
    _rooms(a)
    _wire(a, talk)
    _native(a, talk)
    _run(a, _poll_voted(12249, "4"), _poll_voted(12252, "4"))
    assert len(a.handled_events) == 1


# -- reaction poll -----------------------------------------------------------------


def test_keycap_reaction_answers_reaction_poll(monkeypatch):
    a, talk = _adapter(), Talk()
    _rooms(a)
    _wire(a, talk)
    _reaction_poll(a)
    talk.reactions["900"] = {
        KEY1: [{"actorType": "users", "actorId": "hermes-bot", "actorDisplayName": "Hermes"}],
        KEY2: [{"actorType": "users", "actorId": "hermes-bot", "actorDisplayName": "Hermes"},
               {"actorType": "users", "actorId": "chef", "actorDisplayName": "Chef"}]}
    import user.adapter as ad
    monkeypatch.setattr(ad, "REACTION_POLL_SETTLE", 0)
    _run(a, _reaction(901, KEY2, "900", thread="10", parent_text="poll"), room="dmx1")
    ev = a.handled_events[0]
    assert ev.source.thread_id == "10" and "Ship (Chef)" in ev.text
    assert talk.did("PUT", "/chat/dmx1/900")  # poll message edited to read answered
    assert pollstate.get(_store(a), "reaction:dmx1:900")["status"] == "decided"


def test_our_own_and_other_agents_keycaps_do_nothing():
    a, talk = _adapter(agent_users="jarvis"), Talk()
    _rooms(a)
    _wire(a, talk)
    _reaction_poll(a)
    _run(a, _reaction(901, KEY1, "900", actor="hermes-bot", thread="10"),
         _reaction(902, KEY1, "900", actor="jarvis", thread="10"), room="dmx1")
    assert a.handled_events == [] and talk.calls == []


# -- check mark / cross on the agent's own message -----------------------------------


def test_check_mark_on_our_question_is_a_yes_turn():
    a, talk = _adapter(), Talk()
    _rooms(a)
    _wire(a, talk)
    _run(a, _reaction(950, "✅", "940", thread="10"), room="dmx1")
    ev = a.handled_events[0]
    assert ev.text.startswith("[Reaction]") and "YES" in ev.text and "Shall I deploy" in ev.text
    assert "talk_thread_close" in ev.text and ev.source.thread_id == "10"
    assert ev.message_id == "940"


def test_cross_is_a_no_turn():
    a, talk = _adapter(), Talk()
    _rooms(a)
    _wire(a, talk)
    _run(a, _reaction(950, "❌", "940"), room="dmx1")
    assert "NO" in a.handled_events[0].text


def test_reactions_that_are_not_answers_are_ignored():
    a, talk = _adapter(), Talk()
    _rooms(a)
    _wire(a, talk)
    _run(a,
         _reaction(950, "\U0001F44D", "940"),                       # thumbs up: not in the default set
         _reaction(951, "✅", "941", parent_actor="casey"),     # on someone else's message
         _reaction(952, "✅", "942", actor="mallory"),          # sender not allowed
         room="dmx1")
    assert a.handled_events == []


def test_reaction_turns_configurable_and_can_be_off():
    a = _adapter(reaction_turns="")
    _rooms(a)
    _wire(a, Talk())
    _run(a, _reaction(950, "✅", "940"), room="dmx1")
    assert a.handled_events == []


# -- fill in: a person's reply while a poll is open ------------------------------------


def test_reply_in_thread_answers_open_poll_and_closes_it():
    a, talk = _adapter(room_modes="grp1=free"), Talk()
    _rooms(a)
    _wire(a, talk)
    _native(a, talk)
    _run(a, _say(12260, "neither, do it the old way", thread=12245))
    ev = a.handled_events[0]
    assert "instead of picking an option" in ev.text and ev.text.endswith("neither, do it the old way")
    assert talk.did("DELETE", "/poll/grp1/4")
    assert pollstate.get(_store(a), "native:grp1:4")["status"] == "answered"


def test_group_main_timeline_chatter_is_not_a_fill_in():
    a, talk = _adapter(room_modes="grp1=free"), Talk()
    _rooms(a)
    a._pair_rooms["grp1"] = (False, 9e18)
    _wire(a, talk)
    _native(a, talk, thread="")
    _run(a, _say(12260, "lunch?"))
    assert "instead of picking" not in a.handled_events[0].text
    assert pollstate.get(_store(a), "native:grp1:4")["status"] == "open"


def test_reply_in_dm_answers_reaction_poll():
    a, talk = _adapter(), Talk()
    _rooms(a)
    _wire(a, talk)
    _reaction_poll(a, thread="")
    _run(a, _say(12260, "park it for now"), room="dmx1")
    assert "instead of picking" in a.handled_events[0].text
    assert talk.did("PUT", "/chat/dmx1/900")


# -- own poll message, placeholders ------------------------------------------------------


def test_our_poll_message_echo_records_its_id():
    a, talk = _adapter(), Talk()
    _rooms(a)
    _wire(a, talk)
    _native(a, talk)
    echo = _say(12247, "{object}", thread=12245, actor="hermes-bot")
    echo["messageParameters"] = {"object": {"type": "talk-poll", "id": "4", "name": "Which direction?"}}
    _run(a, echo)
    assert pollstate.get(_store(a), "native:grp1:4")["message_id"] == "12247"


def test_poll_shared_by_a_person_reads_as_text():
    params = {"object": {"type": "talk-poll", "id": "5", "name": "Pizza?"}}
    assert _resolve_object_placeholders("{object}", params) == "[poll: Pizza?]"
    assert _resolve_object_placeholders("plain", params) == "plain"


# -- capability note ---------------------------------------------------------------------


def test_capability_note_wording():
    feats = ["threads", "talk-polls", "scheduled-messages", "silent-send"]
    dm = _capability_note(feats, ["Deck", "Calendar"], chat_type="dm", federated=False)
    assert "no polls in one-to-one" in dm and "scheduled messages" in dm and "Deck, Calendar" in dm
    fed = _capability_note(feats, [], chat_type="group", federated=True, host_features=["threads", "talk-polls"])
    assert "hosted on another server" in fed and "scheduled messages (" not in fed
    assert "\u2014" not in dm + fed and "\u2013" not in dm + fed


def test_capability_note_once_per_conversation():
    a, talk = _adapter(room_modes="grp1=free"), Talk()
    _rooms(a)
    a._server_features = ["threads", "talk-polls"]
    a._server_apps = ["Deck"]
    _wire(a, talk)
    _run(a, _say(1, "hi"), _say(2, "again"), _say(3, "in thread", thread=3))
    texts = [e.text for e in a.handled_events]
    assert texts[0].startswith("[Server capabilities")
    assert not texts[1].startswith("[Server capabilities")
    assert "[Server capabilities" in texts[2]  # a thread is its own conversation


def test_capability_note_can_be_off():
    a = _adapter(capability_note="off")
    _rooms(a)
    a._server_features = ["threads"]
    _wire(a, Talk())
    _run(a, _say(1, "hi"), room="dmx1")
    assert "[Server capabilities" not in a.handled_events[0].text
