# hermes-nextcloud-talk

Two independent Nextcloud Talk connectors for [Hermes Agent](https://github.com/NousResearch/hermes-agent),
packaged as one plugin that registers a single `nctalk` platform:


> **Upgrading from 1.2.x:** the platform id is now `nctalk` (was
> `nextcloud_talk`). Rename that key in `config.yaml` and restart. See
> CHANGELOG 1.3.0.

- **`bot/`** -- a webhook bot, using Nextcloud Talk's own
  [Bot API](https://nextcloud-talk.readthedocs.io/en/latest/bots/)
  (HMAC-SHA256 signed requests both directions). No Nextcloud user account
  for the agent; a moderator enables the bot per room with
  `occ talk:bot:setup`.
- **`user/`** -- a genuine Nextcloud user account (an app password, not a
  webhook), long-polling the Chat REST API the way a human client would.
  Supports DMs, federation invitations (allowlisted), and every room the
  account is a member of.

They are not merged into one adapter class -- a webhook receiver and a
long-polling REST client are different I/O models, and forcing a shared
base class across them would make both harder to read and riskier to
change independently. What they share (room modes, chunking, the
working-reaction UX, the allowlist env vars) is registered once at the
repo root; see [`__init__.py`](./__init__.py) for how mode selection works
and why.

Both modes support three per-room behaviors:

| Mode | Behavior |
|---|---|
| `mention` (default) | Reply only when the agent is @-mentioned (rich mention parameter, or plain-text `@name`/`@"Display Name"`). |
| `watch` | Buffer every message; a periodic background task decides whether anything is worth saying (reply with the literal token `NO_REPLY` to say nothing -- the adapter intercepts and discards it). |
| `free` | Reply to every message (a dedicated room for the agent). |

In user mode, rooms not listed in `NEXTCLOUD_TALK_ROOM_MODES` that are one-to-one
conversations (Talk room type 1 or 5), or group conversations whose only participants
are the agent and exactly one user from `NEXTCLOUD_TALK_ALLOWED_USERS`, run in
`NEXTCLOUD_TALK_DM_MODE` (default `free`): there is no third party to be quiet in front
of. A group room's participants are re-read every 5 minutes and whenever someone joins
or leaves, so a third participant drops it back to `mention`. Set
`NEXTCLOUD_TALK_DM_MODE=mention` to require an @mention there too. A room listed in
`NEXTCLOUD_TALK_ROOM_MODES` always uses its listed mode.

## Which mode do I want?

- **Bot mode** if you want a clean, no-personal-account integration that
  a moderator opts specific rooms into, and you are comfortable running an
  inbound HTTP listener (even a private one) for the webhook.
- **User mode** if you want the agent to behave like any other Nextcloud
  user -- joinable to rooms by invite, usable in DMs, reachable across
  federated servers -- without exposing any inbound port at all (it only
  makes outbound long-poll requests).

You can run only one mode per Hermes instance (both register the same
platform name). Set whichever credential applies and the plugin picks the
matching mode automatically (see "Mode selection" below).

## Setup

### Bot mode

1. On the Nextcloud server, install the bot:
   ```
   occ talk:bot:install "Hermes" "<64+ char secret>" "https://your-hermes-host:18797/webhooks/nextcloud-talk" --feature webhook --feature response
   ```
   `--feature webhook` lets Talk call the URL for every message;
   `--feature response` lets the bot post replies. Generate the secret
   with something like `openssl rand -hex 32`.
2. Enable the bot in specific rooms (it is not enabled everywhere by
   default):
   ```
   occ talk:bot:setup <bot-id> <room-token>
   ```
3. On the Hermes side, set:
   ```
   NEXTCLOUD_TALK_SECRET=<the same 64+ char secret>
   NEXTCLOUD_TALK_BASE_URL=https://your-nextcloud-instance.example.com
   ```
   and optionally `NEXTCLOUD_TALK_HOST` / `NEXTCLOUD_TALK_PORT` /
   `NEXTCLOUD_TALK_PATH` to match the URL you registered above (defaults:
   `127.0.0.1:18797/webhooks/nextcloud-talk` -- change the host to a
   private interface your Nextcloud server can actually reach; never bind
   a public interface to this).

See [`bot/plugin.yaml`](./bot/plugin.yaml) for every optional env var
(per-room modes, mention trigger words, allowlisting, reaction emoji).

### Inbound webhook hardening (bot mode)

Every inbound webhook is HMAC-verified before anything else runs. Two
further admission checks sit ahead of payload parsing: the request body
is capped at an explicit 1 MiB (`MAX_WEBHOOK_BODY_BYTES`), and the
`X-Nextcloud-Talk-Random` nonce on a signed request is tracked in a
bounded, TTL cache so an exact replay of a previously-accepted request is
rejected (`409`) outright, independent of (and ahead of) the existing
message-id dedup that already catches retried deliveries. Both checks
were ported, in concept, from
[Cliff3c/hermes-nextcloud-talk](https://github.com/Cliff3c/hermes-nextcloud-talk)
(MIT) -- see [THIRD_PARTY.md](./THIRD_PARTY.md).

### User mode

1. Create a dedicated Nextcloud account for the agent (do not reuse a
   human account).
2. Generate an app password for it: **Settings -> Security -> Devices &
   sessions -> Create new app password**. Use the app password, never the
   account's real login password.
3. On the Hermes side, set:
   ```
   NEXTCLOUD_TALK_SERVER_URL=https://your-nextcloud-instance.example.com
   NEXTCLOUD_TALK_APP_PASSWORD=<the app password>
   ```
4. Invite the account to rooms normally, or set up server-to-server
   federation and invite it from a remote instance (see "Federation"
   below).

See [`user/plugin.yaml`](./user/plugin.yaml) for every optional env var
(room modes, federation inviter allowlist, discovery interval, reaction
emoji, home channel for cron delivery).

### Multi-agent rooms (user mode)

Running more than one agent connected to the same Nextcloud server (each
its own user-mode account) needs two extra things so a `free` room doesn't
turn into an infinite agent-to-agent reply loop:

- `NEXTCLOUD_TALK_AGENT_USERS` -- comma-separated Nextcloud user ids of the
  *other* agents sharing the server (e.g. `ops-bot,research-bot`). A message from one
  of these (or from any Talk webhook bot, detected by `actorType`) still
  needs an explicit `@mention` of this account even in a `free` room --
  "free" only means free for humans.
- `NEXTCLOUD_TALK_AGENT_CHAIN_LIMIT` (default `6`) -- once an addressed
  agent-to-agent exchange runs this many messages in a row with no human
  message in between, this account stops answering that agent in that
  room until a human speaks again. A bare id in `NEXTCLOUD_TALK_AGENT_USERS`
  matches both the local user and the same id on any federated instance
  (`ops-bot@remote-instance`); a qualified entry matches only that exact id.

### Threads (Talk 22+)

Talk 22 added threads: a titled side conversation rooted at one message of a
group room (the thread id is the root message id). When the server's
capabilities list `threads` (read once at connect), user mode:

- gives each thread its own Hermes session (the session key gets the thread
  id), its own context backlog and its own agent-chain counter
  (`NEXTCLOUD_TALK_AGENT_CHAIN_LIMIT` counts per room *and* thread). The main
  timeline backlog never holds thread replies, and a thread's first turn is
  told the thread's title and root message.
- answers in the thread it was addressed in: every chunk of a long reply and
  every file share (`talkMetaData.threadId`) carries the thread id, not just
  the first chunk's `replyTo`. Cron and `send_message` deliveries honour a
  thread id too; `NEXTCLOUD_TALK_HOME_CHANNEL_THREAD_ID` points cron delivery
  into a thread of the home channel.
- keeps one long-poll per room and routes by each message's `threadId`, since
  Talk does not proxy a thread-filtered chat read for federated rooms.
- adds four tools (toolset `nctalk-threads`, on whenever user-mode credentials
  are set, `NEXTCLOUD_TALK_THREAD_TOOLS=off` hides them):
  `talk_thread_start(title, message, room?)` opens a thread and returns its
  `thread_id`; `talk_thread_reply(message, thread_id?, reopen?, room?)` posts
  into one (default: the thread the current turn came from);
  `talk_thread_list(limit?, include_closed?, room?)` lists the room's open
  threads, newest activity first; `talk_thread_close(thread_id?, summary?, room?)`
  puts a finished thread away. `room` follows the same rule as the call tools:
  empty means this chat. Thread support is checked per room on the server that
  HOSTS it (`GET /room/{token}/capabilities`, proxied for federated rooms), so a
  federated room on a host without threads gets a clear error instead of a
  plain message posing as a thread.

**Direct messages and federated rooms (1.10.0).** Threads work the same in a
one-to-one conversation as in a group room: each thread is its own session and
the main DM timeline stays the lobby. The prompt asks the agent to give each
separate matter in a DM its own thread. Federated rooms (a room hosted on
another Nextcloud) carry `isThread`/`threadTitle` through the proxy, and
creating, replying, renaming and follow levels are all proxied, so the same
code path serves them. Talk itself cannot federate a one-to-one conversation:
a "DM" with an agent on another server is a two or three person group room.

**Closing threads.** Talk has no close or archive for a thread, and each
person's follow level ("unfollow" in the clients) is private, so the agent
cannot see that someone unfollowed. `talk_thread_close` is the agent-side
close: it sets the agent's own follow level to "never", prefixes the title with
a check mark (the thread's starter or a moderator may rename; in someone else's
group-room thread the rename is skipped) and makes `talk_thread_reply` refuse
unprompted posts into it unless `reopen=true`. When a person writes in a closed
thread it reopens by itself (the agent's level goes back to default and the
turn says so).

**Resume after a quiet spell** (`NEXTCLOUD_TALK_THREAD_RESUME_HOURS`, default
6, `0` off). When a thread has been quiet that long, the next turn in it gets
the thread's recent lines (up to 12, allowed senders and the agent only) and a
request to open with a one-line recap, so a reset Hermes session still knows
where the matter stood. Local rooms read the thread with `threadId`; Talk does
not proxy that filter for federated rooms (spreed `ChatController` FIXME), so
there the last 100 room messages are read and filtered client-side. The first
message a fresh process sees in a thread costs one history read; after that
the gap is tracked in memory.

**Thread ownership** (`NEXTCLOUD_TALK_THREAD_OWNERSHIP`, default `on`). In a
room shared by several agents, a thread started by another agent listed in
`NEXTCLOUD_TALK_AGENT_USERS` is that agent's work: this account answers there
only when @mentioned, whatever the room mode (also in `free` and `watch`
rooms). A thread this account started answers its people without an @mention
(other agents still have to address it). Threads started by humans follow the
room mode. The thread's creator comes from its root message, or from
`GET /chat/{token}/threads/{id}` when the root predates the connection; if that
read fails the thread follows the room mode. `off` makes every thread follow
the room mode.

The Talk channel prompt tells the model the convention: people post at top
level, task work happens in a thread, reply where you were addressed, post a
one-line result at top level when a task finishes, and stay out of other
agents' threads unless @mentioned.

Bot mode reads `object.threadId` from the webhook (Talk 22.0.10+) into the
session and posts replies with `threadId`; bots cannot open threads here.

Without the `threads` capability (or if the capability read fails) nothing of
this applies and the adapter behaves exactly like 1.8.4.

### Free rooms: Chief of Staff (user mode)

In a `free` group room (listed in `NEXTCLOUD_TALK_ROOM_MODES`; DMs and
private two-person rooms are not affected) the agent acts as the room's
Chief of Staff:

- a human message that @mentions this account expects a reply;
- a human message that @mentions someone else and not this account (a user,
  guest, federated user or group, or a plain `@<agent>` from
  `NEXTCLOUD_TALK_AGENT_USERS`) is kept as context only, no turn;
- any other human message reaches the agent with `reply_expected=False`: it
  answers questions meant for the team and may stay silent on chatter between
  people, without Hermes posting its silence-marker warning.

### Active listening (user mode)

A message this account does not answer -- an un-mentioned message in a
`mention`-mode room, or another agent not addressing it -- is not simply
dropped. It is kept per room (`NEXTCLOUD_TALK_CONTEXT_MESSAGES`, default
`40`; `0` disables this) and handed over as context with the next message
this account does answer, so a reply in `mention` mode is informed by what
the room has been saying, not just by the one line that named it. The
backlog is consumed on use, not replayed on every subsequent reply.

### Room rules from the room description (user mode)

In any group room (not a DM), the room's Nextcloud Talk conversation
*description* is read on every discovery pass and, if non-empty, prepended
to the next message handed to the agent as "room rules". Editing the
description in Talk itself -- no config change, no restart -- changes the
agent's instructions for that room on its very next reply.

### Long-poll timeout behind a reverse proxy (user mode)

Each room's long-poll request asks Nextcloud Talk to hold it open for up
to `NEXTCLOUD_TALK_LONG_POLL_TIMEOUT` seconds (default `25`; Talk's own
ceiling is `30`) before returning "nothing new". If Hermes sits behind a
reverse proxy, load balancer, or PaaS ingress with its own read timeout
shorter than Talk's hold time, an idle poll that doesn't get a response
before *that* timeout comes back as a connection error instead of a plain
304 -- which this adapter already retries, but needlessly. Set
`NEXTCLOUD_TALK_LONG_POLL_TIMEOUT` a few seconds under your front door's
timeout (or raise it back to `30` if nothing sits in front of Hermes).

### Team roles from Nextcloud profiles (user mode)

Define each agent's job on the team in Nextcloud, not on the agent's host. The
agent reads its own profile fields (**Role**, **Headline**, **Organisation**,
**Pronouns**, **About**) and those of its teammates listed in
`NEXTCLOUD_TALK_AGENT_USERS` on the same server, refreshes them every five
minutes, and treats them as a system-level "your role on this team" note
alongside its own identity (Hermes' SOUL). Edit them in the agent account's
Settings > Personal info, or as an admin:

```bash
occ user:profile jarvis role "Infra, coordination boards and terminal work"
occ user:profile jarvis biography "Owns the chi fleet. Hands memory and calendar to Lumina."
```

Teammates on another (federated) server are not read, since their profile
lives there. Empty fields add nothing; set `NEXTCLOUD_TALK_PROFILE_ROLE=off` to
disable.

### Files, images and voice (user mode)

Files shared in a room reach the agent: images go to vision, voice messages to
speech-to-text, small text files are inlined, other documents, audio and video
are attached. Only messages the agent answers trigger a download; files in a
room it is just listening to show up as `[file: name (type)]` in its context.
The cap is 20 MB per file.

The agent can send files too. They are uploaded to `Talk/<unique folder>/` in
the agent account's own Files and shared into the room, so give the account
some storage quota. Voice replies arrive as Talk voice messages.

Talk cannot share files into a federated conversation. Set
`NEXTCLOUD_TALK_FEDERATED_MEDIA=link` to post a read-only public link that
expires after 7 days instead; it is off by default because anyone holding the
link can read the file.

### Editing (user mode)

The agent edits its own messages, which is what lets Hermes show one
tool-progress message that updates as it works (set
`display.platforms.nctalk.cleanup_progress: true` to delete it once the answer
is posted). When someone edits or deletes a message the agent is holding as
context, its context follows.

### Nextcloud tools (optional)

The plugin puts the agent *in* Talk. To let it also read Nextcloud files,
calendars, contacts, Deck boards and more, add a Nextcloud MCP server to
Hermes. See [docs/nextcloud-mcp.md](docs/nextcloud-mcp.md) for a tested config
that reuses this plugin's credentials and a read-only tool allowlist to start
with.

### Joining calls (optional, opt-in)

With `NEXTCLOUD_TALK_CALL_TOOLS=on` the agent gets three tools (toolset
`nctalk-calls`) so "@agent join call" works without the agent improvising a
process in the terminal:

- `call_join(room?, other_room=false, ring=false, greeting?)` starts the
  `skcall-join@<room>.service` systemd --user unit and returns at once with
  `joining` or `already in call`. It is never a background process the agent
  watches or restarts.
- `call_leave(room?)` stops that unit (a clean leave).
- `call_status(room?)` reports in call / not in call / the last failure.

`room` defaults to the Talk chat the request came from and must match
`^[a-z0-9]{4,32}$`. Invoked from a Talk chat, the tools act on that chat: a
different explicit `room` is ignored (the result's `room_note` says so) unless
the call also sets `other_room: true`, because models tend to fill `room` with
whatever token they can see, such as the home channel. Outside Talk, `room` is
required. `ring` and `greeting` are handed to the unit through
`$XDG_RUNTIME_DIR/skcall/join-<room>.env`. The unit and the call itself come
from [skcall](https://github.com/smilinTux/skcall) (`systemd/skcall-join@.service`),
which also guarantees one instance per agent and room and posts a summary to
the room's chat when the call ends. Without the unit installed, `call_join`
says so and does nothing. Tool descriptions tell the model to use these tools
for every join/leave request and never to launch skcall through the terminal.

**Federated rooms.** A federated room is a chat copy of a room hosted on
another Nextcloud; the call lives on the host. `NEXTCLOUD_TALK_CALL_ROOM_MAP`
(`<chat room>:<estate>:<call room>,...`) maps such a room to the host's call:
`m4jyfpxq:nam:og63pxfh` makes the tools drive `skcall-join-nam@og63pxfh.service`
(the agent's native nam login, from skcall's `systemd/skcall-join-nam@.service`)
when asked from m4jyfpxq, with options in `join-og63pxfh.env`.

**Always visible, even with tool_search on.** Hermes progressive disclosure
(`tools.tool_search`) hides every plugin tool behind `tool_search` /
`tool_describe` / `tool_call` and shows only its bare name in a long listing.
Hidden that way, the model ignored the call tools and started skcall from the
terminal. Hermes has no per-tool or config switch to keep a plugin tool eager
(`tools.tool_search.defer` can only add tools to the deferred set), so the
plugin adds `nctalk-calls` to the toolsets tool_search never defers
(`_DIRECT_SURFACE_TOOLSETS` in `tools/tool_search.py`) when it registers. The
three schemas cost about 560 tokens per turn in every session where the
toolset is enabled; disable it per platform with `hermes tools` if you only
want calls from Talk. If a future Hermes moves that internal, the plugin logs
a warning and the tools still work, deferred. With the tools on, both
connectors also add one line to the Talk channel prompt: use `call_join` /
`call_leave` / `call_status` for calls, never start skcall from the terminal.

### Federation (user mode)

Nextcloud Talk supports inviting a user on another federated server into
a room. This adapter auto-accepts a pending federation invitation **only**
from an inviter on `NEXTCLOUD_TALK_ALLOWED_INVITERS` (comma-separated;
matches either the bare local username or the full `user@remote-instance`
cloud id). Everyone else's invitation is left pending -- never
auto-declined, never silently accepted -- so a human still has to look at
it. Once accepted, the federated room is discovered and polled exactly
like a local one; `_extract_actor()` keeps a federated sender's fully
qualified cloud id so downstream memory/attribution can tell local
senders from federated ones.

## Mode selection

The root `register(ctx)` picks a mode once, by which credential is set:

- `NEXTCLOUD_TALK_APP_PASSWORD` set (and no secret) -> user mode.
- `NEXTCLOUD_TALK_SECRET` set (and no app password) -> bot mode.
- Both set -> `NEXTCLOUD_TALK_MODE=bot` or `NEXTCLOUD_TALK_MODE=user`
  breaks the tie (bot wins if that is also unset).
- Neither set -> the platform reports not-configured; Hermes does not try
  to connect it.

## Installing

Combined (both modes available, pick one via credentials as above):

```
mkdir -p ~/.hermes/plugins/nctalk
cp -r __init__.py plugin.yaml tools.py bot user ~/.hermes/plugins/nctalk/
```

Bot mode only:

```
mkdir -p ~/.hermes/plugins/nctalk_bot
cp bot/adapter.py bot/core.py bot/plugin.yaml ~/.hermes/plugins/nctalk_bot/
cp bot/__init__.py ~/.hermes/plugins/nctalk_bot/__init__.py
```

User mode only:

```
mkdir -p ~/.hermes/plugins/nctalk_user
cp user/adapter.py user/plugin.yaml ~/.hermes/plugins/nctalk_user/
cp user/__init__.py ~/.hermes/plugins/nctalk_user/__init__.py
```

## Repo layout

- `__init__.py`, `plugin.yaml` -- combined plugin entry point (mode
  dispatch) and manifest.
- `bot/` -- webhook bot connector. `core.py` is pure logic (HMAC
  sign/verify, mention detection, payload parsing, dedupe, per-room
  buffer, watch-mode rate limiting), zero dependency on the Hermes
  gateway, fully unit-tested on its own. `adapter.py` is the
  `BasePlatformAdapter` subclass.
- `user/` -- real-user connector. `adapter.py` has both the pure helpers
  and the adapter class (it is a single, much simpler transport than the
  bot's webhook/HMAC machinery, so it was not worth splitting into a
  separate core module).
- `tools.py` -- the opt-in call tools (`call_join`, `call_leave`,
  `call_status`) and the thread tools (`talk_thread_start`,
  `talk_thread_reply`); stdlib at import time, tested in `tests/call_tools/`.
- `tests/bot/`, `tests/user_mode/` -- each mode's tests (the user-mode test
  directory is not named `tests/user` to avoid shadowing the `user/`
  package on `sys.path`), against
  `tests/stubs/gateway/` (a minimal, dependency-free stand-in for the
  pieces of Hermes's `gateway` package these adapters use -- no real
  Hermes checkout required to run either suite).

## Running the tests

```
pip install aiohttp httpx pytest pytest-asyncio
python -m pytest tests/ -q
```

## Security model

See [SECURITY.md](./SECURITY.md).

## License

MIT, see [LICENSE](./LICENSE). Third-party credit for ported ideas: see
[THIRD_PARTY.md](./THIRD_PARTY.md).
