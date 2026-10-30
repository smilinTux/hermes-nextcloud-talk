"""Who may steer the agent in Talk: a per-agent access file, re-read when it changes.

The file is meant to be written by a management surface (the Agent Seats app)
rather than by hand in ``~/.hermes/.env``. YAML or JSON, default path
``~/.skcapstone/agents/<agent>/config/talk-access.yaml``::

    users:                 # people: they can talk to the agent and answer its polls
      - dknestri
      - mrarch@skhub.nativeassetmanagement.com
    agents:                # other agents it must hear (the agent-loop guard still applies)
      - jarvis@skhub.nativeassetmanagement.com
    rooms:                 # optional, per room token
      x7cppncn:
        users: [someone]   # added to the lists above, in this room only
        only: false        # true: in this room ONLY these users (plus agents)

Ids compare case-insensitively. A federated person is ``user@their-server``.

No file: the env allowlist (``NEXTCLOUD_TALK_ALLOWED_USERS`` /
``NEXTCLOUD_TALK_ALLOW_ALL_USERS``) applies exactly as before. A file that
fails to parse keeps the last good version (or the env list if there never
was one), so a typo never opens the agent to everyone.
"""

from __future__ import annotations

import json
import logging
import os
import time
from typing import Any, Dict, FrozenSet, Iterable, List, Optional

logger = logging.getLogger(__name__)

ACCESS_FILE_ENV = "NEXTCLOUD_TALK_ACCESS_FILE"
RECHECK_SECONDS = 5.0


def default_path(username: str = "") -> str:
    agent = (os.getenv("SKAGENT") or os.getenv("SKCAPSTONE_AGENT")
             or os.getenv("SKMEMORY_AGENT") or username or "lumina")
    return os.path.expanduser(f"~/.skcapstone/agents/{agent}/config/talk-access.yaml")


def _ids(raw: Any) -> List[str]:
    if raw is None:
        return []
    if isinstance(raw, str):
        raw = raw.split(",")
    if not isinstance(raw, (list, tuple, set)):
        return []
    out = []
    for item in raw:
        s = str(item or "").strip()
        if s.startswith("federated_users/"):
            s = s[len("federated_users/"):]
        if s:
            out.append(s)
    return out


def parse(text: str) -> Dict[str, Any]:
    """Parse the file body (YAML when PyYAML is present, else JSON) into a normalized dict."""
    data: Any = None
    try:
        import yaml  # type: ignore

        data = yaml.safe_load(text)
    except ImportError:
        data = json.loads(text)
    if data is None:
        data = {}
    if not isinstance(data, dict):
        raise ValueError("access file must be a mapping")
    rooms: Dict[str, Dict[str, Any]] = {}
    for token, spec in (data.get("rooms") or {}).items():
        spec = spec if isinstance(spec, dict) else {"users": spec}
        rooms[str(token)] = {"users": _ids(spec.get("users")), "only": bool(spec.get("only"))}
    return {"users": _ids(data.get("users")), "agents": _ids(data.get("agents")), "rooms": rooms}


class TalkAccess:
    """Allowlist decisions for one agent account, from the access file or the env."""

    def __init__(self, path: Optional[str], env_users: Iterable[str] = (), env_allow_all: bool = False):
        self.path = path or ""
        self._env_users: FrozenSet[str] = frozenset(u.lower() for u in env_users)
        self._env_allow_all = bool(env_allow_all)
        self._spec: Optional[Dict[str, Any]] = None
        self._mtime: Optional[float] = None
        self._checked = 0.0
        self.reload(force=True)

    @property
    def source(self) -> str:
        return "file" if self._spec is not None else "env"

    def reload(self, force: bool = False) -> bool:
        """Re-read the file if it changed. True when the effective lists changed."""
        now = time.monotonic()
        if not force and now - self._checked < RECHECK_SECONDS:
            return False
        self._checked = now
        if not self.path:
            return False
        try:
            mtime = os.stat(self.path).st_mtime
        except OSError:
            if self._spec is not None:
                logger.warning("[nctalk] access file %s is gone: keeping the last good list", self.path)
                self._mtime = None
            return False
        if mtime == self._mtime and not force:
            return False
        try:
            with open(self.path, encoding="utf-8") as fh:
                spec = parse(fh.read())
        except Exception as e:
            logger.warning("[nctalk] access file %s unreadable (%s): keeping the %s list",
                           self.path, e, "last good" if self._spec is not None else "env")
            self._mtime = mtime
            return False
        self._mtime = mtime
        changed = spec != self._spec
        self._spec = spec
        if changed:
            logger.info("[nctalk] access file %s: %d people, %d agents, %d room rules",
                        self.path, len(spec["users"]), len(spec["agents"]), len(spec["rooms"]))
        return changed

    def allowed(self, user_id: Optional[str], room: Optional[str] = None) -> bool:
        uid = (user_id or "").strip().lower()
        if uid.startswith("federated_users/"):
            uid = uid[len("federated_users/"):]
        if self._spec is None:
            return self._env_allow_all or (bool(uid) and uid in self._env_users)
        if not uid:
            return False
        spec = self._spec
        agents = {a.lower() for a in spec["agents"]}
        rule = spec["rooms"].get(room or "") if room else None
        people = {u.lower() for u in (rule or {}).get("users", [])}
        if not (rule and rule.get("only")):
            people |= {u.lower() for u in spec["users"]}
        return uid in people or uid in agents

    def people(self) -> FrozenSet[str]:
        """The listed people (not agents, not room-only entries), lowercase."""
        if self._spec is None:
            return self._env_users
        return frozenset(u.lower() for u in self._spec["users"])

    def gateway_ids(self) -> List[str]:
        """Every id the file admits anywhere, original spelling and lowercase, for the
        gateway's own allowlist check (which compares exact strings)."""
        if self._spec is None:
            return []
        seen: List[str] = []
        names = list(self._spec["users"]) + list(self._spec["agents"])
        for rule in self._spec["rooms"].values():
            names += rule["users"]
        for n in names:
            for v in (n, n.lower()):
                if v not in seen:
                    seen.append(v)
        return seen
