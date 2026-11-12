"""``hermes nctalk nc-live-setup``: add the optional sk-nc-live MCP server to config.yaml.

sk-nc-live (https://github.com/smilinTux/sk-nc-live) lets the agent edit
Nextcloud Text markdown and Collabora office documents live, while people
have them open. This command writes one ``mcp_servers.nc-live`` entry that
reuses this plugin's credentials BY REFERENCE: the entry holds
``${NEXTCLOUD_TALK_SERVER_URL}``-style refs that Hermes expands when it
starts the server, so no secret is ever copied into config.yaml.

It is idempotent and conservative:

- an existing ``nc-live`` entry is never touched (the command says so and exits 0);
- the block is shown first and written only after a yes (``--yes`` skips the
  prompt, ``--dry-run`` only shows it);
- the edit is a text insertion, so the comments and layout of config.yaml are
  kept, a timestamped backup is written next to it, and the result is
  re-parsed and compared before it replaces the file. If anything does not
  match, nothing is written and the block is printed for pasting by hand.
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import sys
import tempfile
import time
from typing import Any, Callable, Dict, List, Optional, Tuple

SERVER_NAME = "nc-live"
DEFAULT_COMMAND = "sk-nc-live-mcp"
DEFAULT_PASSWORD_ENV = "NEXTCLOUD_TALK_APP_PASSWORD"
_ENV_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_HEADER_RE = re.compile(r"^mcp_servers:[ \t]*(\{\}|null|~)?[ \t]*(#.*)?$")


class SetupError(Exception):
    pass


def _yaml():
    try:
        import yaml
    except ImportError as e:  # pragma: no cover - Hermes depends on PyYAML
        raise SetupError("PyYAML is needed to check config.yaml") from e
    return yaml


def entry_lines(command: str = DEFAULT_COMMAND, password_env: str = DEFAULT_PASSWORD_ENV,
                indent: str = "  ") -> List[str]:
    """The ``nc-live:`` mapping, indented as a child of ``mcp_servers``."""
    if not _ENV_NAME_RE.match(password_env):
        # Never echo the value: a pasted secret here must not land in a terminal log.
        raise SetupError("--password-env must be an environment variable name (letters, digits, _)")
    if not command or "\n" in command:
        raise SetupError("--command must be a single command or path")
    i1, i2, i3 = indent, indent * 2, indent * 3
    quoted = '"' + command.replace("\\", "\\\\").replace('"', '\\"') + '"'
    return [
        f"{i1}{SERVER_NAME}:",
        f"{i2}# sk-nc-live: live editing of Nextcloud Text and office docs. Credentials are",
        f"{i2}# references to the Talk plugin's env, expanded by Hermes; no secret is stored here.",
        f"{i2}command: {quoted}",
        f"{i2}args: []",
        f"{i2}env:",
        f'{i3}NEXTCLOUD_HOST: "${{NEXTCLOUD_TALK_SERVER_URL}}"',
        f'{i3}NEXTCLOUD_USERNAME: "${{NEXTCLOUD_TALK_USERNAME}}"',
        f'{i3}NEXTCLOUD_PASSWORD: "${{{password_env}}}"',
        f"{i2}enabled: true",
    ]


def expected_entry(command: str, password_env: str) -> Dict[str, Any]:
    return {
        "command": command,
        "args": [],
        "env": {
            "NEXTCLOUD_HOST": "${NEXTCLOUD_TALK_SERVER_URL}",
            "NEXTCLOUD_USERNAME": "${NEXTCLOUD_TALK_USERNAME}",
            "NEXTCLOUD_PASSWORD": "${" + password_env + "}",
        },
        "enabled": True,
    }


def _child_indent(lines: List[str], start: int) -> str:
    for line in lines[start:]:
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if line[:1] in (" ", "\t"):
            return line[: len(line) - len(line.lstrip())]
        break
    return "  "


def plan(text: str, command: str = DEFAULT_COMMAND,
         password_env: str = DEFAULT_PASSWORD_ENV) -> Tuple[str, Optional[str]]:
    """Return ``("exists", None)`` or ``("add", new_text)``. Raises SetupError if unsafe."""
    yaml = _yaml()
    try:
        before = yaml.safe_load(text) if text.strip() else {}
    except yaml.YAMLError as e:
        raise SetupError(f"config.yaml does not parse, not touching it: {e}") from e
    if before is None:
        before = {}
    if not isinstance(before, dict):
        raise SetupError("config.yaml is not a mapping, not touching it")
    servers = before.get("mcp_servers")
    if isinstance(servers, dict) and SERVER_NAME in servers:
        return "exists", None
    if servers is not None and not isinstance(servers, dict):
        raise SetupError("mcp_servers is not a mapping, add the block by hand")

    lines = text.splitlines()
    header = next((n for n, line in enumerate(lines) if _HEADER_RE.match(line)), None)
    if "mcp_servers" not in before:
        block = ["mcp_servers:"] + entry_lines(command, password_env)
        new_lines = lines + ([""] if lines and lines[-1].strip() else []) + block
    elif header is None:
        raise SetupError("mcp_servers is written in a style this command does not edit "
                         "(flow style or an anchor), add the block by hand")
    else:
        indent = _child_indent(lines, header + 1)
        end = header + 1
        while end < len(lines):
            line = lines[end]
            if line.strip() and not line.lstrip().startswith("#") and line[:1] not in (" ", "\t"):
                break  # the next top-level key
            end += 1
        # Blank lines and column-0 comments right before the next key belong to that key.
        while end > header + 1 and (not lines[end - 1].strip() or lines[end - 1].startswith("#")):
            end -= 1
        head = lines[:header] + ["mcp_servers:"] if _HEADER_RE.match(lines[header]).group(1) else lines[:header + 1]
        new_lines = head + lines[header + 1:end] + entry_lines(command, password_env, indent) + lines[end:]
    new_text = "\n".join(new_lines) + "\n"

    after = yaml.safe_load(new_text)
    want = dict(before)
    want["mcp_servers"] = {**(servers or {}), SERVER_NAME: expected_entry(command, password_env)}
    if after != want:
        raise SetupError("the edited config.yaml did not parse back as expected, not writing it")
    return "add", new_text


def write_config(path: str, new_text: str) -> str:
    """Back up, then atomically replace *path*. Returns the backup path ("" if new file)."""
    backup = ""
    if os.path.exists(path):
        backup = f"{path}.bak-nc-live-{time.strftime('%Y%m%dT%H%M%S')}"
        shutil.copy2(path, backup)
    directory = os.path.dirname(os.path.abspath(path))
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=".config.yaml.")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(new_text)
        if backup:
            shutil.copymode(backup, tmp)
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise
    return backup


def default_config_path() -> str:
    try:
        from .user import threadsmode
    except ImportError:  # pragma: no cover - loaded as a loose module
        from user import threadsmode  # type: ignore
    return os.path.join(threadsmode.hermes_home(), "config.yaml")


def run(config_path: str, *, command: str = DEFAULT_COMMAND, password_env: str = DEFAULT_PASSWORD_ENV,
        assume_yes: bool = False, dry_run: bool = False,
        ask: Callable[[str], str] = input, out=None) -> int:
    out = out or sys.stdout

    def say(msg: str = "") -> None:
        print(msg, file=out)

    try:
        entry_lines(command, password_env)
    except SetupError as e:
        say(f"nc-live setup: {e}")
        return 2
    text = ""
    if os.path.exists(config_path):
        with open(config_path, encoding="utf-8") as f:
            text = f.read()
    try:
        status, new_text = plan(text, command, password_env)
    except SetupError as e:
        say(f"nc-live setup: {e}")
        say("Block to add under mcp_servers:")
        say("\n".join(entry_lines(command, password_env)))
        return 1
    if status == "exists":
        say(f"nc-live setup: mcp_servers.{SERVER_NAME} already exists in {config_path}; left as it is.")
        return 0
    say(f"nc-live setup: this block will be added under mcp_servers in {config_path}:\n")
    say("\n".join(entry_lines(command, password_env)))
    say("")
    missing = [n for n in ("NEXTCLOUD_TALK_SERVER_URL", "NEXTCLOUD_TALK_USERNAME", password_env)
               if not os.getenv(n)]
    if missing:
        say("Note: not set in this shell: " + ", ".join(missing)
            + ". Hermes resolves them from the profile's .env when it starts the server.")
    if dry_run:
        say("Dry run: nothing written.")
        return 0
    if not assume_yes:
        try:
            answer = ask("Write it? [y/N] ").strip().lower()
        except EOFError:
            answer = ""
        if answer not in ("y", "yes"):
            say("Nothing written.")
            return 0
    backup = write_config(config_path, new_text or "")
    if backup:
        say(f"Written. Backup: {backup}")
    else:
        say("Written.")
    say(f"Install sk-nc-live so that {command!r} is on the gateway's PATH, then restart the gateway.")
    return 0


def _setup_parser(parser: argparse.ArgumentParser) -> None:
    sub = parser.add_subparsers(dest="nctalk_command")
    p = sub.add_parser("nc-live-setup", help="add the optional sk-nc-live MCP server to config.yaml")
    p.add_argument("--config", default=None, help="config.yaml to edit (default: this profile's)")
    p.add_argument("--command", default=DEFAULT_COMMAND,
                   help=f"how to start sk-nc-live (default {DEFAULT_COMMAND}, or a wrapper path)")
    p.add_argument("--password-env", default=DEFAULT_PASSWORD_ENV,
                   help="env var holding the app password (default NEXTCLOUD_TALK_APP_PASSWORD; "
                        "a separate app password is recommended)")
    p.add_argument("--yes", "-y", action="store_true", help="write without asking")
    p.add_argument("--dry-run", action="store_true", help="only show what would be written")


def _handle(args: argparse.Namespace) -> int:
    if getattr(args, "nctalk_command", None) != "nc-live-setup":
        print("usage: hermes nctalk nc-live-setup [--dry-run] [--yes] [--command CMD] "
              "[--password-env NAME] [--config PATH]")
        return 2
    return run(args.config or default_config_path(), command=args.command,
               password_env=args.password_env, assume_yes=args.yes, dry_run=args.dry_run)


def register_cli(ctx) -> None:
    if not hasattr(ctx, "register_cli_command"):
        return  # older Hermes: the README shows the block to add by hand
    ctx.register_cli_command(
        name="nctalk", help="Nextcloud Talk plugin helpers",
        setup_fn=_setup_parser, handler_fn=_handle,
        description="Helpers for the Nextcloud Talk plugin. See: hermes nctalk nc-live-setup --help")
