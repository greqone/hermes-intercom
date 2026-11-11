# hermes-intercom

Cross-session discovery and inter-agent messaging plugin for Hermes Agent (`>=0.22.5`).

Integration notes: [independent audit reconciliation](INTEGRATION.md). Compression
alias and persisted-hop traversal are bounded to 256 identities; unresolved hop
cycles or bound exhaustion refuse delivery. A current scoped hook observation
takes precedence over an older persisted row; empty input and pure synthetic
summaries do not reset the budget. Caller-supplied request IDs are validated
without whitespace trimming or surrogate replacement. Configuration parsing
supports the host's ruamel.yaml as well as older PyYAML environments.

## Features

- **`session_list`**: Read-only discovery of active and recent sessions in the current profile (`state.db` + `gateway_routing`), filtering out subagents, cron jobs, compression-ancestor segments, and archived chats by default. The store is read schema-tolerantly: a `state.db` missing optional `sessions` columns gets `NULL` substitutions, `gateway_routing` column presence is probed before querying, and result materialization is bounded; query/source filters run before the result window, though matching can still scan the table. Every entry reports `session_id`, `root_session_id`, `title`, `source`, `model`, `cwd`, `session_key`, `message_count`, `last_activity_at`, `addressable`, `routed`, and `is_self`.
- **`session_send`**: Delivers messages to another session (`Desktop`, `TUI`, `CLI`, `Discord`, `Telegram`) via `ctx.inject_message(session_key=...)`. The result is `scheduled` when the host accepted the turn for scheduling. The gateway WebSocket fallback returns the host's own receipt status, which is `queued`, `started`, `unknown` or `terminal`. Neither means the turn has run. Targets resolve by exact `session_id`/root id/`session_key`, a `>=6` char id prefix, or a unique title (exact title first, active before archived); an ended pre-compression segment resolves to the live continuation that replaced it, and a branch, reset or delegate descendant is never chosen by recency.
- **`routed` versus synthesized keys**: `routed: false` means the entry's key was only synthesized as `agent:<ns>:local:dm:<session_id>`. Hermes core's admissibility check returns a false-positive `true` for unknown keys and then drops the message asynchronously, so such targets are mounted in the gateway authority first.
- **Dormant Local Session Auto-Mount**: When injection is refused for a `:local:dm:` target, mounts that session into `authority.sessions` over the local Gateway WebSocket (`session.resume(omit_messages=True)` + `session.detach`) and retries once. Endpoint discovery, resume and detach share a single `mount_timeout_seconds` deadline.
- **External-process WS fallback**: When in-process injection is unavailable and `allow_gateway_injection: true` is set, delivers with `prompt.submit` over the local Gateway WebSocket. Mount and submit share one `mount_timeout_seconds` budget. A timed-out submit keeps the reservation. The gateway deduplicates a second submit that reuses the same `request_id`. A refusal from the gateway (`queue_full`, `permission_denied`, `runtime_draining`) is reported as a refusal, not as an unknown outcome. Successful sends are appended to `$HERMES_HOME/cache/hermes-intercom/audit.jsonl`, rotated at 2 MiB.
- **Desktop Attributed Cards**: Formats outbound messages with `[Message from agent '<sender>'] ` so Hermes Desktop renders them as attributed agent timeline notices rather than human user bubbles. Sender labels are stripped of quotes, brackets, control characters, lone UTF-16 surrogates and bidi/zero-width marks so the card header stays renderable. Lone UTF-16 surrogates in the body are replaced so the card stays UTF-8 encodable. Directive-looking text (`@file`, `@diff`, `@staged`, and a registered `@kind:value`) is neutralized in the header and the body; the readable value stays.
- **Multi-Hop Loop Guard & Rate Limiting**: Tracks hop depth (`[intercom from=<id> hop=<n>/<max> mode=<reply|fyi>]`) via `pre_llm_call` and transcript fallback — including the parent segment of a fresh post-compression continuation. A card counts when it leads the message, or when it starts its own line after a host queued-prompt marker. A card quoted after ordinary text does not count. Two sessions can exchange a bounded reply without an infinite ping-pong.
- **Deduplication**: within `dedup_window_seconds` (default 120; `0` disables) a repeat send is refused when the sender session, the `expect_reply` flag and the message body all match. While its identity is retained, a caller-supplied `request_id` names one operation: the same id with the same body replays an inject outcome or retries a gateway admission, and the same id with a different body is refused. Both the dedup window and the rate limit are per process and reset on restart. Per-target rate limiting counts distinct operations inside `burst_window_seconds`.

## Reliability boundaries and host API disclosure

The current `gateway_routing` owner takes precedence over stale `sessions.session_key`
metadata. Before sending, an indexed route must name the target or a verified compression
ancestor; a conflicting owner is not proof of delivery. WebSocket mount/submit addresses
the resolved physical session, not an old root embedded in its route key. Retries retain
the original target and envelope across compression, and never inherit a reset or branch.

Operation/retry state is process-local and bounded to 4096 entries. Finished entries, and
non-terminal gateway entries older than 120 seconds when no call is actively using them,
can be evicted under capacity pressure. After eviction or restart, reuse of an old request
id is not protected by this plugin's retained transport identity. The gateway applies its
own admission identity; do not interpret this cache as an exactly-once delivery service.
Known RPC refusal reasons release a new reservation; a storage/internal error after
submit stays `delivery_unknown`. A reason name alone is not proof of a pre-commit boundary:
the installed host can also emit `not_found` during post-commit publication after target
deletion. No duplicate execution was demonstrated for that case. Refusing a refresh does
not undo an earlier uncertain send.

An original ID prefix can stop resolving after compression, even while its operation is
retained (R7-D01, unresolved selector limitation). Recover with the exact
`target_session_id` from the first result, the same `request_id`, body and `expect_reply`.
Do not mint a new operation to recover an uncertain send. After eviction or restart,
inspect target history before resending; the exact-ID recovery also depends on retention.

Catalog Rule 9 remains an explicit review gate: this build uses private
`hermes_cli.gateway_runtime` / `hermes_cli.gateway_client` helpers and reads
`ctx._manager._cli_ref` / `ctx._manager.home_path`. A clean automated validator is not a
public-API-only compliance verdict. No Hermes core or Desktop markup is patched.

Discovery reads the same profile's `sessions`, `gateway_routing` and `messages` tables;
only the gateway-permission boolean is read from `config.yaml`. The discovery thread
pool and local loopback RPCs are runtime behavior, not third-party network services.
`0600` on the audit file is requested via `os.open`; effective Windows access is governed
by the directory's ACL, not POSIX permission bits. The minimum Hermes version is declared,
not independently qualified here; tests use the available 0.23.0-canary checkout.

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

## Python test prerequisites

The full suite requires a complete Hermes Agent checkout and its compatible Python test
environment, not just this plugin plus pytest. Qualified host source:
`21ae0e1238fdf466fa52a85a6ebf75d6899e49da` (0.23.0-canary). The tests import real
`hermes_cli.gateway_runtime` / `gateway_client` during collection. Do not replace them
with stub modules to manufacture a pass. From the plugin root, using bash:

```bash
HOST=/absolute/path/to/hermes-agent
PYTHONPATH="$HOST" HERMES_AGENT_REPO="$HOST" PYTHONDONTWRITEBYTECODE=1 \
  "$HOST/venv/Scripts/python.exe" -B -m pytest -q
# On Linux use the compatible native Linux environment, e.g. "$HOST/.venv/bin/python".
# Windows and Linux results do not qualify macOS or the declared >=0.22.5 floor.
```

## Desktop presentation (`desktop/plugin.js`)

Hermes Desktop shows every `[Message from agent '…']` delivery as its own native notice ("Message from X · show message"), and the expanded body starts with the intercom envelope. The desktop half ships so the package loads as a desktop plugin, and it renders nothing.

The Desktop Plugin SDK has no contribution that draws an incoming agent message (its areas are panes, pages, composer rows, transcript directives and tool-media renderers). Catalog rule 8 forbids reaching past the SDK into app markup, so this build does not. The message is still delivered and still visible; it uses the host's own note instead of a custom card.

Tests (Node >= 20, jsdom from a Hermes desktop checkout):

```bash
HERMES_DESKTOP_ROOT=<hermes-agent>/apps/desktop node --test tests/desktop/intercom-note.test.mjs
```

The fixture in `tests/desktop/native-fixture.mjs` reproduces the DOM of `AgentMessageNote` and `UserMessageText`. Test messages are generated by the real Python `store.format_intercom_message` (`HERMES_PYTHON` picks the interpreter).
