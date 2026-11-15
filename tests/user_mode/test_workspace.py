"""v1.17.0: the room workspace (folders shared into the room) and owner approvals.

- user/workspace.py: path safety, which shares count as the room's folder, list/read/write.
- user/access.py: ``approval`` / ``approvers`` with a global ``defaults`` and a per-room override.
- tools.py: talk_workspace_* and talk_ask_owner; a guest's write waits for an approver when
  the room requires it, an owner's write goes straight through.
- adapter: only an approver's thumbs up / down on the request decides it.
No network."""

import asyncio
import importlib.util
import json
import os
from urllib.parse import unquote

import httpx
import pytest

from user import access, pollstate, workspace
from user.adapter import ROOM_TYPE_GROUP, NextcloudTalkUserAdapter
from gateway.config import PlatformConfig

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_spec = importlib.util.spec_from_file_location("nctalk_ws_tools", os.path.join(_ROOT, "tools.py"))
ct = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ct)

ROOM, FAMILY = "u7o3gwt7", "4umdv4g3"
AGENT = "hermes-bot"
WS = "/Workshop - Chef x Manny x Lumina"
ACCESS = f"""
users: [dknestri]
defaults:
  approval: required
  approvers: [dknestri]
rooms:
  {ROOM}: {{users: [manolo], guest_tools: [history, workspace, ask]}}
  {FAMILY}: {{users: [chknestri, janice], guest_tools: [workspace], approval: off}}
  side1: {{users: [g2], approval: maybe, approvers: [g2]}}
"""


# ---------------------------------------------------------------- fake Nextcloud
class NC:
    """Shares API + WebDAV over a dict of files, as the agent's account."""

    def __init__(self):
        self.files = {}          # dav path (unquoted) -> text
        self.dirs = set()
        self.calls = []
        self.shares = [
            {"share_type": 10, "share_with": ROOM, "path": WS, "item_type": "folder",
             "uid_owner": AGENT, "permissions": 15, "item_source": 20229498},
            {"share_type": 10, "share_with": FAMILY, "path": "/Chloe's College HQ",
             "item_type": "folder", "uid_owner": AGENT, "permissions": 31, "item_source": 20229422},
            # not the room's workspace:
            {"share_type": 10, "share_with": ROOM, "path": f"/Talk/{ROOM}/manolo", "item_type": "folder",
             "uid_owner": AGENT, "permissions": 31, "item_source": 1},
            {"share_type": 10, "share_with": ROOM, "path": "/doc.md", "item_type": "file",
             "uid_owner": AGENT, "permissions": 31, "item_source": 2},
            {"share_type": 0, "share_with": "dknestri", "path": "/GTD", "item_type": "folder",
             "uid_owner": AGENT, "permissions": 31, "item_source": 3},
            {"share_type": 10, "share_with": ROOM, "path": "/Theirs", "item_type": "folder",
             "uid_owner": "someone", "permissions": 31, "item_source": 4},
        ]
        base = f"/remote.php/dav/files/{AGENT}"
        self.dirs |= {base + WS, base + "/Chloe's College HQ"}
        self.files[base + WS + "/README.md"] = "# Workshop\n"

    def ocs(self, method, path, params=None):
        self.calls.append(("OCS", method, path))
        return 200, list(self.shares)

    def dav(self, method, path, headers=None, content=None):
        p = unquote(path)
        headers = headers or {}
        self.calls.append((method, p, dict(headers)))
        if method == "PROPFIND":
            if p not in self.dirs:
                return 404, {}, ""
            kids = [f for f in list(self.files) + list(self.dirs)
                    if f.rsplit("/", 1)[0] == p]
            rows = [self._row(p, True)] + [self._row(k, k in self.dirs) for k in sorted(kids)]
            return 207, {}, ('<?xml version="1.0"?><d:multistatus xmlns:d="DAV:" '
                             'xmlns:oc="http://owncloud.org/ns">' + "".join(rows) + "</d:multistatus>")
        if method == "GET":
            if p not in self.files:
                return 404, {}, ""
            return 200, {"ETag": '"e1"'}, self.files[p]
        if method == "MKCOL":
            if p in self.dirs:
                return 405, {}, ""
            self.dirs.add(p)
            return 201, {}, ""
        if method == "PUT":
            exists = p in self.files
            if headers.get("If-None-Match") == "*" and exists:
                return 412, {}, ""
            if headers.get("If-Match") and not exists:
                return 412, {}, ""
            if p.rsplit("/", 1)[0] not in self.dirs:
                return 409, {}, ""
            self.files[p] = content.decode("utf-8")
            return (204 if exists else 201), {"OC-FileId": "777oc7o1m10z6z7"}, ""
        return 405, {}, ""

    def _row(self, p, is_dir):
        rt = "<d:collection/>" if is_dir else ""
        size = "" if is_dir else f"<oc:size>{len(self.files[p])}</oc:size>"
        return (f"<d:response><d:href>{p}{'/' if is_dir else ''}</d:href><d:propstat><d:prop>"
                f"<d:resourcetype>{rt}</d:resourcetype>{size}<oc:fileid>9</oc:fileid>"
                f"</d:prop></d:propstat></d:response>")

    def writes(self):
        return [c for c in self.calls if c[0] == "PUT"]


def P(rel, root=WS):
    return f"/remote.php/dav/files/{AGENT}{root}/{rel}"


# ---------------------------------------------------------------- workspace.py
@pytest.mark.parametrize("bad", ["/etc/passwd", "~/x", "../x.md", "a/../../x.md", "./x.md",
                                 ".hidden.md", "a\\b.md", "a\x00.md", "a/\nb.md"])
def test_paths_that_could_escape_are_refused(bad):
    with pytest.raises(workspace.WorkspaceError):
        workspace.clean_rel(bad)


def test_only_folders_the_agent_owns_and_shared_to_this_room_count():
    nc = NC()
    rts = workspace.roots(nc.ocs, ROOM, AGENT)
    assert [(r["name"], r["perms"]) for r in rts] == [("Workshop - Chef x Manny x Lumina", 15)]
    assert workspace.roots(nc.ocs, "elsewhere", AGENT) == []


def test_list_read_and_write_inside_the_folder():
    nc = NC()
    rts = workspace.roots(nc.ocs, ROOM, AGENT)
    listing = workspace.list_dir(nc.dav, AGENT, rts, "")
    assert [e["name"] for e in listing["entries"]] == ["README.md"] and listing["writable"]
    assert workspace.read_file(nc.dav, AGENT, rts, "README.md")["content"] == "# Workshop\n"
    out = workspace.write_file(nc.dav, AGENT, rts, "notes/day1.md", "hello", "create")
    assert out["created"] and nc.files[P("notes/day1.md")] == "hello" and out["fileid"] == "777"
    with pytest.raises(workspace.WorkspaceError, match="already exists"):
        workspace.write_file(nc.dav, AGENT, rts, "notes/day1.md", "again", "create")
    workspace.write_file(nc.dav, AGENT, rts, "notes/day1.md", "more", "append")
    assert nc.files[P("notes/day1.md")] == "hello\nmore"
    workspace.write_file(nc.dav, AGENT, rts, "notes/day1.md", "new", "replace")
    assert nc.files[P("notes/day1.md")] == "new"
    with pytest.raises(workspace.WorkspaceError, match="changed or does not exist"):
        workspace.write_file(nc.dav, AGENT, rts, "nope.md", "x", "replace")


def test_text_files_only_and_share_permissions_apply():
    nc = NC()
    rts = workspace.roots(nc.ocs, ROOM, AGENT)
    with pytest.raises(workspace.WorkspaceError, match="only text files"):
        workspace.write_file(nc.dav, AGENT, rts, "run.sh", "x", "create")
    rts[0]["perms"] = 1  # read-only share
    with pytest.raises(workspace.WorkspaceError, match="read-only"):
        workspace.write_file(nc.dav, AGENT, rts, "a.md", "x", "create")
    assert nc.writes() == []


def test_several_folders_need_the_folder_name():
    nc = NC()
    nc.shares.append({"share_type": 10, "share_with": ROOM, "path": "/Second", "item_type": "folder",
                      "uid_owner": AGENT, "permissions": 31, "item_source": 5})
    nc.dirs.add(f"/remote.php/dav/files/{AGENT}/Second")
    rts = workspace.roots(nc.ocs, ROOM, AGENT)
    top = workspace.list_dir(nc.dav, AGENT, rts, "")
    assert [e["name"] for e in top["entries"]] == ["Second", "Workshop - Chef x Manny x Lumina"]
    with pytest.raises(workspace.WorkspaceError, match="several folders"):
        workspace.write_file(nc.dav, AGENT, rts, "a.md", "x", "create")
    workspace.write_file(nc.dav, AGENT, rts, "Second/a.md", "x", "create")
    assert nc.files[P("a.md", "/Second")] == "x"


# ---------------------------------------------------------------- access.py
def test_approval_defaults_and_room_overrides():
    acc = access.TalkAccess(None)
    acc._spec = access.parse(ACCESS)
    assert acc.approval_required(ROOM) is True          # defaults
    assert acc.approval_required(FAMILY) is False       # room: off
    assert acc.approval_required("side1") is True       # typo never turns it off
    assert acc.approval_required("unknown") is True
    assert acc.approvers(ROOM) == frozenset({"dknestri"})
    assert acc.approvers("side1") == frozenset({"g2"})
    assert acc.guest_tools(ROOM) == frozenset({"history", "workspace", "ask"})
    bare = access.TalkAccess(None)
    bare._spec = access.parse("users: [a]\n")
    assert bare.approval_required(ROOM) is True and bare.approvers(ROOM) == frozenset()


def test_mini_yaml_reads_the_defaults_block():
    assert access._mini_yaml(ACCESS)["defaults"] == {"approval": "required", "approvers": ["dknestri"]}


# ---------------------------------------------------------------- tools.py
class Chat:
    def __init__(self):
        self.posts = []

    def __call__(self, method, path, *, json_body=None, params=None):
        self.posts.append((method, path, json_body))
        return 201, {"id": 5000 + len(self.posts)}


@pytest.fixture
def tools(monkeypatch, tmp_path):
    for k in ("HERMES_SESSION_THREAD_ID", "NEXTCLOUD_TALK_THREAD_TOOLS", "NEXTCLOUD_TALK_ROOM_READ_TOOLS"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("NEXTCLOUD_TALK_SERVER_URL", "https://cloud.example.com")
    monkeypatch.setenv("NEXTCLOUD_TALK_USERNAME", AGENT)
    monkeypatch.setenv("NEXTCLOUD_TALK_APP_PASSWORD", "x" * 12)  # filler, not a credential
    monkeypatch.setenv("HERMES_SESSION_PLATFORM", "nctalk")
    monkeypatch.setenv("HERMES_SESSION_CHAT_ID", ROOM)
    monkeypatch.setenv("HERMES_SESSION_USER_ID", "manolo")
    monkeypatch.setenv("HERMES_SESSION_USER_NAME", "Manolo")
    p = tmp_path / "talk-access.yaml"
    p.write_text(ACCESS)
    monkeypatch.setenv("NEXTCLOUD_TALK_ACCESS_FILE", str(p))
    ct._access_cache.clear()
    nc, chat = NC(), Chat()
    monkeypatch.setattr(ct, "_ws_transport_override", (nc.dav, nc.ocs))
    monkeypatch.setattr(ct, "_request", chat)
    monkeypatch.setattr(ct, "_is_owner_turn", lambda: False)
    return nc, chat


def j(s):
    return json.loads(s)


def test_guest_write_waits_for_an_approver(tools):
    nc, chat = tools
    out = j(ct.talk_workspace_write({"path": "plan.md", "content": "Step 1\nStep 2"}))
    assert out["status"] == "waiting_for_approval" and out["approvers"] == ["dknestri"]
    assert nc.writes() == []
    msg = chat.posts[0][2]["message"]
    assert '@"dknestri"' in msg and "Manolo asks to create plan.md" in msg and "> Step 1" in msg
    rec = pollstate.find(ct._poll_store(), room=ROOM, kind="approval")[0]
    assert rec["action"] == "write" and rec["content"] == "Step 1\nStep 2"
    assert rec["requester"] == "manolo" and rec["message_id"] == out["message_id"]


def test_invalid_guest_writes_are_refused_before_asking(tools):
    nc, chat = tools
    assert "error" in j(ct.talk_workspace_write({"path": "../x.md", "content": "x"}))
    assert "error" in j(ct.talk_workspace_write({"path": "x.exe", "content": "x"}))
    assert chat.posts == [] and nc.writes() == []


def test_room_with_approval_off_writes_at_once(tools, monkeypatch):
    nc, chat = tools
    monkeypatch.setenv("HERMES_SESSION_CHAT_ID", FAMILY)
    monkeypatch.setenv("HERMES_SESSION_USER_ID", "chknestri")
    out = j(ct.talk_workspace_write({"path": "essay.md", "content": "Draft"}))
    assert out["ok"] and out["created"] and chat.posts == []
    assert nc.files[P("essay.md", "/Chloe's College HQ")] == "Draft"


def test_owner_turn_writes_without_approval(tools, monkeypatch):
    nc, chat = tools
    monkeypatch.setattr(ct, "_is_owner_turn", lambda: True)
    assert j(ct.talk_workspace_write({"path": "plan.md", "content": "x"}))["ok"]
    assert chat.posts == [] and nc.files[P("plan.md")] == "x"


def test_list_and_read(tools):
    assert [e["name"] for e in j(ct.talk_workspace_list({}))["entries"]] == ["README.md"]
    assert j(ct.talk_workspace_read({"path": "README.md"}))["content"] == "# Workshop\n"


def test_ask_owner_posts_a_request(tools):
    _nc, chat = tools
    out = j(ct.talk_ask_owner({"request": "Add a Deck card 'Call Twilio' to the Workshop board"}))
    assert out["status"] == "waiting_for_approval"
    assert "have me do this: Add a Deck card" in chat.posts[0][2]["message"]
    rec = pollstate.find(ct._poll_store(), room=ROOM, kind="approval")[0]
    assert rec["action"] == "task" and rec["task"].startswith("Add a Deck card")


def test_ask_owner_in_an_owner_turn_says_just_do_it(tools, monkeypatch):
    monkeypatch.setattr(ct, "_is_owner_turn", lambda: True)
    assert "already an owner turn" in j(ct.talk_ask_owner({"request": "x"}))["error"]


def test_guard_opens_workspace_and_ask_only_where_listed(tools, monkeypatch):
    from types import SimpleNamespace as NS
    turn = NS(platform="nctalk", chat_id=ROOM, chat_type="group", user_id="manolo", thread_id="")
    assert ct._guest_guard("workspace")(turn, {"path": "a.md"}) is True
    assert ct._guest_guard("ask")(turn, {"request": "x"}) is True
    assert ct._guest_guard("workspace")(turn, {"path": "a.md", "room": FAMILY}) is False
    monkeypatch.setenv("HERMES_SESSION_CHAT_ID", FAMILY)
    fam = NS(platform="nctalk", chat_id=FAMILY, chat_type="group", user_id="janice", thread_id="")
    assert ct._guest_guard("workspace")(fam, {}) is True
    assert ct._guest_guard("ask")(fam, {}) is False  # not listed there


def test_is_owner_turn_fails_closed_without_hermes():
    assert ct._is_owner_turn() is False


# ---------------------------------------------------------------- adapter
def _adapter(tmp_path, nc):
    p = tmp_path / "access.yaml"
    p.write_text(ACCESS)
    a = NextcloudTalkUserAdapter(PlatformConfig(extra={"access_file": str(p), "username": AGENT,
                                                       "server_url": "https://cloud.example.com"}))
    a._threads_enabled = True
    a._capabilities_checked = True
    a._ws_transport = (nc.dav, nc.ocs)
    a._room_meta[ROOM] = {"token": ROOM, "type": ROOM_TYPE_GROUP, "displayName": "Workshop"}
    posts = []

    def talk(request):
        posts.append(json.loads(request.content or b"{}"))
        return httpx.Response(201, json={"ocs": {"data": {"id": 1}}})
    a._http_client = httpx.AsyncClient(base_url="https://cloud.example.com",
                                       transport=httpx.MockTransport(talk))
    return a, posts


def _request(a, action="write", mid="6001", **fields):
    rec = {"key": pollstate.record_key("approval", ROOM, mid), "kind": "approval", "room": ROOM,
           "thread": "", "message_id": mid, "action": action, "summary": "create plan.md",
           "requester": "manolo", "requester_name": "Manolo"}
    rec.update(fields or ({"path": "plan.md", "mode": "create", "content": "Step 1"}
                          if action == "write" else {"task": "Add a Deck card"}))
    return pollstate.add(pollstate.state_path(a._server, a._username), rec)


def _react(a, emoji, actor, mid="6001", msg_id=7001):
    m = {"id": msg_id, "actorId": actor, "actorType": "users", "actorDisplayName": actor.title(),
         "message": emoji, "messageType": "system", "systemMessage": "reaction",
         "messageParameters": {}, "timestamp": 1700000000,
         "parent": {"id": mid, "actorId": AGENT, "actorType": "users", "message": "Approval needed",
                    "messageParameters": {}}}
    asyncio.run(a._on_answer_signal(ROOM, m))


def _status(a, mid="6001"):
    return pollstate.get(pollstate.state_path(a._server, a._username),
                         pollstate.record_key("approval", ROOM, mid))["status"]


def test_approver_thumbs_up_runs_the_write(tmp_path):
    nc = NC()
    a, posts = _adapter(tmp_path, nc)
    _request(a)
    _react(a, "\U0001F44D", "dknestri")
    assert nc.files[P("plan.md")] == "Step 1" and _status(a) == "approved"
    assert "Approved by Dknestri" in posts[-1]["message"] and posts[-1]["replyTo"] == 6001
    assert posts[-1]["message"].endswith("/index.php/f/777")
    assert a.handled_events == []
    _react(a, "\U0001F44D", "dknestri", msg_id=7002)   # a second tap changes nothing
    assert len(nc.writes()) == 1


def test_guest_cannot_approve_their_own_request(tmp_path):
    nc = NC()
    a, posts = _adapter(tmp_path, nc)
    _request(a)
    _react(a, "\U0001F44D", "manolo")
    _react(a, "✅", "manolo", msg_id=7002)
    assert nc.writes() == [] and _status(a) == "open" and posts == [] and a.handled_events == []


def test_thumbs_down_declines(tmp_path):
    nc = NC()
    a, posts = _adapter(tmp_path, nc)
    _request(a)
    _react(a, "\U0001F44E", "dknestri")
    assert nc.writes() == [] and _status(a) == "declined"
    assert "declined Manolo's request" in posts[-1]["message"]


def test_approved_task_is_a_turn_as_the_approver(tmp_path):
    nc = NC()
    a, _posts = _adapter(tmp_path, nc)
    _request(a, action="task")
    _react(a, "\U0001F44D", "dknestri")
    ev = a.handled_events[0]
    assert ev.text.startswith("[Approved request]") and "Add a Deck card" in ev.text
    assert ev.source.user_id == "dknestri" and ev.message_id == "6001"


def test_other_reactions_on_a_request_are_not_turns(tmp_path):
    nc = NC()
    a, posts = _adapter(tmp_path, nc)
    _request(a)
    _react(a, "\U0001F389", "dknestri")  # party popper
    assert _status(a) == "open" and posts == [] and a.handled_events == []
