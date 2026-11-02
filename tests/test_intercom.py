from __future__ import annotations

import importlib.util
import json
import sqlite3
import sys
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _load_plugin():
    import hermes_intercom_plugin
    return hermes_intercom_plugin


def _create_state_db(db_path: Path) -> None:
    con = sqlite3.connect(str(db_path))
    con.execute(
        """
        CREATE TABLE sessions (
            id TEXT PRIMARY KEY,
            source TEXT,
            user_id TEXT,
            model TEXT,
            parent_session_id TEXT,
            started_at REAL,
            ended_at REAL,
            end_reason TEXT,
            message_count INTEGER,
            cwd TEXT,
            title TEXT,
            archived INTEGER DEFAULT 0,
            hidden INTEGER DEFAULT 0,
            session_key TEXT,
            chat_id TEXT,
            chat_type TEXT,
            thread_id TEXT,
            last_activity_at REAL
        )
        """
    )
    con.execute(
        """
        CREATE TABLE gateway_routing (
            session_key TEXT PRIMARY KEY,
            entry_json TEXT
        )
        """
    )
    con.execute(
        """
        CREATE TABLE messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT,
            role TEXT,
            content TEXT,
            timestamp REAL
        )
        """
    )
    con.commit()
    con.close()


class RecordingContext:
    def __init__(self, home_path: Path, settings: dict[str, Any] | None = None, allow_inject: bool = True) -> None:
        self.plugin_id = "hermes-intercom"
        self.home_path = home_path
        self._settings = dict(settings or {})
        self._allow_inject = allow_inject
        self.tools: dict[str, dict[str, Any]] = {}
        self.hooks: dict[str, list[Any]] = {}
        self.skills: dict[str, Path] = {}
        self.injected: list[dict[str, Any]] = []

    def get_config(self, key: str, default: Any = None) -> Any:
        return self._settings.get(key, default)

    def register_tool(self, name: str, toolset: str, schema: dict, handler: Any, **kwargs: Any) -> None:
        self.tools[name] = {"toolset": toolset, "schema": schema, "handler": handler, **kwargs}

    def register_hook(self, hook_name: str, callback: Any) -> None:
        self.hooks.setdefault(hook_name, []).append(callback)

    def register_skill(self, name: str, path: Path) -> None:
        self.skills[name] = path

    def inject_message(self, content: str, role: str = "user", *, session_key: str | None = None, origin: Any = None) -> bool:
        if not self._allow_inject:
            return False
        self.injected.append({"content": content, "role": role, "session_key": session_key, "origin": origin})
        return True


def test_register_and_session_list_filters_noise_and_marks_self(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    hermes_intercom = _load_plugin()

    home = tmp_path / "hermes_home"
    home.mkdir()
    db_path = home / "state.db"
    _create_state_db(db_path)

    con = sqlite3.connect(str(db_path))
    rows = [
        # Active desktop session A (caller)
        (
            "local-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
            "local",
            "gemini-4-argon-high",
            None,
            1000.0,
            None,
            None,
            12,
            "E:/Projekty/alpha",
            "Alpha architecture review",
            0,
            0,
            "agent:main:local:dm:local-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
            1500.0,
        ),
        # Active desktop session B (target)
        (
            "local-bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
            "gui",
            "claude-opus-5-5",
            None,
            1100.0,
            None,
            None,
            25,
            "E:/Projekty/beta",
            "Beta KDP pipeline",
            0,
            0,
            "agent:main:local:dm:local-bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
            1600.0,
        ),
        # Subagent session (should be excluded)
        (
            "20261102_010000_sub123",
            "local",
            "grok-4.7",
            "local-bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
            1200.0,
            1250.0,
            "agent_close",
            5,
            "E:/Projekty/beta",
            "Subagent: check something",
            0,
            0,
            None,
            1250.0,
        ),
        # Cron session (should be excluded by default)
        (
            "cron_073710369e94_20261102_070000",
            "cron",
            "grok-4.7",
            None,
            1300.0,
            1310.0,
            None,
            2,
            "/workspace/cron",
            "Morning brief",
            0,
            0,
            "agent:main:local:dm:cron_073710369e94",
            1310.0,
        ),
        # Archived session (should be excluded by default)
        (
            "local-cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc",
            "local",
            "grok-4.7",
            None,
            900.0,
            950.0,
            None,
            8,
            "/workspace/archive",
            "Old archived chat",
            1,
            0,
            "agent:main:local:dm:local-cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc",
            950.0,
        ),
    ]
    con.executemany(
        """
        INSERT INTO sessions (
            id, source, model, parent_session_id, started_at, ended_at, end_reason,
            message_count, cwd, title, archived, hidden, session_key, last_activity_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        rows,
    )
    con.commit()
    con.close()

    monkeypatch.setenv("HERMES_HOME", str(home))
    ctx = RecordingContext(home)
    hermes_intercom.register(ctx)

    assert "session_list" in ctx.tools
    assert "session_send" in ctx.tools
    assert "pre_llm_call" in ctx.hooks

    caller_id = "local-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    raw = ctx.tools["session_list"]["handler"]({}, session_id=caller_id)
    res = json.loads(raw)

    assert res["ok"] is True
    assert res["count"] == 2
    ids = [s["session_id"] for s in res["sessions"]]
    assert ids == [
        "local-bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
        "local-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
    ]
    assert res["sessions"][0]["is_self"] is False
    assert res["sessions"][0]["addressable"] is True
    assert res["sessions"][1]["is_self"] is True


DESKTOP_AGENT_MESSAGE_RE = (
    r"^(?:Message from (?:🤖\s*)?([^:\n(]{1,64}?)(?:\s*\(@([a-z0-9][a-z0-9_-]{0,63})"
    r"(?:@[a-zA-Z0-9][a-zA-Z0-9_-]{0,63})?\))?:\s*|\[Message from agent '([^']{1,64})'\]\s*)([\s\S]*)$"
)


def _seed_two_sessions(db_path: Path) -> tuple[str, str]:
    sid_a = "local-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    sid_b = "local-bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
    con = sqlite3.connect(str(db_path))
    con.executemany(
        """
        INSERT INTO sessions (
            id, source, model, parent_session_id, started_at, ended_at, end_reason,
            message_count, cwd, title, archived, hidden, session_key, last_activity_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            (
                sid_a,
                "local",
                "gemini-4-argon-high",
                None,
                1000.0,
                None,
                None,
                10,
                "E:/Projekty/alpha",
                "Grege's Alpha 'quoted' session with a very long title that exceeds sixty four characters easily",
                0,
                0,
                f"agent:main:local:dm:{sid_a}",
                1500.0,
            ),
            (
                sid_b,
                "gui",
                "claude-opus-5-5",
                None,
                1100.0,
                None,
                None,
                20,
                "E:/Projekty/beta",
                "Beta KDP pipeline",
                0,
                0,
                f"agent:main:local:dm:{sid_b}",
                1600.0,
            ),
        ],
    )
    con.commit()
    con.close()
    return sid_a, sid_b


def test_session_send_formats_desktop_card_and_blocks_self_and_duplicates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import re

    hermes_intercom = _load_plugin()
    home = tmp_path / "hermes_home"
    home.mkdir()
    db_path = home / "state.db"
    _create_state_db(db_path)
    sid_a, sid_b = _seed_two_sessions(db_path)

    monkeypatch.setenv("HERMES_HOME", str(home))
    ctx = RecordingContext(home)
    hermes_intercom.register(ctx)
    send_fn = ctx.tools["session_send"]["handler"]

    # 1. Self-send is refused
    self_res = json.loads(send_fn({"target": "Alpha", "message": "hello me"}, session_id=sid_a))
    assert self_res["ok"] is False
    assert self_res["error"] == "target_is_self"
    assert len(ctx.injected) == 0

    # 2. Send by title substring succeeds and formats a valid Desktop agent card
    res = json.loads(
        send_fn({"target": "Beta KDP", "message": "elo xD", "expect_reply": True}, session_id=sid_a)
    )
    assert res["ok"] is True
    assert res["target_session_id"] == sid_b
    assert res["hop"] == 1
    assert res["max_hops"] == 3
    assert len(ctx.injected) == 1

    injected_text = ctx.injected[0]["content"]
    m = re.match(DESKTOP_AGENT_MESSAGE_RE, injected_text)
    assert m is not None, f"Injected text did not match Desktop AGENT_MESSAGE_RE: {injected_text!r}"
    card_sender = m.group(3)
    assert card_sender is not None and "'" not in card_sender and 1 <= len(card_sender) <= 64
    assert "elo xD" in m.group(4)
    assert f"[intercom from={sid_a} hop=1/3 mode=reply]" in m.group(4)

    # 3. Immediate duplicate message to same target is dropped
    dup_res = json.loads(send_fn({"target": sid_b[:14], "message": "elo xD"}, session_id=sid_a))
    assert dup_res["ok"] is False
    assert dup_res["error"] == "duplicate_message"
    assert len(ctx.injected) == 1


def test_multihop_loop_guard_allows_reply_then_stops_at_max_hops(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    hermes_intercom = _load_plugin()
    home = tmp_path / "hermes_home"
    home.mkdir()
    db_path = home / "state.db"
    _create_state_db(db_path)
    sid_a, sid_b = _seed_two_sessions(db_path)

    monkeypatch.setenv("HERMES_HOME", str(home))
    ctx = RecordingContext(home, settings={"max_hops": 2, "dedup_window_seconds": 0})
    hermes_intercom.register(ctx)
    send_fn = ctx.tools["session_send"]["handler"]
    hook_fn = ctx.hooks["pre_llm_call"][0]

    # Hop 1: A -> B
    r1 = json.loads(send_fn({"target": sid_b, "message": "ping 1", "expect_reply": True}, session_id=sid_a))
    assert r1["ok"] is True and r1["hop"] == 1
    msg_to_b = ctx.injected[-1]["content"]

    # B receives msg_to_b in pre_llm_call and replies B -> A (Hop 2, allowed since max_hops=2)
    hook_fn(session_id=sid_b, user_message=msg_to_b)
    r2 = json.loads(send_fn({"target": sid_a, "message": "pong 2"}, session_id=sid_b))
    assert r2["ok"] is True and r2["hop"] == 2
    msg_to_a = ctx.injected[-1]["content"]

    # A receives msg_to_a (even without pre_llm_call, via DB transcript fallback!) and tries Hop 3 -> refused!
    con = sqlite3.connect(str(db_path))
    con.execute(
        "INSERT INTO messages (session_id, role, content, timestamp) VALUES (?, 'user', ?, 2000.0)",
        (sid_a, msg_to_a),
    )
    con.commit()
    con.close()

    r3 = json.loads(send_fn({"target": sid_b, "message": "ping 3 should block"}, session_id=sid_a))
    assert r3["ok"] is False
    assert r3["error"] == "max_hops_exceeded"
    assert r3["hop"] == 3
    assert r3["max_hops"] == 2


def test_dormant_local_session_auto_mounts_then_injects(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    hermes_intercom = _load_plugin()
    home = tmp_path / "hermes_home"
    home.mkdir()
    db_path = home / "state.db"
    _create_state_db(db_path)
    sid_a, sid_b = _seed_two_sessions(db_path)

    monkeypatch.setenv("HERMES_HOME", str(home))
    mounted_sessions: list[str] = []

    class DormantUntilMountedContext(RecordingContext):
        def inject_message(self, content: str, role: str = "user", *, session_key: str | None = None, origin: Any = None) -> bool:
            if sid_b not in mounted_sessions:
                return False
            return super().inject_message(content, role, session_key=session_key, origin=origin)

    ctx = DormantUntilMountedContext(home)
    hermes_intercom.register(ctx)

    def fake_mount(home_dir: Path, target_sid: str, *, timeout: float = 8.0) -> tuple[bool, str]:
        assert home_dir == home.resolve()
        assert target_sid == sid_b
        mounted_sessions.append(target_sid)
        return True, "mounted"

    monkeypatch.setattr(hermes_intercom.delivery, "mount_dormant_local_session", fake_mount)
    send_fn = ctx.tools["session_send"]["handler"]

    res = json.loads(send_fn({"target": sid_b, "message": "wake up Neo"}, session_id=sid_a))
    assert res["ok"] is True
    assert res["mounted_dormant"] is True
    assert len(ctx.injected) == 1


def test_resilient_per_key_settings_and_rate_limiting(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    hermes_intercom = _load_plugin()
    home = tmp_path / "hermes_home"
    home.mkdir()
    db_path = home / "state.db"
    _create_state_db(db_path)
    sid_a, sid_b = _seed_two_sessions(db_path)

    class BrokenKeyContext(RecordingContext):
        def get_config(self, key: str, default: Any = None) -> Any:
            if key == "max_hops":
                raise ValueError("simulated bad config key")
            if key == "dedup_window_seconds":
                return 0
            if key == "max_messages_per_window":
                return 2
            if key == "burst_window_seconds":
                return -50  # invalid negative -> must fall back to default 600
            return super().get_config(key, default)

    monkeypatch.setenv("HERMES_HOME", str(home))
    ctx = BrokenKeyContext(home)
    hermes_intercom.register(ctx)
    send_fn = ctx.tools["session_send"]["handler"]

    cfg = hermes_intercom.delivery.read_settings(ctx)
    assert cfg["max_hops"] == 3
    assert cfg["dedup_window_seconds"] == 0
    assert cfg["max_messages_per_window"] == 2
    assert cfg["burst_window_seconds"] == 600

    r1 = json.loads(send_fn({"target": sid_b, "message": "msg 1"}, session_id=sid_a))
    r2 = json.loads(send_fn({"target": sid_b, "message": "msg 2"}, session_id=sid_a))
    r3 = json.loads(send_fn({"target": sid_b, "message": "msg 3"}, session_id=sid_a))
    assert r1["ok"] is True
    assert r2["ok"] is True
    assert r3["ok"] is False
    assert r3["error"] == "rate_limited"


def test_external_process_ws_submit_fallback_respects_injection_gate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    hermes_intercom = _load_plugin()
    home = tmp_path / "hermes_home"
    home.mkdir()
    db_path = home / "state.db"
    _create_state_db(db_path)
    sid_a, sid_b = _seed_two_sessions(db_path)

    monkeypatch.setenv("HERMES_HOME", str(home))

    # Context where in-process inject_message always returns False (like a standalone CLI/kernel process)
    ctx_denied = RecordingContext(home, allow_inject=False)
    ctx_denied.allow_gateway_injection = False  # type: ignore[attr-defined]
    hermes_intercom.register(ctx_denied)

    ws_calls: list[tuple[str, str]] = []

    def fake_ws_submit(home_dir: Path, target_sid: str, content: str, *, timeout: float = 8.0) -> tuple[bool, str]:
        ws_calls.append((target_sid, content))
        return True, "submitted_via_ws"

    monkeypatch.setattr(hermes_intercom.delivery, "submit_via_gateway_ws", fake_ws_submit)
    monkeypatch.setattr(hermes_intercom.delivery, "mount_dormant_local_session", lambda *a, **kw: (False, "no_inproc"))

    # 1. When allow_gateway_injection is False, WS fallback must NOT be called (fail-closed)
    res_denied = json.loads(
        ctx_denied.tools["session_send"]["handler"]({"target": sid_b, "message": "blocked"}, session_id=sid_a)
    )
    assert res_denied["ok"] is False
    assert res_denied["error"] == "injection_refused"
    assert len(ws_calls) == 0

    # 2. When allow_gateway_injection is True, WS fallback delivers the message
    ctx_allowed = RecordingContext(home, allow_inject=False)
    ctx_allowed.allow_gateway_injection = True  # type: ignore[attr-defined]
    hermes_intercom.register(ctx_allowed)
    res_ok = json.loads(
        ctx_allowed.tools["session_send"]["handler"]({"target": sid_b, "message": "hello via ws"}, session_id=sid_a)
    )
    assert res_ok["ok"] is True
    assert res_ok["delivery_method"] == "gateway_ws"
    assert len(ws_calls) == 1
    assert ws_calls[0][0] == sid_b
    assert "hello via ws" in ws_calls[0][1]


