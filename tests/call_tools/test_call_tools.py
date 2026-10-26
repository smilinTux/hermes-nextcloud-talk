"""Call tools (tools.py): room resolution, opt-in gate, and the systemctl
calls they make, with a fake runner. No systemd, no network."""

import importlib.util
import json
import os
import subprocess

import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_spec = importlib.util.spec_from_file_location("nctalk_call_tools", os.path.join(_ROOT, "tools.py"))
ct = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ct)


class FakeSystemd:
    """Records commands; answers `show` from a per-unit state table."""

    def __init__(self, states=None, fail=()):
        self.states = states or {}
        self.fail = set(fail)
        self.cmds = []

    def __call__(self, cmd):
        self.cmds.append(cmd)
        verb = cmd[2] if cmd[0] == "systemctl" else cmd[0]
        if verb in self.fail:
            return subprocess.CompletedProcess(cmd, 1, "", f"{verb} failed")
        if verb == "show":
            st = self.states.get(cmd[3], {"ActiveState": "inactive", "SubState": "dead",
                                          "LoadState": "loaded", "ExecMainStatus": "0"})
            return subprocess.CompletedProcess(cmd, 0, "".join(f"{k}={v}\n" for k, v in st.items()), "")
        if verb == "list-units":
            out = "".join(f"{u} loaded {s['ActiveState']} {s.get('SubState', '')} skcall\n"
                          for u, s in self.states.items())
            return subprocess.CompletedProcess(cmd, 0, out, "")
        if verb == "journalctl":
            return subprocess.CompletedProcess(cmd, 0, "PermissionError: room 'abcd1234' is not in the allowlist\n", "")
        return subprocess.CompletedProcess(cmd, 0, "", "")

    def verbs(self):
        return [c[2] if c[0] == "systemctl" else c[0] for c in self.cmds]


@pytest.fixture
def sysd(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    for k in ("HERMES_SESSION_PLATFORM", "HERMES_SESSION_CHAT_ID"):
        monkeypatch.delenv(k, raising=False)
    fake = FakeSystemd()
    monkeypatch.setattr(ct, "_run", fake)
    return fake


def j(s):
    return json.loads(s)


ACTIVE = {"ActiveState": "active", "SubState": "running", "LoadState": "loaded",
          "ActiveEnterTimestamp": "Sun 2026-10-25 21:00:00 EDT"}


def test_opt_in_gate(monkeypatch):
    monkeypatch.delenv("NEXTCLOUD_TALK_CALL_TOOLS", raising=False)
    assert not ct.tools_enabled()
    for v in ("on", "1", "true", "YES"):
        monkeypatch.setenv("NEXTCLOUD_TALK_CALL_TOOLS", v)
        assert ct.tools_enabled()
    monkeypatch.setenv("NEXTCLOUD_TALK_CALL_TOOLS", "off")
    assert not ct.tools_enabled()


def test_join_starts_unit_without_blocking(sysd):
    r = j(ct.call_join({"room": "xazze5d7"}))
    assert r["status"] == "joining" and r["unit"] == "skcall-join@xazze5d7.service"
    assert ["systemctl", "--user", "start", "--no-block", "skcall-join@xazze5d7.service"] in sysd.cmds
    assert not ct.env_file("xazze5d7").exists()     # no options: no env file


def test_join_already_in_call_does_not_start(sysd):
    sysd.states["skcall-join@xazze5d7.service"] = ACTIVE
    r = j(ct.call_join({"room": "xazze5d7"}))
    assert r["status"] == "already in call"
    assert "start" not in sysd.verbs()


def test_join_activating_counts_as_in_call(sysd):
    sysd.states["skcall-join@xazze5d7.service"] = {**ACTIVE, "ActiveState": "activating"}
    assert j(ct.call_join({"room": "xazze5d7"}))["status"] == "already in call"


def test_join_defaults_to_current_talk_room(sysd, monkeypatch):
    monkeypatch.setenv("HERMES_SESSION_PLATFORM", "nctalk")
    monkeypatch.setenv("HERMES_SESSION_CHAT_ID", "gaquurgh")
    assert j(ct.call_join({}))["room"] == "gaquurgh"


def test_join_requires_room_outside_talk(sysd, monkeypatch):
    monkeypatch.setenv("HERMES_SESSION_PLATFORM", "telegram")
    monkeypatch.setenv("HERMES_SESSION_CHAT_ID", "1594678363")
    r = j(ct.call_join({}))
    assert "room is required" in r["error"]
    assert sysd.cmds == []


@pytest.mark.parametrize("room", ["XAZZE5D7", "abc", "../../etc", "a" * 33, "room-1", "x y z w",
                                  "xazz\ne5d7", "xazze5d7;rm"])
def test_invalid_room_rejected(sysd, room):
    for fn in (ct.call_join, ct.call_leave, ct.call_status):
        assert "invalid room token" in j(fn({"room": room}))["error"]
    assert sysd.cmds == []


def test_ring_and_greeting_go_to_env_file(sysd):
    j(ct.call_join({"room": "xazze5d7", "ring": True, "greeting": 'Hey "Chef"\n$(id) `x` \\ hi'}))
    path = ct.env_file("xazze5d7")
    text = path.read_text()
    assert "SKCALL_RING=1\n" in text
    assert 'SKCALL_GREETING="Hey Chef (id) x hi"' in text
    assert oct(path.stat().st_mode & 0o777) == "0o600"


def test_greeting_trimmed_and_ring_string(sysd):
    j(ct.call_join({"room": "xazze5d7", "ring": "false", "greeting": "x" * 1000}))
    text = ct.env_file("xazze5d7").read_text()
    assert "SKCALL_RING" not in text
    assert len(text.split('"')[1]) == ct.GREETING_MAX


def test_stale_options_removed_on_plain_join(sysd):
    j(ct.call_join({"room": "xazze5d7", "ring": True}))
    assert ct.env_file("xazze5d7").exists()
    j(ct.call_join({"room": "xazze5d7"}))
    assert not ct.env_file("xazze5d7").exists()


def test_join_unit_missing(sysd):
    sysd.states["skcall-join@xazze5d7.service"] = {"ActiveState": "inactive", "LoadState": "not-found"}
    assert "not installed" in j(ct.call_join({"room": "xazze5d7"}))["error"]
    assert "start" not in sysd.verbs()


def test_join_start_failure_reported(sysd):
    sysd.fail.add("start")
    assert "failed" in j(ct.call_join({"room": "xazze5d7"}))["error"]


def test_leave_stops_running_unit(sysd):
    sysd.states["skcall-join@xazze5d7.service"] = ACTIVE
    r = j(ct.call_leave({"room": "xazze5d7"}))
    assert r["status"] == "leaving"
    assert ["systemctl", "--user", "stop", "--no-block", "skcall-join@xazze5d7.service"] in sysd.cmds


def test_leave_when_not_in_call(sysd):
    assert j(ct.call_leave({"room": "xazze5d7"}))["status"] == "not in call"
    assert "stop" not in sysd.verbs()


def test_status_one_room(sysd):
    sysd.states["skcall-join@xazze5d7.service"] = ACTIVE
    r = j(ct.call_status({"room": "xazze5d7"}))
    assert r["status"] == "in call" and r["since"].startswith("Sun")


def test_status_failed_shows_log_tail(sysd):
    sysd.states["skcall-join@abcd1234.service"] = {"ActiveState": "failed", "Result": "exit-code",
                                                   "ExecMainStatus": "1", "LoadState": "loaded"}
    r = j(ct.call_status({"room": "abcd1234"}))
    assert r["status"] == "last join failed" and "allowlist" in r["last_log"][-1]


def test_status_already_in_call_exit(sysd):
    sysd.states["skcall-join@xazze5d7.service"] = {"ActiveState": "inactive", "ExecMainStatus": "3",
                                                   "LoadState": "loaded"}
    assert "already in call" in j(ct.call_status({"room": "xazze5d7"}))["last_exit"]


def test_status_all_rooms(sysd):
    sysd.states["skcall-join@xazze5d7.service"] = ACTIVE
    sysd.states["skcall-join@gaquurgh.service"] = {"ActiveState": "inactive", "LoadState": "loaded"}
    r = j(ct.call_status({}))
    assert r["in_call"] == ["xazze5d7"] and len(r["calls"]) == 2


def test_register_tools_gated():
    seen = []

    class Ctx:
        def register_tool(self, **kw):
            seen.append(kw)
    ct.register_tools(Ctx())
    assert [k["name"] for k in seen] == ["call_join", "call_leave", "call_status"]
    assert all(k["toolset"] == "nctalk-calls" and k["check_fn"] is ct.tools_enabled for k in seen)
    for k in seen:
        assert "never launch skcall" in k["description"].lower()
        assert k["schema"]["name"] == k["name"]


def test_descriptions_have_no_long_dashes():
    for _, schema, _, _ in ct.TOOLS:
        assert "—" not in json.dumps(schema, ensure_ascii=False)
        assert "–" not in json.dumps(schema, ensure_ascii=False)
