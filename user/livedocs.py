"""Shared-document context for user mode, and nc-live detection.

When a Talk message carries a shared file (a ``file`` rich object in
``messageParameters``) or a pasted link to a file on the same Nextcloud
(``https://<server>/index.php/f/<fileid>``), the agent gets one short line
with the file's facts: name, fileid, path. When the optional sk-nc-live MCP
server (``nc-live``) is configured for this platform, documents it can edit
also get a hint that the ``live_doc_*`` tools work on them, so "fix the
intro" works without anyone pasting a link.

The last few documents per room are remembered in memory (bounded, never on
disk), so a follow-up in the same room can say "the doc".

Stdlib only; PyYAML is used only if importable, to read the Hermes config.
"""

from __future__ import annotations

import os
import re
from collections import OrderedDict
from typing import Any, Dict, Iterable, List, Mapping, Optional
from urllib.parse import urlsplit

LIVE_DOCS_ENV = "NEXTCLOUD_TALK_LIVE_DOCS"  # auto (default) | on | off
RECENT_DOCS_ENV = "NEXTCLOUD_TALK_RECENT_DOCS"  # per room, default 3, 0 disables
DEFAULT_RECENT_DOCS = 3
MAX_ROOMS = 200
NC_LIVE_SERVER = "nc-live"
PLATFORM = "nctalk"

# What sk-nc-live edits: Text markdown, and office files through Collabora.
EDITABLE_EXTENSIONS = frozenset({
    ".md", ".markdown",
    ".docx", ".doc", ".odt", ".rtf",
    ".xlsx", ".xls", ".ods",
    ".pptx", ".ppt", ".odp",
})
_EDITABLE_MIME_PREFIXES = (
    "text/markdown",
    "application/vnd.openxmlformats-officedocument.",
    "application/vnd.oasis.opendocument.text",
    "application/vnd.oasis.opendocument.spreadsheet",
    "application/vnd.oasis.opendocument.presentation",
    "application/msword",
    "application/vnd.ms-excel",
    "application/vnd.ms-powerpoint",
)

_LINK_RE = re.compile(r"https?://([^\s/<>\"')\]]+)(?:/index\.php)?/f/(\d+)\b")


def is_editable(name: str, mimetype: str = "") -> bool:
    ext = os.path.splitext(name or "")[1].lower()
    if ext in EDITABLE_EXTENSIONS:
        return True
    mime = (mimetype or "").lower()
    return bool(mime) and mime.startswith(_EDITABLE_MIME_PREFIXES)


def _clean(value: Any, limit: int = 200) -> str:
    """One line, no brackets: the value is user-controlled and lands in the prompt."""
    text = re.sub(r"[\r\n\t\[\]]+", " ", str(value or "")).strip()
    return text[:limit]


def docs_from_parameters(parameters: Optional[Mapping[str, Any]]) -> List[Dict[str, str]]:
    """Facts of the files shared in a message that this account can reach.

    A file shared from a federated room has no path on this server, so it is
    left out: the fileid belongs to the other server.
    """
    docs = []
    for key, p in sorted((parameters or {}).items()):
        if not (isinstance(p, dict) and p.get("type") == "file" and str(key).startswith("file")):
            continue
        fileid = str(p.get("id") or "").strip()
        if not fileid.isdigit() or not p.get("path"):
            continue
        name = _clean(p.get("name")) or "file"
        mime = _clean(p.get("mimetype"), 100)
        docs.append({
            "id": fileid,
            "name": name,
            "path": _clean(p.get("path"), 400),
            "mimetype": mime,
            "editable": "1" if is_editable(name, mime) else "",
        })
    return docs


def docs_from_links(text: str, server_url: str) -> List[Dict[str, str]]:
    """Fileids from pasted ``/f/<id>`` links that point at our own server."""
    own = (urlsplit(server_url or "").netloc or "").lower()
    if not own or not text:
        return []
    seen, docs = set(), []
    for host, fileid in _LINK_RE.findall(text):
        if host.lower() != own or fileid in seen:
            continue
        seen.add(fileid)
        # Name and type are unknown from a link alone; sk-nc-live takes the id.
        docs.append({"id": fileid, "name": "", "path": "", "mimetype": "", "editable": "?"})
    return docs


def describe(doc: Mapping[str, str], live: bool) -> str:
    """``College Tracker.md, fileid 123, path Talk/College Tracker.md``."""
    parts = [doc["name"]] if doc.get("name") else ["linked file"]
    parts.append(f"fileid {doc['id']}")
    if doc.get("path"):
        parts.append(f"path {doc['path']}")
    if live and doc.get("editable") == "1":
        parts.append("editable live with live_doc_* tools")
    elif live and doc.get("editable") == "?":
        parts.append("live_doc_* tools take this fileid if it is a text or office document")
    return ", ".join(parts)


class RecentDocs:
    """The last few documents per room, newest first, in memory only."""

    def __init__(self, per_room: int = DEFAULT_RECENT_DOCS, max_rooms: int = MAX_ROOMS) -> None:
        self.per_room = max(0, per_room)
        self.max_rooms = max(1, max_rooms)
        self._rooms: "OrderedDict[str, List[Dict[str, str]]]" = OrderedDict()

    def note(self, room: str, docs: Iterable[Mapping[str, str]]) -> None:
        if self.per_room <= 0:
            return
        current = self._rooms.pop(room, [])
        for doc in docs:
            if doc.get("editable") == "" or not doc.get("id"):
                continue  # images, PDFs and the like are not "the doc"
            known = next((d for d in current if d["id"] == doc["id"]), None)
            current = [d for d in current if d["id"] != doc["id"]]
            entry = dict(doc)
            if known and not entry.get("name"):
                entry = known  # a link to a doc we already know keeps its name
            current.insert(0, entry)
        if current:
            self._rooms[room] = current[: self.per_room]
            while len(self._rooms) > self.max_rooms:
                self._rooms.popitem(last=False)

    def get(self, room: str) -> List[Dict[str, str]]:
        return list(self._rooms.get(room, []))


def context_line(current: List[Mapping[str, str]], recent: List[Mapping[str, str]],
                 live: bool) -> str:
    """The block added to the turn: this message's files, then earlier docs."""
    lines = [f"[shared file: {describe(d, live)}]" for d in current]
    seen = {d["id"] for d in current}
    earlier = [d for d in recent if d["id"] not in seen]
    if earlier:
        lines.append("[earlier shared doc in this room, newest first: "
                     + "; ".join(describe(d, live) for d in earlier) + "]")
    return "\n".join(lines)


def _toolset_allows(cfg: Mapping[str, Any], platform: str, server: str) -> bool:
    toolsets = (cfg.get("platform_toolsets") or {}).get(platform)
    if not isinstance(toolsets, list):
        return True  # no allowlist for this platform: every MCP server is exposed
    return server in toolsets or f"mcp-{server}" in toolsets


def nc_live_configured(cfg: Optional[Mapping[str, Any]], server: str = NC_LIVE_SERVER,
                       platform: str = PLATFORM) -> bool:
    """True when the Hermes config has an enabled ``nc-live`` MCP server for Talk."""
    if not isinstance(cfg, Mapping):
        return False
    entry = (cfg.get("mcp_servers") or {}).get(server)
    if not isinstance(entry, Mapping):
        return False
    if str(entry.get("enabled", True)).strip().lower() in ("false", "0", "off", "no"):
        return False
    return _toolset_allows(cfg, platform, server)


def read_hermes_config(home: str) -> Optional[Dict[str, Any]]:
    try:
        import yaml  # Hermes depends on PyYAML
        with open(os.path.join(home, "config.yaml"), encoding="utf-8") as f:
            data = yaml.safe_load(f)
    except Exception:
        return None
    return data if isinstance(data, dict) else None


def live_tools_available(home: str, raw: Optional[str] = None) -> bool:
    """``NEXTCLOUD_TALK_LIVE_DOCS``: ``on``/``off`` force it, ``auto`` reads the config."""
    mode = (raw if raw is not None else os.getenv(LIVE_DOCS_ENV, "auto")).strip().lower()
    if mode in ("on", "1", "true", "yes"):
        return True
    if mode in ("off", "0", "false", "no"):
        return False
    return nc_live_configured(read_hermes_config(home))


def recent_docs_limit(raw: Optional[str] = None) -> int:
    value = raw if raw is not None else os.getenv(RECENT_DOCS_ENV, str(DEFAULT_RECENT_DOCS))
    try:
        return max(0, int(str(value).strip()))
    except (TypeError, ValueError):
        return DEFAULT_RECENT_DOCS
