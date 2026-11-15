"""A Talk room's workspace: the folders the agent's account shared into the room.

The root is every folder the agent OWNS and has shared to the room as a Talk room share
(share_type 10), outside the automatic ``Talk/`` upload folder. It is resolved on every call,
so unsharing a folder in the Files app closes it at once, and the share's own permissions
apply: a share without "create" refuses new files, one without "edit" refuses changes.

Guests may list, read, create and edit text files in it. Nothing here deletes, moves,
renames or shares; overwritten content stays in Nextcloud's file versions.

Paths are relative to a root: ``notes.md`` when the room has one folder, ``<folder>/notes.md``
when it has several. Absolute paths, ``..``, ``.`` segments, hidden names, backslashes and
control characters are refused before any request is made.

Stdlib only. ``dav`` and ``ocs`` are callables so tests (and the async adapter, via a
thread) can supply their own transport:

- ``dav(method, path, headers=None, content=None) -> (status, headers, text)``
- ``ocs(method, path, params=None) -> (status, data)``
"""

from __future__ import annotations

import posixpath
import re
import xml.etree.ElementTree as ET
from typing import Any, Callable, Dict, List, Optional, Tuple
from urllib.parse import quote, unquote

ROOM_SHARE = 10
PERM_UPDATE, PERM_CREATE = 2, 4
SHARES_PATH = "/ocs/v2.php/apps/files_sharing/api/v1/shares"
TEXT_EXT = frozenset({"md", "markdown", "txt", "csv", "tsv", "json", "yaml", "yml", "html",
                      "htm", "xml", "ics", "vcf", "org", "rst", "log", "ini", "toml"})
READ_MAX = 60000      # characters returned by a read
WRITE_MAX = 200000    # characters accepted by a write
LIST_MAX = 200
MODES = ("create", "replace", "append")
_BAD_CHARS = re.compile(r"[\x00-\x1f\x7f\\]")

Dav = Callable[..., Tuple[int, Dict[str, str], str]]
Ocs = Callable[..., Tuple[int, Any]]


class WorkspaceError(Exception):
    """A refusal or failure, worded for the person."""


def clean_rel(path: Any) -> List[str]:
    """``path`` split into safe segments; raises WorkspaceError on anything that could escape."""
    raw = str(path if path is not None else "").strip()
    if _BAD_CHARS.search(raw):
        raise WorkspaceError("that path has characters that are not allowed (backslash or control)")
    if raw.startswith("/") or raw.startswith("~"):
        raise WorkspaceError("use a path inside this room's folder, not an absolute one")
    parts = [p for p in raw.split("/") if p != ""]
    for p in parts:
        if p in (".", "..") or p.startswith("."):
            raise WorkspaceError("that path leaves this room's folder or names a hidden file")
        if len(p) > 200:
            raise WorkspaceError("a name in that path is too long")
    if len(parts) > 12:
        raise WorkspaceError("that path is nested too deep")
    return parts


def roots(ocs: Ocs, room: str, user: str) -> List[Dict[str, Any]]:
    """The folders ``user`` owns and shared into ``room`` (not the Talk upload folders)."""
    status, data = ocs("GET", SHARES_PATH, params={"shared_with_me": "false"})
    if status >= 300 or not isinstance(data, list):
        raise WorkspaceError(f"could not look up this room's folders (HTTP {status})")
    out = []
    for s in data:
        if not isinstance(s, dict):
            continue
        try:
            stype = int(s.get("share_type"))
        except (TypeError, ValueError):
            continue
        path = str(s.get("path") or "")
        if (stype != ROOM_SHARE or str(s.get("share_with") or "") != room
                or s.get("item_type") != "folder"
                or str(s.get("uid_owner") or "").lower() != user.lower()
                or not path.startswith("/") or path.rstrip("/") == "/Talk"
                or path.startswith("/Talk/")):
            continue
        try:
            perms = int(s.get("permissions") or 0)
        except (TypeError, ValueError):
            perms = 0
        out.append({"name": posixpath.basename(path.rstrip("/")), "path": path.rstrip("/"),
                    "fileid": str(s.get("item_source") or s.get("file_source") or ""),
                    "perms": perms})
    out.sort(key=lambda r: r["name"].lower())
    return out


def resolve(rts: List[Dict[str, Any]], parts: List[str]) -> Tuple[Dict[str, Any], List[str]]:
    """(root, path inside it). One root: paths are inside it. Several: the first segment
    names the root (a single root may also be named, so links stay valid if a second is added)."""
    if not rts:
        raise WorkspaceError("this room has no shared folder yet: the owner can share one to the "
                             "room from the Files app (Share, then pick this conversation)")
    if parts:
        for r in rts:
            if parts[0] == r["name"]:
                return r, parts[1:]
    if len(rts) == 1:
        return rts[0], parts
    names = ", ".join(r["name"] for r in rts)
    raise WorkspaceError(f"this room has several folders; start the path with one of: {names}")


def dav_path(user: str, root: Dict[str, Any], parts: List[str]) -> str:
    segs = [s for s in root["path"].split("/") if s] + parts
    return f"/remote.php/dav/files/{quote(user, safe='@')}/" + "/".join(quote(s, safe="") for s in segs)


def shown(root: Dict[str, Any], parts: List[str], multi: bool) -> str:
    return "/".join(([root["name"]] if multi else []) + parts)


_NS = {"d": "DAV:", "oc": "http://owncloud.org/ns"}
_PROPFIND = ('<?xml version="1.0"?><d:propfind xmlns:d="DAV:" xmlns:oc="http://owncloud.org/ns">'
             "<d:prop><d:resourcetype/><d:getcontentlength/><d:getlastmodified/>"
             "<d:getcontenttype/><oc:fileid/><oc:size/></d:prop></d:propfind>")


def _propfind(dav: Dav, path: str, depth: str) -> List[Dict[str, Any]]:
    status, _h, text = dav("PROPFIND", path, headers={"Depth": depth,
                                                       "Content-Type": "application/xml"},
                           content=_PROPFIND)
    if status == 404:
        raise WorkspaceError("not found")
    if status >= 300:
        raise WorkspaceError(f"could not read the folder (HTTP {status})")
    try:
        tree = ET.fromstring(text)
    except ET.ParseError:
        raise WorkspaceError("the server sent an unreadable folder listing")
    out = []
    for resp in tree.findall("d:response", _NS):
        href = unquote(resp.findtext("d:href", "", _NS))
        prop = resp.find("d:propstat/d:prop", _NS)
        if prop is None:
            continue
        is_dir = prop.find("d:resourcetype/d:collection", _NS) is not None
        size = prop.findtext("oc:size", "", _NS) or prop.findtext("d:getcontentlength", "", _NS)
        out.append({"href": href.rstrip("/"), "dir": is_dir,
                    "size": int(size) if str(size).isdigit() else None,
                    "modified": prop.findtext("d:getlastmodified", "", _NS),
                    "type": prop.findtext("d:getcontenttype", "", _NS),
                    "fileid": prop.findtext("oc:fileid", "", _NS)})
    return out


def list_dir(dav: Dav, user: str, rts: List[Dict[str, Any]], rel: Any = "") -> Dict[str, Any]:
    parts = clean_rel(rel)
    multi = len(rts) > 1
    if multi and not parts:
        return {"path": "", "entries": [{"name": r["name"], "dir": True, "fileid": r["fileid"],
                                         "writable": bool(r["perms"] & PERM_CREATE)} for r in rts]}
    root, inner = resolve(rts, parts)
    entries = _propfind(dav, dav_path(user, root, inner), "1")
    if not entries:
        raise WorkspaceError("not found")
    here, items = entries[0], entries[1:]
    if not here["dir"]:
        raise WorkspaceError("that is a file; read it with talk_workspace_read")
    out = []
    for e in items[:LIST_MAX]:
        name = posixpath.basename(e["href"])
        if name.startswith("."):
            continue
        out.append({"name": name, "dir": e["dir"], "size": e["size"], "modified": e["modified"],
                    "fileid": e["fileid"]})
    out.sort(key=lambda x: (not x["dir"], x["name"].lower()))
    return {"path": shown(root, inner, multi), "entries": out,
            "writable": bool(root["perms"] & PERM_CREATE)}


def _ext_ok(name: str) -> bool:
    return "." in name and name.rsplit(".", 1)[1].lower() in TEXT_EXT


def read_file(dav: Dav, user: str, rts: List[Dict[str, Any]], rel: Any) -> Dict[str, Any]:
    parts = clean_rel(rel)
    if not parts:
        raise WorkspaceError("name a file to read")
    root, inner = resolve(rts, parts)
    if not inner:
        raise WorkspaceError("that is the folder itself; list it with talk_workspace_list")
    if not _ext_ok(inner[-1]):
        raise WorkspaceError("only text files (md, txt, csv, json, yaml, html and similar) can be read here")
    status, _h, text = dav("GET", dav_path(user, root, inner))
    if status == 404:
        raise WorkspaceError("not found")
    if status >= 300:
        raise WorkspaceError(f"could not read the file (HTTP {status})")
    out = {"path": shown(root, inner, len(rts) > 1), "content": text[:READ_MAX]}
    if len(text) > READ_MAX:
        out["truncated"] = f"first {READ_MAX} of {len(text)} characters"
    return out


def check_write(rts: List[Dict[str, Any]], rel: Any, content: Any, mode: Any) -> Tuple[Dict[str, Any], List[str], str, str]:
    """Validate a write without touching the server: (root, inner path, content, mode)."""
    mode = str(mode or "create").strip().lower()
    if mode not in MODES:
        raise WorkspaceError("mode must be create, replace or append")
    parts = clean_rel(rel)
    root, inner = resolve(rts, parts)
    if not inner:
        raise WorkspaceError("name the file to write, e.g. notes.md")
    if not _ext_ok(inner[-1]):
        raise WorkspaceError("only text files (md, txt, csv, json, yaml, html and similar) can be written here")
    text = str(content if content is not None else "")
    if len(text) > WRITE_MAX:
        raise WorkspaceError(f"that is too long to write in one go (max {WRITE_MAX} characters)")
    need = PERM_CREATE if mode == "create" else PERM_UPDATE
    if not root["perms"] & need:
        raise WorkspaceError("this room's folder is shared read-only for that "
                             f"({'new files' if mode == 'create' else 'changes'} are off)")
    return root, inner, text, mode


def write_file(dav: Dav, user: str, rts: List[Dict[str, Any]], rel: Any, content: Any,
               mode: Any = "create") -> Dict[str, Any]:
    """Create, replace or append to a text file. Parent folders are created as needed.
    ``create`` never overwrites; ``append`` adds to the current text with an etag check."""
    root, inner, text, mode = check_write(rts, rel, content, mode)
    path = dav_path(user, root, inner)
    headers = {"Content-Type": "text/plain; charset=utf-8"}
    if mode == "create":
        headers["If-None-Match"] = "*"
        for i in range(1, len(inner)):
            st, _h, _t = dav("MKCOL", dav_path(user, root, inner[:i]))
            if st not in (201, 405):  # 405: already there
                raise WorkspaceError(f"could not create folder {'/'.join(inner[:i])} (HTTP {st})")
    elif mode == "append":
        st, h, cur = dav("GET", path)
        if st == 404:
            raise WorkspaceError("there is no such file to append to; use mode create")
        if st >= 300:
            raise WorkspaceError(f"could not read the file to append (HTTP {st})")
        etag = {k.lower(): v for k, v in (h or {}).items()}.get("etag")
        if etag:
            headers["If-Match"] = etag
        sep = "" if not cur or cur.endswith("\n") else "\n"
        text = cur + sep + text
    else:
        headers["If-Match"] = "*"  # replace an existing file only
    st, h, body = dav("PUT", path, headers=headers, content=text.encode("utf-8"))
    if st == 412:
        raise WorkspaceError("that file already exists; use mode replace or append"
                             if mode == "create" else
                             "the file changed or does not exist; read it again and retry")
    if st == 423:
        raise WorkspaceError("the file is open in the editor (locked); try again when it is closed")
    if st >= 300:
        raise WorkspaceError(f"could not write the file (HTTP {st})")
    fid = {k.lower(): v for k, v in (h or {}).items()}.get("oc-fileid", "")
    return {"path": shown(root, inner, len(rts) > 1), "mode": mode, "chars": len(text),
            "fileid": str(fid), "created": st == 201}


def http_transport(server: str, user: str, password: str, timeout: float = 30.0) -> Tuple[Dav, Ocs]:
    """Blocking httpx transport as the agent's account (httpx is a Hermes dependency)."""
    import httpx

    def dav(method: str, path: str, headers: Optional[dict] = None, content: Any = None):
        with httpx.Client(base_url=server, auth=(user, password), timeout=timeout) as c:
            r = c.request(method, path, headers=headers or {}, content=content)
        return r.status_code, dict(r.headers), r.text

    def ocs(method: str, path: str, params: Optional[dict] = None):
        with httpx.Client(base_url=server, auth=(user, password), timeout=timeout,
                          headers={"OCS-APIRequest": "true", "Accept": "application/json"}) as c:
            r = c.request(method, path, params={**(params or {}), "format": "json"})
        try:
            return r.status_code, r.json().get("ocs", {}).get("data")
        except Exception:
            return r.status_code, r.text[:300]

    return dav, ocs
