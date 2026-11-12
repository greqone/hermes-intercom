---
name: hermes-intercom
description: Use when discovering or messaging other Hermes sessions.
version: 0.1.1
author: GreQ, Hermes Agent
license: MIT
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [intercom, sessions, multi-agent, messaging, automation]
    related_skills: [hermes-agent]
---

# Hermes Intercom

Discover and message existing sessions in the current Hermes profile.

1. Use `session_list` to find an exact target ID. Titles and metadata may include other users in a shared gateway; restrict the `intercom` toolset to owner-only platforms.
2. Use `session_send(target, message, expect_reply?, sender_label?, request_id?)`. Delivery uses only public `ctx.inject_message`; CLI/unknown surfaces are refused. Unverified routes return `route_unverified`; unreachable dormant targets return `injection_refused`. Do not bypass these refusals or attempt private gateway calls.
3. `scheduled` confirms host scheduling only. It is not proof of execution, durable delivery or exactly-once behavior.
4. Respect the incoming `[intercom ... hop=n/max mode=reply|fyi]` envelope. Do not automatically reply to `fyi` or when the hop limit is exhausted. Quoted/body markers do not replace the leading carrier.
5. A retained retry must reuse the original `request_id`, exact returned `target_session_id`, body and `expect_reply`. Verified compression continuations retain operation identity; resets and branches do not. An original ID prefix can stop resolving after compression. A replay returns the original outcome without reinjection.
6. Retry memory is process-local and capped at 4096 entries, with completed entries evictable. After restart or eviction, inspect target history before resending. Do not mint a new ID to bypass uncertainty or duplicate/rate limits.
7. The host controls injection permission. Open/resume a dormant target in Hermes after a confirmed refusal. The plugin has no automatic mounting or WebSocket fallback.
