"""Every test keeps poll state in its own temp dir, never the real ~/.hermes."""

import pytest


@pytest.fixture(autouse=True)
def _poll_state_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("NEXTCLOUD_TALK_STATE_DIR", str(tmp_path / "nctalk-state"))
