"""v1.16.0: room-locked read tools a room guest may use (talk_room_history / _threads / _files).

The tools take no room argument: the room is the bound turn's. Their Hermes guest guard allows
a non-owner call only from a Talk room whose access-file rule lists the tool's group under
``guest_tools``, by a person allowed in that room, with no argument aiming elsewhere.
No network."""

import importlib.util
import json
import os
from types import SimpleNamespace

import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_spec = importlib.util.spec_from_file_location("nctalk_room_read_tools", os.path.join(_ROOT, "tools.py"))
ct = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ct)

ROOM, OTHER = "u7o3gwt7", "gaquurgh"
ACCESS = f"""
users: [dknestri]
rooms:
  {ROOM}: {{users: [manolo], only: false, guest_tools: [history, threads, files]}}
  4umdv4g3: {{users: [chknestri], only: false}}
  side1:
    users: [guest2]
    guest_tools: [files, bogus]
"""


class Talk:
    def __init__(self):
        self.calls = []

    def __call__(self, method, path, *, json_body=None, params=None):
        self.calls.append((method, path, dict(params or {})))
        if path.endswith("/capabilities") and "/room/" in path:
            return 200, {"features": ["chat-v2", "threads"]}
        if path == "/ocs/v2.php/cloud/capabilities":
            return 200, {"capabilities": {"spreed": {"features": ["chat-v2", "threads"]}}}
        if path.endswith("/threads/recent"):
            return 200, [{"thread": {"id": 15652, "title": "Doorway", "numReplies": 3, "lastActivity": 1}}]
        if path.endswith("/share"):
            return 200, {"15624": {"id": 15624, "actorId": "manolo", "actorDisplayName": "Manolo",
                                   "timestamp": 5, "messageParameters": {"file": {
                                       "type": "file", "id": "20229877", "name": "DOORWAY.md",
                                       "link": "https://x/f/20229877", "mimetype": "text/markdown"}}}}
        return 200, [
            {"id": 15652, "actorId": "manolo", "actorDisplayName": "Manolo", "timestamp": 3,
             "message": "{mention-user1} the ISV is set", "messageType": "comment", "systemMessage": "",
             "isThread": True, "threadId": 15652,
             "messageParameters": {"mention-user1": {"type": "user", "id": "lumina", "name": "Lumina"}}},
            {"id": 15651, "actorId": "lumina", "messageType": "comment", "systemMessage": "message_deleted",
             "message": "x"},
            {"id": 15625, "actorId": "manolo", "actorDisplayName": "Manolo", "timestamp": 2,
             "message": "Hello to both.", "messageType": "comment", "systemMessage": "",
             "messageParameters": {"actor": {"type": "user", "id": "manolo"},
                                   "file": {"type": "file", "name": "OFFICE.md"}}},
        ]


@pytest.fixture
def talk(monkeypatch, tmp_path):
    for k in ("HERMES_SESSION_PLATFORM", "HERMES_SESSION_CHAT_ID", "HERMES_SESSION_THREAD_ID",
              "NEXTCLOUD_TALK_THREAD_TOOLS", "NEXTCLOUD_TALK_ROOM_READ_TOOLS"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("NEXTCLOUD_TALK_SERVER_URL", "https://cloud.example.com")
    monkeypatch.setenv("NEXTCLOUD_TALK_APP_PASSWORD", "x" * 12)  # filler, not a credential
    p = tmp_path / "talk-access.yaml"
    p.write_text(ACCESS)
    monkeypatch.setenv("NEXTCLOUD_TALK_ACCESS_FILE", str(p))
    fake = Talk()
    monkeypatch.setattr(ct, "_request", fake)
    ct._access_cache.clear()
    ct._caps_cache.clear()
    ct._room_caps_cache.clear()
    return fake


def _in(monkeypatch, room=ROOM):
    monkeypatch.setenv("HERMES_SESSION_PLATFORM", "nctalk")
    monkeypatch.setenv("HERMES_SESSION_CHAT_ID", room)


def _turn(user="manolo", room=ROOM, platform="nctalk"):
    return SimpleNamespace(platform=platform, chat_id=room, chat_type="group", user_id=user, thread_id="")


def j(s):
    return json.loads(s)


# ---- the guard ---------------------------------------------------------------------------
def test_guard_allows_a_room_guest_for_listed_groups(talk, monkeypatch):
    _in(monkeypatch)
    for group in ("history", "threads", "files"):
        assert ct._guest_guard(group)(_turn(), {}) is True


def test_guard_refuses_other_people_rooms_platforms_and_aimed_args(talk, monkeypatch):
    _in(monkeypatch)
    g = ct._guest_guard("history")
    assert g(_turn(user="stranger"), {}) is False            # not allowed in this room
    assert g(_turn(user="chknestri"), {}) is False           # allowed only in another room
    assert g(_turn(platform="telegram"), {}) is False
    assert g(_turn(room=OTHER), {}) is False                 # turn and session disagree
    for k in ("room", "other_room", "token", "chat_id"):
        assert g(_turn(), {k: OTHER}) is False
    _in(monkeypatch, room="4umdv4g3")
    assert ct._guest_guard("history")(_turn(user="chknestri", room="4umdv4g3"), {}) is False  # no guest_tools


def test_guard_only_opens_listed_groups_and_drops_unknown_names(talk, monkeypatch):
    _in(monkeypatch, room="side1")
    assert ct._guest_guard("files")(_turn(user="guest2", room="side1"), {}) is True
    assert ct._guest_guard("history")(_turn(user="guest2", room="side1"), {}) is False
    assert ct._access().guest_tools("side1") == frozenset({"files"})


def test_guard_needs_an_access_file(talk, monkeypatch, tmp_path):
    monkeypatch.setenv("NEXTCLOUD_TALK_ACCESS_FILE", str(tmp_path / "missing.yaml"))
    ct._access_cache.clear()
    _in(monkeypatch)
    assert ct._guest_guard("history")(_turn(), {}) is False


def test_guard_rereads_the_file(talk, monkeypatch, tmp_path):
    _in(monkeypatch)
    assert ct._guest_guard("history")(_turn(), {}) is True
    p = tmp_path / "talk-access.yaml"
    p.write_text(f"users: [dknestri]\nrooms:\n  {ROOM}: {{users: [manolo]}}\n")
    os.utime(p, (os.stat(p).st_atime, os.stat(p).st_mtime + 10))
    acc = ct._access()
    acc._checked = 0.0  # skip the 5s re-check window
    assert ct._guest_guard("history")(_turn(), {}) is False


# ---- the tools stay on this room ----------------------------------------------------------------
def test_history_reads_this_room_only_and_renders(talk, monkeypatch):
    _in(monkeypatch)
    out = j(ct.talk_room_history({"room": OTHER, "limit": 500}))
    method, path, params = talk.calls[-1]
    assert path.endswith(f"/chat/{ROOM}") and OTHER not in path
    assert params["limit"] == ct.HISTORY_MAX and params["lookIntoFuture"] == 0 and params["setReadMarker"] == 0
    assert [m["id"] for m in out["messages"]] == [15625, 15652]       # oldest first, deleted dropped
    assert out["messages"][0]["text"] == "Hello to both. [file: OFFICE.md]"
    assert out["messages"][1]["text"] == "@Lumina the ISV is set" and out["messages"][1]["thread_id"] == "15652"
    assert out["older_before_id"] == "15625"


def test_history_thread_and_paging_params(talk, monkeypatch):
    _in(monkeypatch)
    ct.talk_room_history({"thread_id": "15652", "before_id": "15700"})
    params = talk.calls[-1][2]
    assert params["threadId"] == 15652 and params["lastKnownMessageId"] == 15700


def test_threads_and_files_are_this_room(talk, monkeypatch):
    _in(monkeypatch)
    th = j(ct.talk_room_threads({}))
    assert th["threads"][0]["title"] == "Doorway" and th["room"] == ROOM
    files = j(ct.talk_room_files({"room": OTHER}))
    assert files["files"][0]["name"] == "DOORWAY.md" and files["files"][0]["fileid"] == "20229877"
    assert all(OTHER not in c[1] for c in talk.calls)
    assert talk.calls[-1][2]["objectType"] == "file"


def test_tools_refuse_outside_talk(talk):
    for fn in (ct.talk_room_history, ct.talk_room_threads, ct.talk_room_files):
        assert "only from a Nextcloud Talk room" in j(fn({"room": ROOM}))["error"]


# ---- registration -----------------------------------------------------------------------------
def test_registration_passes_guards_only_to_a_core_that_takes_them():
    seen = []

    class NewCore:
        def register_tool(self, name, toolset, schema, handler, check_fn=None, requires_env=None,
                          is_async=False, description="", emoji="", override=False, guest_guard=None):
            seen.append((name, guest_guard))

    ct.register_tools(NewCore())
    guarded = {n: g for n, g in seen if g is not None}
    assert set(guarded) == {"talk_room_history", "talk_room_threads", "talk_room_files",
                            "talk_workspace_list", "talk_workspace_read", "talk_workspace_write",
                            "talk_ask_owner"}
    assert guarded["talk_workspace_write"].__name__ == "nctalk_guest_guard_workspace"
    assert guarded["talk_ask_owner"].__name__ == "nctalk_guest_guard_ask"
    assert guarded["talk_room_files"].__name__ == "nctalk_guest_guard_files"


GUEST_TOOLS = {"talk_room_history", "talk_room_threads", "talk_room_files", "talk_workspace_list",
               "talk_workspace_read", "talk_workspace_write", "talk_ask_owner"}


def test_upstream_core_without_guest_guard_registers_every_tool_owner_only():
    """Upstream Hermes: register_tool has no guest_guard; nothing may crash or be lost."""
    seen = []

    class UpstreamCore:
        def register_tool(self, name, toolset, schema, handler, check_fn=None, requires_env=None,
                          is_async=False, description="", emoji="", override=False):
            seen.append(name)

    ct.register_tools(UpstreamCore())
    assert GUEST_TOOLS <= set(seen) and len(seen) == len(set(seen))


def test_core_that_advertises_but_rejects_guest_guard_falls_back():
    """A **kwargs register_tool that raises on the unknown keyword: retry without it."""
    seen = []

    class Picky:
        def register_tool(self, name, toolset, schema, handler, check_fn=None, requires_env=None,
                          is_async=False, description="", emoji="", override=False, guest_guard=None,
                          **kw):
            if guest_guard is not None:
                raise TypeError("register_tool() got an unexpected keyword argument 'guest_guard'")
            seen.append(name)

    ct.register_tools(Picky())
    assert GUEST_TOOLS <= set(seen) and len(seen) == len(set(seen))


def test_unrelated_type_error_is_not_swallowed():
    class Broken:
        def register_tool(self, name, toolset, schema, handler, check_fn=None, requires_env=None,
                          is_async=False, description="", emoji="", override=False, guest_guard=None):
            if guest_guard is not None:
                raise TypeError("schema must be a dict")

    with pytest.raises(TypeError, match="schema"):
        ct.register_tools(Broken())


def test_off_switch(talk, monkeypatch):
    monkeypatch.setenv("NEXTCLOUD_TALK_ROOM_READ_TOOLS", "off")
    assert ct.room_read_tools_enabled() is False
