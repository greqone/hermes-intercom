# hermes-intercom

Cross-session discovery and inter-agent messaging plugin for Hermes Agent (`>=0.22.5`).

## Features

- **`session_list`**: Read-only discovery of active and recent sessions in the current profile (`state.db` + `gateway_routing`), filtering out subagents, cron jobs, compression-ancestor segments, and archived chats by default. The store is read schema-tolerantly: a `state.db` missing optional `sessions` columns gets `NULL` substitutions, `gateway_routing` column presence is probed before querying, and the scan is bounded so a 50k-row store never materializes every row. Every entry reports `session_id`, `root_session_id`, `title`, `source`, `model`, `cwd`, `session_key`, `message_count`, `last_activity_at`, `addressable`, `routed`, and `is_self`.
- **`session_send`**: Delivers messages to another session (`Desktop`, `TUI`, `CLI`, `Discord`, `Telegram`) via `ctx.inject_message(session_key=...)`, which confirms the host scheduled the turn. A durable admission is only known on the gateway WebSocket fallback, which returns an admission id. Targets resolve by exact `session_id`/root id/`session_key`, a `>=6` char id prefix, or a unique title (exact title first, active before archived); an ended pre-compression segment resolves to the live continuation that replaced it.
- **`routed` versus synthesized keys**: `routed: false` means the entry's key was only synthesized as `agent:<ns>:local:dm:<session_id>`. Hermes core's admissibility check returns a false-positive `true` for unknown keys and then drops the message asynchronously, so such targets are mounted in the gateway authority first.
- **Dormant Local Session Auto-Mount**: When injection is refused for a `:local:dm:` target, mounts that session into `authority.sessions` over the local Gateway WebSocket (`session.resume(omit_messages=True)` + `session.detach`) and retries once. Endpoint discovery, resume and detach share a single `mount_timeout_seconds` deadline.
- **External-process WS fallback**: When in-process injection is unavailable and `allow_gateway_injection: true` is set, delivers with `prompt.submit` over the local Gateway WebSocket under the same `mount_timeout_seconds` budget; a timed-out submit is treated as in flight so a retry is never double-delivered.
- **Desktop Attributed Cards**: Formats outbound messages with `[Message from agent '<sender>'] ` so Hermes Desktop renders them as attributed agent timeline notices rather than human user bubbles. Sender labels are stripped of quotes, brackets, control characters, lone UTF-16 surrogates and bidi/zero-width marks so the card header stays renderable. Message bodies keep their text, including quotes and brackets; only lone surrogates are replaced so the card is always UTF-8 encodable.
- **Multi-Hop Loop Guard & Rate Limiting**: Tracks hop depth (`[intercom from=<id> hop=<n>/<max> mode=<reply|fyi>]`) via `pre_llm_call` and transcript fallback — including the parent segment of a fresh post-compression continuation and a card that starts its own line after a prepended queued prompt — so two sessions can exchange a bounded reply without infinite ping-pong loops.
- **Deduplication**: within `dedup_window_seconds` (default 120; `0` disables) a repeat send is refused when the sender session, the `expect_reply` flag and the message body all match. Per-target rate limiting counts every sender's messages inside `burst_window_seconds`.

## Configuration

Enable gateway injection in `config.yaml`:

```yaml
plugins:
  enabled:
    - hermes-intercom
  entries:
    hermes-intercom:
      allow_gateway_injection: true
      settings:
        max_hops: 3
        dedup_window_seconds: 120
        burst_window_seconds: 600
        max_messages_per_window: 10
        auto_mount_dormant: true
        mount_timeout_seconds: 8.0
```

| Setting | Meaning |
| --- | --- |
| `max_hops` | Autonomous hop ceiling; a reply that would exceed it fails with `error=max_hops_exceeded`. |
| `dedup_window_seconds` | Window for the per-sender + `expect_reply` + body duplicate check (`0` disables). |
| `burst_window_seconds` / `max_messages_per_window` | Per-target rate limit shared by all senders; over-limit sends fail with `error=rate_limited`. |
| `auto_mount_dormant` | Mount a dormant `:local:dm:` target in the gateway authority and retry the refused injection. |
| `mount_timeout_seconds` | Overall deadline for one gateway operation (mount, or the WS `prompt.submit` fallback), not per step. |

## Desktop presentation (`desktop/plugin.js`)

Hermes Desktop shows every `[Message from agent '…']` delivery as its own native notice ("Message from X · show message"), and the expanded body starts with the intercom envelope. The desktop half ships so the package loads as a desktop plugin, and it renders nothing.

The Desktop Plugin SDK has no contribution that draws an incoming agent message (its areas are panes, pages, composer rows, transcript directives and tool-media renderers). Catalog rule 8 forbids reaching past the SDK into app markup, so this build does not. The message is still delivered and still visible; it uses the host's own note instead of a custom card.

Tests (Node >= 20, jsdom from a Hermes desktop checkout):

```bash
HERMES_DESKTOP_ROOT=<hermes-agent>/apps/desktop node --test tests/desktop/intercom-note.test.mjs
```

The fixture in `tests/desktop/native-fixture.mjs` reproduces the DOM of `AgentMessageNote` and `UserMessageText`. Test messages are generated by the real Python `store.format_intercom_message` (`HERMES_PYTHON` picks the interpreter).
