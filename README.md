# hermes-intercom

Cross-session discovery and messaging for Hermes Agent (`>=0.22.5`).

## Tools and supported delivery

- `session_list` reads the active profile's `state.db` without writing to it. It lists active/recent sessions with IDs, titles, source, model, cwd, route, message count and activity. Optional query/source filters apply before the result limit; subagents, cron, hidden/archived sessions and superseded compression ancestors are excluded by default.
- `session_send` resolves an exact session ID, route key, ID prefix of at least six characters, or unique title. It sends only through **`ctx.inject_message(..., session_key=...)`**. Successful calls return `scheduled`: the host accepted scheduling, not proof the recipient ran or that delivery is durable.
- CLI and unknown sender surfaces are refused using `ctx.session_surface()`. This conservatively includes gateway-backed CLI turns because the public surface does not distinguish them from classic CLI. Desktop, TUI, ACP and messaging-channel turns may send when the host grants injection.
- An unverified route returns `route_unverified`. A dormant target that the public API cannot reach returns `injection_refused`. Open/resume that target using Hermes and retry only after a confirmed refusal. **The plugin does not mount sessions or use a private WebSocket fallback.** Old `auto_mount_dormant` and `mount_timeout_seconds` settings no longer have any effect.

`addressable` means a route key can be named; `routed` is discovery metadata. The send path rechecks `gateway_routing` when available and refuses stale or differently owned routes. Neither field guarantees that a target is currently mounted. In older schemas without a routing index, explicit route metadata is used and the public API decides admission.

## Shared-gateway access

In a gateway serving several users in one profile, **`session_list` can show other users' chat titles and metadata, and `session_send` can start model/tool turns in their chats**. Restrict the **`intercom` toolset to owner-only platforms** using Hermes tool permissions. This plugin's profile boundary is not a per-user authorization boundary. Turns use the recipient's configured model and tool permissions.

## Reliability and safeguards

- Attributed messages carry a sanitized sender label and an intercom hop envelope. Incoming hop budgets are inherited across verified compression ancestry and cannot be raised by the sender. A fresh human turn resets the budget; empty input and synthetic summaries do not. Literal markers in the body cannot replace the leading carrier. An unreadable/ambiguous hop history refuses the send.
- Duplicate detection is per sender, `expect_reply` and body (default 120 seconds). Rate limits count distinct operations from all senders to a target (default 10 per 600 seconds).
- A caller-supplied `request_id` identifies one operation in the current process. While retained, repeating an accepted operation returns its original result without reinjection, including verified sender/target compression continuations. Different bodies or unrelated sessions conflict; resets, branches and delegates do not inherit identity. IDs cannot contain whitespace, controls or lone surrogates.
- Retry memory is capped at 4096 operations. Active calls are protected; completed entries can be evicted under capacity pressure. Restart or eviction loses replay protection. **There is no exactly-once or durable-delivery guarantee.** Check target history before resending after either. Retention of the removed WebSocket transport's 120-second uncertain receipts is no longer part of this build.
- An original target ID prefix may stop resolving after compression (R7-D01). For a retained retry use the exact `target_session_id` returned by the first result with the same `request_id`, body and `expect_reply`; do not create a new operation to recover an uncertain delivery.
- Compression traversal is bounded to 256 identities. Cycles, bounds and reset/branch/delegate boundaries fail closed. Route preflight and host scheduling are not an atomic transaction; later host failures are visible in Hermes logs rather than a durable receipt here.

## Host API and data access

Profile home comes from public `hermes_constants.get_hermes_home()` in the host's scoped tool/hook context. Settings use `ctx.get_config`; injection permission is enforced by `ctx.inject_message`. The plugin does not read `ctx._manager`, set a home override, parse host `config.yaml`, or import gateway runtime/client helpers.

Discovery reads the profile's `sessions`, `gateway_routing` and `messages` tables with schema-tolerant projections. It adds no external service or runtime dependency. Audit metadata is appended to `$HERMES_HOME/cache/hermes-intercom/audit.jsonl`, rotating at 2 MiB. It includes session/request IDs and a body-hash prefix, but no message plaintext. POSIX mode 0600 is requested; Windows access follows directory ACLs. Hermes itself may call configured external model/tool services.

## Configuration

The host owns this configuration; the plugin reads settings through its public context:

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
```

`max_hops` is 1–10; `dedup_window_seconds` is 0–3600 (0 disables deduplication); `burst_window_seconds` is 1–86400; `max_messages_per_window` is 1–200. Invalid low/nonfinite values use documented defaults; high values are capped. These limits do not grant permission to send.

## Desktop presentation

The host renders `[Message from agent '…']` as its native attributed notice. `desktop/plugin.js` renders nothing and does not modify the Desktop DOM. Directive-like body/header text is neutralized; sender control/bidi/zero-width characters and lone surrogates are sanitized. The Node fixtures retain the original Nous Research MIT notice.

## Tests

Use a complete Hermes Agent checkout and its compatible Python environment, not stubbed host modules. Set a disposable profile for tests:

```bash
HOST=/absolute/path/to/hermes-agent
HERMES_HOME=/absolute/path/to/disposable-home PYTHONPATH="$HOST" HERMES_AGENT_REPO="$HOST" PYTHONDONTWRITEBYTECODE=1 "$HOST/.venv/bin/python" -B -m pytest -q
# Windows host environments may use "$HOST/venv/Scripts/python.exe".
HERMES_DESKTOP_ROOT="$HOST/apps/desktop" HERMES_PYTHON="$HOST/.venv/bin/python" node --test tests/desktop/intercom-note.test.mjs
```

Tests include real SQLite, public-API refusal/replay controls and real PluginManager registration/dispatch with an isolated test injector. They do not certify live cross-surface delivery or macOS. See [PUBLIC_API_REVIEW.md](PUBLIC_API_REVIEW.md) for the maintainer delta and test migration, and [INTEGRATION.md](INTEGRATION.md) for historical audit integration decisions.
