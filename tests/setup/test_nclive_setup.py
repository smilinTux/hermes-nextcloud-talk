"""v1.15.0: ``hermes nctalk nc-live-setup`` writes mcp_servers.nc-live safely."""

import argparse
import importlib.util
import io
import os
import sys

import pytest
import yaml

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)  # for the ``user`` package (default config path)
_spec = importlib.util.spec_from_file_location("nctalk_nclive_setup", os.path.join(_ROOT, "nclive_setup.py"))
ns = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ns)

ENTRY = ns.expected_entry(ns.DEFAULT_COMMAND, ns.DEFAULT_PASSWORD_ENV)

EXISTING = """\
# my hermes config
model:
  default: some-model   # keep me

mcp_servers:
  # the general Nextcloud MCP
  nextcloud:
    command: uvx
    args: ["nextcloud-mcp-server", "run"]
    enabled: true

# platforms below
platforms:
  nctalk:
    enabled: true
"""


def _run(path, **kw):
    out = io.StringIO()
    rc = ns.run(str(path), out=out, **kw)
    return rc, out.getvalue()


def test_entry_holds_references_never_secrets():
    text = "\n".join(ns.entry_lines())
    assert '"${NEXTCLOUD_TALK_SERVER_URL}"' in text
    assert '"${NEXTCLOUD_TALK_USERNAME}"' in text
    assert '"${NEXTCLOUD_TALK_APP_PASSWORD}"' in text
    assert yaml.safe_load("mcp_servers:\n" + text)["mcp_servers"]["nc-live"] == ENTRY


def test_adds_into_existing_mcp_servers_keeping_comments(tmp_path, monkeypatch):
    monkeypatch.setenv("NEXTCLOUD_TALK_APP_PASSWORD", "real-secret-value")
    cfg = tmp_path / "config.yaml"
    cfg.write_text(EXISTING)
    rc, out = _run(cfg, assume_yes=True)
    assert rc == 0 and "Written. Backup:" in out
    text = cfg.read_text()
    assert "real-secret-value" not in text
    for keep in ("# my hermes config", "# keep me", "# the general Nextcloud MCP", "# platforms below"):
        assert keep in text
    data = yaml.safe_load(text)
    assert data["mcp_servers"]["nc-live"] == ENTRY
    assert data["mcp_servers"]["nextcloud"]["command"] == "uvx"
    assert data["platforms"] == {"nctalk": {"enabled": True}}
    # inserted inside mcp_servers, before the comment that belongs to the next key
    assert text.index("nc-live:") < text.index("# platforms below")
    backups = [p for p in os.listdir(tmp_path) if p.startswith("config.yaml.bak-nc-live-")]
    assert len(backups) == 1 and (tmp_path / backups[0]).read_text() == EXISTING


def test_idempotent_never_overwrites_an_existing_entry(tmp_path):
    cfg = tmp_path / "config.yaml"
    cfg.write_text("mcp_servers:\n  nc-live:\n    command: /opt/my-wrapper\n")
    rc, out = _run(cfg, assume_yes=True)
    assert rc == 0 and "already exists" in out
    assert cfg.read_text() == "mcp_servers:\n  nc-live:\n    command: /opt/my-wrapper\n"
    assert not [p for p in os.listdir(tmp_path) if "bak" in p]


def test_second_run_is_a_no_op(tmp_path):
    cfg = tmp_path / "config.yaml"
    cfg.write_text(EXISTING)
    _run(cfg, assume_yes=True)
    first = cfg.read_text()
    rc, out = _run(cfg, assume_yes=True)
    assert rc == 0 and "already exists" in out and cfg.read_text() == first


def test_asks_before_writing_and_no_means_no(tmp_path):
    cfg = tmp_path / "config.yaml"
    cfg.write_text(EXISTING)
    asked = []
    rc, out = _run(cfg, ask=lambda q: asked.append(q) or "n")
    assert rc == 0 and asked and "Nothing written." in out
    assert "nc-live:" in out  # the block was shown
    assert cfg.read_text() == EXISTING


def test_yes_answer_writes(tmp_path):
    cfg = tmp_path / "config.yaml"
    cfg.write_text(EXISTING)
    rc, _ = _run(cfg, ask=lambda q: "y")
    assert rc == 0 and "nc-live" in yaml.safe_load(cfg.read_text())["mcp_servers"]


def test_dry_run_writes_nothing(tmp_path):
    cfg = tmp_path / "config.yaml"
    cfg.write_text(EXISTING)
    rc, out = _run(cfg, dry_run=True, ask=lambda q: pytest.fail("must not ask"))
    assert rc == 0 and "Dry run" in out and cfg.read_text() == EXISTING


def test_no_mcp_servers_key_appends_one(tmp_path):
    cfg = tmp_path / "config.yaml"
    cfg.write_text("model:\n  default: x\n")
    assert _run(cfg, assume_yes=True)[0] == 0
    data = yaml.safe_load(cfg.read_text())
    assert data == {"model": {"default": "x"}, "mcp_servers": {"nc-live": ENTRY}}


def test_missing_config_file_is_created(tmp_path):
    cfg = tmp_path / "config.yaml"
    rc, out = _run(cfg, assume_yes=True)
    assert rc == 0 and yaml.safe_load(cfg.read_text()) == {"mcp_servers": {"nc-live": ENTRY}}


def test_empty_flow_mapping_header_is_rewritten(tmp_path):
    cfg = tmp_path / "config.yaml"
    cfg.write_text("mcp_servers: {}\nother: 1\n")
    assert _run(cfg, assume_yes=True)[0] == 0
    assert yaml.safe_load(cfg.read_text()) == {"mcp_servers": {"nc-live": ENTRY}, "other": 1}


def test_mcp_servers_as_last_block_with_four_space_indent(tmp_path):
    cfg = tmp_path / "config.yaml"
    cfg.write_text("a: 1\nmcp_servers:\n    nextcloud:\n        command: x\n\n")
    assert _run(cfg, assume_yes=True)[0] == 0
    text = cfg.read_text()
    assert "\n    nc-live:\n" in text
    assert yaml.safe_load(text)["mcp_servers"] == {"nextcloud": {"command": "x"}, "nc-live": ENTRY}


def test_flow_style_is_refused_and_block_printed(tmp_path):
    cfg = tmp_path / "config.yaml"
    original = "mcp_servers: {nextcloud: {command: x}}\n"
    cfg.write_text(original)
    rc, out = _run(cfg, assume_yes=True)
    assert rc == 1 and "by hand" in out and "nc-live:" in out
    assert cfg.read_text() == original


def test_unparseable_config_is_refused(tmp_path):
    cfg = tmp_path / "config.yaml"
    cfg.write_text("a: [unclosed\n")
    rc, out = _run(cfg, assume_yes=True)
    assert rc == 1 and "does not parse" in out and cfg.read_text() == "a: [unclosed\n"


def test_custom_command_and_password_env(tmp_path):
    cfg = tmp_path / "config.yaml"
    _run(cfg, assume_yes=True, command="/opt/nc-live/bin/run", password_env="NC_LIVE_APP_PASSWORD")
    entry = yaml.safe_load(cfg.read_text())["mcp_servers"]["nc-live"]
    assert entry["command"] == "/opt/nc-live/bin/run"
    assert entry["env"]["NEXTCLOUD_PASSWORD"] == "${NC_LIVE_APP_PASSWORD}"


def test_password_env_must_be_a_name_not_a_value(tmp_path):
    cfg = tmp_path / "config.yaml"
    rc, out = _run(cfg, assume_yes=True, password_env="hunter2 secret!")
    assert rc == 2 and not cfg.exists() and "hunter2" not in out


def test_cli_parser_and_handler(tmp_path):
    parser = argparse.ArgumentParser()
    ns._setup_parser(parser)
    cfg = tmp_path / "config.yaml"
    args = parser.parse_args(["nc-live-setup", "--config", str(cfg), "--yes"])
    assert ns._handle(args) == 0 and "nc-live" in yaml.safe_load(cfg.read_text())["mcp_servers"]
    assert ns._handle(parser.parse_args([])) == 2


def test_register_cli_uses_hermes_hook_when_present():
    calls = []

    class Ctx:
        def register_cli_command(self, **kw):
            calls.append(kw)

    ns.register_cli(Ctx())
    assert calls and calls[0]["name"] == "nctalk"
    ns.register_cli(object())  # older Hermes without the hook: no error
