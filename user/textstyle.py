"""Outbound text style: no em or en dashes as punctuation.

The operator banned long typographic dashes in everything the agents write.
Persona prompts ask for it, but models still slip, so every chat post goes
through :func:`undash` as a deterministic last step.

Rules, applied outside code spans, fenced code blocks and URLs:

* a numeric range keeps its meaning with a hyphen: ``5–10`` -> ``5-10``
* a dash opening a line (a list marker) becomes a hyphen
* a dash closing a line becomes a colon
* an unspaced en dash between words (``Mon–Fri``) becomes a hyphen
* any other em or en dash becomes a comma

Plain hyphens are never touched. Stdlib only.

Markdown tables: the Talk phone apps print a table as raw pipes, so
:func:`untable` turns each table into a short list first (a two column table
becomes ``- **left**: right``; a wider one becomes a bold first cell with one
``Header: value`` line per other column). Set ``NEXTCLOUD_TALK_TABLES=keep``
to post tables unchanged. :func:`tidy` applies both steps.
"""

from __future__ import annotations

import os
import re

DASHES = "–—―"  # en dash, em dash, horizontal bar
_D = f"[{DASHES}]"

# Segments left exactly as written: fenced code, inline code, URLs.
_PROTECTED = re.compile(r"```.*?(?:```|\Z)|`[^`\n]*`|https?://\S+", re.S)

_RANGE = re.compile(rf"(\d)\s*{_D}\s*(\d)")
_LINE_START = re.compile(rf"(?m)^([ \t]*){_D}[ \t]*")
_LINE_END = re.compile(rf"(?m)[ \t]*{_D}[ \t]*$")
_EN_COMPOUND = re.compile(r"(?<=\w)–(?=\w)")
_ANY = re.compile(rf"[ \t]*{_D}+[ \t]*")
_COMMA_TIDY = re.compile(r",\s*([,.;:!?])")


def _fix(text: str) -> str:
    text = _RANGE.sub(r"\1-\2", text)
    text = _LINE_START.sub(r"\1- ", text)
    text = _LINE_END.sub(":", text)
    text = _EN_COMPOUND.sub("-", text)
    text = _ANY.sub(", ", text)
    return _COMMA_TIDY.sub(r"\1", text)


def undash(text: str) -> str:
    """Return *text* with em/en dash punctuation rewritten (see module doc)."""
    if not text or not any(ch in text for ch in DASHES):
        return text
    kept: list = []

    def _hold(m: "re.Match") -> str:
        kept.append(m.group(0))
        return f"\x00{len(kept) - 1}\x00"

    held = _PROTECTED.sub(_hold, text)
    fixed = _fix(held)
    return re.sub(r"\x00(\d+)\x00", lambda m: kept[int(m.group(1))], fixed)


def undash_body(body):
    """Apply :func:`undash` to the human-readable fields of a Talk request body."""
    if not isinstance(body, dict):
        return body
    fixed = dict(body)
    for key in ("message", "threadTitle", "title", "question"):
        if isinstance(fixed.get(key), str):
            fixed[key] = tidy(fixed[key]) if key == "message" else undash(fixed[key])
    if isinstance(fixed.get("options"), list):
        fixed["options"] = [undash(o) if isinstance(o, str) else o for o in fixed["options"]]
    return fixed


# --------------------------------------------------------------- tables

_FENCE = re.compile(r"```.*?(?:```|\Z)", re.S)
_SEP_ROW = re.compile(r"^\s*\|?\s*:?-+:?\s*(\|\s*:?-+:?\s*)*\|?\s*$")


def _cells(line: str) -> list:
    line = line.strip()
    if line.startswith("|"):
        line = line[1:]
    if line.endswith("|") and not line.endswith("\\|"):
        line = line[:-1]
    out, cur = [], ""
    for m in re.finditer(r"`[^`]*`|\\\||\||[^`|\\]+|.", line):
        tok = m.group(0)
        if tok == "|":
            out.append(cur)
            cur = ""
        else:
            cur += "|" if tok == "\\|" else tok
    out.append(cur)
    return [c.strip() for c in out]


def _render(header: list, rows: list) -> list:
    lines = []
    for row in rows:
        row = row + [""] * (len(header) - len(row))
        first = row[0] or "(blank)"
        rest = [(h, v) for h, v in zip(header[1:], row[1:]) if v]
        if len(header) <= 2:
            lines.append(f"- **{first}**: {rest[0][1]}" if rest else f"- **{first}**")
        else:
            lines.append(f"**{first}**")
            lines.extend(f"  {h}: {v}" if h else f"  {v}" for h, v in rest)
    return lines


def _untable_plain(text: str) -> str:
    src = text.split("\n")
    out, i = [], 0
    while i < len(src):
        line = src[i]
        if ("|" in line and i + 1 < len(src) and _SEP_ROW.match(src[i + 1])
                and "-" in src[i + 1]):
            header = _cells(line)
            j, rows = i + 2, []
            while j < len(src) and "|" in src[j] and src[j].strip():
                rows.append(_cells(src[j]))
                j += 1
            if len(header) >= 2 and rows:
                out.extend(_render(header, rows))
                i = j
                continue
        out.append(line)
        i += 1
    return "\n".join(out)


def untable(text: str) -> str:
    """Return *text* with markdown tables rewritten as lists (see module doc)."""
    if not text or "|" not in text or os.environ.get("NEXTCLOUD_TALK_TABLES", "").lower() == "keep":
        return text
    parts, last = [], 0
    for m in _FENCE.finditer(text):
        parts.append(_untable_plain(text[last:m.start()]))
        parts.append(m.group(0))
        last = m.end()
    parts.append(_untable_plain(text[last:]))
    return "".join(parts)


def tidy(text: str) -> str:
    """Phone-safe outbound text: tables as lists, then no long dashes."""
    return undash(untable(text))
