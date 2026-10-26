# Changelog

All notable changes to this project are documented here. Versioning follows
[Semantic Versioning](https://semver.org/); the version tracked is the
combined plugin's (`plugin.yaml` at the repo root).

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
