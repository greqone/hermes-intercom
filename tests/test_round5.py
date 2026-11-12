"""Round 5 defects, written to fail on 3b2cf22.

Each test states the behavior the repair must have. Exit codes here are the
opposite of the defect probes: a failure means the defect is still present.
Nothing in this file talks to a live gateway.
"""
from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

import importlib.util
import sys
import pytest


_SPEC = importlib.util.spec_from_file_location(
    "_intercom_helpers", Path(__file__).resolve().parent / "test_intercom.py"
)
_HELPERS = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_HELPERS)
RecordingContext = _HELPERS.RecordingContext
_create_state_db = _HELPERS._create_state_db
_load_plugin = _HELPERS._load_plugin


def _home(tmp_path: Path) -> Path:
    home = tmp_path / "hermes_home"
    home.mkdir()
    _create_state_db(home / "state.db")
    return home


def _insert(home: Path, rows: list[tuple]) -> None:
    con = sqlite3.connect(str(home / "state.db"))
    for row in rows:
        con.execute(
            "INSERT INTO sessions (id, source, model, parent_session_id, started_at, "
            "ended_at, end_reason, message_count, title, archived, session_key, last_activity_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            row,
        )
    con.commit()
    con.close()


def _send(ctx: RecordingContext, **kwargs) -> dict:
    plugin = _load_plugin()
    caller = str(kwargs.pop("caller", "local-caller-0001"))
    raw = plugin.delivery.tool_session_send(kwargs, ctx=ctx, session_id=caller)
    return json.loads(raw)


def _pair(tmp_path: Path):
    home = _home(tmp_path)
    _insert(home, [
        ("local-caller-0001", "local", "m", None, 1.0, None, None, 1, "Caller", 0,
         "agent:main:local:dm:local-caller-0001", 9.0),
        ("local-target-0001", "local", "m", None, 1.0, None, None, 1, "Target", 0,
         "agent:main:local:dm:local-target-0001", 8.0),
    ])
    con = sqlite3.connect(str(home / "state.db"))
    con.execute(
        "INSERT INTO gateway_routing (session_key, entry_json) VALUES (?, ?)",
        ("agent:main:local:dm:local-target-0001", '{"session_id": "local-target-0001"}'),
    )
    con.commit()
    con.close()
    return home, RecordingContext(home)


# --- R5-D01: capacity must not throw away work that is still live ----------


# --- R5-D02: a retry finds the operation it already sent -------------------

def test_r5_d02_compression_rotation_replays_the_same_operation(tmp_path) -> None:
    home, ctx = _pair(tmp_path)
    first = _send(ctx, target="local-target-0001", message="hello", request_id="same-op")
    assert first["ok"] is True

    con = sqlite3.connect(str(home / "state.db"))
    con.execute("UPDATE sessions SET ended_at=10, end_reason='compression' WHERE id='local-target-0001'")
    con.execute(
        "INSERT INTO sessions (id, source, model, parent_session_id, started_at, ended_at, end_reason, "
        "message_count, title, archived, session_key, last_activity_at) "
        "VALUES ('local-target-child', 'local', 'm', 'local-target-0001', 11.0, NULL, NULL, 1, "
        "'Target', 0, 'agent:main:local:dm:local-target-0001', 12.0)"
    )
    con.commit()
    con.close()

    retry = _send(ctx, target="local-target-0001", message="hello", request_id="same-op")
    assert retry["ok"] is True and retry.get("replayed") is True
    assert len(ctx.injected) == 1

    by_key = _send(
        ctx, target="agent:main:local:dm:local-target-0001", message="hello", request_id="same-op"
    )
    assert by_key["ok"] is True and by_key.get("replayed") is True, by_key
    by_title = _send(ctx, target="Target", message="hello", request_id="same-op")
    assert by_title["ok"] is True and by_title.get("replayed") is True, by_title
    assert len(ctx.injected) == 1


def test_r6_a_a_request_id_does_not_replay_across_two_homes(tmp_path) -> None:
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    home_a, ctx_a = _pair(tmp_path / "a")
    home_b, ctx_b = _pair(tmp_path / "b")
    first = _send(ctx_a, target="local-target-0001", message="hello", request_id="shared-id")
    assert first["ok"] is True
    second = _send(ctx_b, target="local-target-0001", message="hello", request_id="shared-id")
    assert second.get("replayed") is not True, second
    assert len(ctx_b.injected) == 1


def test_r5_d02_a_later_hop_does_not_hide_an_accepted_result(tmp_path) -> None:
    home, ctx = _pair(tmp_path)
    first = _send(ctx, target="local-target-0001", message="hello", request_id="same-op")
    assert first["ok"] is True
    plugin = _load_plugin()
    plugin.delivery._TURN_HOP[(str(home), "local-caller-0001")] = (4, 3)

    retry = _send(ctx, target="local-target-0001", message="hello", request_id="same-op")
    assert retry["ok"] is True and retry.get("replayed") is True
    assert len(ctx.injected) == 1


# --- R5-D03: a read error after the claim must release it ------------------

def test_r5_d03_a_routing_read_error_releases_the_unsent_claim(tmp_path, monkeypatch) -> None:
    home, ctx = _pair(tmp_path)
    plugin = _load_plugin()
    real = plugin.store._connect_ro
    calls = {"n": 0}

    def _flaky(path):
        calls["n"] += 1
        if calls["n"] > 2:
            raise sqlite3.OperationalError("fixture: database became unavailable after claim")
        return real(path)

    monkeypatch.setattr(plugin.store, "_connect_ro", _flaky)
    try:
        result = _send(ctx, target="local-target-0001", message="hello", request_id="db-error")
    except sqlite3.OperationalError:
        result = {"ok": False, "error": "escaped"}
    assert result["ok"] is False and result["error"] == "store_unavailable"
    assert "db-error" not in plugin.delivery._OPERATIONS

    monkeypatch.undo()
    again = _send(ctx, target="local-target-0001", message="hello", request_id="db-error")
    assert again["ok"] is True
    assert len(ctx.injected) == 1


# --- R5-D04: an empty routing index is not proof of a route ----------------


def test_r5_d04_an_empty_routing_index_is_not_a_verified_route(tmp_path) -> None:
    home, ctx = _pair(tmp_path)
    con = sqlite3.connect(str(home / "state.db"))
    con.execute("DELETE FROM gateway_routing")
    con.commit()
    con.close()

    result = _send(ctx, target="local-target-0001", message="hello")
    assert result["ok"] is False
    assert result["error"] == "route_unverified"
    assert ctx.injected == []


# --- R5-D05: one receipt, not a collage of three maps ----------------------


def test_r6_a_a_reset_then_a_compression_does_not_replay_the_old_session(tmp_path) -> None:
    home, ctx = _pair(tmp_path)
    key = "agent:main:local:dm:local-target-0001"
    first = _send(ctx, target="local-target-0001", message="hello", request_id="reset-then-compress")
    assert first["ok"] is True
    con = sqlite3.connect(str(home / "state.db"))
    con.execute("UPDATE sessions SET ended_at=10, end_reason='session_reset' WHERE id='local-target-0001'")
    con.execute(
        "INSERT INTO sessions (id, source, model, parent_session_id, started_at, ended_at, end_reason, "
        "message_count, title, archived, session_key, last_activity_at) "
        "VALUES ('local-target-next', 'local', 'm', 'local-target-0001', 11.0, 13.0, 'compression', 1, "
        "'Next', 0, ?, 12.0)",
        (key,),
    )
    con.execute(
        "INSERT INTO sessions (id, source, model, parent_session_id, started_at, ended_at, end_reason, "
        "message_count, title, archived, session_key, last_activity_at) "
        "VALUES ('local-target-grandchild', 'local', 'm', 'local-target-next', 14.0, NULL, NULL, 1, "
        "'Next', 0, ?, 15.0)",
        (key,),
    )
    con.commit()
    con.close()
    retry = _send(ctx, target=key, message="hello", request_id="reset-then-compress")
    assert retry.get("replayed") is not True, retry
    assert retry.get("target_session_id") != "local-target-0001"


def test_r6_a_real_root_key_does_not_cross_a_boundary(tmp_path) -> None:
    home = _home(tmp_path)
    key = "agent:main:local:dm:local-a"
    _insert(home, [
        ("local-a", "local", "m", None, 1.0, 2.0, "compression", 1, "A", 0, key, 2.0),
        ("local-b", "local", "m", "local-a", 3.0, 4.0, "session_reset", 1, "B", 0, key, 4.0),
        ("local-c", "local", "m", "local-b", 5.0, 6.0, "compression", 1, "C", 0, key, 6.0),
        ("local-d", "local", "m", "local-c", 7.0, None, None, 1, "D", 0, key, 8.0),
    ])
    plugin = _load_plugin()
    resolved, ambiguous = plugin.store.resolve_target(home, "local-a", caller_session_id="local-caller-0001")
    assert resolved is None and ambiguous == [], (resolved, ambiguous)


def test_r6_a_literal_nested_summary_quote_has_no_leading_live_envelope(tmp_path) -> None:
    home = _home(tmp_path)
    plugin = _load_plugin()
    card = "[Message from agent 'A'] [intercom from=local-a hop=3/3 mode=fyi]\nbody"
    inner = (
        "[PRIOR CONTEXT — for reference only; not a new message]\n" + card + "\n\n"
        "[END OF PRIOR CONTEXT — COMPACTION SUMMARY BELOW]\n\n"
        "[CONTEXT COMPACTION — REFERENCE ONLY] older\n\n"
        "--- END OF CONTEXT SUMMARY — respond to the message below, not the summary above ---"
    )
    nested = (
        "[CONTEXT COMPACTION — REFERENCE ONLY] " + inner + "\n\n"
        "--- END OF CONTEXT SUMMARY — respond to the message below, not the summary above ---\n\n"
        + card
    )
    con = sqlite3.connect(str(home / "state.db"))
    con.execute("ALTER TABLE messages ADD COLUMN display_kind TEXT")
    con.execute(
        "INSERT INTO messages (session_id, role, content, timestamp, display_kind) "
        "VALUES ('local-caller-0001', 'user', ?, 1.0, NULL)",
        (nested,),
    )
    con.commit()
    con.close()
    # This literal nested-summary quotation has no leading live envelope.
    # Its abbreviated prefix and absent host metadata do not establish a host carrier projection.
    assert plugin.store.latest_user_message_tag(home, "local-caller-0001") == (0, 0)


def test_a_retry_of_a_minted_request_id_does_not_deliver_twice(tmp_path, monkeypatch) -> None:
    _home, ctx = _pair(tmp_path)
    plugin = _load_plugin()
    first = _send(ctx, target="local-target-0001", message="hello")
    assert first["ok"] is True, first
    minted = first["request_id"]
    retry = _send(ctx, target="local-target-0001", message="hello", request_id=minted)
    assert retry.get("replayed") is True, retry
    assert len(ctx.injected) == 1


# --- R5-S01: a reset ends the old conversation -----------------------------

def test_r5_s01_idle_and_daily_endings_are_not_targets(tmp_path) -> None:
    home = _home(tmp_path)
    _insert(home, [
        ("local-caller-0001", "local", "m", None, 1.0, None, None, 1, "Caller", 0,
         "agent:main:local:dm:local-caller-0001", 9.0),
        ("local-old-idle", "local", "m", None, 1.0, 2.0, "idle", 1, "Old", 0,
         "agent:main:local:dm:local-old-idle", 2.0),
        ("local-old-daily", "local", "m", None, 1.0, 2.0, "daily", 1, "Old", 0,
         "agent:main:local:dm:local-old-daily", 2.0),
    ])
    plugin = _load_plugin()
    for target in ("local-old-idle", "local-old-daily"):
        resolved, ambiguous = plugin.store.resolve_target(home, target, caller_session_id="local-caller-0001")
        assert resolved is None and ambiguous == [], target


# --- R5-S02: the host persists one summary column, not two -----------------

def test_r5_s02_a_summary_row_does_not_reset_the_hop_budget(tmp_path) -> None:
    home, ctx = _pair(tmp_path)
    card = "[Message from agent 'A'] [intercom from=local-a hop=3/3 mode=fyi]\nbody"
    con = sqlite3.connect(str(home / "state.db"))
    con.execute("ALTER TABLE messages ADD COLUMN _compressed_summary INTEGER DEFAULT 0")
    con.execute("ALTER TABLE messages ADD COLUMN display_kind TEXT")
    con.execute(
        "INSERT INTO messages (session_id, role, content, timestamp, _compressed_summary) "
        "VALUES ('local-caller-0001', 'user', ?, 1.0, 0)",
        (card,),
    )
    con.execute(
        "INSERT INTO messages (session_id, role, content, timestamp, _compressed_summary, display_kind) "
        "VALUES ('local-caller-0001', 'user', 'summary of the earlier turns', 2.0, 1, 'hidden')"
    )
    con.commit()
    con.close()

    result = _send(ctx, target="local-target-0001", message="hello")
    assert result["ok"] is False and result["error"] == "max_hops_exceeded"


def test_r5_s02_an_inactive_row_cannot_hide_the_active_card(tmp_path) -> None:
    home = _home(tmp_path)
    card = "[Message from agent 'A'] [intercom from=local-a hop=3/3 mode=fyi]\nbody"
    con = sqlite3.connect(str(home / "state.db"))
    con.execute("ALTER TABLE messages ADD COLUMN active INTEGER DEFAULT 1")
    con.execute(
        "INSERT INTO messages (session_id, role, content, timestamp, active) VALUES (?,?,?,?,?)",
        ("local-caller-0001", "user", "Current genuine human request", 1.0, 1),
    )
    con.execute(
        "INSERT INTO messages (session_id, role, content, timestamp, active) VALUES (?,?,?,?,?)",
        ("local-caller-0001", "user", card, 2.0, 0),
    )
    con.commit()
    con.close()

    plugin = _load_plugin()
    assert plugin.store.latest_user_message_tag(home, "local-caller-0001") == (0, 0)


def test_r5_a_compressed_ancestor_does_not_resolve_to_an_archived_child(tmp_path) -> None:
    home = _home(tmp_path)
    _insert(home, [
        ("local-old", "local", "m", None, 1.0, 2.0, "compression", 1, "Old", 0,
         "agent:main:local:dm:local-old", 2.0),
        ("local-child", "local", "m", "local-old", 3.0, 4.0, None, 1, "Child", 1,
         "agent:main:local:dm:local-child", 4.0),
    ])
    plugin = _load_plugin()
    resolved, ambiguous = plugin.store.resolve_target(home, "local-old", caller_session_id="local-caller-0001")
    assert resolved is None and ambiguous == []


def test_r5_b03_an_auto_closed_continuation_still_resolves(tmp_path) -> None:
    home = _home(tmp_path)
    _insert(home, [
        ("local-old", "local", "m", None, 1.0, 2.0, "compression", 1, "Old", 0,
         "agent:main:local:dm:local-old", 2.0),
        ("local-child", "local", "m", "local-old", 3.0, 4.0, "tui_shutdown", 1, "Child", 0,
         "agent:main:local:dm:local-child", 4.0),
    ])
    plugin = _load_plugin()
    resolved, _ambiguous = plugin.store.resolve_target(home, "local-old", caller_session_id="local-caller-0001")
    assert resolved is not None and resolved["session_id"] == "local-child"


def test_r5_b02_another_session_does_not_inherit_the_replay(tmp_path) -> None:
    _home_unused, ctx = _pair(tmp_path)
    first = _send(ctx, target="local-target-0001", message="hello", request_id="shared-id")
    assert first["ok"] is True

    other = _send(ctx, target="local-target-0001", message="hello", request_id="shared-id",
                  caller="local-caller-0002")
    assert other.get("replayed") is not True
    assert other["error"] == "request_conflict"


def test_r4_d06_a_refused_send_forgets_its_bucket_horizon(tmp_path) -> None:
    _home_unused, ctx = _pair(tmp_path)
    ctx._allow_inject = False
    plugin = _load_plugin()
    result = _send(ctx, target="local-target-0001", message="nope")
    assert result["ok"] is False
    assert plugin.delivery._BUCKET_HORIZON == {}


def test_r5_s01_a_reset_rows_title_is_not_a_target(tmp_path) -> None:
    home = _home(tmp_path)
    _insert(home, [
        ("local-caller-0001", "local", "m", None, 1.0, None, None, 1, "Caller", 0,
         "agent:main:local:dm:local-caller-0001", 9.0),
        ("local-old", "local", "m", None, 1.0, 2.0, "session_reset", 1, "Historical topic", 0, "", 2.0),
        ("local-new", "local", "m", None, 3.0, None, None, 1, "New topic", 0, "", 4.0),
    ])
    con = sqlite3.connect(str(home / "state.db"))
    con.execute(
        "INSERT INTO gateway_routing (session_key, entry_json) VALUES (?, ?)",
        ("agent:main:telegram:dm:chat", '{"session_id": "local-new"}'),
    )
    con.commit()
    con.close()
    plugin = _load_plugin()
    resolved, ambiguous = plugin.store.resolve_target(home, "Historical topic", caller_session_id="local-caller-0001")
    assert resolved is None and ambiguous == []


def test_r5_s01_a_reset_stops_the_compression_line(tmp_path) -> None:
    home = _home(tmp_path)
    key = "agent:main:local:dm:local-root"
    _insert(home, [
        ("local-a", "local", "m", None, 1.0, 2.0, "compression", 1, "A", 0, key, 2.0),
        ("local-b", "local", "m", "local-a", 3.0, 4.0, "session_reset", 1, "B", 0, key, 4.0),
        ("local-c", "local", "m", "local-b", 5.0, 6.0, "compression", 1, "C", 0, key, 6.0),
        ("local-d", "local", "m", "local-c", 7.0, None, None, 1, "D", 0, key, 8.0),
    ])
    plugin = _load_plugin()
    resolved, ambiguous = plugin.store.resolve_target(home, "local-a", caller_session_id="local-caller-0001")
    assert resolved is None and ambiguous == []


def test_r5_s01_a_reset_rows_prefix_does_not_reach_its_successor(tmp_path) -> None:
    home = _home(tmp_path)
    key = "agent:main:local:dm:local-oldroot"
    _insert(home, [
        ("local-caller-0001", "local", "m", None, 1.0, None, None, 1, "Caller", 0,
         "agent:main:local:dm:local-caller-0001", 9.0),
        ("local-oldroot", "local", "m", None, 1.0, 2.0, "session_reset", 1, "Old", 0, key, 2.0),
        ("local-newlive", "local", "m", None, 3.0, None, None, 1, "New", 0, key, 4.0),
    ])
    plugin = _load_plugin()
    resolved, ambiguous = plugin.store.resolve_target(home, "local-oldro", caller_session_id="local-caller-0001")
    assert resolved is None and ambiguous == []


# The host's boundary set, copied from hermes_state_common.py. A compression line must not
# cross any of them. session_reset, idle and daily are already covered above.
_HOST_BOUNDARY_REASONS = ("session_switch", "suspended", "resume_pending_expired", "new_session")


@pytest.mark.parametrize("reason", _HOST_BOUNDARY_REASONS)
def test_r6_a_host_boundary_reason_stops_the_compression_line(tmp_path, reason) -> None:
    home = _home(tmp_path)
    key = "agent:main:local:dm:local-root"
    _insert(home, [
        ("local-a", "local", "m", None, 1.0, 2.0, "compression", 1, "A", 0, key, 2.0),
        ("local-b", "local", "m", "local-a", 3.0, 4.0, reason, 1, "B", 0, key, 4.0),
        ("local-c", "local", "m", "local-b", 5.0, 6.0, "compression", 1, "C", 0, key, 6.0),
        ("local-d", "local", "m", "local-c", 7.0, None, None, 1, "D", 0, key, 8.0),
    ])
    plugin = _load_plugin()
    resolved, ambiguous = plugin.store.resolve_target(home, "local-a", caller_session_id="local-caller-0001")
    assert resolved is None and ambiguous == [], reason


def test_r6_a_shared_key_does_not_cross_a_reset(tmp_path) -> None:
    home = _home(tmp_path)
    key = "agent:main:local:dm:local-root"
    _insert(home, [
        ("local-old", "local", "m", None, 1.0, 2.0, "session_reset", 1, "Old", 0, key, 2.0),
        ("local-new", "local", "m", None, 3.0, None, None, 1, "New", 0, key, 4.0),
    ])
    plugin = _load_plugin()
    resolved, ambiguous = plugin.store.resolve_target(home, key, caller_session_id="local-caller-0001")
    assert resolved is not None and resolved["session_id"] == "local-new", (resolved, ambiguous)


def test_r6_a_merged_carrier_keeps_the_exhausted_hop(tmp_path) -> None:
    home, ctx = _pair(tmp_path)
    card = "[Message from agent 'A'] [intercom from=local-a hop=3/3 mode=fyi]\nbody"
    prior = (
        "[PRIOR CONTEXT — for reference only; not a new message]\n" + card
        + "\n\n[END OF PRIOR CONTEXT — COMPACTION SUMMARY BELOW]\n\n"
        "[CONTEXT COMPACTION — REFERENCE ONLY] earlier turns\n\n"
        "--- END OF CONTEXT SUMMARY — respond to the message below, not the summary above ---"
    )
    leading = (
        "[CONTEXT COMPACTION — REFERENCE ONLY] earlier turns\n\n"
        "--- END OF CONTEXT SUMMARY — respond to the message below, not the summary above ---\n\n"
        + card
    )
    con = sqlite3.connect(str(home / "state.db"))
    con.execute("ALTER TABLE messages ADD COLUMN _compressed_summary INTEGER DEFAULT 0")
    con.execute("ALTER TABLE messages ADD COLUMN display_kind TEXT")
    con.executemany(
        "INSERT INTO messages (session_id, role, content, timestamp, _compressed_summary, display_kind) "
        "VALUES (?, 'user', ?, ?, 1, NULL)",
        [("local-caller-0001", prior, 1.0), ("local-caller-0002", leading, 2.0)],
    )
    con.commit()
    con.close()
    result = _send(ctx, target="local-target-0001", message="hello", caller="local-caller-0001")
    assert result["ok"] is False and result["error"] == "max_hops_exceeded", result
    again = _send(ctx, target="local-target-0001", message="hello", caller="local-caller-0002")
    assert again["ok"] is False and again["error"] == "max_hops_exceeded", again


def test_r6_a_genuine_ask_after_the_summary_renews_the_budget(tmp_path) -> None:
    home, ctx = _pair(tmp_path)
    card = "[Message from agent 'A'] [intercom from=local-a hop=3/3 mode=fyi]\nbody"
    merged = (
        "[PRIOR CONTEXT — for reference only; not a new message]\nPlease send a fresh update.\n\n"
        "[END OF PRIOR CONTEXT — COMPACTION SUMMARY BELOW]\n\n"
        "[CONTEXT COMPACTION — REFERENCE ONLY] " + card + "\n\n"
        "--- END OF CONTEXT SUMMARY — respond to the message below, not the summary above ---"
    )
    con = sqlite3.connect(str(home / "state.db"))
    con.execute("ALTER TABLE messages ADD COLUMN _compressed_summary INTEGER DEFAULT 0")
    con.execute("ALTER TABLE messages ADD COLUMN display_kind TEXT")
    con.execute(
        "INSERT INTO messages (session_id, role, content, timestamp, _compressed_summary, display_kind) "
        "VALUES ('local-caller-0001', 'user', ?, 1.0, 1, NULL)",
        (merged,),
    )
    con.commit()
    con.close()
    result = _send(ctx, target="local-target-0001", message="hello")
    assert result["ok"] is True and result["hop"] == 1, result


def test_r6_an_inflight_restatement_keeps_the_exhausted_hop(tmp_path) -> None:
    home, ctx = _pair(tmp_path)
    card = "[Message from agent 'A'] [intercom from=local-a hop=3/3 mode=fyi]\nbody"
    restated = (
        "[STILL IN PROGRESS — this is the active request, restated after the "
        "compaction boundary because it was not finished yet. Continue it; do not "
        "start over.]\n" + card
    )
    con = sqlite3.connect(str(home / "state.db"))
    con.execute(
        "INSERT INTO messages (session_id, role, content, timestamp) VALUES (?, 'user', ?, ?)",
        ("local-caller-0001", restated, 1.0),
    )
    con.commit()
    con.close()
    result = _send(ctx, target="local-target-0001", message="hello")
    assert result["ok"] is False and result["error"] == "max_hops_exceeded", result
