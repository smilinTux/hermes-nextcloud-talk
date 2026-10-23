# Third-party credit

## Cliff3c/hermes-nextcloud-talk

Two of `bot/`'s inbound webhook admission checks (`bot/adapter.py`) were
ported **in concept** from
[Cliff3c/hermes-nextcloud-talk](https://github.com/Cliff3c/hermes-nextcloud-talk)
(MIT License), starting in the v1.2.0 release of this repo:

- An explicit, named cap on the inbound webhook body size
  (`MAX_WEBHOOK_BODY_BYTES`), matching the ceiling Cliff3c's adapter
  enforces (`MAX_BODY_BYTES = 1024 * 1024`).
- A bounded, TTL-based cache of the inbound `X-Nextcloud-Talk-Random`
  nonce, checked immediately after signature verification and before any
  payload parsing, so a byte-for-byte replay of a previously-accepted
  signed request is rejected outright rather than only being caught later
  by message-id dedup. Cliff3c's adapter does this with its own
  `_TTLCache` keyed by nonce (`REPLAY_TTL`); this repo's implementation
  reuses its own pre-existing `DedupeCache` (already used here for
  message-id dedup) as a second instance with a different key and TTL,
  rather than adopting Cliff3c's cache class.

One piece of Cliff3c's hardening was deliberately **not** ported: his
adapter parses the outbound OCS send-confirmation response (JSON, or on
Nextcloud 32, XML) and hardens that XML parse against DTDs. This repo's
`bot/adapter.py` never parses that response body at all (it only checks
the HTTP status and keeps the raw text), so there is no XML parser in its
request path for that hardening to apply to. See the module docstring in
`bot/adapter.py` for detail.

No source code was copied; the above is an independent reimplementation
against this repo's own naming and structure, after reading the public
Cliff3c/hermes-nextcloud-talk source for the underlying idea. The
upstream project's MIT license notice is reproduced below in case any of
this counts as a "substantial portion" of that work for the purposes of
MIT's attribution requirement.

```
MIT License

Copyright (c) 2026 Cliff3c

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```
