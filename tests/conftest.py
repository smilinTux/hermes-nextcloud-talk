"""Every test keeps poll state in its own temp dir, never the real ~/.hermes, and
never reads a real access file."""

import pytest


@pytest.fixture(autouse=True)
def _poll_state_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("NEXTCLOUD_TALK_STATE_DIR", str(tmp_path / "nctalk-state"))
    # Never pick up a real agent's talk-access.yaml from ~/.skcapstone.
    monkeypatch.setenv("NEXTCLOUD_TALK_ACCESS_FILE", str(tmp_path / "no-access-file.yaml"))
