"""Open polls the agent started, shared by the poll tools (tools.py) and the
user-mode adapter. Stdlib only: tools.py must stay import-light.

Two kinds, because Talk 24 only half covers what an agent needs:

- ``native``: a real Talk poll (``POST /poll/{token}``). Talk refuses it in a
  one-to-one conversation (``error: room``), and the federation proxy drops
  ``threadId``, so a poll started from a federated copy of a room lands in the
  host's main timeline instead of the thread. Talk also hides votes from the
  poll's author until it is closed, so the adapter closes it to read them.
- ``reaction``: a numbered message (1, 2, 3 keycaps) the agent pre-reacts to,
  so the person taps a number. Works in DMs and inside federated threads,
  because reactions and their ``reaction`` system messages travel everywhere.

One JSON file per agent account, rewritten atomically under an fcntl lock: the
tools and the adapter run in one gateway process today, but a CLI process may
call the tools too.
"""

from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import os
import time
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional

STATE_DIR_ENV = "NEXTCLOUD_TALK_STATE_DIR"
KEEP_SECONDS = 14 * 24 * 3600   # decided/closed records are pruned after this
OPEN_MAX_AGE = 30 * 24 * 3600   # an open poll nobody answered in a month is dropped
KEYCAPS = tuple(f"{n}️⃣" for n in range(1, 10))  # 1..9 keycap emoji
FILL_IN_LABEL = "Something else (reply in this thread)"


def keycap_index(emoji: str) -> Optional[int]:
    """0-based option index for a keycap reaction; also accepts the bare digit+keycap form."""
    e = (emoji or "").replace("️", "")
    for i, k in enumerate(KEYCAPS):
        if e == k.replace("️", ""):
            return i
    return None


def state_path(server: str, user: str) -> Path:
    base = os.getenv(STATE_DIR_ENV, "").strip()
    if not base:
        home = os.getenv("HERMES_HOME", "").strip() or os.path.expanduser("~/.hermes")
        base = os.path.join(home, "nctalk")
    acct = hashlib.sha256(f"{server.rstrip('/')}|{user.lower()}".encode()).hexdigest()[:12]
    return Path(base) / f"polls-{acct}.json"


@contextlib.contextmanager
def _locked(path: Path) -> Iterator[Dict[str, Any]]:
    """Yield the state dict under an exclusive lock; it is written back on exit."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(str(path) + ".lock", "a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            try:
                data = json.loads(path.read_text() or "{}")
            except (FileNotFoundError, ValueError):
                data = {}
            if not isinstance(data, dict):
                data = {}
            yield data
            _prune(data)
            tmp = path.with_suffix(".tmp")
            tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1))
            os.replace(tmp, path)
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def _prune(data: Dict[str, Any]) -> None:
    now = time.time()
    for key in list(data):
        rec = data[key]
        age = now - float(rec.get("updated") or rec.get("created") or 0)
        if rec.get("status") != "open" and age > KEEP_SECONDS:
            del data[key]
        elif rec.get("status") == "open" and age > OPEN_MAX_AGE:
            del data[key]


def record_key(kind: str, room: str, ident: str) -> str:
    return f"{kind}:{room}:{ident}"


def add(path: Path, rec: Dict[str, Any]) -> Dict[str, Any]:
    rec = dict(rec)
    rec.setdefault("status", "open")
    rec["created"] = rec["updated"] = time.time()
    with _locked(path) as data:
        data[rec["key"]] = rec
    return rec


def update(path: Path, key: str, **fields: Any) -> Optional[Dict[str, Any]]:
    with _locked(path) as data:
        rec = data.get(key)
        if rec is None:
            return None
        rec.update(fields)
        rec["updated"] = time.time()
        return dict(rec)


def claim(path: Path, key: str, status: str, **fields: Any) -> Optional[Dict[str, Any]]:
    """Move an OPEN record to ``status``; None if it was not open (someone else got it)."""
    with _locked(path) as data:
        rec = data.get(key)
        if rec is None or rec.get("status") != "open":
            return None
        rec.update(fields, status=status)
        rec["updated"] = time.time()
        return dict(rec)


def get(path: Path, key: str) -> Optional[Dict[str, Any]]:
    with _locked(path) as data:
        rec = data.get(key)
        return dict(rec) if rec else None


def find(path: Path, *, room: Optional[str] = None, thread: Optional[str] = None,
         status: Optional[str] = "open", kind: Optional[str] = None,
         poll_id: Optional[str] = None, message_id: Optional[str] = None) -> List[Dict[str, Any]]:
    """Records matching every given field, newest first."""
    with _locked(path) as data:
        out = []
        for rec in data.values():
            if room is not None and rec.get("room") != room:
                continue
            if thread is not None and (rec.get("thread") or "") != thread:
                continue
            if status is not None and rec.get("status") != status:
                continue
            if kind is not None and rec.get("kind") != kind:
                continue
            if poll_id is not None and str(rec.get("poll_id") or "") != str(poll_id):
                continue
            if message_id is not None and str(rec.get("message_id") or "") != str(message_id):
                continue
            out.append(dict(rec))
    return sorted(out, key=lambda r: r.get("created", 0), reverse=True)
