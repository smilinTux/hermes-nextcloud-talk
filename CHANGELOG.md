# Changelog

All notable changes to this project are documented here. Versioning follows
[Semantic Versioning](https://semver.org/); the version tracked is the
combined plugin's (`plugin.yaml` at the repo root).

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
