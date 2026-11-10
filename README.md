# hermes-intercom

Cross-session discovery and inter-agent messaging plugin for Hermes Agent (`>=0.22.5`).

## Features

- **`session_list`**: Read-only discovery of active and recent sessions in the current profile (`state.db` + `gateway_routing`), filtering out subagents, cron jobs, compression-ancestor segments, and archived chats by default. The store is read schema-tolerantly: a `state.db` missing optional `sessions` columns gets `NULL` substitutions, `gateway_routing` column presence is probed before querying, and the scan is bounded so a 50k-row store never materializes every row. Every entry reports `session_id`, `root_session_id`, `title`, `source`, `model`, `cwd`, `session_key`, `message_count`, `last_activity_at`, `addressable`, `routed`, and `is_self`.
- **`session_send`**: Delivers messages to another session (`Desktop`, `TUI`, `CLI`, `Discord`, `Telegram`) via `ctx.inject_message(session_key=...)`. The result is `scheduled` when the host accepted the turn for scheduling, and `admitted` only when the gateway WebSocket fallback returned an admission id. Neither means the turn has run. Targets resolve by exact `session_id`/root id/`session_key`, a `>=6` char id prefix, or a unique title (exact title first, active before archived); an ended pre-compression segment resolves to the live continuation that replaced it, and a branch, reset or delegate descendant is never chosen by recency.
- **`routed` versus synthesized keys**: `routed: false` means the entry's key was only synthesized as `agent:<ns>:local:dm:<session_id>`. Hermes core's admissibility check returns a false-positive `true` for unknown keys and then drops the message asynchronously, so such targets are mounted in the gateway authority first.
- **Dormant Local Session Auto-Mount**: When injection is refused for a `:local:dm:` target, mounts that session into `authority.sessions` over the local Gateway WebSocket (`session.resume(omit_messages=True)` + `session.detach`) and retries once. Endpoint discovery, resume and detach share a single `mount_timeout_seconds` deadline.
- **External-process WS fallback**: When in-process injection is unavailable and `allow_gateway_injection: true` is set, delivers with `prompt.submit` over the local Gateway WebSocket. Mount and submit share one `mount_timeout_seconds` budget. A timed-out submit keeps the reservation. The gateway deduplicates a second submit that reuses the same `request_id`. A refusal from the gateway (`queue_full`, `permission_denied`, `runtime_draining`) is reported as a refusal, not as an unknown outcome. Successful sends are appended to `$HERMES_HOME/cache/hermes-intercom/audit.jsonl`, rotated at 2 MiB.
- **Desktop Attributed Cards**: Formats outbound messages with `[Message from agent '<sender>'] ` so Hermes Desktop renders them as attributed agent timeline notices rather than human user bubbles. Sender labels are stripped of quotes, brackets, control characters, lone UTF-16 surrogates and bidi/zero-width marks so the card header stays renderable. Lone UTF-16 surrogates in the body are replaced so the card stays UTF-8 encodable. Directive-looking text (`@file`, `@diff`, `@staged`, and a registered `@kind:value`) is neutralized in the header and the body; the readable value stays.
- **Multi-Hop Loop Guard & Rate Limiting**: Tracks hop depth (`[intercom from=<id> hop=<n>/<max> mode=<reply|fyi>]`) via `pre_llm_call` and transcript fallback — including the parent segment of a fresh post-compression continuation. A card counts when it leads the message, or when it starts its own line after a host queued-prompt marker. A card quoted after ordinary text does not count. Two sessions can exchange a bounded reply without an infinite ping-pong.
- **Deduplication**: within `dedup_window_seconds` (default 120; `0` disables) a repeat send is refused when the sender session, the `expect_reply` flag and the message body all match. A caller-supplied `request_id` names one operation within the current profile: the same id with the same body returns the first inject outcome or refreshes the gateway receipt, and reuse for another caller, target or body is refused. Both the dedup window and the rate limit are per process and reset on restart. Per-target rate limiting counts distinct operations inside `burst_window_seconds`. The operation cache is bounded at 4096 records: active and outcome-unknown records are protected; completed records may be evicted. When no safe slot remains, new sends fail with `operation_capacity` while existing ids can still be reconciled. This is bounded replay protection, not exactly-once delivery.

## Security and operational disclosures

- Reads the active profile's `state.db` read-only (session metadata, routing and latest user-message tags), and reads the injection permission from that profile's `config.yaml`. It does not read `.env`, `auth.json`, browser credentials or other profiles' stores.
- Uses the local Hermes Gateway WebSocket through the host's discovery/client helpers. It calls no third-party service directly, runs no shell command and has no self-updater. **An admitted message can start an agent turn and therefore spend the target session's configured model subscription/API quota and invoke its enabled tools under the host's normal approvals.** There is no separate confirmation per message after the profile grants injection.
- Requires `plugins.entries.hermes-intercom.allow_gateway_injection: true` for Gateway delivery. Disabling `auto_mount_dormant` disables pre-mount/retry, not the independently permission-gated WebSocket fallback.
- Writes a best-effort metadata-only audit log at `$HERMES_HOME/cache/hermes-intercom/audit.jsonl` (2 MiB rotation plus one previous file). Records include session/route ids, hop, timestamp, request/admission id and a body digest, but not message text or credentials. Request ids should be opaque identifiers, not secrets.
- Keeps bounded in-memory replay, burst and hop state. Gateway discovery uses an on-demand worker pool of up to four threads with an unload cleanup hook; transport helpers may also use a daemon bridge thread when invoked from an existing event loop. No resident external daemon is started.
- Relies on the host's SQLite schema, ContextVar home/session helpers and Gateway client protocol. Missing host support fails as an explicit refusal; a validator pass is not a live end-to-end delivery check. Stale routes owned by a different conversation are refused; verified compression ancestors may own their continuation's route.
- `scheduled` confirms host scheduling only; a later refusal may appear only in the Gateway log. `admitted` and returned receipt status/outcome/reference describe Gateway admission, not a guarantee of execution or a durable queue. After an unknown submit, retry the **same** `request_id` and body; do not create a new operation to bypass the uncertainty.

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

## Python checks

Use Python >=3.11 with `pytest>=7.4,<10`. Run from this directory with an isolated `HERMES_HOME` and inherited credentials removed:

```bash
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python -m pytest -q
```

Set `HERMES_AGENT_REPO` to a Hermes checkout and use its complete runtime environment for the real PluginManager/registry smoke test and host-YAML compatibility test. Without that host, those integration tests skip; the remaining tests use synthetic stores and transport doubles. No test intentionally sends a live message or calls an inference provider. The Node fixture is a presentation-contract test, not a running Desktop end-to-end test.

Before catalog submission, validate the exact pinned source with `hermes plugins validate /path/to/hermes-intercom --install-deps` and run the catalog payload scanner. Runtime Python dependencies are supplied by Hermes; this plugin declares no additional package dependencies.
