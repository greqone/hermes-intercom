---
name: hermes-intercom
description: Use when discovering or messaging other Hermes sessions.
version: 0.1.0
author: GreQ, Hermes Agent
license: MIT
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [intercom, sessions, multi-agent, messaging, automation]
    related_skills: [hermes-agent]
---

# Hermes Intercom (`hermes-intercom`)

Cross-session discovery and inter-agent messaging for Hermes Agent sessions (Desktop, TUI, CLI, Discord, Telegram) within the active profile.

## Tools

- `session_list(query?, source?, limit?, include_cron?, include_archived?)`
  Lists recent sessions from the profile's `state.db` (read-only and schema tolerant: older stores missing optional columns, and unkeyed compression continuations, are handled), ordered by most recent activity. Returns `session_id`, `root_session_id`, `title`, `source`, `model`, `cwd`, `session_key`, `message_count`, `last_activity_at`, `addressable`, `routed`, and `is_self`.
  - `addressable` — the entry has a gateway `session_key` (explicit, from `gateway_routing`, or synthesized).
  - `routed` — a **real** route exists (explicit `session_key` or a `gateway_routing` row). When it is `false` the key was only synthesized as `agent:<namespace>:local:dm:<session_id>`; Hermes core's admissibility check passes such keys and then drops them asynchronously, so the session must be mounted in the gateway authority before injecting.
- `session_send(target, message, expect_reply?, sender_label?, request_id?)`
  Sends an attributed agent card (`[Message from agent '<sender>'] ...`) to the target session via `ctx.inject_message(session_key=...)`.
  - `target` accepts a full `session_id`, a `session_id` prefix of **at least 6 characters** (e.g. `local-abc123`), a `session_key`, or a unique title. Resolution ranks all sessions in the profile, not just the most recent page, so older sessions stay reachable. Order: exact id / root id / session key → compressed-ancestor alias → `>=6` char id prefix → title (exact, then substring, active before archived). An id prefix is never beaten by a title, so a chat titled `local-ab` cannot hijack the prefix `local-ab...`, and an archived chat with the same title never blocks the active one.
  - Replying to a **pre-compression session id** still works: an ended `end_reason=compression` segment resolves to the live continuation that replaced it (same `session_key`, or its `parent_session_id` descendant).
  - If the target local session is dormant (not mounted in `authority.sessions`), `session_send` mounts it via `session.resume(..., omit_messages=True)` + `session.detach` over the local Gateway WebSocket and retries the injection once, for any target whose key contains `:local:dm:`. The whole mount — endpoint discovery, resume and detach — shares one `mount_timeout_seconds` deadline.
  - If called from an external process where in-process injection is unavailable **and** `allow_gateway_injection: true` is set, it falls back to `prompt.submit` over the local Gateway WebSocket. Mount and submit share the one `mount_timeout_seconds` budget.
  - `expect_reply=true` marks the card `mode=reply` and tells the target how to reply with `session_send`; `expect_reply=false` (default) marks it `mode=fyi`.

## Rules & Pitfalls

1. **Discover before sending:** Call `session_list` when you only have a vague topic name so you pass an unambiguous `session_id`, a `>=6` char ID prefix, or a unique title substring to `session_send`.
2. **Respect hop limits:** Every intercom message carries `[intercom from=<session_id> hop=<n>/<max> mode=<reply|fyi>]`. Do not reply to `[mode=fyi]` messages or when `hop == max` unless the human explicitly asks; a send that would exceed `max_hops` is refused with `error=max_hops_exceeded`.
3. **Hop tags only count at the start:** The `[intercom ...]` tag (with or without the `[Message from agent '...']` card prefix) is honored at the start of a user message. A card that starts its own line counts only when a host queued-prompt marker precedes it; the first such card is the envelope. Quoting a card after ordinary text never raises the hop counter.
4. **Deduplication is per sender + `expect_reply` + body:** within `dedup_window_seconds` (default 120; `0` disables) the same body from the same session with the same `expect_reply` is refused as `duplicate_message`, while the same text from another session or with `expect_reply` flipped is a new message. A timed-out WebSocket submit keeps the reservation, so a retry of the same body is refused inside this process. The gateway, not this plugin, deduplicates a second `prompt.submit` for the same `request_id`.
5. **`routed: false` means mount first:** a synthesized `:local:dm:` key is not proof the target is reachable — never treat `inject_message` returning true for an unrouted key as delivered.
6. **The card is sanitized, not copied byte for byte:** quotes, brackets, control characters and bidi/zero-width marks are stripped from the sender label. Lone UTF-16 surrogates in the body are replaced so the card stays UTF-8 encodable. Directive-looking text (`@file`, `@diff`, `@staged`, and a registered `@kind:value`) is neutralized in the header and the body; the readable value stays.
7. **Host permission required:** Hermes refuses every plugin-injected turn unless `plugins.entries.hermes-intercom.allow_gateway_injection: true` is set in the profile's `config.yaml`. The plugin cannot deliver anything without it. The WebSocket fallback is the path used when this process has no injector of its own.
