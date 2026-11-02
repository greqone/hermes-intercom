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
  Lists recent sessions from the profile's `state.db` (read-only), ordered by most recent activity. Returns `session_id`, `root_session_id`, `title`, `source`, `model`, `cwd`, `session_key`, `message_count`, `addressable`, and `is_self`.
- `session_send(target, message, expect_reply?, sender_label?)`
  Sends an attributed agent card (`[Message from agent '<sender>'] ...`) to the target session via `ctx.inject_message(session_key=...)`.
  - `target` accepts a full `session_id`, a `session_id` prefix of **at least 6 characters** (e.g. `local-abc123`), a `session_key`, or a unique title substring. Resolution ranks all sessions in the profile, not just the most recent page, so older sessions stay reachable.
  - If the target local session is dormant (not yet mounted in `authority.sessions`), `session_send` automatically mounts it via `session.resume(..., omit_messages=True)` + `session.detach` over the local Gateway WebSocket before injecting.
  - If called from an external process where in-process injection is unavailable and `allow_gateway_injection: true` is set, it falls back to delivering over the local Gateway WebSocket.

## Rules & Pitfalls

1. **Discover before sending:** Call `session_list` when you only have a vague topic name so you pass an unambiguous `session_id`, a `>=6` char ID prefix, or a unique title substring to `session_send`.
2. **Respect hop limits:** Every intercom message carries `[intercom from=<session_id> hop=<n>/<max> mode=<reply|fyi>]`. Do not reply to `[mode=fyi]` messages or when `hop == max` unless the human explicitly asks.
3. **Hop tags only count at the start:** The `[intercom ...]` tag (with or without the `[Message from agent '...']` card prefix) is only honored as the first thing in a user message. Quoting the tag later in a message body never raises the hop counter.
4. **Host permission required:** `plugins.entries.hermes-intercom.allow_gateway_injection: true` must be enabled in the profile's `config.yaml`.
