# Changelog

All notable changes to this project are documented here. Versioning follows
[Semantic Versioning](https://semver.org/); the version tracked is the
combined plugin's (`plugin.yaml` at the repo root).

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
