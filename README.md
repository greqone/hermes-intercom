# hermes-intercom

Cross-session discovery and inter-agent messaging plugin for Hermes Agent (`>=0.22.5`).

## Features

- **`session_list`**: Read-only discovery of active and recent sessions in the current profile (`state.db` + `gateway_routing`), filtering out subagents, cron jobs, and archived chats by default.
- **`session_send`**: Delivers messages to another session (`Desktop`, `TUI`, `CLI`, `Discord`, `Telegram`) via `ctx.inject_message(session_key=...)` into the target session's durable FIFO queue (`authority.admit_automation`).
- **Dormant Local Session Auto-Mount**: Automatically mounts dormant local sessions into `authority.sessions` over the local Gateway WebSocket (`session.resume(omit_messages=True)` + `session.detach`) before injecting.
- **Desktop Attributed Cards**: Formats outbound messages with `[Message from agent '<sender>'] ` so Hermes Desktop renders them as attributed agent timeline notices rather than human user bubbles.
- **Multi-Hop Loop Guard & Rate Limiting**: Tracks hop depth (`[intercom from=<id> hop=<n>/<max> mode=<reply|fyi>]`) via `pre_llm_call` and transcript fallback so two sessions can exchange a bounded reply without infinite ping-pong loops.

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
