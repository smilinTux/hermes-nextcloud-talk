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
"""

from __future__ import annotations

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
            fixed[key] = undash(fixed[key])
    if isinstance(fixed.get("options"), list):
        fixed["options"] = [undash(o) if isinstance(o, str) else o for o in fixed["options"]]
    return fixed
