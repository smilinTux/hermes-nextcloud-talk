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
        assert "\u2014" not in json.dumps(schema, ensure_ascii=False)
        assert "\u2013" not in json.dumps(schema, ensure_ascii=False)


# ------------------------------------------------- tool_search visibility pin
import sys  # noqa: E402
import types  # noqa: E402


def _fake_tool_search(monkeypatch, direct=frozenset({"project", "desktop_ui"})):
    mod = types.ModuleType("tools.tool_search")
    mod._DIRECT_SURFACE_TOOLSETS = direct
    monkeypatch.setitem(sys.modules, "tools.tool_search", mod)
    return mod


def test_pin_visible_adds_toolset_to_direct_surface(monkeypatch):
    mod = _fake_tool_search(monkeypatch)
    assert ct.pin_visible() is True
    assert mod._DIRECT_SURFACE_TOOLSETS == frozenset({"project", "desktop_ui", "nctalk-calls"})
    assert isinstance(mod._DIRECT_SURFACE_TOOLSETS, frozenset)


def test_pin_visible_is_idempotent(monkeypatch):
    mod = _fake_tool_search(monkeypatch)
    ct.pin_visible()
    first = mod._DIRECT_SURFACE_TOOLSETS
    ct.pin_visible()
    assert mod._DIRECT_SURFACE_TOOLSETS is first


def test_pin_visible_degrades_when_hermes_internals_move(monkeypatch):
    mod = types.ModuleType("tools.tool_search")  # attribute renamed/removed upstream
    monkeypatch.setitem(sys.modules, "tools.tool_search", mod)
    assert ct.pin_visible() is False


def test_register_tools_pins_visible(monkeypatch):
    mod = _fake_tool_search(monkeypatch)

    class Ctx:
        def register_tool(self, **kw):
            pass
    ct.register_tools(Ctx())
    assert "nctalk-calls" in mod._DIRECT_SURFACE_TOOLSETS


@pytest.mark.skipif(not os.getenv("HERMES_AGENT_SRC"), reason="set HERMES_AGENT_SRC to a hermes-agent checkout")
def test_pin_against_real_hermes_tool_search():
    """Runs the real Hermes classifier in a subprocess. HERMES_AGENT_PYTHON must be the
    python of an environment that already has Hermes' dependencies (its runtime venv)."""
    src = os.environ["HERMES_AGENT_SRC"]
    py = os.getenv("HERMES_AGENT_PYTHON", "python3")
    code = (
        "import sys, importlib.util; sys.path.insert(0, %r)\n"
        "from tools.registry import registry\n"
        "import tools.tool_search as ts\n"
        "registry.register(name='call_join', toolset='nctalk-calls', schema={'name':'call_join',"
        "'description':'x','parameters':{'type':'object','properties':{}}}, handler=lambda a, **k: '')\n"
        "before = ts.is_deferrable_tool_name('call_join')\n"
        "spec = importlib.util.spec_from_file_location('ct', %r); m = importlib.util.module_from_spec(spec)\n"
        "spec.loader.exec_module(m); ok = m.pin_visible()\n"
        "print(before, ok, ts.is_deferrable_tool_name('call_join'))\n"
    ) % (src, os.path.join(_ROOT, "tools.py"))
    out = subprocess.run([py, "-c", code], capture_output=True, text=True, cwd=src, timeout=120)
    assert out.returncode == 0, out.stderr[-2000:]
    assert out.stdout.split()[-3:] == ["True", "True", "False"]


# --------------------------------------- room resolution from inside a Talk room
# Regression: "@jarvis join call" in room yr3xaj4t; the model passed the home
# channel token (og63pxfh, listed in the session context) as room, and the call
# started in the wrong room.
HERE, HOME = "yr3xaj4t", "og63pxfh"


@pytest.fixture
def in_talk(sysd, monkeypatch):
    monkeypatch.setenv("HERMES_SESSION_PLATFORM", "nctalk")
    monkeypatch.setenv("HERMES_SESSION_CHAT_ID", HERE)
    return sysd


def _started(sysd):
    return [c[-1] for c in sysd.cmds if c[:3] == ["systemctl", "--user", "start"]]


def test_talk_room_no_room_arg_joins_current(in_talk):
    r = j(ct.call_join({}))
    assert r["room"] == HERE and r["status"] == "joining" and "room_note" not in r
    assert _started(in_talk) == [f"skcall-join@{HERE}.service"]


def test_talk_room_same_room_explicit(in_talk):
    r = j(ct.call_join({"room": HERE}))
    assert r["room"] == HERE and "room_note" not in r
    assert _started(in_talk) == [f"skcall-join@{HERE}.service"]


def test_talk_room_different_room_without_override_joins_current(in_talk):
    r = j(ct.call_join({"room": HOME, "ring": False, "greeting": "Jarvis here."}))
    assert r["room"] == HERE and r["status"] == "joining"
    assert HERE in r["room_note"] and HOME in r["room_note"] and "other_room" in r["room_note"]
    assert _started(in_talk) == [f"skcall-join@{HERE}.service"]
    assert not ct.env_file(HOME).exists() and ct.env_file(HERE).exists()


@pytest.mark.parametrize("flag", [False, "false", "no", None])
def test_talk_room_falsy_override_is_no_override(in_talk, flag):
    assert j(ct.call_join({"room": HOME, "other_room": flag}))["room"] == HERE


def test_talk_room_different_room_with_override_joins_that_room(in_talk):
    r = j(ct.call_join({"room": HOME, "other_room": True}))
    assert r["room"] == HOME and r["status"] == "joining"
    assert HOME in r["room_note"] and HERE in r["room_note"]
    assert _started(in_talk) == [f"skcall-join@{HOME}.service"]


def test_talk_room_invalid_room_without_override_uses_current(in_talk):
    # e.g. the model passes the room's display name; it is ignored, not an error
    assert j(ct.call_join({"room": "Jarvis test"}))["room"] == HERE


def test_talk_room_invalid_room_with_override_rejected(in_talk):
    assert "invalid room token" in j(ct.call_join({"room": "Jarvis test", "other_room": True}))["error"]
    assert in_talk.cmds == []


def test_non_talk_context_without_room_errors(sysd, monkeypatch):
    monkeypatch.setenv("HERMES_SESSION_PLATFORM", "telegram")
    monkeypatch.setenv("HERMES_SESSION_CHAT_ID", "-1003899092893")
    for fn in (ct.call_join, ct.call_leave):
        assert "room is required" in j(fn({}))["error"]
    assert sysd.cmds == []


def test_non_talk_context_explicit_room_needs_no_override(sysd, monkeypatch):
    monkeypatch.setenv("HERMES_SESSION_PLATFORM", "telegram")
    monkeypatch.setenv("HERMES_SESSION_CHAT_ID", "-1003899092893")
    r = j(ct.call_join({"room": HOME}))
    assert r["room"] == HOME and "room_note" not in r


def test_talk_room_leave_guarded(in_talk):
    in_talk.states[f"skcall-join@{HERE}.service"] = ACTIVE
    in_talk.states[f"skcall-join@{HOME}.service"] = ACTIVE
    r = j(ct.call_leave({"room": HOME}))
    assert r["room"] == HERE and r["status"] == "leaving" and "room_note" in r
    stops = [c[-1] for c in in_talk.cmds if c[:3] == ["systemctl", "--user", "stop"]]
    assert stops == [f"skcall-join@{HERE}.service"]


def test_talk_room_leave_other_room_with_override(in_talk):
    in_talk.states[f"skcall-join@{HOME}.service"] = ACTIVE
    r = j(ct.call_leave({"room": HOME, "other_room": True}))
    assert r["room"] == HOME and r["status"] == "leaving"


def test_talk_room_status_guarded(in_talk):
    in_talk.states[f"skcall-join@{HERE}.service"] = ACTIVE
    r = j(ct.call_status({"room": HOME}))
    assert r["room"] == HERE and r["status"] == "in call" and "room_note" in r
    assert j(ct.call_status({"room": HOME, "other_room": True}))["room"] == HOME


def test_already_in_call_note_is_agent_neutral(in_talk):
    in_talk.states[f"skcall-join@{HERE}.service"] = ACTIVE
    note = j(ct.call_join({}))["note"]
    assert note.startswith("You are already in this call")
    for word in ("She ", "He ", " her ", " his "):
        assert word not in note


def test_schemas_steer_room_empty_and_offer_override():
    for _, schema, _, _ in ct.TOOLS:
        props = schema["parameters"]["properties"]
        assert props["other_room"]["type"] == "boolean"
        assert "empty" in props["room"]["description"].lower()
        assert "home channel" in schema["description"].lower()


# ------------------------------------------------- room map (federated rooms)
NAM_UNIT = "skcall-join-nam@og63pxfh.service"


@pytest.fixture
def fed(sysd, monkeypatch):
    monkeypatch.setenv("NEXTCLOUD_TALK_CALL_ROOM_MAP", "m4jyfpxq:nam:og63pxfh, bad:x:y,abcd1234:NAM:og63pxfh")
    monkeypatch.setenv("HERMES_SESSION_PLATFORM", "nctalk")
    monkeypatch.setenv("HERMES_SESSION_CHAT_ID", "m4jyfpxq")
    return sysd


def test_room_map_parses_and_skips_bad_entries(fed):
    assert ct.room_map() == {"m4jyfpxq": ("nam", "og63pxfh")}
    assert ct.call_target("m4jyfpxq") == ("og63pxfh", NAM_UNIT, "nam")
    assert ct.call_target("xazze5d7") == ("xazze5d7", "skcall-join@xazze5d7.service", "")


def test_join_from_federated_room_starts_host_estate_unit(fed):
    r = j(ct.call_join({"ring": True}))
    assert r["status"] == "joining" and r["room"] == "m4jyfpxq"
    assert r["unit"] == NAM_UNIT and r["call_room"] == "og63pxfh" and r["estate"] == "nam"
    assert ["systemctl", "--user", "start", "--no-block", NAM_UNIT] in fed.cmds
    assert ct.env_file("og63pxfh").read_text() == "SKCALL_RING=1\n"   # %i of the nam unit
    assert not ct.env_file("m4jyfpxq").exists()


def test_federated_join_already_in_call_via_host_unit(fed):
    fed.states[NAM_UNIT] = ACTIVE
    r = j(ct.call_join({}))
    assert r["status"] == "already in call" and r["unit"] == NAM_UNIT
    assert "start" not in fed.verbs()


def test_federated_status_and_leave_use_host_unit(fed):
    fed.states[NAM_UNIT] = ACTIVE
    s = j(ct.call_status({}))
    assert s["status"] == "in call" and s["unit"] == NAM_UNIT and s["call_room"] == "og63pxfh"
    r = j(ct.call_leave({}))
    assert r["status"] == "leaving"
    assert ["systemctl", "--user", "stop", "--no-block", NAM_UNIT] in fed.cmds


def test_status_lists_estate_units_too(sysd):
    sysd.states["skcall-join@xazze5d7.service"] = ACTIVE
    sysd.states[NAM_UNIT] = ACTIVE
    r = j(ct.call_status({}))
    assert sorted(r["in_call"]) == ["og63pxfh", "xazze5d7"]
    assert {c["unit"] for c in r["calls"]} == {"skcall-join@xazze5d7.service", NAM_UNIT}
