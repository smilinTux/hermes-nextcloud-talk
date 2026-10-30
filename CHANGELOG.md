# Changelog

All notable changes to this project are documented here. Versioning follows
[Semantic Versioning](https://semver.org/); the version tracked is the
combined plugin's (`plugin.yaml` at the repo root).

## [1.11.1] - 2026-10-30

### Fixed

- After posting a poll the agent restated every option as text and explained
  why it was a reaction poll (seen live on nor and over federation). The tool
  result now tells it the person already sees the poll and to end the turn
  with one short line at most.

## [1.11.0] - 2026-10-30

### Added

- Poll tools (toolset `nctalk-polls`): `talk_poll_create`, `talk_poll_status`,
  `talk_poll_close`. The agent offers its top 2 to 4 choices plus a "something
  else, reply here" line. Where Talk allows it this is a native Talk poll; in a
  one-to-one chat (Talk 24 refuses polls there, `error: room`) and inside a
  thread of a federated room (the federation proxy drops `threadId`, so the
  poll would land in the host's main timeline) it is a numbered message the
  person answers by tapping a keycap reaction the agent already placed.
- The agent wakes in the poll's own thread when it is answered: a vote (after
  `NEXTCLOUD_TALK_POLL_SETTLE_SECONDS`, default 15; the adapter closes the poll
  to read it, because Talk shows the author only the voter count while a poll
  is open), a keycap reaction, or a person's message in that thread, which is
  taken as their own answer and closes the poll. One turn per poll. Open polls
  live in `$HERMES_HOME/nctalk/polls-<account>.json`.
- Answer reactions: a check mark or cross a person puts on one of the agent's
  messages gives it a turn (yes / no to a question, or "done" on a thread's
  last message, which closes the thread). `NEXTCLOUD_TALK_REACTION_TURNS`.
- Server capabilities: the adapter reads the server's capabilities (daily) and,
  for a federated room, the host's Talk features, and tells the model once per
  conversation what the chat supports (threads, polls, scheduled and silent
  messages, reactions) and which apps the agent's account has.
  `NEXTCLOUD_TALK_CAPABILITY_NOTE=off` turns it off.
- `talk_message_schedule` (toolset `nctalk-server`): schedule, list or cancel a
  Talk scheduled message, in the current thread by default. Federated rooms
  refuse it (HTTP 406), so the tool says so instead of trying.
- `nextcloud_ocs_call`: read-only lookups as the agent's account (someone's
  status, a profile, unified search, participants, message context, threads,
  polls, reactions, capabilities) plus adding or removing its own reaction.
  Every other path is refused by an allowlist.
- `silent` on `talk_thread_start` and `talk_thread_reply`.
- A poll shared by a person reads as `[poll: question]` instead of `{object}`.

## [1.10.2] - 2026-10-30

### Fixed

- A closed thread reopened by a person's message kept the check mark on its
  title (only `talk_thread_reply(reopen=true)` removed it). The adapter now
  removes it too, so the title says what the thread's state is.

## [1.10.1] - 2026-10-30

### Fixed

- A person writing in a thread the agent closed moments earlier did not
  reopen it: thread info was cached for 60 seconds, and live the reply came
  13 seconds after the close. A person's message in a thread now always
  re-reads the thread (agents still use the cache).

## [1.10.0] - 2026-10-30

### Added

- Threads in direct messages are a first-class workflow: the channel prompt
  now covers DMs (one thread per separate matter, the main DM timeline is the
  lobby). Routing already worked there (verified live on Talk 24.0.6: each DM
  thread is its own Hermes session, replies land in their thread).
- `talk_thread_list` (open threads in this room, newest activity first) and
  `talk_thread_close` (optional one-line summary, the agent's own follow level
  set to "never", a check mark on the title). `talk_thread_reply` refuses an
  unprompted post into a closed thread unless `reopen=true`.
- A person writing in a closed thread reopens it (follow level back to
  default) and the turn is told so. A person's message re-reads thread info
  known for over a minute, so a close made through the tools is seen.
- Resume after a quiet spell (`NEXTCLOUD_TALK_THREAD_RESUME_HOURS`, default 6,
  0 off): the next turn gets the thread's recent lines and a recap request.
  Federated rooms filter the room history client-side because Talk does not
  proxy a thread-filtered read.

### Changed

- The thread tools check thread support per room on the server hosting it
  (`GET /room/{token}/capabilities`), so a federated room whose host lacks
  threads is refused clearly. Tool text mentions DMs.

## [1.9.2] - 2026-10-27

### Fixed

- Thread detection in user mode. Talk puts `threadId` on every chat message
  (`topmostParentId ?: id`, spreed `Message::toArray`), thread or not, and
  1.9.0 took that field alone as "in a thread". On the live skhubs every
  main-timeline message became its own one-message "thread" session, and a
  follow-up chunk would have posted with the `threadId` of a message that is
  not a thread. Only `isThread` marks a real thread (the root and every reply
  carry it with `threadTitle`; `threadId` is then the root's id). Tests now
  use the payload shape captured from Talk 24.0.6.

## [1.9.1] - 2026-10-27

### Changed

- A `free` group room (listed in `NEXTCLOUD_TALK_ROOM_MODES`, not a DM or a
  private two-person room) now treats the agent as the room's Chief of Staff
  instead of "every line is for me". A human message that does not mention
  this account but @mentions another participant (rich mention of a user,
  guest, federated user or group, or a plain `@<agent>` from
  `NEXTCLOUD_TALK_AGENT_USERS`) is kept as context only, so "@jarvis do X"
  no longer forces Lumina to answer. Any other unaddressed human message is
  delivered with `reply_expected=False`, so the agent can stay quiet on
  human-to-human chatter without the core posting its "model returned only a
  silence marker" warning into the room (seen four times in Operators AI,
  2026-10-24/25). Messages that mention this account still expect a reply;
  DMs, private pair rooms and `mention`/`watch` rooms are unchanged.
- The Talk channel prompt says so: answer unaddressed questions meant for the
  team, stay silent on human-to-human chatter.

## [1.9.0] - 2026-10-27

### Added

- Nextcloud Talk threads (Talk 22+, capability `threads`, present on both
  skhubs running Talk 24.0.6). User mode reads each message's `threadId`
  (a thread root's own id when `isThread`) and passes it to Hermes as the
  session's `thread_id`, so every thread is its own session. The context
  backlog and the agent-chain counter are kept per room and thread: thread
  replies never ride into a main-timeline reply and the reverse, and a
  thread's first turn is told the thread title and root message.
- Replies stay in the thread: every chunk carries `threadId` (only the first
  has `replyTo`, so chunks 2+ used to land in the main timeline), file and
  voice shares put `threadId` in `talkMetaData`, the federated link fallback
  posts into the thread, and the standalone sender (cron, `send_message`)
  honours `thread_id`. `NEXTCLOUD_TALK_HOME_CHANNEL_THREAD_ID` is now seeded
  into the home channel, so cron delivery can post into a thread.
- Thread ownership for multi-agent rooms, `NEXTCLOUD_TALK_THREAD_OWNERSHIP`
  (default `on`): a thread started by another agent from
  `NEXTCLOUD_TALK_AGENT_USERS` only gets an answer when this account is
  @mentioned there, whatever the room mode; a thread this account started
  answers its people without an @mention. The creator comes from the root
  message or, for older threads, `GET /chat/{token}/threads/{id}` (cached).
- `talk_thread_start(title, message, room?)` and
  `talk_thread_reply(message, thread_id?, room?)` tools (toolset
  `nctalk-threads`, on with user-mode credentials,
  `NEXTCLOUD_TALK_THREAD_TOOLS=off` hides them), pinned visible past
  tool_search like the call tools.
- Bot mode parses `object.threadId` from the webhook into the session and
  posts replies with `threadId` on every chunk.

### Changed

- The Talk channel prompt explains threads (top level for requests, task work
  in a thread, reply where addressed, one-line result at top level, stay out
  of other agents' threads unless @mentioned) instead of claiming there are
  no threads.
- Threads are capability gated: if the server does not list `threads`, or the
  capabilities read fails, the adapter behaves exactly like 1.8.4.
- README install line copies `tools.py` too (the call tools need it).

## [1.8.4] - 2026-10-26

### Added

- A group conversation whose only participants are the agent and exactly one
  user from `NEXTCLOUD_TALK_ALLOWED_USERS` now answers without an @mention,
  like a one-to-one room. Talk types a conversation made with just two people
  as a group (type 2), so Chef had to @jarvis in his own chat with Jarvis. The
  participant list is cached, re-read every 5 minutes and at once on a
  join/leave system message; a third participant drops the room back to
  `mention`. Every participant counts (guest, group, federated user), and a
  failed participant read fails closed to `mention`. Federated rooms and rooms listed in `NEXTCLOUD_TALK_ROOM_MODES`
  are not checked.
- `NEXTCLOUD_TALK_DM_MODE` (default `free`) sets the mode for unlisted
  one-to-one rooms and those two-person group rooms; `mention` restores the
  @mention requirement.

### Changed

- A one-to-one room listed in `NEXTCLOUD_TALK_ROOM_MODES` now uses its listed
  mode; before, DMs were always `free` whatever the list said.

## [1.8.3] - 2026-10-26

### Added

- `NEXTCLOUD_TALK_CALL_ROOM_MAP=<chat room>:<estate>:<call room>,...` sends a
  call request from one chat room to a call on another estate. Talk federation
  carries the chat but the agent should join the host's call as its own native
  user there: with `m4jyfpxq:nam:og63pxfh`, "@lumina join call" in nor's
  federated Operators AI starts `skcall-join-nam@og63pxfh.service` (skcall's
  `systemd/skcall-join-nam@.service`), and `call_leave` / `call_status` act on
  that unit. Results add `call_room` and `estate` when a room is mapped;
  `call_status` with no room also lists `skcall-join-<estate>@` units.

## [1.8.2] - 2026-10-26

### Fixed

- The call tools could act on the wrong room. "@jarvis join call" in one Talk
  room started the call in another: the model passed `room` explicitly, filled
  from the only room token it could see in its context (the Talk home channel,
  listed under "Home Channels" in the Hermes session context), and an explicit
  `room` always won. Invoked from a Talk room, `call_join`, `call_leave` and
  `call_status` now act on that room; a different explicit `room` is ignored
  unless the args also set `other_room: true`, and the result carries a
  `room_note` saying which room was used and why.
- Tool descriptions and the Talk channel prompt now say to leave `room` empty
  to mean this chat, and never to fill it from a home channel or memory.
- The "already in call" note was gendered ("She is ..."); it is now
  agent-neutral ("You are already in this call").

## [1.8.1] - 2026-10-25

### Fixed

- The call tools were invisible in practice: Hermes tool_search deferred them
  with every other plugin tool (246 deferred, names-only listing), and on
  "@lumina join call" the agent built its own venv and ran skcall from the
  terminal instead. The plugin now keeps the `nctalk-calls` toolset eager by
  adding it to tool_search's never-defer toolsets at registration (Hermes has
  no config key or per-tool flag for this). Best effort: if the internal moves
  the tools fall back to deferred and a warning is logged.
- With `NEXTCLOUD_TALK_CALL_TOOLS=on`, both connectors append one line to the
  Talk channel prompt: always use `call_join` / `call_leave` / `call_status`
  for calls, never start skcall from the terminal.

## [1.8.0] - 2026-10-25

### Added

- Opt-in call tools, `NEXTCLOUD_TALK_CALL_TOOLS=on`: `call_join`,
  `call_leave` and `call_status` (toolset `nctalk-calls`). They start, stop
  and inspect skcall's `skcall-join@<room>` systemd --user unit, so the agent
  joins a Talk call from chat through one supervised unit per room instead
  of launching skcall from the terminal (which produced duplicate tiles and
  relaunch loops). `call_join` returns immediately with `joining` or
  `already in call`. Room defaults to the current Talk chat; tokens are
  validated. Ring and greeting are passed through a per-room env file.

## [1.7.0] - 2026-10-25

### Added

- Team roles from Nextcloud profiles. The agent reads its own profile (Role,
  Headline, Organisation, Pronouns, About) and the profiles of local accounts
  in `NEXTCLOUD_TALK_AGENT_USERS`, every five minutes, and passes them to the
  core as `channel_prompt` (a system-level note, not part of the transcript).
  Changing an agent's job on the team is now a profile edit, by the agent's
  account or an admin (`occ user:profile <uid> role "..."`), with no change on
  the agent host. Empty profiles add nothing; `NEXTCLOUD_TALK_PROFILE_ROLE=off`
  disables it.

## [1.6.0] - 2026-10-25

### Added

- Shared files reach the agent. `{file}` placeholders become
  `[file: name (type)]` in text and in the listening backlog. For a message the
  agent answers, each file is downloaded over WebDAV from the account's own
  Files and attached through Hermes' media cache: images as PHOTO (vision),
  voice messages as VOICE (speech-to-text), audio, video and documents, with
  small text files inlined. Files with no local path (shared from a federated
  room) or over 20 MB become an attachment notice. Files in rooms the agent is
  only listening to are never downloaded.
- The agent can send files: `send_image` (URL), `send_image_file`,
  `send_animation`, `send_video`, `send_document` and `send_voice` upload to
  `Talk/<unique folder>/<name>` in the agent's Files and share into the room,
  with caption and reply-to; `send_voice` posts a Talk voice message.
- `NEXTCLOUD_TALK_FEDERATED_MEDIA=link`: Talk cannot share files into a
  federated conversation, so this posts a read-only public link that expires
  after 7 days instead. Off by default, since anyone holding the link can read
  the file.

## [1.5.0] - 2026-10-25

### Added

- User mode edits and deletes its own messages (`edit_message`, `delete_message`
  over Talk's `edit-messages` / `delete-messages` API). This is what turns on
  Hermes' tool-progress bubble and edit-based streaming in Talk: the runner
  silently drops every progress update on a platform whose adapter keeps the
  base no-op. Over-long content and server refusals (Talk's edit window) return
  failure, so the caller sends a fresh message instead.
- The active-listening backlog follows edits and deletions: when another
  participant (often an agent streaming through edits) edits or deletes a
  message we are holding as context, the backlog line is rewritten or dropped
  instead of handing over the first draft.

## [1.4.4] - 2026-10-25

### Fixed

- Staying quiet is a valid answer to another agent. A mention from an agent
  sender is passed to the core with `reply_expected = False`, so a deliberate
  silence marker that closes a bot-to-bot exchange no longer surfaces the
  "model returned only a silence marker" warning in the room. A human's
  mention still expects a reply.

## [1.4.3] - 2026-10-25

### Fixed

- A mention quoted inside code no longer summons the agent. Lumina told Dave
  to "use `@Jarvis /approve`", and Jarvis treated the example as an address and
  replied. Plain-text and rich mentions now count only outside inline code and
  fenced blocks (Talk's server still makes a `{mention-userN}` placeholder for
  a quoted `@jarvis`, so its position is checked too).

## [1.4.2] - 2026-10-24

### Fixed

- Slash commands work in mention-mode rooms. `@jarvis /sethome` reached the
  core as `[Room rules...] @Jarvis /sethome`, so it was never recognised as a
  command and `/sethome` could not be run from a group room. A leading
  self-mention is now stripped from commands, and commands skip the room-rules
  and backlog prefixes.

## [1.4.1] - 2026-10-24

### Fixed

- Rich mention ids are compared case-insensitively. A federated client resolved
  "@Jarvis" to id `Jarvis` for the local account `jarvis`, so the mention was
  missed and agent-to-agent replies stopped after one round.

## [1.4.0] - 2026-10-24

### Breaking

- **Manifest renamed `nextcloud-talk` -> `nextcloud-talk-platform`.** The old
  name clashed with the other catalog Talk plugin in `plugins.enabled`.
  Update `plugins.enabled` in `config.yaml` to `nextcloud-talk-platform`.

### Security (PR 140197 review, round 2)

- Room context (active listening) only carries lines from senders that pass
  the allowlist, the same filter watch digests use.
- Room descriptions are applied as rules only in rooms listed in
  `NEXTCLOUD_TALK_ROOM_MODES`, or everywhere with the explicit opt-in
  `NEXTCLOUD_TALK_ROOM_RULES_ALL_ROOMS=true`.

### Fixed

- `{mention-...}` placeholders are resolved to `@name` before the model sees
  the message, so "reply only when @mentioned" rules work.
- `NEXTCLOUD_TALK_LONG_POLL_TIMEOUT` is read when the adapter starts (per
  profile), not at import time; `long_poll_timeout` in config extra wins.

## [1.3.0] - 2026-10-24

### Breaking

- **Platform id renamed `nextcloud_talk` -> `nctalk`.** Another author's Talk
  plugin in the Hermes catalog registers `nextcloud_talk`, and two plugins
  claiming one id silently replace each other. Rename the `platforms:` key
  (and any `platform_toolsets:` / `home_channel.platform:` entries) in
  `config.yaml` from `nextcloud_talk` to `nctalk`, then restart the gateway.
  Env var names are unchanged (`NEXTCLOUD_TALK_*`). Sessions keyed on the
  old id start fresh.
- **`[watch]`/`[free]`/`[mention]` display-name prefixes only apply to rooms
  listed in `NEXTCLOUD_TALK_ROOM_MODES`.** Any Nextcloud user can name a room
  `[watch] ...`; unlisted rooms are now always `mention`.

### Fixed

- Watch-mode digests (both modes) are dispatched as ordinary inbound events,
  not `internal`, so the gateway allowlist and pause gate apply. Lines from
  senders outside `NEXTCLOUD_TALK_ALLOWED_USERS` are dropped before the
  digest is built (all kept with `NEXTCLOUD_TALK_ALLOW_ALL_USERS`).
- User mode no longer stamps `metadata` (`source_tag`, `mentioned`,
  `room_mode`, `room_context_lines`) on inbound events. Newer Hermes cores
  refuse non-WhatsApp metadata at native admission
  (`invalid_params (metadata)`), which dropped every addressed message.
- `NEXTCLOUD_TALK_APP_PASSWORD` and `NEXTCLOUD_TALK_SECRET` are read through
  `agent.secret_scope.get_secret`, falling back to the environment on Hermes
  builds without it.

## [1.2.0] - 2026-10-23

### Added (bot mode)

- Explicit inbound webhook hardening: a named 1 MiB body-size cap
  (`MAX_WEBHOOK_BODY_BYTES`) and a bounded, TTL-based nonce-replay cache
  checked immediately after signature verification, before any payload
  parsing. A byte-for-byte replay of a previously-accepted signed request
  is now rejected outright (`409`) instead of only being caught later, and
  only for `Create` events, by message-id dedup.
- Hardening ported from
  [Cliff3c/hermes-nextcloud-talk](https://github.com/Cliff3c/hermes-nextcloud-talk)
  (MIT) -- reimplemented against this repo's own structure and naming, not
  copied. See `THIRD_PARTY.md` and the module docstring in
  `bot/adapter.py` for what was ported, what was deliberately left out
  (the DTD-hardened XML parse of the outbound send response, which this
  adapter's `_post_message` has no equivalent of, since it never parses
  that response body), and why.

## [1.1.1] - 2026-10-23

### Fixed (user mode)

- The per-room long-poll's server-side hold time is now configurable
  (`NEXTCLOUD_TALK_LONG_POLL_TIMEOUT`, default `25`) instead of a hardcoded
  `30`. Default lowered a few seconds under Nextcloud Talk's own `30`
  ceiling so a deployment behind a reverse proxy, load balancer, or PaaS
  ingress with a shorter read timeout doesn't turn every idle poll into a
  connection error at that boundary.

## [1.1.0] - 2026-10-23

### Added (user mode)

- Agent-to-agent loop guard: `NEXTCLOUD_TALK_AGENT_USERS` and
  `NEXTCLOUD_TALK_AGENT_CHAIN_LIMIT` so a `free` room with more than one
  agent connected does not turn into an infinite agent-to-agent reply
  loop. A message from a listed agent, or from any Talk webhook bot, needs
  an explicit `@mention` even in a `free` room, and an addressed exchange
  that runs too long without a human message is silenced until a human
  speaks again.
- Active listening: `NEXTCLOUD_TALK_CONTEXT_MESSAGES` keeps unanswered
  room messages (unmentioned in `mention` mode, or an unaddressed agent
  message) as per-room context, handed over with the next message the
  agent does answer.
- Room rules from the room description: in group rooms, a non-empty
  Nextcloud Talk conversation description is prepended to the next
  message handed to the agent, re-read on every discovery pass.

## [1.0.0] - 2026-10-16

### Added

- Initial release: two independent Nextcloud Talk connectors for Hermes
  Agent behind one `nextcloud_talk` platform plugin.
  - `bot/` -- a webhook bot using Nextcloud Talk's Bot API (HMAC-SHA256
    signed requests both directions).
  - `user/` -- a genuine Nextcloud user account (app password), long-
    polling the Chat REST API, with federation-invite support
    (allowlisted auto-accept) and DM support.
  - Shared: per-room modes (`mention` / `watch` / `free`), 32000-char
    message chunking, a working-indicator reaction while a turn is in
    flight, `NEXTCLOUD_TALK_ALLOWED_USERS` / `NEXTCLOUD_TALK_ALLOW_ALL_USERS`
    / `NEXTCLOUD_TALK_HOME_CHANNEL`.
