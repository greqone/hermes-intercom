# Integration of the independent audit into ROUND7

Base: `3a9de27b94ec66ccb945727f732eeabf12909b72`.
Independent audit: `13d90902a4efba6b3eef5e65b193228373f88255` and
`884574dd260ae0fa3f8fdc0a3fe93661eaa805b5`.

The newer ROUND7 implementation is the base, not a wholesale replacement by the
older parallel audit. All 33 independent test cases were run against unmodified
ROUND7 first: 15 passed and 18 failed. Failures included contract differences,
not 18 independent product defects. The merged tests retain the independent
cases under `tests/test_integrated_audit.py` without replacing ROUND7 tests.

## Decisions

| Audit item | Decision |
| --- | --- |
| A01 reset alias | Keep ROUND7 refusal; independent control passes. |
| A02 real summary schema | Keep ROUND7 carrier/active-row handling, including literal delimiter repair. |
| A03 new conversation boundaries | Keep ROUND7 boundaries; exclude tool/subagent parents from hop traversal. |
| A04 operation capacity | Keep ROUND7 active-claim protection and explicit 4096/120s retention; do not restore indefinite unknown retention. |
| A05 physical compression lineage | Adopt physical traversal before discovery deduplication; exclude tool, branch/reset/delegate edges; retain ROUND7 automatically closed/resumable continuations. Bound traversal to 256 identities. |
| A06 empty route index | Keep ROUND7 fail-closed `route_unverified`. An unconfirmed route does not gain a second fallback attempt merely to satisfy an older test. |
| A07 authoritative routing | Keep ROUND7 routing index precedence. |
| A08 receipt reference | Keep the documented session-id string projection, not the older dictionary-shaped output. |
| A09 shared deadline | Adopt zero remaining budget and refuse starting a new mount/WS submit after expiry. This does not impose a hard wall-clock limit on an already running host operation. |
| A10 receipt refresh | Keep ROUND7 monotonic receipts. A terminal cancellation must not regress to unknown or lose its outcome. |
| A11 current user turn | Keep an explicit scoped zero-hop observation so stale DB rows cannot restore an old budget. Missing/empty hook input and pure synthetic summaries cannot establish a new turn. |
| A12 invalid settings | Keep documented ROUND7 fallback defaults. Clamping every invalid value to its minimum is not uniformly safer: reducing a 600s rate window to 1s relaxes protection. |
| A13 bucket cleanup | Keep ROUND7 cleanup. |
| A14 routing read failures | Keep ROUND7 claim release, sticky gateway transport and `store_unavailable` result at the delivery-stage boundary. |
| A15 deep hop recovery | Follow up to 256 verified compression identities. Cycles and bound exhaustion fail closed with `hop_state_unreadable`, never a fresh hop budget. |
| A16 profile isolation | Keep ROUND7 scoped operations/receipts. |
| A17 request IDs | Adopt rejection of whitespace, control characters and lone surrogates; do not silently trim or rewrite identifiers. Empty/omitted IDs still request a generated ID. |
| A18 route ownership | Keep ROUND7 ownership and compression-boundary checks. |
| A19 YAML compatibility | Prefer public ruamel.yaml safe parsing with YAML 1.1; fall back to PyYAML only when ruamel is absent. Invalid configuration still fails closed. |
| 884574d test timing | Adopt a deterministic clock for the rate-limiting test, retaining ROUND7's documented 600s fallback. |

Five additional controls cover missing/synthetic hook input and fail-closed hop
cycles/bounds. Tests requiring old behavior were renamed/updated to assert the
explicit contract decisions above, rather than weakening safety assertions.

## Remaining boundaries

Rule 9 remains unresolved: private gateway helpers and manager reads are still
used. This integration is not catalog approval. R7-D01 prefix retry limitation
remains documented; use the first result's exact target ID and unchanged request
ID/body/reply flag. No exactly-once, durable replay, macOS, live gateway or live
cross-surface qualification is implied. Video presentation is separate evidence.

Final validation results and immutable archive hashes are recorded externally
after the final merge commit, so this document does not claim results for itself.
