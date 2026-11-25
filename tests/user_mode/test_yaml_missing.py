"""PyYAML is a declared dependency; without it the plugin says so instead of going quiet."""

import logging
import sys

import pytest

from user import access, livedocs


@pytest.fixture
def no_yaml(monkeypatch):
    monkeypatch.setitem(sys.modules, "yaml", None)  # import yaml -> ImportError
    monkeypatch.setattr(access, "_YAML_WARNED", False)
    monkeypatch.setattr(livedocs, "_YAML_WARNED", False)


def test_access_file_falls_back_to_subset_parser_with_a_warning(no_yaml, caplog):
    with caplog.at_level(logging.WARNING):
        parsed = access.parse("users: [chef]\n")
        access.parse("users: [chef]\n")
    assert parsed["users"] == ["chef"]
    warnings = [r for r in caplog.records if "PyYAML is not installed" in r.getMessage()]
    assert len(warnings) == 1  # once, not per reload


def test_live_docs_auto_warns_and_treats_nc_live_as_absent(no_yaml, tmp_path, caplog):
    (tmp_path / "config.yaml").write_text("mcp_servers:\n  nc-live: {command: x}\n")
    with caplog.at_level(logging.WARNING):
        assert livedocs.read_hermes_config(str(tmp_path)) is None
    assert any("PyYAML is not installed" in r.getMessage() for r in caplog.records)


def test_nc_live_setup_refuses_visibly(no_yaml):
    import nclive_setup
    with pytest.raises(nclive_setup.SetupError, match="PyYAML is needed"):
        nclive_setup._yaml()


def test_manifest_declares_pyyaml_with_an_upper_bound():
    import os
    import yaml as real_yaml  # the real one; this test does not use the fixture
    root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    with open(os.path.join(root, "plugin.yaml"), encoding="utf-8") as f:
        deps = real_yaml.safe_load(f)["python_dependencies"]
    assert any(d.startswith("PyYAML") and "<" in d for d in deps)
