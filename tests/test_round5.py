"""Round 5 defects, written to fail on 3b2cf22.

Each test states the behavior the repair must have. Exit codes here are the
opposite of the defect probes: a failure means the defect is still present.
Nothing in this file talks to a live gateway.
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

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

def test_r5_d01_full_buffer_refuses_new_work_instead_of_evicting_unknown(tmp_path, monkeypatch) -> None:
    home, ctx = _pair(tmp_path)
    ctx._allow_inject = False
    ctx.allow_gateway_injection = True
    plugin = _load_plugin()
    monkeypatch.setattr(plugin.delivery, "_OPERATION_MAX", 2)
    monkeypatch.setattr(plugin.delivery, "submit_via_gateway_ws", lambda *a, **k: (False, "TimeoutError"))

    first = _send(ctx, target="local-target-0001", message="keep me", request_id="live-unknown")
    assert first["error"] == "delivery_unknown"
    _send(ctx, target="local-target-0001", message="filler", request_id="filler-1")

    refused = _send(ctx, target="local-target-0001", message="one too many", request_id="new-one")
    assert refused["ok"] is False and refused["error"] == "operation_capacity"
    assert "live-unknown" in plugin.delivery._OPERATIONS

    retry = _send(ctx, target="local-target-0001", message="keep me", request_id="live-unknown")
    assert retry["error"] == "delivery_unknown"
    assert ctx.injected == []


def test_r5_d01_unknown_results_cannot_grow_past_the_cap(tmp_path, monkeypatch) -> None:
    _home_unused, ctx = _pair(tmp_path)
    ctx._allow_inject = False
    ctx.allow_gateway_injection = True
    plugin = _load_plugin()
    monkeypatch.setattr(plugin.delivery, "_OPERATION_MAX", 2)
    monkeypatch.setattr(plugin.delivery, "submit_via_gateway_ws", lambda *a, **k: (False, "TimeoutError"))

    for n in range(3):
        _send(ctx, target="local-target-0001", message=f"unknown {n}", request_id=f"unknown-{n}")
    assert len(plugin.delivery._OPERATIONS) <= 2


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

def test_r5_d05_a_queued_receipt_does_not_inherit_a_cancelled_outcome(tmp_path, monkeypatch) -> None:
    _home_unused, ctx = _pair(tmp_path)
    ctx._allow_inject = False
    ctx.allow_gateway_injection = True
    plugin = _load_plugin()
    receipts = [
        {"admission_id": "adm-1", "status": "terminal", "outcome": "cancelled", "ref": {"profile_id": "default"}},
        {"admission_id": "adm-1", "status": "queued", "outcome": None, "ref": {"profile_id": "default"}},
    ]

    class _Client:
        async def rpc(self, method, **_kwargs):
            if method == "session.resume":
                return {"session_id": "local-target-0001", "subscription_id": "sub-1"}
            return receipts.pop(0)

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_exc):
            return False

    class _Discovery:
        endpoint = "ws://127.0.0.1:9"
        state = "ready"

    runtime = importlib.import_module("hermes_cli.gateway_runtime")
    client_mod = importlib.import_module("hermes_cli.gateway_client")

    def _attr(parts, name):
        if name == "discover_gateway_endpoint":
            return lambda *_a, **_k: _Discovery()
        if name == "connect_gateway":
            return lambda **_k: _Client()
        raise AssertionError(name)

    import threading

    monkeypatch.setattr(plugin.delivery, "submit_via_gateway_ws", lambda *a, **k: (False, "TimeoutError"))
    seed = _send(ctx, target="local-target-0001", message="hello", request_id="receipt-race")
    assert seed["error"] == "delivery_unknown"
    plugin.delivery._OPERATIONS["receipt-race"]["delivered"] = True
    monkeypatch.setattr(plugin.delivery, "_load_core_attr", _attr)
    entered = threading.Event()
    release = threading.Event()
    order = {"n": 0}
    returned: dict[str, dict] = {}

    real_rpc = _Client.rpc

    async def rpc(self, method, **kwargs):
        if method == "prompt.submit":
            order["n"] += 1
            if order["n"] == 1:
                entered.set()
                release.wait(5)
        return await real_rpc(self, method, **kwargs)

    monkeypatch.setattr(_Client, "rpc", rpc)

    def _call(slot: str) -> None:
        try:
            returned[slot] = _send(ctx, target="local-target-0001", message="hello", request_id="receipt-race")
        except BaseException as exc:
            returned[slot] = {"error": repr(exc)}

    first_thread = threading.Thread(target=_call, args=("first",))
    first_thread.start()
    entered.wait(3)
    assert returned.get("first"), returned
    _call("second")
    release.set()
    first_thread.join(5)
    assert returned.get("first"), returned

    queued = returned["first"]
    terminal = returned["second"]
    assert terminal.get("admission_status") == "terminal", terminal
    assert queued.get("admission_status") == "queued", queued


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
    con.execute(
        "INSERT INTO messages (session_id, role, content, timestamp, _compressed_summary) "
        "VALUES ('local-caller-0001', 'user', ?, 2.0, 1)",
        (card,),
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
