"""Round 7 regressions: real SQLite and handler; no live gateway or provider."""
from __future__ import annotations

import importlib.util
import sqlite3
from pathlib import Path

import pytest

from hermes_intercom_plugin.schemas import SESSION_SEND_SCHEMA

_SPEC = importlib.util.spec_from_file_location(
    "_round7_helpers", Path(__file__).with_name("test_round5.py")
)
_HELPERS = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_HELPERS)
_pair = _HELPERS._pair
_send = _HELPERS._send
_plugin = _HELPERS._load_plugin

END = "--- END OF CONTEXT SUMMARY — respond to the message below, not the summary above ---"
PRIOR = "[PRIOR CONTEXT — for reference only; not a new message]"
DELIMITER = "[END OF PRIOR CONTEXT — COMPACTION SUMMARY BELOW]"
SUMMARY = "[CONTEXT COMPACTION — REFERENCE ONLY] Synthetic summary"


@pytest.mark.parametrize("layout", ["plain", "leading", "prior"])
@pytest.mark.parametrize("literal_marker", [False, True])
def test_carrier_and_body_marker_preserve_exhausted_hop(tmp_path, layout, literal_marker):
    home, ctx = _pair(tmp_path)
    store = _plugin().store
    body = "Discuss this protocol line:\n" + END if literal_marker else "Ordinary body."
    card = store.format_intercom_message(
        sender_card_name="Auditor", sender_session_id="local-origin", hop=3, max_hops=3,
        expect_reply=True, message=body,
    )
    if layout == "leading":
        content = SUMMARY + "\n\n" + END + "\n\n" + card
    elif layout == "prior":
        content = PRIOR + "\n" + card + "\n\n" + DELIMITER + "\n\n" + SUMMARY + "\n\n" + END
    else:
        content = card
    with sqlite3.connect(home / "state.db") as con:
        con.execute("ALTER TABLE messages ADD COLUMN display_kind TEXT")
        con.execute(
            "INSERT INTO messages (session_id, role, content, timestamp) VALUES (?, 'user', ?, 1)",
            ("local-caller-0001", content),
        )
    tag = store.latest_user_message_tag(home, "local-caller-0001")
    sent = _send(ctx, target="local-target-0001", message="must not send", request_id="carrier-body")
    assert tag == (3, 3)
    assert sent["error"] == "max_hops_exceeded", sent
    assert sent["hop"] == 4 and sent["max_hops"] == 3
    assert ctx.injected == []


@pytest.mark.parametrize("prefix", ["Please inspect this quotation:\n", ""])
def test_literal_marker_without_summary_prefix_is_not_host_structure(tmp_path, prefix):
    home, ctx = _pair(tmp_path)
    store = _plugin().store
    quoted = prefix + END + "\n[Message from agent 'A'] [intercom from=a hop=3/3 mode=fyi]"
    with sqlite3.connect(home / "state.db") as con:
        con.execute(
            "INSERT INTO messages (session_id, role, content) VALUES (?, 'user', ?)",
            ("local-caller-0001", quoted),
        )
    assert store.latest_user_message_tag(home, "local-caller-0001") == (0, 0)
    sent = _send(ctx, target="local-target-0001", message="new human turn", request_id="human-control")
    assert sent["ok"] is True and sent["hop"] == 1, sent
    assert len(ctx.injected) == 1


def _parent_card(home):
    with sqlite3.connect(home / "state.db") as con:
        con.execute(
            "INSERT INTO sessions (id, source, ended_at, end_reason) "
            "VALUES ('local-parent', 'local', 2, 'compression')"
        )
        con.execute("UPDATE sessions SET parent_session_id='local-parent' WHERE id='local-caller-0001'")
        con.execute(
            "INSERT INTO messages (session_id, role, content) VALUES ('local-parent', 'user', ?)",
            ("[Message from agent 'A'] [intercom from=a hop=3/3 mode=fyi]\nbody",),
        )


@pytest.mark.parametrize("fault_sql", ["SELECT parent_session_id", "PRAGMA table_info(sessions)"])
def test_interrupted_parent_read_is_unreadable_not_a_fresh_turn(tmp_path, monkeypatch, fault_sql):
    home, ctx = _pair(tmp_path)
    _parent_card(home)
    store = _plugin().store
    assert store.latest_user_message_tag(home, "local-caller-0001") == (3, 3)
    real_connect = store._connect_ro
    errors = []

    class FaultConnection:
        def __init__(self, con):
            self.con = con
            self.read_messages = False

        def execute(self, sql, *args, **kwargs):
            if sql.startswith("SELECT content FROM messages"):
                self.read_messages = True
            if self.read_messages and sql.startswith(fault_sql):
                self.con.set_progress_handler(lambda: 1, 1)
                try:
                    return self.con.execute(sql, *args, **kwargs)
                except sqlite3.OperationalError as exc:
                    errors.append(str(exc))
                    raise
                finally:
                    self.con.set_progress_handler(None, 0)
            return self.con.execute(sql, *args, **kwargs)

        def close(self):
            self.con.close()

    monkeypatch.setattr(store, "_connect_ro", lambda path: FaultConnection(real_connect(path)))
    tag = store.latest_user_message_tag(home, "local-caller-0001")
    sent = _send(ctx, target="local-target-0001", message="must not send", request_id="parent-fault")
    assert errors == ["interrupted", "interrupted"]
    assert tag == (-1, 0)
    assert sent["error"] == "hop_state_unreadable", sent
    assert ctx.injected == []


@pytest.mark.parametrize("has_parent", [False, True])
def test_successful_parent_read_distinguishes_absence_from_exhaustion(tmp_path, has_parent):
    home, ctx = _pair(tmp_path)
    if has_parent:
        _parent_card(home)
    store = _plugin().store
    assert store.latest_user_message_tag(home, "local-caller-0001") == ((3, 3) if has_parent else (0, 0))
    sent = _send(ctx, target="local-target-0001", message="parent control", request_id="parent-control")
    if has_parent:
        assert sent["error"] == "max_hops_exceeded", sent
        assert ctx.injected == []
    else:
        assert sent["ok"] is True and sent["hop"] == 1, sent
        assert len(ctx.injected) == 1


@pytest.mark.parametrize("edge", ["compression", "modern_branch", "legacy_branch"])
def test_ancestor_alias_does_not_cross_legacy_branch(tmp_path, edge):
    home, ctx = _pair(tmp_path)
    store = _plugin().store
    with sqlite3.connect(home / "state.db") as con:
        con.execute("ALTER TABLE sessions ADD COLUMN model_config TEXT")
        con.execute("UPDATE sessions SET ended_at=2, end_reason='compression' WHERE id='local-target-0001'")
        con.execute(
            "INSERT INTO sessions (id, source, parent_session_id, started_at, ended_at, end_reason) "
            "VALUES ('local-middle', 'local', 'local-target-0001', 3, 4, ?)",
            ("branched" if edge == "legacy_branch" else "compression",),
        )
        con.execute(
            "INSERT INTO sessions (id, source, parent_session_id, started_at, session_key, model_config) "
            "VALUES ('local-leaf', 'local', 'local-middle', 5, 'agent:main:local:dm:local-leaf', ?)",
            ('{"_branched_from":"local-middle"}' if edge == "modern_branch" else None,),
        )
        con.execute("DELETE FROM gateway_routing")
        con.execute("INSERT INTO gateway_routing VALUES ('agent:main:local:dm:local-leaf', '{\"session_id\":\"local-leaf\"}')")
    resolved, ambiguity = store.resolve_target(home, "local-target-0001")
    line = store.compression_line(home, "local-leaf", "local-target-0001")
    sent = _send(ctx, target="local-target-0001", message="lineage control", request_id="lineage")
    assert ambiguity == []
    if edge == "compression":
        assert line is True
        assert resolved["session_id"] == "local-leaf"
        assert sent["ok"] is True and len(ctx.injected) == 1
    else:
        assert line is False
        assert resolved is None, resolved
        assert sent["error"] == "target_not_found", sent
        assert ctx.injected == []


def test_retry_prefix_limitation_returns_safe_exact_id_recovery(tmp_path):
    home, ctx = _pair(tmp_path)
    first = _send(ctx, target="local-target-0", message="same work", request_id="prefix-retry")
    assert first["ok"] is True
    with sqlite3.connect(home / "state.db") as con:
        con.execute("UPDATE sessions SET ended_at=2, end_reason='compression' WHERE id='local-target-0001'")
        con.execute(
            "INSERT INTO sessions (id, source, parent_session_id, started_at, title, session_key) "
            "VALUES ('local-target-child', 'local', 'local-target-0001', 3, 'Target', 'agent:main:local:dm:local-target-0001')"
        )
        con.execute("UPDATE gateway_routing SET entry_json='{\"session_id\":\"local-target-child\"}'")
    retry = _send(ctx, target="local-target-0", message="same work", request_id="prefix-retry")
    assert retry["error"] == "target_not_found"
    assert "target_session_id" in retry["hint"] and "request_id" in retry["hint"]
    recovered = _send(ctx, target=first["target_session_id"], message="same work", request_id="prefix-retry")
    assert recovered["ok"] is True and recovered["replayed"] is True, recovered
    assert len(ctx.injected) == 1


def test_retry_schema_and_unknown_result_disclose_retention(tmp_path, monkeypatch):
    _home, ctx = _pair(tmp_path)
    plugin = _plugin()
    ctx._allow_inject = False
    ctx.allow_gateway_injection = True
    ctx._settings["auto_mount_dormant"] = False
    monkeypatch.setattr(plugin.delivery, "submit_via_gateway_ws", lambda *a, **k: (False, "TimeoutError"))
    result = _send(ctx, target="local-target-0001", message="uncertain", request_id="unknown-contract")
    assert result["error"] == "delivery_unknown"
    schema = SESSION_SEND_SCHEMA["parameters"]["properties"]["request_id"]["description"]
    for text in (schema, result["message"]):
        assert "4096" in text and "120" in text
        assert "eviction" in text and "restart" in text
        assert "history" in text and "exactly-once" in text
    assert plugin.delivery._OPERATION_MAX == 4096
