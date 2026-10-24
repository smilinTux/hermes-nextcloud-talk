# Security model

Both connectors in this repo share the same broad shape -- a credential
Nextcloud trusts, scoped by Nextcloud's own room/federation controls, with
no Hermes-level per-user allowlist layered underneath unless you opt in.

## Bot mode (`bot/`, webhook)

- **Authentication**: Nextcloud Talk's native bot HMAC-SHA256 scheme
  (shared secret, `HMAC(secret, RANDOM + body)`), both directions. No API
  key, no app password, no Nextcloud user account.
- **Authorization**: room-scoped by Nextcloud itself (`occ talk:bot:setup`
  per room, moderator-toggleable). This adapter adds no per-user allowlist
  on top by default -- the HMAC secret plus Nextcloud's own room enablement
  *is* the authorization boundary, the same model `Platform.WEBHOOK`
  already uses elsewhere in Hermes's gateway. Set
  `NEXTCLOUD_TALK_ALLOW_ALL_USERS=true` to make that explicit, or set
  `NEXTCLOUD_TALK_ALLOWED_USERS` for a Hermes-side allowlist on top of it.
- **Secret handling**: a 64+ char shared secret, read from Hermes's own
  secret store (`~/.hermes/.env` or the plugin's configured env var),
  never printed, never committed, never placed in `config.yaml`.
- **Blast radius if the secret leaks**: whoever has it can post messages
  into any room the bot is enabled in and can hit the bot's `send()`
  endpoint -- but still cannot read rooms the bot isn't enabled in, cannot
  act as any Nextcloud user, and the agent triggered this way only has
  whatever toolset you grant the `nctalk` platform (keep it
  narrow: no terminal/filesystem/code-exec/outbound-messaging tools for a
  bot that untrusted room members can message).
- **Network exposure**: the webhook listener should bind to a private
  interface (a VPN/tailnet IP, a reverse-proxy-only loopback), never
  `0.0.0.0` directly on a box with a public interface. `NEXTCLOUD_TALK_HOST`
  defaults to `127.0.0.1` (inert) specifically so a misconfiguration fails
  closed.
- **Reversibility**: `occ talk:bot:remove`/`uninstall` on the Nextcloud
  side, plus disabling the plugin on the Hermes side, fully removes this
  integration.

## User mode (`user/`, app password)

- **Authentication**: a Nextcloud app password (`Settings -> Security ->
  Devices & sessions -> Create new app password`), never the account's
  real login password. Basic-Auth over HTTPS, same as any Nextcloud
  client.
- **Authorization**: the account's own room memberships decide which
  rooms the adapter can see at all; `NEXTCLOUD_TALK_ROOM_MODES` only
  controls *behavior* (mention/watch/free) inside rooms it is already a
  member of. Federation invitations are auto-accepted only from inviters
  on the explicit `NEXTCLOUD_TALK_ALLOWED_INVITERS` allowlist -- everyone
  else's invite is left pending, never auto-declined, never silently
  accepted.
- **Blast radius if the app password leaks**: whoever has it can act as
  this Nextcloud account inside Nextcloud (post, react, join rooms it is
  invited to) but cannot read the account's real password, cannot act on
  any other Nextcloud service that password-gates separately, and is
  still bound by whatever toolset you grant this platform.
- **Revocation**: delete the app password from the account's security
  settings; the adapter's next request fails closed (401) immediately,
  no server-side state to clean up.

## Shared across both modes

- **Message length**: both connectors chunk at Nextcloud Talk's ~32000
  character message limit; neither silently truncates.
- **Working-reaction indicator**: both add a configurable reaction
  (default: the eyes emoji) to a message the instant a turn begins and
  remove it when the turn ends, since Talk has no bot/typing indicator.
  A failed reaction call never blocks or fails the underlying turn --
  it is a UI nicety, not something either connector depends on.
- **No telemetry.** Neither connector phones home anywhere other than the
  Nextcloud instance you configure it against.
