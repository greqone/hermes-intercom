"""Desired-behavior regressions for the Phase 2 repairs.

Each test names the finding it closes and asserts the behavior the repaired
candidate must have. They fail on the baseline tree (4e97299) and pass once
the matching repair lands. Nothing here exercises a live gateway.
"""
from __future__ import annotations

import json
import sqlite3
import sys
import time
import types
from pathlib import Path

import pytest

import importlib.util

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
    for row in rows:
        key = row[10]
        if key:
            con.execute(
                "INSERT OR REPLACE INTO gateway_routing (session_key, entry_json) VALUES (?, ?)",
                (key, json.dumps({"session_id": row[0]})),
            )
    con.commit()
    con.close()


def _send(ctx: RecordingContext, **kwargs) -> dict:
    plugin = _load_plugin()
    caller = str(kwargs.pop("caller", "local-caller-0001"))
    raw = plugin.delivery.tool_session_send(kwargs, ctx=ctx, session_id=caller)
    return json.loads(raw)


# --- D05 -------------------------------------------------------------------

def test_d05_body_keeps_significant_whitespace(tmp_path: Path) -> None:
    plugin = _load_plugin()
    body = "    code()\nnext\n"
    rendered = plugin.store.format_intercom_message(
        sender_card_name="A", sender_session_id="local-a", hop=1, max_hops=3,
        expect_reply=False, message=body,
    )
    assert rendered.endswith("\n\n" + body)


def test_d05_whitespace_only_body_is_rejected(tmp_path: Path) -> None:
    """Emptiness is judged on the trimmed view, so this must be refused on BOTH trees.
    It guards the repair against rejecting nothing while keeping real whitespace."""
    home = _home(tmp_path)
    _insert(home, [("local-target-0001", "local", "m", None, 1.0, None, None, 1,
                    "Target", 0, "agent:main:local:dm:local-target-0001", 5.0)])
    ctx = RecordingContext(home)
    result = _send(ctx, target="local-target-0001", message="   \n\t  ")
    assert result["ok"] is False
    assert result["error"] == "empty_message"
    assert ctx.injected == []


# --- D02 / D01 / D04 -------------------------------------------------------

def _routed_pair(tmp_path: Path) -> tuple[Path, RecordingContext]:
    home = _home(tmp_path)
    _insert(home, [
        ("local-caller-0001", "local", "m", None, 1.0, None, None, 1, "Caller", 0,
         "agent:main:local:dm:local-caller-0001", 9.0),
        ("local-target-0001", "local", "m", None, 1.0, None, None, 1, "Target", 0,
         "agent:main:local:dm:local-target-0001", 8.0),
    ])
    return home, RecordingContext(home)


def test_d02_same_request_id_is_not_delivered_twice(tmp_path: Path) -> None:
    _home_unused, ctx = _routed_pair(tmp_path)
    first = _send(ctx, target="local-target-0001", message="hello", request_id="op-1")
    second = _send(ctx, target="local-target-0001", message="hello", request_id="op-1")
    assert first["ok"] is True and second["ok"] is True
    assert second["replayed"] is True
    assert len(ctx.injected) == 1


def test_d02_changed_payload_under_same_id_conflicts(tmp_path: Path) -> None:
    _home_unused, ctx = _routed_pair(tmp_path)
    _send(ctx, target="local-target-0001", message="hello", request_id="op-1")
    conflict = _send(ctx, target="local-target-0001", message="hello!", request_id="op-1")
    assert conflict["ok"] is False
    assert conflict["error"] == "request_conflict"
    assert len(ctx.injected) == 1


def test_d04_retry_does_not_consume_a_new_burst_slot(tmp_path: Path) -> None:
    home, ctx = _routed_pair(tmp_path)
    ctx._settings["max_messages_per_window"] = 1
    first = _send(ctx, target="local-target-0001", message="hello", request_id="op-1")
    replay = _send(ctx, target="local-target-0001", message="hello", request_id="op-1")
    assert first["ok"] is True and replay["ok"] is True
    blocked = _send(ctx, target="local-target-0001", message="another")
    assert blocked["error"] == "rate_limited"


# --- D03 -------------------------------------------------------------------

def test_d03_unrouted_key_is_not_injected_when_mount_fails(tmp_path: Path, monkeypatch) -> None:
    home = _home(tmp_path)
    _insert(home, [("local-unrouted-01", "local", "m", None, 1.0, None, None, 1,
                    "Loose", 0, None, 5.0)])
    ctx = RecordingContext(home, allow_inject=True)
    plugin = _load_plugin()
    result = _send(ctx, target="local-unrouted-01", message="ping")
    assert result["ok"] is False
    assert ctx.injected == []


# --- S01 -------------------------------------------------------------------

def test_s01_branch_sibling_does_not_win_over_continuation(tmp_path: Path) -> None:
    """The live continuation has no explicit session_key, so only the ancestor alias can
    reach it. The baseline alias ranks every descendant by recency and returns the newer
    branch; the repair must follow the compression edge and ignore the branch marker."""
    home = _home(tmp_path)
    rows = [
        ("local-ancestor", "local", "m", None, 1.0, 2.0, "compression", 3, "Old", 0, None, 2.0),
        ("local-continuation", "local", "m", "local-ancestor", 3.0, None, None, 2, "Live", 0,
         None, 10.0),
        ("local-branch", "local", "m", "local-ancestor", 4.0, None, None, 1, "Branch", 0,
         "agent:main:local:dm:local-branch", 20.0),
    ]
    _insert(home, rows)
    con = sqlite3.connect(str(home / "state.db"))
    con.execute("ALTER TABLE sessions ADD COLUMN model_config TEXT")
    con.execute(
        "UPDATE sessions SET model_config = ? WHERE id = 'local-branch'",
        (json.dumps({"_branched_from": "local-ancestor"}),),
    )
    con.commit()
    con.close()
    plugin = _load_plugin()
    resolved, ambiguous = plugin.store.resolve_target(home, "local-ancestor")
    assert ambiguous == []
    assert resolved is not None and resolved["session_id"] == "local-continuation"


def test_s01_two_live_children_are_ambiguous(tmp_path: Path) -> None:
    home = _home(tmp_path)
    rows = [
        ("local-ancestor", "local", "m", None, 1.0, 2.0, "compression", 3, "Old", 0, None, 2.0),
        ("local-child-a", "local", "m", "local-ancestor", 3.0, None, None, 1, "A", 0,
         "agent:main:local:dm:local-child-a", 10.0),
        ("local-child-b", "local", "m", "local-ancestor", 3.0, None, None, 1, "B", 0,
         "agent:main:local:dm:local-child-b", 30.0),
    ]
    _insert(home, rows)
    plugin = _load_plugin()
    resolved, ambiguous = plugin.store.resolve_target(home, "local-ancestor")
    assert resolved is None
    assert {row["session_id"] for row in ambiguous} == {"local-child-a", "local-child-b"}


# --- S02 -------------------------------------------------------------------

def test_s02_unicode_title_resolves_and_stays_ambiguous(tmp_path: Path) -> None:
    home = _home(tmp_path)
    _insert(home, [
        ("local-pl-upper", "local", "m", None, 1.0, None, None, 1, "ŻÓŁĆ audyt", 0,
         "agent:main:local:dm:local-pl-upper", 5.0),
        ("local-pl-lower", "local", "m", None, 1.0, None, None, 1, "żółć audyt", 0,
         "agent:main:local:dm:local-pl-lower", 4.0),
    ])
    plugin = _load_plugin()
    resolved, ambiguous = plugin.store.resolve_target(home, "ŻÓŁĆ audyt")
    assert resolved is None
    assert {row["session_id"] for row in ambiguous} == {"local-pl-upper", "local-pl-lower"}


def test_s02_single_unicode_title_resolves(tmp_path: Path) -> None:
    home = _home(tmp_path)
    _insert(home, [("local-de", "local", "m", None, 1.0, None, None, 1, "Straße", 0,
                    "agent:main:local:dm:local-de", 5.0)])
    plugin = _load_plugin()
    resolved, ambiguous = plugin.store.resolve_target(home, "STRASSE")
    assert ambiguous == []
    assert resolved["session_id"] == "local-de"


# --- S03 -------------------------------------------------------------------

def test_s03_unc_uri_keeps_the_server_share(tmp_path: Path) -> None:
    plugin = _load_plugin()
    uri = plugin.store._sqlite_uri(Path("//server/share/state.db"))
    assert uri.startswith("file:////server/share/state.db")


# --- S04 -------------------------------------------------------------------

def test_s04_a_rows_own_id_prefix_matches(tmp_path: Path) -> None:
    home = _home(tmp_path)
    _insert(home, [("local-child-9999", "local", "m", None, 1.0, None, None, 1, "Child", 0,
                    "agent:main:local:dm:local-rootabcdef", 5.0)])
    plugin = _load_plugin()
    resolved, ambiguous = plugin.store.resolve_target(home, "local-child-9")
    assert ambiguous == []
    assert resolved["session_id"] == "local-child-9999"


# --- F06 -------------------------------------------------------------------

def test_f06_tag_parser_is_linear_on_blank_lines() -> None:
    plugin = _load_plugin()
    started = time.perf_counter()
    hop, _max = plugin.store.parse_intercom_tag("x" + "\n" * 200_000)
    assert time.perf_counter() - started < 0.5
    assert hop == 0


# --- F08 -------------------------------------------------------------------

def test_f08_quoted_card_does_not_raise_the_hop() -> None:
    plugin = _load_plugin()
    quoted = "what about this?\n[Message from agent 'Zed'] [intercom from=local-z hop=4/4 mode=fyi]"
    assert plugin.store.parse_intercom_tag(quoted) == (0, 0)


# --- F10 -------------------------------------------------------------------

def test_f10_subagent_rows_do_not_hide_real_sessions(tmp_path: Path) -> None:
    home = _home(tmp_path)
    con = sqlite3.connect(str(home / "state.db"))
    for n in range(600):
        con.execute(
            "INSERT INTO sessions (id, source, title, message_count, last_activity_at) VALUES (?, ?, ?, 1, ?)",
            (f"sub-{n:04d}", "subagent", f"Subagent: {n}", 10_000 - n),
        )
    con.execute(
        "INSERT INTO sessions (id, source, title, message_count, session_key, last_activity_at) "
        "VALUES ('local-real', 'local', 'Real', 2, 'agent:main:local:dm:local-real', 1.0)"
    )
    con.commit()
    con.close()
    plugin = _load_plugin()
    found = plugin.store.query_sessions(home, limit=20)
    assert [row["session_id"] for row in found] == ["local-real"]


# --- F16 -------------------------------------------------------------------

def test_f16_shared_key_prefers_the_live_segment(tmp_path: Path) -> None:
    home = _home(tmp_path)
    key = "agent:main:local:dm:local-live"
    _insert(home, [
        ("local-old", "local", "m", None, 1.0, 2.0, "session_reset", 1, "Old", 0, key, 2.0),
        ("local-live", "local", "m", "local-old", 3.0, None, None, 1, "Live", 0, key, 9.0),
    ])
    plugin = _load_plugin()
    found = plugin.store.lookup_exact_session(home, key)
    assert found["session_id"] == "local-live"


# --- F18 -------------------------------------------------------------------

def test_f18_oversized_body_is_refused(tmp_path: Path) -> None:
    _unused, ctx = _routed_pair(tmp_path)
    result = _send(ctx, target="local-target-0001", message="x" * 40_000)
    assert result["ok"] is False
    assert result["error"] == "message_too_large"
    assert ctx.injected == []


# --- F23 -------------------------------------------------------------------

def test_f23_sender_label_cannot_impersonate_another_id(tmp_path: Path) -> None:
    _unused, ctx = _routed_pair(tmp_path)
    _send(ctx, target="local-target-0001", message="hi", sender_label="Boss (local-ffff)")
    assert len(ctx.injected) == 1
    card = ctx.injected[0]["content"]
    head = card.split("]", 1)[0]
    assert head.endswith("(local-caller-0)'") or "(local-caller-0)" in head
    assert head.index("local-caller-0") > head.index("Boss")


# --- FG-17 -----------------------------------------------------------------

def test_fg17_directive_markers_are_neutralized(tmp_path: Path) -> None:
    plugin = _load_plugin()
    rendered = plugin.store.format_intercom_message(
        sender_card_name="A", sender_session_id="local-a", hop=1, max_hops=3,
        expect_reply=False, message="see @file:secrets.env and @url:http://x",
    )
    body = rendered.rsplit("\n\n", 1)[-1]
    assert "@file:" not in body and "@url:" not in body
    assert "file:secrets.env" in body


# --- FO-09 -----------------------------------------------------------------

def test_fo09_newest_route_wins(tmp_path: Path) -> None:
    home = _home(tmp_path)
    con = sqlite3.connect(str(home / "state.db"))
    con.execute("ALTER TABLE gateway_routing ADD COLUMN updated_at REAL")
    con.execute("ALTER TABLE gateway_routing ADD COLUMN scope TEXT")
    scope = str((home / "sessions").resolve())
    con.execute(
        "INSERT INTO gateway_routing (session_key, entry_json, updated_at, scope) VALUES (?, ?, ?, ?)",
        ("agent:main:local:dm:old", json.dumps({"session_id": "local-s"}), 10.0, scope),
    )
    con.execute(
        "INSERT INTO gateway_routing (session_key, entry_json, updated_at, scope) VALUES (?, ?, ?, ?)",
        ("agent:main:local:dm:new", json.dumps({"session_id": "local-s"}), 20.0, scope),
    )
    con.commit()
    con.close()
    plugin = _load_plugin()
    con = sqlite3.connect(str(home / "state.db"))
    con.row_factory = sqlite3.Row
    sid_to_key, _entries = plugin.store._load_routing_map(con, home)
    con.close()
    assert sid_to_key["local-s"] == "agent:main:local:dm:new"


# --- C03 -------------------------------------------------------------------

def test_c03_collection_hook_is_optional() -> None:
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "_conftest_check", Path(__file__).resolve().parent / "conftest.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    hook = module._RepoRootIsNotATestPackage.pytest_collect_directory
    markers = getattr(getattr(hook, "pytestmark", None), "mark", None)
    source_ok = "optionalhook=True" in Path(__file__).resolve().parent.joinpath("conftest.py").read_text(encoding="utf-8")
    assert source_ok or (markers and getattr(markers, "kwargs", {}).get("optionalhook"))


# --- C01 -------------------------------------------------------------------

def test_c01_public_text_makes_no_durability_overclaim() -> None:
    root = Path(__file__).resolve().parents[1]
    blob = "\n".join(
        (root / name).read_text(encoding="utf-8")
        for name in ("schemas.py", "README.md", "plugin.yaml", "skills/hermes-intercom/SKILL.md")
    )
    for banned in ("durable FIFO", "never double", "exactly once"):
        assert banned not in blob
    assert "exactly-once guarantee" not in blob.replace("not an exactly-once guarantee", "")
    assert "own identical budget" not in (root / "plugin.yaml").read_text(encoding="utf-8")
    assert "highest hop" not in (root / "skills/hermes-intercom/SKILL.md").read_text(encoding="utf-8")


# --- Round 2 ---------------------------------------------------------------

def test_r2_include_cron_returns_cron_sessions(tmp_path: Path) -> None:
    home = _home(tmp_path)
    _insert(home, [
        ("local-real", "local", "m", None, 1.0, None, None, 1, "Real", 0,
         "agent:main:local:dm:local-real", 5.0),
        ("cron_daily", "cron", "m", None, 1.0, None, None, 1, "Daily", 0, None, 9.0),
    ])
    plugin = _load_plugin()
    hidden = plugin.store.query_sessions(home, limit=20)
    shown = plugin.store.query_sessions(home, limit=20, include_cron=True)
    assert [row["session_id"] for row in hidden] == ["local-real"]
    assert "cron_daily" in {row["session_id"] for row in shown}


def test_r2_branch_descendant_does_not_inherit_the_reply(tmp_path: Path) -> None:
    home = _home(tmp_path)
    rows = [
        ("local-ancestor", "local", "m", None, 1.0, 2.0, "compression", 3, "Old", 0, None, 2.0),
        ("local-continuation", "local", "m", "local-ancestor", 3.0, None, None, 2, "Live", 0,
         "agent:main:local:dm:local-continuation", 10.0),
        ("local-branch", "local", "m", "local-ancestor", 4.0, 5.0, "compression", 1, "Branch", 0,
         None, 20.0),
        ("local-branch-child", "local", "m", "local-branch", 6.0, None, None, 1, "Child", 0,
         "agent:main:local:dm:local-branch-child", 30.0),
    ]
    _insert(home, rows)
    con = sqlite3.connect(str(home / "state.db"))
    con.execute("ALTER TABLE sessions ADD COLUMN model_config TEXT")
    con.execute(
        "UPDATE sessions SET model_config = ? WHERE id = 'local-branch'",
        (json.dumps({"_branched_from": "local-ancestor"}),),
    )
    con.commit()
    con.close()
    plugin = _load_plugin()
    resolved, ambiguous = plugin.store.resolve_target(home, "local-ancestor")
    assert ambiguous == []
    assert resolved is not None and resolved["session_id"] == "local-continuation"


def test_r2_doubled_at_and_bare_diff_are_neutralized() -> None:
    plugin = _load_plugin()
    rendered = plugin.store.format_intercom_message(
        sender_card_name="A", sender_session_id="local-a", hop=1, max_hops=3,
        expect_reply=False, message="see @@file:secrets.env and @diff",
    )
    body = rendered.rsplit("\n\n", 1)[-1]
    assert "@file:" not in body and "@diff" not in body


def test_r2_title_that_looks_like_an_id_still_resolves(tmp_path: Path) -> None:
    home = _home(tmp_path)
    _insert(home, [("local-actual", "local", "m", None, 1.0, None, None, 1, "local-topic", 0,
                    "agent:main:local:dm:local-actual", 5.0)])
    plugin = _load_plugin()
    resolved, ambiguous = plugin.store.resolve_target(home, "local-topic")
    assert ambiguous == []
    assert resolved is not None and resolved["session_id"] == "local-actual"


def test_r2_same_request_id_on_another_target_conflicts(tmp_path: Path) -> None:
    home = _home(tmp_path)
    _insert(home, [
        ("local-caller-0001", "local", "m", None, 1.0, None, None, 1, "Caller", 0,
         "agent:main:local:dm:local-caller-0001", 9.0),
        ("local-target-0001", "local", "m", None, 1.0, None, None, 1, "One", 0,
         "agent:main:local:dm:local-target-0001", 8.0),
        ("local-target-0002", "local", "m", None, 1.0, None, None, 1, "Two", 0,
         "agent:main:local:dm:local-target-0002", 7.0),
    ])
    ctx = RecordingContext(home)
    first = _send(ctx, target="local-target-0001", message="hello", request_id="op-1")
    second = _send(ctx, target="local-target-0002", message="hello", request_id="op-1")
    assert first["ok"] is True
    assert second["ok"] is False and second["error"] == "request_conflict"
    assert len(ctx.injected) == 1


def test_r2_long_sender_label_keeps_the_real_id(tmp_path: Path) -> None:
    _unused, ctx = _routed_pair(tmp_path)
    _send(ctx, target="local-target-0001", message="hi", sender_label="Boss " + "X" * 80)
    head = ctx.injected[0]["content"].split("]", 1)[0]
    assert "local-caller-0" in head


def test_f21_audit_log_is_documented() -> None:
    root = Path(__file__).resolve().parents[1]
    readme = (root / "README.md").read_text(encoding="utf-8")
    assert "audit.jsonl" in readme
    assert "2 MiB" in readme


def test_r3_doubled_bare_directives_are_neutralized() -> None:
    plugin = _load_plugin()
    rendered = plugin.store.format_intercom_message(
        sender_card_name="A", sender_session_id="local-a", hop=1, max_hops=3,
        expect_reply=False, message="see @@diff and @@staged",
    )
    body = rendered.rsplit("\n\n", 1)[-1]
    assert "@diff" not in body and "@staged" not in body


def _card(plugin, hop: int) -> str:
    return plugin.store.format_intercom_message(
        sender_card_name="A", sender_session_id="local-a", hop=hop, max_hops=3,
        expect_reply=True, message="chain",
    )


def _message(home: Path, session_id: str, content: str, summary: int, has_user_turn: int = 0) -> None:
    con = sqlite3.connect(str(home / "state.db"))
    cols = {row[1] for row in con.execute("PRAGMA table_info(messages)")}
    if "_compressed_summary" not in cols:
        con.execute("ALTER TABLE messages ADD COLUMN _compressed_summary INTEGER DEFAULT 0")
    if "_compressed_summary_has_user_turn" not in cols:
        con.execute("ALTER TABLE messages ADD COLUMN _compressed_summary_has_user_turn INTEGER DEFAULT 0")
    con.execute(
        "INSERT INTO messages (session_id, role, content, timestamp, _compressed_summary, "
        "_compressed_summary_has_user_turn) VALUES (?, 'user', ?, 1, ?, ?)",
        (session_id, content, summary, has_user_turn),
    )
    con.commit()
    con.close()


def test_r4_reset_target_is_not_delivered_to_the_new_conversation(tmp_path: Path) -> None:
    """S-R01: an exact id ended by session_reset must not ride the shared key onward.

    The host dispatcher follows that key into the live conversation. Reporting the old id
    while injecting the shared key delivers the message to the wrong conversation.
    """
    home = _home(tmp_path)
    key = "agent:main:telegram:dm:example-room"
    _insert(home, [
        ("telegram-old", "telegram", "m", None, 1.0, 2.0, "session_reset", 1, "Old", 0, key, 2.0),
        ("telegram-new", "telegram", "m", None, 3.0, None, None, 1, "New", 0, key, 5.0),
        ("local-caller-0001", "local", "m", None, 1.0, None, None, 1, "Caller", 0,
         "agent:main:local:dm:local-caller-0001", 9.0),
    ])
    ctx = RecordingContext(home)
    result = _send(ctx, target="telegram-old", message="hello", request_id="op-stale")
    assert result["ok"] is False
    assert result["error"] == "target_not_found"
    assert ctx.injected == []


def test_r4_compression_summary_does_not_reset_the_hop(tmp_path: Path) -> None:
    """S-R02: a synthetic summary row must not hide the parent's exhausted hop.

    Without the summary the same child is refused at hop 4. With it, the send must
    still be refused.
    """
    home = _home(tmp_path)
    _insert(home, [
        ("local-parent", "local", "m", None, 1.0, 2.0, "compression", 1, "Parent", 0, "", 2.0),
        ("local-child", "local", "m", "local-parent", 3.0, None, None, 1, "Child", 0,
         "agent:main:local:dm:local-child", 4.0),
        ("local-target-0001", "local", "m", None, 1.0, None, None, 1, "Target", 0,
         "agent:main:local:dm:local-target-0001", 8.0),
    ])
    plugin = _load_plugin()
    _message(home, "local-parent", _card(plugin, 3), 0)
    _message(home, "local-child", "Summary of the prior conversation", 1)
    assert plugin.store.latest_user_message_tag(home, "local-child") == (3, 3)
    child = _send(
        RecordingContext(home), target="local-target-0001", message="onward",
        request_id="op-sum", caller="local-child",
    )
    assert child["ok"] is False and child["error"] == "max_hops_exceeded"


def test_r4_summary_carrier_with_a_real_ask_is_a_turn(tmp_path: Path) -> None:
    """S-R02 correction: a carrier that still holds a human ask is a turn, not a summary.

    Skipping every _compressed_summary row would hide that ask and keep the old hop.
    The host marks such a row with _compressed_summary_has_user_turn.
    """
    home = _home(tmp_path)
    _insert(home, [
        ("local-parent", "local", "m", None, 1.0, 2.0, "compression", 1, "Parent", 0, "", 2.0),
        ("local-child", "local", "m", "local-parent", 3.0, None, None, 1, "Child", 0,
         "agent:main:local:dm:local-child", 4.0),
        ("local-target-0001", "local", "m", None, 1.0, None, None, 1, "Target", 0,
         "agent:main:local:dm:local-target-0001", 8.0),
    ])
    plugin = _load_plugin()
    _message(home, "local-parent", _card(plugin, 3), 0)
    _message(home, "local-child", "please do this new thing", 1, has_user_turn=1)
    assert plugin.store.latest_user_message_tag(home, "local-child") == (0, 0)
    sent = _send(
        RecordingContext(home), target="local-target-0001", message="onward",
        request_id="op-carrier", caller="local-child",
    )
    assert sent["ok"] is True and sent["hop"] == 1


def test_r4_shorter_window_does_not_erase_same_target_history(tmp_path: Path, monkeypatch) -> None:
    """R4-D02: a short-window send must not delete history a longer window still owns.

    A body sent under a 3600s guard, then a different body at +700s under a 600s guard,
    must still be a duplicate when the 3600s guard is restored one second later.
    """
    _home_unused, ctx = _routed_pair(tmp_path)
    plugin = _load_plugin()
    clock = {"now": 1000.0}
    monkeypatch.setattr(plugin.delivery.time, "monotonic", lambda: clock["now"])

    def _at(now: float, **kwargs) -> dict:
        clock["now"] = now
        return _send(ctx, **kwargs)

    ctx._settings.update(burst_window_seconds=3600, dedup_window_seconds=3600)
    first = _at(1000.0, target="local-target-0001", message="first", request_id="history-1")
    ctx._settings.update(burst_window_seconds=600, dedup_window_seconds=120)
    second = _at(1700.0, target="local-target-0001", message="second", request_id="history-2")
    ctx._settings.update(burst_window_seconds=3600, dedup_window_seconds=3600)
    third = _at(1701.0, target="local-target-0001", message="first", request_id="history-3")
    assert first["ok"] is True and second["ok"] is True
    assert third["ok"] is False and third["error"] == "duplicate_message"
    assert len(ctx.injected) == 2


def test_r4_card_header_and_custom_reference_are_neutralized(tmp_path: Path) -> None:
    """R4-D04: the host parses the whole card, not just the message body.

    A sender label of @file:`secrets.env` and a body token @issue:123 both match the
    installed reference grammar. Neutralizing only the body leaves the header live.
    """
    import re
    home = _home(tmp_path)
    _insert(home, [
        ("local-caller-0001", "local", "m", None, 1.0, None, None, 1, "Caller", 0,
         "agent:main:local:dm:local-caller-0001", 9.0),
        ("local-target-0001", "local", "m", None, 1.0, None, None, 1, "Target", 0,
         "agent:main:local:dm:local-target-0001", 8.0),
    ])
    ctx = RecordingContext(home)
    result = _send(
        ctx, target="local-target-0001", message="see @issue:123",
        sender_label="@file:`secrets.env`", request_id="op-ref",
    )
    assert result["ok"] is True and len(ctx.injected) == 1
    card = ctx.injected[0]["content"]
    grammar = re.compile(
        r"(?<![\w/])@(?:(?:diff|staged)\b|(?:file|folder|git|url):(?:`[^`\n]+`|\"[^\"\n]+\"|'[^'\n]+'|\S+))"
        r"|(?<![\w/])@[a-zA-Z][a-zA-Z0-9_-]*:(?:`[^`\n]+`|\"[^\"\n]+\"|'[^'\n]+'|\S+)"
    )
    assert grammar.search(card) is None
    assert "secrets.env" in card and "123" in card


def test_r4_recent_sends_has_a_bucket_cap(tmp_path: Path, monkeypatch) -> None:
    """R4-D06: one distinct target per send must not grow the map without a ceiling.

    The cap is small on purpose. A process that talks to more targets than it can remember
    drops the oldest buckets instead of keeping every one.
    """
    home = _home(tmp_path)
    rows = [
        ("local-caller-0001", "local", "m", None, 1.0, None, None, 1, "Caller", 0,
         "agent:main:local:dm:local-caller-0001", 9.0),
    ]
    for n in range(4):
        sid = f"local-cap-{n:04d}"
        rows.append((sid, "local", "m", None, 1.0, None, None, 1, f"Cap {n}", 0,
                     f"agent:main:local:dm:{sid}", 8.0))
    _insert(home, rows)
    plugin = _load_plugin()
    monkeypatch.setattr(plugin.delivery, "_RECENT_SENDS_MAX_BUCKETS", 2)
    plugin.delivery._RECENT_SENDS.clear()
    ctx = RecordingContext(home)
    for n in range(4):
        sent = _send(ctx, target=f"local-cap-{n:04d}", message=f"body {n}", request_id=f"cap-{n}")
        assert sent["ok"] is True
    assert len(plugin.delivery._RECENT_SENDS) <= 2


def test_r4_bucket_eviction_drops_the_horizon_and_receipts_are_capped(tmp_path: Path, monkeypatch) -> None:
    """R4-D06, the part the bucket cap left open.

    Dropping the oldest send bucket must drop its horizon too, or the horizon map grows
    for every target the process ever talked to. Receipt maps follow the operation cap,
    so a finished send does not leave its status behind after the operation is gone.
    """
    home = _home(tmp_path)
    rows = [
        ("local-caller-0001", "local", "m", None, 1.0, None, None, 1, "Caller", 0,
         "agent:main:local:dm:local-caller-0001", 9.0),
    ]
    for n in range(4):
        sid = f"local-side-{n:04d}"
        rows.append((sid, "local", "m", None, 1.0, None, None, 1, f"Side {n}", 0,
                     f"agent:main:local:dm:{sid}", 8.0))
    _insert(home, rows)
    plugin = _load_plugin()
    monkeypatch.setattr(plugin.delivery, "_RECENT_SENDS_MAX_BUCKETS", 2)
    monkeypatch.setattr(plugin.delivery, "_OPERATION_MAX", 2)
    plugin.delivery._RECENT_SENDS.clear()
    plugin.delivery._BUCKET_HORIZON.clear()
    ctx = RecordingContext(home)
    for n in range(4):
        sent = _send(ctx, target=f"local-side-{n:04d}", message=f"body {n}", request_id=f"side-{n}")
        assert sent["ok"] is True
    assert len(plugin.delivery._RECENT_SENDS) <= 2
    assert set(plugin.delivery._BUCKET_HORIZON) <= set(plugin.delivery._RECENT_SENDS)
    assert len(plugin.delivery._OPERATIONS) <= 2


def test_r4_compression_ancestors_do_not_hide_an_older_live_session(tmp_path: Path) -> None:
    """S-R05: compressed rows must not eat the discovery window.

    501 recently active compression ancestors and one older live session. The live one
    is still listed.
    """
    home = _home(tmp_path)
    rows = []
    for n in range(501):
        sid = f"local-anc-{n:04d}"
        rows.append((sid, "local", "m", None, 1.0, 2.0, "compression", 1, f"Anc {n}", 0, "", 1000.0 + n))
    rows.append((
        "local-live-older", "local", "m", None, 1.0, None, None, 1, "Older live", 0,
        "agent:main:local:dm:local-live-older", 10.0,
    ))
    _insert(home, rows)
    found = _load_plugin().store.query_sessions(home)
    assert any(item["session_id"] == "local-live-older" for item in found)


def test_r4_send_to_the_callers_own_reset_predecessor_is_refused(tmp_path: Path, monkeypatch) -> None:
    """S-R04: the caller's ended predecessor is not an external target.

    No environment key is available and both rows share one route. S-R01 already refuses the
    stale reset id, so nothing is injected. A different conversation still goes through.
    """
    home = _home(tmp_path)
    shared = "agent:main:local:dm:local-caller"
    _insert(home, [
        ("local-old", "local", "m", None, 1.0, 2.0, "session_reset", 1, "Old", 0, shared, 2.0),
        ("local-caller", "local", "m", "local-old", 3.0, None, None, 1, "Caller", 0, shared, 9.0),
        ("local-other-0001", "local", "m", None, 1.0, None, None, 1, "Other", 0,
         "agent:main:local:dm:local-other-0001", 8.0),
    ])
    plugin = _load_plugin()
    monkeypatch.setattr(plugin.store, "calling_session_key", lambda: "")
    ctx = RecordingContext(home)
    self_send = _send(ctx, target="local-old", message="back to me", caller="local-caller", request_id="self-1")
    assert self_send["ok"] is False and self_send["error"] == "target_not_found"
    assert ctx.injected == []
    other = _send(ctx, target="local-other-0001", message="outward", caller="local-caller", request_id="self-2")
    assert other["ok"] is True and len(ctx.injected) == 1


def test_r4_reset_prefix_does_not_reach_the_successor(tmp_path: Path) -> None:
    """A prefix of a reset row's id must not land on the live child that kept its key.

    The exact id is already refused. Matching the prefix against the shared key handed the
    same conversation to the successor, which is the S-R01 leak through the prefix tier.
    """
    home = _home(tmp_path)
    key = "agent:main:local:dm:local-rootabcdef"
    _insert(home, [
        ("local-rootabcdef", "local", "m", None, 1.0, 2.0, "session_reset", 1, "Old", 0, key, 2.0),
        ("local-child-9999", "local", "m", "local-rootabcdef", 3.0, None, None, 1, "Live", 0, key, 5.0),
        ("local-caller-0001", "local", "m", None, 1.0, None, None, 1, "Caller", 0,
         "agent:main:local:dm:local-caller-0001", 9.0),
    ])
    plugin = _load_plugin()
    resolved, ambiguous = plugin.store.resolve_target(
        home, "local-rootabc", caller_session_id="local-caller-0001"
    )
    assert resolved is None and ambiguous == []


def test_r4_an_empty_routing_index_is_not_a_route(tmp_path: Path, monkeypatch) -> None:
    """R5-D04 supersedes R4-D05. An empty gateway_routing table is not evidence of a route.

    A metadata session_key with nothing behind it used to skip the mount and report success
    for a message the gateway drops. Both the empty table and a table naming someone else
    now refuse until a route is confirmed.
    """
    plugin = _load_plugin()
    mounts: list[str] = []

    home, ctx = _routed_pair(tmp_path)
    con = sqlite3.connect(str(home / "state.db"))
    con.execute("DELETE FROM gateway_routing")
    con.commit()
    con.close()
    ctx.allow_gateway_injection = True
    bare = _send(ctx, target="local-target-0001", message="hello", request_id="bare-1")
    assert bare["ok"] is False and bare["error"] == "route_unverified"

    con = sqlite3.connect(str(home / "state.db"))
    con.execute(
        "INSERT INTO gateway_routing (session_key, entry_json) VALUES (?, ?)",
        ("agent:main:local:dm:someone-else", '{"session_id": "local-other"}'),
    )
    con.commit()
    con.close()
    indexed = _send(ctx, target="local-target-0001", message="again", request_id="indexed-1")
    assert indexed["error"] == "route_unverified", (mounts, indexed)
