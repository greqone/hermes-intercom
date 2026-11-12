# Public API review follow-up

This change follows teknium1's review of catalog PR #153885 on 2026-11-12.
Baseline: `0d31c9cd01bfeb962e302d8f87941695c155a2e5`. That immutable candidate
and all previous audit histories remain ancestors; no history was rewritten.

## Four requested changes

1. Delivery exclusively uses `ctx.inject_message(..., session_key=...)`.
   Private gateway discovery, resume/detach, prompt submission, receipt handling,
   home overrides and the discovery executor are removed. Unverified routes
   return `route_unverified`; host refusals return `injection_refused`.
2. Profile resolution uses public `hermes_constants.get_hermes_home()`.
   `ctx.session_surface()` refuses CLI and unknown surfaces without reading a
   manager. Because that API cannot distinguish gateway-backed CLI, those turns
   are also conservatively refused. Discovery remains available.
3. Host config files are not parsed. Settings use `ctx.get_config`; the public
   injection API decides permission. Obsolete mounting settings are removed.
4. README, tool schemas and the bundled skill disclose shared-gateway title
   visibility and cross-user turn initiation, recommending owner-only platforms
   for the `intercom` toolset.

The manifest and catalog description must be updated together with the source
pin so neither claims removed behavior. Historical audit integration decisions
in INTEGRATION.md describe the baseline, not this reduced public API contract.

## Retained and repaired safeguards

Hop limits and carrier parsing, strict request IDs, fail-closed route ownership,
deduplication, bounded rate history, active operation claims and audit rotation
remain. Two previously disclosed compression defects are repaired: retry sender
identity follows verified compression, and route/retry ancestry uses the shared
256-identity bound instead of six steps. Reset/branch/cycle/bound controls verify
that an unrelated conversation cannot inherit an operation.

The public API returns a scheduling boolean, not a durable receipt. There is no
new exactly-once claim. The original-prefix selector limitation R7-D01 remains.
The removed WebSocket receipt state is not reinterpreted as a public API result.

## Test migration

Existing applicable store, hop, identity, quota and real PluginManager tests stay
in the suite. Unit doubles emulate the host's profile scope using test-only
public home scope helpers; they do not replace production `resolve_home`.
The real PluginManager tests use the actual registry, isolated profiles and a
test gateway injector. No live user session, active core or global config changes.

`tests/test_public_api.py` adds positive/negative surface and permission controls,
proof that refused/unrouted targets have no private fallback, real public home
scoping, compression retries and boundaries, concurrent retry exclusion, and a
runtime source check against reintroducing private delivery calls. Tests of
removed private transport/receipts are retired, not counted as passing. Their
original source remains available at the baseline SHA. Specific retired test
functions are listed below; relevant public behaviors have replacement coverage.

- `test_inline_audit.py::test_active_gateway_retry_is_not_evicted_by_age`
- `test_inline_audit.py::test_gateway_retry_across_compression_keeps_original_operation`
- `test_inline_audit.py::test_only_proven_precommit_rpc_refusals_release_reservation`
- `test_inline_audit.py::test_refused_gateway_retry_releases_inflight`
- `test_inline_audit.py::test_refused_refresh_cannot_erase_an_earlier_uncertain_delivery`
- `test_inline_audit.py::test_route_mount_names_reset_successor_not_embedded_old_root`
- `test_integrated_audit.py::test_gateway_permission_uses_host_yaml_when_pyyaml_is_absent`
- `test_integrated_audit.py::test_gateway_receipt_is_profile_scoped_for_the_same_id`
- `test_integrated_audit.py::test_no_submit_starts_after_the_shared_mount_deadline`
- `test_integrated_audit.py::test_receipt_reference_preserves_round7_session_id_contract`
- `test_integrated_audit.py::test_terminal_receipt_does_not_regress_on_retry`
- `test_integrated_audit.py::test_unknown_gateway_retry_cannot_lose_its_transport_to_routing_read_failure`
- `test_integrated_audit.py::test_unknown_operations_are_bounded_without_forgetting_possible_delivery`
- `test_intercom.py::test_a_second_call_without_request_id_is_not_the_same_operation`
- `test_intercom.py::test_classic_cli_send_does_not_land_in_the_senders_repl`
- `test_intercom.py::test_cli_surface_without_a_classic_repl_still_uses_the_gateway_injector`
- `test_intercom.py::test_dormant_local_session_auto_mounts_then_injects`
- `test_intercom.py::test_external_process_ws_submit_fallback_respects_injection_gate`
- `test_intercom.py::test_mount_dormant_local_session_tolerates_detach_error`
- `test_intercom.py::test_pre_submit_ws_timeout_rolls_back_dedup_while_post_submit_keeps_it`
- `test_intercom.py::test_unrouted_local_dm_target_mounts_before_inject_message`
- `test_intercom.py::test_ws_submit_classifies_pre_submit_failures_and_keeps_admission_receipts`
- `test_intercom.py::test_ws_submit_runs_even_when_auto_mount_dormant_is_false`
- `test_intercom.py::test_ws_submit_works_from_inside_a_running_event_loop`
- `test_regression_phase2.py::test_d01_unknown_outcome_returns_the_request_id`
- `test_regression_phase2.py::test_f05_mount_requires_the_gateway_grant`
- `test_regression_phase2.py::test_f14_manager_home_beats_the_ambient_env`
- `test_regression_phase2.py::test_f19_terminal_receipt_is_not_reported_as_admitted`
- `test_regression_phase2.py::test_f20_register_shuts_the_discovery_pool_on_unload`
- `test_regression_phase2.py::test_r3_gateway_retry_replays_the_accepted_result`
- `test_regression_phase2.py::test_r3_unknown_delivery_can_be_retried`
- `test_regression_phase2.py::test_r4_confirmed_gateway_send_survives_a_refused_retry`
- `test_regression_phase2.py::test_r4_evicting_an_operation_forgets_its_receipt`
- `test_regression_phase2.py::test_r4_receipt_keeps_outcome_and_reference`
- `test_regression_phase2.py::test_r4_retry_keeps_the_first_envelope_after_a_rename`
- `test_regression_phase2.py::test_r4_unknown_retry_stays_on_the_gateway_when_inject_appears`
- `test_round5.py::test_a_gateway_retry_that_loses_its_route_does_not_stay_in_flight`
- `test_round5.py::test_queued_gateway_sends_fill_the_operation_table`
- `test_round5.py::test_r5_a02_a_host_ref_object_stays_a_session_id`
- `test_round5.py::test_r5_d01_full_buffer_refuses_new_work_instead_of_evicting_unknown`
- `test_round5.py::test_r5_d01_unknown_results_cannot_grow_past_the_cap`
- `test_round5.py::test_r5_d03_a_retry_after_an_unknown_gateway_submit_is_not_stuck_in_flight`
- `test_round5.py::test_r5_d05_a_queued_receipt_does_not_inherit_a_cancelled_outcome`
- `test_round5.py::test_r5_d05_b_a_late_queued_reply_does_not_keep_the_terminal_outcome`
- `test_round5.py::test_r6_a_a_receipt_does_not_cross_two_homes`
- `test_round5.py::test_r6_a_gateway_retry_by_prefix_does_not_submit_again`
- `test_round5.py::test_r6_a_gateway_retry_replays_before_the_hop_check`
- `test_round5.py::test_r6_a_late_queued_reply_does_not_erase_a_started_receipt`
- `test_round5.py::test_r6_a_late_queued_reply_does_not_erase_a_terminal_receipt`
- `test_round5.py::test_r6_a_prefix_retry_does_not_replay_a_different_session`
- `test_round5.py::test_r6_a_queued_gateway_send_is_not_evicted`
- `test_round5.py::test_r6_a_retry_after_a_reset_does_not_replay_the_old_session`
- `test_round5.py::test_r6_a_retry_by_a_title_fragment_or_other_case_does_not_submit_again`
- `test_round5.py::test_r6_a_shorter_exact_id_does_not_replay_a_longer_session`
- `test_round5.py::test_r6_a_title_equal_to_another_sessions_id_does_not_replay_it`
- `test_round5.py::test_r6_an_unknown_receipt_is_reported_as_unknown`
- `test_round5.py::test_r6_two_gateway_retries_do_not_both_submit`
- `test_round7.py::test_retry_schema_and_unknown_result_disclose_retention`
