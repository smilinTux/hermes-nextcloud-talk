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

## Guest read tools (user mode, v1.16.0)

- `talk_room_history`, `talk_room_threads` and `talk_room_files` read as the
  agent's account, which is in many rooms. So for a non-owner they take no
  room argument and act only on the turn's own room, which the gateway binds
  (the model cannot set it).
- Hermes runs them for a non-owner only through the plugin's guest guard,
  and only with operator consent (`tools.guest`). On every call the guard
  re-checks the platform, that the turn's room is the session's room, that
  the access file opens this tool group (`guest_tools`) in that room, and
  that the speaker is allowed there. Any argument that names another room
  is refused. With no access file the call is refused.
- What they return is what any member of the room already sees in Talk.
  They never write, and they never read other rooms, the agent's files or
  its memory.

## Room workspace and approvals (user mode, v1.17.0)

- The workspace root is re-read from the Sharing API on every call: folders
  the agent's account OWNS and shared to THIS room (share type 10), outside
  `Talk/`. Unsharing closes it at once; the share's permissions bound create
  and edit. Paths are relative and checked before any request: absolute
  paths, `..`, `.` and hidden segments, backslashes and control characters
  are refused. Only text file types are read or written.
- No delete, move, rename or share exists in the tool set. `create` never
  overwrites (`If-None-Match: *`), `replace` only an existing file, `append`
  uses the file's etag. Nextcloud keeps the previous versions.
- Approval is decided by the access file as it is when the reaction
  arrives: `approvers` for the room, else `defaults.approvers`, else the
  Hermes owner check for that room. An unrecognised `approval` value counts
  as required, so a typo never opens writes. A request is claimed once; a
  second reaction does nothing.
- An approved write runs the stored path and content, not a new model
  turn, so what runs is what the request previewed (the preview shows the
  first 400 characters and the full length). An approved `talk_ask_owner`
  runs as the approver's own turn: Hermes gives it the approver's scope, and
  no more.

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
