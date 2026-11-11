from __future__ import annotations

import asyncio
import importlib.util
import json
import sqlite3
import sys
import time
import types
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
    for sid in (sid_a, sid_b):
        con.execute(
            "INSERT OR REPLACE INTO gateway_routing (session_key, entry_json) VALUES (?, ?)",
            (f"agent:main:local:dm:{sid}", json.dumps({"session_id": sid})),
        )
    con.commit()
    con.close()
    return sid_a, sid_b


class _ClassicCliHost:
    """The host behavior plugins_injection.py lines 20-23 implement, not a plugin mock.

    A live classic CLI REPL (``manager._cli_ref is not None``) with no ``origin``
    queues the payload into the local REPL and returns True, ignoring session_key.
    """

    def __init__(self) -> None:
        self.local_queue: list[str] = []

    def inject_message(self, content: str, role: str = "user", *, session_key: str | None = None, origin: Any = None) -> bool:
        if origin is None:
            self.local_queue.append(content)
            return True
        return False


def test_classic_cli_send_does_not_land_in_the_senders_repl(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A Desktop session B must receive the card; the sender's classic CLI must not.

    Positive control: the same send through a gateway-style injector (the default
    RecordingContext) queues exactly one payload tagged with B's session_key.
    """
    hermes_intercom = _load_plugin()
    home = tmp_path / "hermes_home"
    home.mkdir()
    db_path = home / "state.db"
    _create_state_db(db_path)
    sid_a, sid_b = _seed_two_sessions(db_path)
    monkeypatch.setenv("HERMES_HOME", str(home))

    host = _ClassicCliHost()

    class CliContext(RecordingContext):
        # The host's real signal: plugins_injection steals the payload only when this is set.
        _manager = types.SimpleNamespace(_cli_ref=object())

        def inject_message(self, content: str, role: str = "user", *, session_key: str | None = None, origin: Any = None) -> bool:
            self.injected.append({"content": content, "role": role, "session_key": session_key, "origin": origin})
            return host.inject_message(content, role, session_key=session_key, origin=origin)

    delivered: list[tuple[str, str]] = []

    def fake_ws(home_dir: Path, target_sid: str, content: str, *, timeout: float = 8.0, request_id: str = ""):
        delivered.append((target_sid, content))
        return True, "admitted"

    monkeypatch.setattr(hermes_intercom.delivery, "submit_via_gateway_ws", fake_ws)
    monkeypatch.setattr(
        hermes_intercom.delivery, "mount_dormant_local_session", lambda *a, **kw: (False, "not_needed")
    )

    ctx = CliContext(home, allow_inject=True)
    ctx.allow_gateway_injection = True  # type: ignore[attr-defined]
    hermes_intercom.register(ctx)
    res = json.loads(
        ctx.tools["session_send"]["handler"](
            {"target": sid_b, "message": "for B only"}, session_id=sid_a
        )
    )
    assert res["ok"] is True, res
    assert res["target_session_id"] == sid_b
    assert res["delivery_method"] == "gateway_ws"
    assert host.local_queue == [], "payload was delivered to the sender's own CLI REPL"
    assert delivered and delivered[0][0] == sid_b and "for B only" in delivered[0][1]

    # The same host with the gateway gate closed must refuse instead of lying.
    closed = CliContext(home)
    closed.allow_gateway_injection = False  # type: ignore[attr-defined]
    hermes_intercom.register(closed)
    refused = json.loads(
        closed.tools["session_send"]["handler"](
            {"target": sid_b, "message": "for B only, second"}, session_id=sid_a
        )
    )
    assert refused["ok"] is False and refused["error"] == "surface_cannot_route"
    assert host.local_queue == []

    control = RecordingContext(home)
    hermes_intercom.register(control)
    control_res = json.loads(
        control.tools["session_send"]["handler"](
            {"target": sid_b, "message": "for B only, via gateway"}, session_id=sid_a
        )
    )
    assert control_res["ok"] is True
    assert len(control.injected) == 1
    assert control.injected[0]["session_key"].endswith(sid_b)



def test_cli_surface_without_a_classic_repl_still_uses_the_gateway_injector(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A gateway turn can report surface 'cli' while _cli_ref stays None.

    That turn's injector honors session_key, so refusing it would drop a working route.
    The target here is a messaging session: the WS fallback only covers :local:dm:.
    """
    hermes_intercom = _load_plugin()
    home = tmp_path / "hermes_home"
    home.mkdir()
    db_path = home / "state.db"
    _create_state_db(db_path)
    sid_a, _sid_b = _seed_two_sessions(db_path)
    telegram = "telegram-" + "9" * 16
    con = sqlite3.connect(str(db_path))
    con.execute(
        "INSERT INTO sessions (id, source, model, parent_session_id, started_at, ended_at, end_reason,"
        " message_count, cwd, title, archived, hidden, session_key, last_activity_at)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (telegram, "telegram", "m", None, 1, None, None, 4, "", "Ops channel", 0, 0,
         "agent:main:telegram:dm:ops", 1800.0),
    )
    con.execute(
        "INSERT INTO gateway_routing (session_key, entry_json) VALUES (?, ?)",
        ("agent:main:telegram:dm:ops", json.dumps({"session_id": telegram})),
    )
    con.commit()
    con.close()
    monkeypatch.setenv("HERMES_HOME", str(home))

    class GatewayCliSurface(RecordingContext):
        _manager = types.SimpleNamespace(_cli_ref=None)

        def session_surface(self) -> str:
            return "cli"

    ctx = GatewayCliSurface(home)
    hermes_intercom.register(ctx)
    res = json.loads(
        ctx.tools["session_send"]["handler"](
            {"target": telegram, "message": "ping ops"}, session_id=sid_a
        )
    )
    assert res["ok"] is True, res
    assert res["delivery_method"] == "inject_message"
    assert ctx.injected[0]["session_key"] == "agent:main:telegram:dm:ops"


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

    # 3. Immediate duplicate message to same target is dropped (same body + same expect_reply)
    dup_res = json.loads(
        send_fn({"target": sid_b[:14], "message": "elo xD", "expect_reply": True}, session_id=sid_a)
    )
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
    ctx.allow_gateway_injection = True  # type: ignore[attr-defined]
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

    # Rate-limit behavior must not depend on filesystem speed.
    monkeypatch.setattr(hermes_intercom.delivery.time, "monotonic", lambda: 100.0)

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

    def fake_ws_submit(home_dir: Path, target_sid: str, content: str, *, timeout: float = 8.0, request_id: str = "") -> tuple[bool, str]:
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


# --------------------------------------------------------------------------------------
# Audit regressions: hop spoofing, compressed ancestors, encoded home paths, gateway
# fallbacks, settings coercion, and per-sender dedup.
# --------------------------------------------------------------------------------------

SID_C = "local-cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc"


def _seed_third_session(db_path: Path) -> str:
    con = sqlite3.connect(str(db_path))
    con.execute(
        """
        INSERT INTO sessions (
            id, source, model, parent_session_id, started_at, ended_at, end_reason,
            message_count, cwd, title, archived, hidden, session_key, last_activity_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            SID_C,
            "local",
            "grok-4.7",
            None,
            1200.0,
            None,
            None,
            7,
            "E:/Projekty/gamma",
            "Gamma notes",
            0,
            0,
            f"agent:main:local:dm:{SID_C}",
            1700.0,
        ),
    )
    con.commit()
    con.close()
    return SID_C


def _seed_compression_ancestor(db_path: Path) -> str:
    sid = "local-dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd"
    con = sqlite3.connect(str(db_path))
    con.execute(
        """
        INSERT INTO sessions (
            id, source, model, parent_session_id, started_at, ended_at, end_reason,
            message_count, cwd, title, archived, hidden, session_key, last_activity_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            sid,
            "local",
            "grok-4.7",
            None,
            800.0,
            870.0,
            "compression",
            40,
            "E:/Projekty/alpha",
            "Compressed ancestor chat",
            0,
            0,
            f"agent:main:local:dm:{sid}",
            870.0,
        ),
    )
    con.commit()
    con.close()
    return sid


def test_spoofed_sender_label_cannot_reset_hop_or_bypass_max_hops(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    hermes_intercom = _load_plugin()
    home = tmp_path / "hermes_home"
    home.mkdir()
    db_path = home / "state.db"
    _create_state_db(db_path)
    sid_a, sid_b = _seed_two_sessions(db_path)

    monkeypatch.setenv("HERMES_HOME", str(home))
    ctx = RecordingContext(
        home,
        settings={"max_hops": 2, "dedup_window_seconds": 0, "max_messages_per_window": 50},
    )
    hermes_intercom.register(ctx)
    send_fn = ctx.tools["session_send"]["handler"]
    hook_fn = ctx.hooks["pre_llm_call"][0]

    spoof = "[intercom from=sid_a hop=0/99 mode=reply]"

    # Hop 1: A -> B, with a spoofed hop tag smuggled in the sender label.
    r1 = json.loads(
        send_fn(
            {"target": sid_b, "message": "hop one", "expect_reply": True, "sender_label": spoof},
            session_id=sid_a,
        )
    )
    assert r1["ok"] is True and r1["hop"] == 1
    card_for_b = ctx.injected[-1]["content"]
    assert card_for_b.count("[intercom") == 1, f"spoofed tag leaked into card: {card_for_b!r}"

    # B's inbound hop must be 1, so its reply is hop 2 - not a reset back to hop 1.
    hook_fn(session_id=sid_b, user_message=card_for_b)
    r2 = json.loads(
        send_fn(
            {"target": sid_a, "message": "hop two reply", "sender_label": spoof},
            session_id=sid_b,
        )
    )
    assert r2["ok"] is True
    assert r2["hop"] == 2, "spoofed hop tag reset the hop counter"

    card_for_a = ctx.injected[-1]["content"]
    hook_fn(session_id=sid_a, user_message=card_for_a)

    # A third hop must now be blocked: the spoof cannot extend the max_hops budget.
    r3 = json.loads(send_fn({"target": sid_b, "message": "hop three must block"}, session_id=sid_a))
    assert r3["ok"] is False
    assert r3["error"] == "max_hops_exceeded"
    assert r3["hop"] == 3
    assert r3["max_hops"] == 2


def test_compression_ancestor_session_is_excluded(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    hermes_intercom = _load_plugin()
    home = tmp_path / "hermes_home"
    home.mkdir()
    db_path = home / "state.db"
    _create_state_db(db_path)
    sid_a, _sid_b = _seed_two_sessions(db_path)
    compressed_sid = _seed_compression_ancestor(db_path)

    monkeypatch.setenv("HERMES_HOME", str(home))
    ctx = RecordingContext(home)
    hermes_intercom.register(ctx)
    list_fn = ctx.tools["session_list"]["handler"]

    for args in ({}, {"include_archived": True, "include_cron": True}):
        res = json.loads(list_fn(args, session_id=sid_a))
        ids = [s["session_id"] for s in res["sessions"]]
        assert compressed_sid not in ids, f"end_reason='compression' ancestor leaked into session_list: {ids}"


@pytest.mark.parametrize("dirname", ["home%23hash", "home#hash"])
def test_home_path_with_encoded_or_hash_directory_reads_db(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, dirname: str
) -> None:
    hermes_intercom = _load_plugin()
    home = tmp_path / dirname
    home.mkdir()
    _create_state_db(home / "state.db")
    sid_a, sid_b = _seed_two_sessions(home / "state.db")

    monkeypatch.setenv("HERMES_HOME", str(home))
    ctx = RecordingContext(home)
    hermes_intercom.register(ctx)

    res = json.loads(ctx.tools["session_list"]["handler"]({}, session_id=sid_a))
    assert res["ok"] is True
    ids = {s["session_id"] for s in res["sessions"]}
    assert ids == {sid_a, sid_b}

    # Sending to the resolved target must also work, i.e. the read-only DB really opened.
    send_res = json.loads(ctx.tools["session_send"]["handler"]({"target": sid_b, "message": "hi"}, session_id=sid_a))
    assert send_res["ok"] is True


class _FakeGatewayClient:
    def __init__(self, calls: list[tuple[str, dict[str, Any]]], *, detach_raises: bool) -> None:
        self.calls = calls
        self.detach_raises = detach_raises

    async def rpc(self, method: str, **payload: Any) -> dict[str, Any]:
        self.calls.append((method, payload))
        if method == "session.resume":
            return {
                "session_id": payload.get("session_id"),
                "subscription_id": f"sub-{payload.get('session_id')}",
            }
        if method == "session.detach":
            if self.detach_raises:
                raise RuntimeError("detach exploded")
            return {"ok": True}
        if method == "prompt.submit":
            return {"admission_id": "adm-42"}
        return {}


class _FakeGatewayConnection:
    def __init__(self, client: _FakeGatewayClient) -> None:
        self._client = client

    async def __aenter__(self) -> _FakeGatewayClient:
        return self._client

    async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> bool:
        return False


class _FakeDiscovery:
    state = "ready"
    endpoint = {"url": "ws://fake-gateway.invalid"}


def _install_fake_gateway(monkeypatch: pytest.MonkeyPatch, *, detach_raises: bool = False) -> list[tuple[str, dict[str, Any]]]:
    calls: list[tuple[str, dict[str, Any]]] = []
    client = _FakeGatewayClient(calls, detach_raises=detach_raises)

    runtime = types.ModuleType("hermes_cli.gateway_runtime")
    runtime.discover_gateway_endpoint = lambda home, timeout=5.0: _FakeDiscovery()  # type: ignore[attr-defined]
    gateway_client = types.ModuleType("hermes_cli.gateway_client")
    gateway_client.connect_gateway = lambda endpoint=None: _FakeGatewayConnection(client)  # type: ignore[attr-defined]
    package = types.ModuleType("hermes_cli")
    package.__path__ = []  # type: ignore[attr-defined]

    monkeypatch.setitem(sys.modules, "hermes_cli", package)
    monkeypatch.setitem(sys.modules, "hermes_cli.gateway_runtime", runtime)
    monkeypatch.setitem(sys.modules, "hermes_cli.gateway_client", gateway_client)
    return calls


def test_mount_dormant_local_session_tolerates_detach_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    hermes_intercom = _load_plugin()
    home = tmp_path / "hermes_home"
    home.mkdir()
    _create_state_db(home / "state.db")
    _sid_a, sid_b = _seed_two_sessions(home / "state.db")

    calls = _install_fake_gateway(monkeypatch, detach_raises=True)

    mounted, info = hermes_intercom.delivery.mount_dormant_local_session(home, sid_b, timeout=2.0)
    assert mounted is True, f"detach failure after a good resume must not fail the mount: {info!r}"
    assert info == sid_b
    assert [method for method, _ in calls] == ["session.resume", "session.detach"]


def test_ws_submit_runs_even_when_auto_mount_dormant_is_false(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    hermes_intercom = _load_plugin()
    delivery = hermes_intercom.delivery
    home = tmp_path / "hermes_home"
    home.mkdir()
    _create_state_db(home / "state.db")
    sid_a, sid_b = _seed_two_sessions(home / "state.db")

    calls = _install_fake_gateway(monkeypatch)

    # Direct call: the gateway WS path works on its own.
    ok, info = delivery.submit_via_gateway_ws(home, sid_b, "direct submit", timeout=2.0)
    assert ok is True and info == "adm-42"

    monkeypatch.setenv("HERMES_HOME", str(home))
    ctx = RecordingContext(home, settings={"auto_mount_dormant": False}, allow_inject=False)
    ctx.allow_gateway_injection = True  # type: ignore[attr-defined]
    hermes_intercom.register(ctx)

    def _must_not_mount(*_a: Any, **_kw: Any) -> tuple[bool, str]:
        raise AssertionError("dormant mount must not run when auto_mount_dormant is False")

    monkeypatch.setattr(delivery, "mount_dormant_local_session", _must_not_mount)

    res = json.loads(
        ctx.tools["session_send"]["handler"]({"target": sid_b, "message": "hello over ws"}, session_id=sid_a)
    )
    assert res["ok"] is True
    assert res["delivery_method"] == "gateway_ws"
    assert res["mounted_dormant"] is False

    submitted = [payload for method, payload in calls if method == "prompt.submit"]
    assert len(submitted) == 2  # the direct call above plus the tool-level delivery
    delivered = [payload for payload in submitted if "hello over ws" in payload["text"]]
    assert len(delivered) == 1


def test_read_settings_rejects_bools_and_non_finite_numbers(tmp_path: Path) -> None:
    hermes_intercom = _load_plugin()
    delivery = hermes_intercom.delivery

    ctx = RecordingContext(
        tmp_path / "hermes_home",
        settings={
            "max_hops": True,
            "dedup_window_seconds": False,
            "burst_window_seconds": float("inf"),
            "max_messages_per_window": float("nan"),
            "mount_timeout_seconds": float("-inf"),
            "auto_mount_dormant": "false",
        },
    )

    cfg = delivery.read_settings(ctx)
    assert cfg["max_hops"] == 3
    assert cfg["dedup_window_seconds"] == 120
    assert cfg["burst_window_seconds"] == 600
    assert cfg["max_messages_per_window"] == 10
    assert cfg["mount_timeout_seconds"] == 8.0
    assert cfg["auto_mount_dormant"] is False

    # Direct value handling
    assert delivery._pos_int(float("inf"), 7) == 7
    assert delivery._pos_int(float("nan"), 7) == 7
    assert delivery._pos_float(float("nan"), 4.5) == 4.5
    assert delivery._pos_float(True, 4.5) == 4.5
    assert delivery._parse_bool("on", False) is True
    assert delivery._parse_bool(" No ", True) is False
    assert delivery._parse_bool(1, False) is True
    assert delivery._parse_bool(0, True) is False
    assert delivery._parse_bool(7, True) is True
    assert delivery._parse_bool("maybe", True) is True
    assert delivery._parse_bool(None, False) is False


def test_dedup_is_per_sender_and_expect_reply_aware(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    hermes_intercom = _load_plugin()
    home = tmp_path / "hermes_home"
    home.mkdir()
    db_path = home / "state.db"
    _create_state_db(db_path)
    sid_a, sid_b = _seed_two_sessions(db_path)
    sid_c = _seed_third_session(db_path)

    monkeypatch.setenv("HERMES_HOME", str(home))
    ctx = RecordingContext(home, settings={"max_messages_per_window": 20})
    hermes_intercom.register(ctx)
    send_fn = ctx.tools["session_send"]["handler"]

    first = json.loads(send_fn({"target": sid_b, "message": "done"}, session_id=sid_a))
    assert first["ok"] is True

    # A different sender saying the exact same thing must not be treated as a duplicate.
    other_sender = json.loads(send_fn({"target": sid_b, "message": "done"}, session_id=sid_c))
    assert other_sender["ok"] is True, other_sender

    # Same sender, same body, same expect_reply -> duplicate.
    repeat = json.loads(send_fn({"target": sid_b, "message": "done"}, session_id=sid_a))
    assert repeat["ok"] is False
    assert repeat["error"] == "duplicate_message"

    # Same sender, same body, flipped expect_reply -> a genuinely different message.
    flipped = json.loads(send_fn({"target": sid_b, "message": "done", "expect_reply": True}, session_id=sid_a))
    assert flipped["ok"] is True, flipped


def test_burst_rate_limit_counts_every_sender_to_the_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    hermes_intercom = _load_plugin()
    home = tmp_path / "hermes_home"
    home.mkdir()
    db_path = home / "state.db"
    _create_state_db(db_path)
    sid_a, sid_b = _seed_two_sessions(db_path)
    sid_c = _seed_third_session(db_path)

    monkeypatch.setenv("HERMES_HOME", str(home))
    ctx = RecordingContext(home, settings={"dedup_window_seconds": 0, "max_messages_per_window": 1})
    hermes_intercom.register(ctx)
    send_fn = ctx.tools["session_send"]["handler"]

    assert json.loads(send_fn({"target": sid_b, "message": "one"}, session_id=sid_a))["ok"] is True
    second = json.loads(send_fn({"target": sid_b, "message": "two"}, session_id=sid_c))
    assert second["ok"] is False
    assert second["error"] == "rate_limited"


# --------------------------------------------------------------------------------------
# Wave 2 regressions: compression continuations, dormant mounting order, hop recovery,
# id-prefix vs title resolution, surrogate safety, WS receipt classification, and strict
# argument validation.
# --------------------------------------------------------------------------------------

SID_E = "local-eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee"


def _insert_session(db_path: Path, **fields: Any) -> str:
    """Insert one session row (sane defaults) and return its id."""
    row: dict[str, Any] = {
        "id": SID_E,
        "source": "local",
        "model": "grok-4.7",
        "parent_session_id": None,
        "started_at": 1000.0,
        "ended_at": None,
        "end_reason": None,
        "message_count": 3,
        "cwd": "E:/Projekty/delta",
        "title": "",
        "archived": 0,
        "hidden": 0,
        "session_key": None,
        "last_activity_at": 1500.0,
    }
    row.update(fields)
    con = sqlite3.connect(str(db_path))
    con.execute(
        """
        INSERT INTO sessions (
            id, source, model, parent_session_id, started_at, ended_at, end_reason,
            message_count, cwd, title, archived, hidden, session_key, last_activity_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            row["id"],
            row["source"],
            row["model"],
            row["parent_session_id"],
            row["started_at"],
            row["ended_at"],
            row["end_reason"],
            row["message_count"],
            row["cwd"],
            row["title"],
            row["archived"],
            row["hidden"],
            row["session_key"],
            row["last_activity_at"],
        ),
    )
    con.commit()
    key = row["session_key"]
    if key:
        con.execute(
            "INSERT OR REPLACE INTO gateway_routing (session_key, entry_json) VALUES (?, ?)",
            (key, json.dumps({"session_id": row["id"]})),
        )
        con.commit()
    con.close()
    return str(row["id"])


class _ScriptedGatewayClient:
    """Gateway double whose failures are scripted per RPC method."""

    def __init__(
        self,
        calls: list[tuple[str, dict[str, Any]]],
        *,
        fail_resume: bool = False,
        fail_submit: bool = False,
        fail_detach: bool = False,
        admission: bool = True,
    ) -> None:
        self.calls = calls
        self.fail_resume = fail_resume
        self.fail_submit = fail_submit
        self.fail_detach = fail_detach
        self.admission = admission

    async def rpc(self, method: str, **payload: Any) -> dict[str, Any]:
        self.calls.append((method, payload))
        if method == "session.resume":
            if self.fail_resume:
                raise asyncio.TimeoutError("resume timed out")
            return {
                "session_id": payload.get("session_id"),
                "subscription_id": f"sub-{payload.get('session_id')}",
            }
        if method == "prompt.submit":
            if self.fail_submit:
                raise asyncio.TimeoutError("submit timed out")
            return {"admission_id": "adm-42"} if self.admission else {"error": "refused"}
        if method == "session.detach":
            if self.fail_detach:
                raise RuntimeError("detach exploded")
            return {"ok": True}
        return {}


def _install_scripted_gateway(monkeypatch: pytest.MonkeyPatch, client: _ScriptedGatewayClient) -> None:
    runtime = types.ModuleType("hermes_cli.gateway_runtime")
    runtime.discover_gateway_endpoint = lambda home, timeout=5.0: _FakeDiscovery()  # type: ignore[attr-defined]
    gateway_client = types.ModuleType("hermes_cli.gateway_client")
    gateway_client.connect_gateway = lambda endpoint=None: _FakeGatewayConnection(client)  # type: ignore[attr-defined]
    package = types.ModuleType("hermes_cli")
    package.__path__ = []  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "hermes_cli", package)
    monkeypatch.setitem(sys.modules, "hermes_cli.gateway_runtime", runtime)
    monkeypatch.setitem(sys.modules, "hermes_cli.gateway_client", gateway_client)


def test_compression_continuation_without_session_key_is_listed_and_routable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """W201: an unkeyed continuation child stays visible instead of vanishing with its parent."""
    hermes_intercom = _load_plugin()
    home = tmp_path / "hermes_home"
    home.mkdir()
    db_path = home / "state.db"
    _create_state_db(db_path)
    sid_a, _sid_b = _seed_two_sessions(db_path)
    child = _insert_session(
        db_path,
        id=SID_E,
        parent_session_id=sid_a,
        title="Continuation of Alpha",
        message_count=4,
        last_activity_at=1800.0,
    )

    monkeypatch.setenv("HERMES_HOME", str(home))
    ctx = RecordingContext(home)
    hermes_intercom.register(ctx)

    res = json.loads(ctx.tools["session_list"]["handler"]({}, session_id=sid_a))
    by_id = {s["session_id"]: s for s in res["sessions"]}
    assert child in by_id, f"unkeyed compression continuation missing from session_list: {sorted(by_id)}"
    item = by_id[child]
    assert item["addressable"] is True
    assert item["routed"] is False, "a synthesized :local:dm: key must not claim a real gateway route"
    assert item["session_key"] == f"agent:main:local:dm:{child}"


def test_unrouted_local_dm_target_mounts_before_inject_message(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """W202: core returns True for unknown keys and drops them, so mount must run first."""
    hermes_intercom = _load_plugin()
    delivery = hermes_intercom.delivery
    home = tmp_path / "hermes_home"
    home.mkdir()
    db_path = home / "state.db"
    _create_state_db(db_path)
    sid_a, _sid_b = _seed_two_sessions(db_path)
    child = _insert_session(
        db_path,
        id=SID_E,
        parent_session_id=sid_a,
        title="Continuation of Alpha",
        message_count=4,
        last_activity_at=1800.0,
    )

    monkeypatch.setenv("HERMES_HOME", str(home))

    events: list[str] = []

    class OrderedContext(RecordingContext):
        def inject_message(
            self, content: str, role: str = "user", *, session_key: str | None = None, origin: Any = None
        ) -> bool:
            events.append("inject")
            return super().inject_message(content, role, session_key=session_key, origin=origin)

    ctx = OrderedContext(home)
    ctx.allow_gateway_injection = True  # type: ignore[attr-defined]
    hermes_intercom.register(ctx)

    def fake_mount(home_dir: Path, target_sid: str, *, timeout: float = 8.0) -> tuple[bool, str]:
        assert home_dir == home.resolve()
        assert target_sid == child
        events.append("mount")
        return True, child

    monkeypatch.setattr(delivery, "mount_dormant_local_session", fake_mount)

    res = json.loads(
        ctx.tools["session_send"]["handler"]({"target": child, "message": "wake up"}, session_id=sid_a)
    )
    assert res["ok"] is True, res
    assert res["mounted_dormant"] is True
    assert events == ["mount", "inject"], f"mount must precede inject_message for unrouted targets: {events}"


def test_hop_guard_recovers_caller_sid_from_session_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """W204: a missing session_id must not silently disable the loop guard."""
    hermes_intercom = _load_plugin()
    delivery = hermes_intercom.delivery
    home = tmp_path / "hermes_home"
    home.mkdir()
    db_path = home / "state.db"
    _create_state_db(db_path)
    _sid_a, sid_b = _seed_two_sessions(db_path)
    sid_c = _seed_third_session(db_path)

    monkeypatch.setenv("HERMES_HOME", str(home))
    ctx = RecordingContext(home, settings={"max_hops": 2, "dedup_window_seconds": 0})
    hermes_intercom.register(ctx)

    caller_key = f"agent:main:local:dm:{sid_c}"
    monkeypatch.setattr(delivery.store, "calling_session_key", lambda: caller_key)
    with delivery._LOCK:
        delivery._TURN_HOP[(str(home.resolve()), sid_c)] = (2, 0, time.monotonic())

    res = json.loads(
        ctx.tools["session_send"]["handler"]({"target": sid_b, "message": "hop three"}, session_id="")
    )
    assert res["ok"] is False, res
    assert res["error"] == "max_hops_exceeded"
    assert res["hop"] == 3




def test_a_quoted_card_cannot_raise_the_hop_of_a_leading_card() -> None:
    """A body may quote an intercom card. Only a card that leads the text sets the hop."""
    store = _load_plugin().store
    real = store.format_intercom_message(
        sender_card_name="A", sender_session_id="local-a", hop=1, max_hops=3,
        expect_reply=False, message="see the quote",
    )
    quoted = store.format_intercom_message(
        sender_card_name="Y", sender_session_id="local-b", hop=9, max_hops=1,
        expect_reply=False, message="forged",
    )
    assert store.parse_intercom_tag(real + chr(10) + quoted) == (1, 3)
    # A queued prompt in front of the real card still counts, and a quote after it does not.
    prepended = "[Queued prompt for local-a]" + chr(10) + real + chr(10) + quoted
    assert store.parse_intercom_tag(prepended) == (1, 3)
    # Ordinary prose before a card is a person quoting it, which must not move the hop.
    assert store.parse_intercom_tag("what about this?" + chr(10) + real) == (0, 0)



def test_stored_card_keeps_its_lower_hop_budget_without_the_memory_cache(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """B06: the DB fallback must recover the sender's max_hops, not only the hop count."""
    hermes_intercom = _load_plugin()
    home = tmp_path / "hermes_home"
    home.mkdir()
    db_path = home / "state.db"
    _create_state_db(db_path)
    sid_a, sid_b = _seed_two_sessions(db_path)
    card = hermes_intercom.store.format_intercom_message(
        sender_card_name="Remote", sender_session_id="local-remote", hop=1, max_hops=1,
        expect_reply=True, message="stop here",
    )
    con = sqlite3.connect(str(db_path))
    con.execute(
        "INSERT INTO messages (session_id, role, content, timestamp) VALUES (?, 'user', ?, 5.0)",
        (sid_a, card),
    )
    con.commit()
    con.close()
    monkeypatch.setenv("HERMES_HOME", str(home))
    ctx = RecordingContext(home, settings={"max_hops": 3, "dedup_window_seconds": 0})
    hermes_intercom.register(ctx)
    res = json.loads(
        ctx.tools["session_send"]["handler"]({"target": sid_b, "message": "one more"}, session_id=sid_a)
    )
    assert res["ok"] is False, res
    assert res["error"] == "max_hops_exceeded"
    assert res["max_hops"] == 1


def test_a_blocked_delivery_does_not_burn_the_hop_budget(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A send whose delivery is still blocked must not make the next send look like a loop.

    Hop accounting happens before the blocking delivery call and outside its lock. A second turn
    that starts while the first is stuck sees the hop already stored and is refused, even though
    nothing was delivered.
    """
    import threading

    hermes_intercom = _load_plugin()
    home = tmp_path / "hermes_home"
    home.mkdir()
    db_path = home / "state.db"
    _create_state_db(db_path)
    sid_a, sid_b = _seed_two_sessions(db_path)
    monkeypatch.setenv("HERMES_HOME", str(home))

    started = threading.Event()
    release = threading.Event()

    class BlockingContext(RecordingContext):
        def inject_message(self, content, role="user", *, session_key=None, origin=None):
            started.set()
            release.wait(5)
            return super().inject_message(content, role=role, session_key=session_key, origin=origin)

    ctx = BlockingContext(home, settings={"max_hops": 1, "dedup_window_seconds": 0, "auto_mount_dormant": False})
    hermes_intercom.register(ctx)
    box: dict[str, Any] = {}

    def _first() -> None:
        box["first"] = json.loads(
            ctx.tools["session_send"]["handler"]({"target": sid_b, "message": "first"}, session_id=sid_a)
        )

    worker = threading.Thread(target=_first)
    worker.start()
    assert started.wait(5), "the first delivery never blocked"
    try:
        second = json.loads(
            ctx.tools["session_send"]["handler"]({"target": sid_b, "message": "second"}, session_id=sid_a)
        )
    finally:
        release.set()
        worker.join(5)
    assert second["ok"] is True, second
    assert box["first"]["ok"] is True, box["first"]


def test_exact_session_id_resolves_beyond_the_recency_window(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """B07: an exact id older than the scan window is still a target, not target_not_found."""
    hermes_intercom = _load_plugin()
    home = tmp_path / "hermes_home"
    home.mkdir()
    db_path = home / "state.db"
    _create_state_db(db_path)
    sid_a, _sid_b = _seed_two_sessions(db_path)
    ancient = "local-" + "a" * 58
    con = sqlite3.connect(str(db_path))
    con.execute(
        "INSERT INTO sessions (id, source, model, parent_session_id, started_at, ended_at, end_reason,"
        " message_count, cwd, title, archived, hidden, session_key, last_activity_at)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (ancient, "local", "m", None, 1, None, None, 2, "/w", "Ancient", 0, 0,
         f"agent:main:local:dm:{ancient}", 1.0),
    )
    con.execute(
        "INSERT INTO gateway_routing (session_key, entry_json) VALUES (?, ?)",
        (f"agent:main:local:dm:{ancient}", json.dumps({"session_id": ancient})),
    )
    con.executemany(
        "INSERT INTO sessions (id, source, title, message_count, last_activity_at, session_key)"
        " VALUES (?,?,?,?,?,?)",
        [(f"local-n{i:062d}", "local", f"noise {i}", 1, 1000 + i, f"agent:main:local:dm:local-n{i:062d}")
         for i in range(10050)],
    )
    con.commit()
    con.close()
    monkeypatch.setenv("HERMES_HOME", str(home))
    ctx = RecordingContext(home)
    hermes_intercom.register(ctx)
    res = json.loads(
        ctx.tools["session_send"]["handler"]({"target": ancient, "message": "still there"}, session_id=sid_a)
    )
    assert res["ok"] is True, res
    assert res["target_session_id"] == ancient


def test_reply_to_compressed_ancestor_id_resolves_live_continuation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """W205: replying to a pre-compression session id reaches the active continuation."""
    hermes_intercom = _load_plugin()
    delivery = hermes_intercom.delivery
    home = tmp_path / "hermes_home"
    home.mkdir()
    db_path = home / "state.db"
    _create_state_db(db_path)
    sid_a, _sid_b = _seed_two_sessions(db_path)
    ancestor = _seed_compression_ancestor(db_path)
    child = _insert_session(
        db_path,
        id="local-ffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff",
        parent_session_id=ancestor,
        title="Alpha after compression",
        message_count=6,
        last_activity_at=1900.0,
        session_key="agent:main:local:dm:local-ffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff",
    )

    monkeypatch.setenv("HERMES_HOME", str(home))
    ctx = RecordingContext(home)
    hermes_intercom.register(ctx)
    monkeypatch.setattr(
        delivery, "mount_dormant_local_session", lambda *a, **kw: (False, "no_gateway_in_tests")
    )

    res = json.loads(
        ctx.tools["session_send"]["handler"]({"target": ancestor, "message": "still there?"}, session_id=sid_a)
    )
    assert res["ok"] is True, res
    assert res["target_session_id"] == child, "compressed ancestor id must alias to its live continuation"
    assert "still there?" in ctx.injected[-1]["content"]


def test_title_local_ab_does_not_hijack_id_prefix(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """W209: an exact title match must never beat a unique >=6-char id prefix."""
    hermes_intercom = _load_plugin()
    home = tmp_path / "hermes_home"
    home.mkdir()
    db_path = home / "state.db"
    _create_state_db(db_path)
    sid_a, _sid_b = _seed_two_sessions(db_path)

    prefix_target = "local-ab" + "1" * 58
    decoy = _insert_session(
        db_path,
        id="local-zz" + "2" * 56,
        title="local-ab",
        message_count=5,
        session_key=None,
        last_activity_at=1750.0,
    )
    _insert_session(
        db_path,
        id=prefix_target,
        title="Actual prefix target",
        message_count=5,
        last_activity_at=1700.0,
        session_key=f"agent:main:local:dm:{prefix_target}",
    )

    monkeypatch.setenv("HERMES_HOME", str(home))
    ctx = RecordingContext(home)
    hermes_intercom.register(ctx)

    res = json.loads(
        ctx.tools["session_send"]["handler"]({"target": "local-ab", "message": "prefix please"}, session_id=sid_a)
    )
    assert res["ok"] is True, res
    assert res["target_session_id"] == prefix_target
    assert res["target_session_id"] != decoy, "a chat titled 'local-ab' hijacked the id prefix"


def test_lone_surrogate_in_message_and_label_does_not_raise(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """W210: a lone UTF-16 surrogate must be replaced, never raise UnicodeEncodeError."""
    hermes_intercom = _load_plugin()
    home = tmp_path / "hermes_home"
    home.mkdir()
    db_path = home / "state.db"
    _create_state_db(db_path)
    sid_a, _sid_b = _seed_two_sessions(db_path)

    monkeypatch.setenv("HERMES_HOME", str(home))
    ctx = RecordingContext(home)
    hermes_intercom.register(ctx)

    raw = ctx.tools["session_send"]["handler"](
        {"target": "Beta KDP", "message": "hello \ud800 world \udfff", "sender_label": "bad \ud800 label"},
        session_id=sid_a,
    )
    res = json.loads(raw)
    assert res["ok"] is True, res
    injected = ctx.injected[-1]["content"]
    assert "\ud800" not in injected and "\udfff" not in injected
    assert "hello" in injected and "world" in injected
    injected.encode("utf-8")  # the delivered card must be encodable


def test_pre_submit_ws_timeout_rolls_back_dedup_while_post_submit_keeps_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """W212: only provable pre-submit failures may release the dedup reservation."""
    hermes_intercom = _load_plugin()
    delivery = hermes_intercom.delivery
    home = tmp_path / "hermes_home"
    home.mkdir()
    db_path = home / "state.db"
    _create_state_db(db_path)
    sid_a, sid_b = _seed_two_sessions(db_path)

    monkeypatch.setenv("HERMES_HOME", str(home))
    ctx = RecordingContext(home, allow_inject=False)
    ctx.allow_gateway_injection = True  # type: ignore[attr-defined]
    hermes_intercom.register(ctx)
    send_fn = ctx.tools["session_send"]["handler"]

    monkeypatch.setattr(delivery, "mount_dormant_local_session", lambda *a, **kw: (False, "no_inproc"))

    ws_results: list[tuple[bool, str]] = [(False, "pre_submit:TimeoutError")]

    def fake_ws_submit(home_dir: Path, target_sid: str, content: str, *, timeout: float = 8.0, request_id: str = "") -> tuple[bool, str]:
        return ws_results[-1]

    monkeypatch.setattr(delivery, "submit_via_gateway_ws", fake_ws_submit)

    first = json.loads(send_fn({"target": sid_b, "message": "pre submit body"}, session_id=sid_a))
    assert first["ok"] is False and first["error"] == "injection_refused"
    assert first["ws_error"] == "pre_submit:TimeoutError"

    retry = json.loads(send_fn({"target": sid_b, "message": "pre submit body"}, session_id=sid_a))
    assert retry["error"] == "injection_refused", "a pre-submit failure must roll back the dedup entry"

    ws_results.append((False, "TimeoutError"))
    post = json.loads(send_fn({"target": sid_b, "message": "post submit body"}, session_id=sid_a))
    assert post["ok"] is False and post["ws_error"] == "TimeoutError"

    duplicate = json.loads(send_fn({"target": sid_b, "message": "post submit body"}, session_id=sid_a))
    assert duplicate["error"] == "duplicate_message", "a post-submit timeout may still land; keep the entry"

    ws_results.append((False, "ConnectionClosedError"))
    dropped = json.loads(send_fn({"target": sid_b, "message": "transport drop body"}, session_id=sid_a))
    assert dropped["ok"] is False and dropped["ws_error"] == "ConnectionClosedError"

    dropped_retry = json.loads(send_fn({"target": sid_b, "message": "transport drop body"}, session_id=sid_a))
    assert dropped_retry["error"] == "duplicate_message", "a post-submit transport drop may still land"


def test_ws_submit_classifies_pre_submit_failures_and_keeps_admission_receipts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """W212c/W211b: prefix pre-submit errors, and never lose a real admission receipt."""
    delivery = _load_plugin().delivery
    home = tmp_path / "hermes_home"
    home.mkdir()
    _create_state_db(home / "state.db")
    _sid_a, sid_b = _seed_two_sessions(home / "state.db")

    calls: list[tuple[str, dict[str, Any]]] = []

    _install_scripted_gateway(monkeypatch, _ScriptedGatewayClient(calls, fail_resume=True))
    ok, label = delivery.submit_via_gateway_ws(home, sid_b, "pre", timeout=2.0)
    assert ok is False
    assert label == "pre_submit:TimeoutError", label

    _install_scripted_gateway(monkeypatch, _ScriptedGatewayClient(calls, fail_submit=True))
    ok, label = delivery.submit_via_gateway_ws(home, sid_b, "during", timeout=2.0)
    assert ok is False
    assert label == "TimeoutError", label

    _install_scripted_gateway(monkeypatch, _ScriptedGatewayClient(calls, fail_detach=True))
    ok, label = delivery.submit_via_gateway_ws(home, sid_b, "after", timeout=2.0)
    assert ok is True, label
    assert label == "adm-42"


def test_ws_submit_works_from_inside_a_running_event_loop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """W211a/c: a synchronous tool call made inside the gateway loop must not deadlock."""
    delivery = _load_plugin().delivery
    home = tmp_path / "hermes_home"
    home.mkdir()
    _create_state_db(home / "state.db")
    _sid_a, sid_b = _seed_two_sessions(home / "state.db")

    calls: list[tuple[str, dict[str, Any]]] = []
    _install_scripted_gateway(monkeypatch, _ScriptedGatewayClient(calls))

    async def _caller() -> tuple[bool, str]:
        return delivery.submit_via_gateway_ws(home, sid_b, "loop submit", timeout=2.0)

    ok, info = asyncio.run(_caller())
    assert ok is True, info
    assert info == "adm-42"
    assert [method for method, _ in calls] == ["session.resume", "prompt.submit", "session.detach"]



def test_an_unreadable_store_is_not_reported_as_an_empty_list(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """B09: a corrupt state.db is store_unreadable, never ok with zero sessions."""
    hermes_intercom = _load_plugin()
    home = tmp_path / "hermes_home"
    home.mkdir()
    (home / "state.db").write_bytes(b"this is not a sqlite database")
    monkeypatch.setenv("HERMES_HOME", str(home))
    ctx = RecordingContext(home)
    hermes_intercom.register(ctx)
    listed = json.loads(ctx.tools["session_list"]["handler"]({}, session_id="local-a"))
    assert listed["ok"] is False and listed["error"] == "store_unreadable"
    sent = json.loads(
        ctx.tools["session_send"]["handler"]({"target": "anything", "message": "hi"}, session_id="local-a")
    )
    assert sent["ok"] is False and sent["error"] == "store_unreadable"

    # A home with no database at all is genuinely empty, not an error.
    empty = tmp_path / "empty_home"
    empty.mkdir()
    ctx2 = RecordingContext(empty)
    hermes_intercom.register(ctx2)
    none = json.loads(ctx2.tools["session_list"]["handler"]({}))
    assert none["ok"] is True and none["count"] == 0


def test_invalid_arguments_return_json_envelopes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """W215: non-dict args and non-string fields produce a JSON error envelope, never a traceback."""
    hermes_intercom = _load_plugin()
    delivery = hermes_intercom.delivery
    home = tmp_path / "hermes_home"
    home.mkdir()
    db_path = home / "state.db"
    _create_state_db(db_path)
    sid_a, sid_b = _seed_two_sessions(db_path)

    monkeypatch.setenv("HERMES_HOME", str(home))
    ctx = RecordingContext(home)
    hermes_intercom.register(ctx)
    list_fn = ctx.tools["session_list"]["handler"]
    send_fn = ctx.tools["session_send"]["handler"]

    bad_list_args = json.loads(list_fn(["nope"], session_id=sid_a))
    assert bad_list_args["ok"] is False
    assert bad_list_args["error"] == "invalid_arguments"
    assert "JSON object" in bad_list_args["message"]

    bad_query = json.loads(list_fn({"query": ["x"]}, session_id=sid_a))
    assert bad_query["error"] == "invalid_arguments"
    assert "must be strings" in bad_query["message"]

    bad_send_args = json.loads(send_fn("not-a-mapping", session_id=sid_a))
    assert bad_send_args["error"] == "invalid_arguments"

    for args in (
        {"target": sid_b, "message": 42},
        {"target": sid_b, "message": ["hello"]},
        {"target": [sid_b], "message": "hello"},
        {"target": sid_b, "message": "hello", "sender_label": {"a": 1}},
    ):
        res = json.loads(send_fn(args, session_id=sid_a))
        assert res["ok"] is False, (args, res)
        assert res["error"] == "invalid_arguments", (args, res)
        assert "must be strings" in res["message"]

    assert json.loads(list_fn(None, session_id=sid_a))["ok"] is True

    unavailable = json.loads(delivery.tool_session_send({"target": "whatever", "message": "hi"}))
    assert unavailable["error"] == "plugin_context_unavailable"
    assert isinstance(unavailable.get("message"), str) and unavailable["message"]


def test_register_passes_skill_metadata_and_keeps_minimal_hosts_working(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """W216a: forward SKILL.md frontmatter when supported, fall back to (name, path) otherwise."""
    hermes_intercom = _load_plugin()
    home = tmp_path / "hermes_home"
    home.mkdir()
    _create_state_db(home / "state.db")

    class MetadataSkillContext(RecordingContext):
        def __init__(self, home_path: Path) -> None:
            super().__init__(home_path)
            self.skill_meta: dict[str, dict[str, Any]] = {}

        def register_skill(
            self, name: str, path: Path, description: str = "", frontmatter: Any = None
        ) -> None:
            self.skill_meta[name] = {"description": description, "frontmatter": dict(frontmatter or {})}
            super().register_skill(name, path)

    monkeypatch.setenv("HERMES_HOME", str(home))
    rich_ctx = MetadataSkillContext(home)
    hermes_intercom.register(rich_ctx)

    assert "hermes-intercom" in rich_ctx.skill_meta, "bundled skill was not registered"
    meta = rich_ctx.skill_meta["hermes-intercom"]
    assert meta["description"].startswith("Use when"), meta
    assert meta["frontmatter"].get("name") == "hermes-intercom"

    plain_ctx = RecordingContext(home)
    hermes_intercom.register(plain_ctx)
    assert "hermes-intercom" in plain_ctx.skills, "two-arg register_skill hosts must keep working"


def test_hop_memory_and_send_buckets_stay_bounded(tmp_path: Path) -> None:
    """W213a/W212b: bounded hop memory and a 600s floor on send-bucket eviction."""
    delivery = _load_plugin().delivery
    now = time.monotonic()

    with delivery._LOCK:
        for idx in range(delivery._TURN_HOP_MAX + 64):
            delivery._remember_turn_hop(("h", f"s{idx}"), 1, 3, now)
        assert len(delivery._TURN_HOP) <= delivery._TURN_HOP_MAX

        delivery._TURN_HOP[("h", "stale")] = (1, 0, now - delivery._TURN_HOP_TTL - 10.0)
        delivery._evict_stale_turn_hops(now)
        assert ("h", "stale") not in delivery._TURN_HOP

        delivery._RECENT_SENDS[("h", "target")] = [(now - 120.0, "sid", "digest", "token")]
        delivery._BUCKET_HORIZON[("h", "target")] = 600.0
        delivery._evict_stale_send_buckets(now)
        assert ("h", "target") in delivery._RECENT_SENDS, "a 1s caller must not evict a 600s bucket"


def test_a_second_call_without_request_id_is_not_the_same_operation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """B03: the public contract must reuse a caller-supplied request id.

    A fresh uuid minted inside every call, and a schema that never accepts one,
    cannot collapse an ambiguous retry. Two different operations stay two ids.
    """
    hermes_intercom = _load_plugin()
    delivery = hermes_intercom.delivery
    home = tmp_path / "hermes_home"
    home.mkdir()
    db_path = home / "state.db"
    _create_state_db(db_path)
    sid_a, sid_b = _seed_two_sessions(db_path)

    monkeypatch.setenv("HERMES_HOME", str(home))
    ctx = RecordingContext(
        home,
        settings={"max_messages_per_window": 10, "auto_mount_dormant": True},
        allow_inject=False,
    )
    ctx.allow_gateway_injection = True  # type: ignore[attr-defined]
    hermes_intercom.register(ctx)
    send_fn = ctx.tools["session_send"]["handler"]
    schema = ctx.tools["session_send"]["schema"]["parameters"]["properties"]
    assert "request_id" in schema, "the public schema never accepts a retry identity"

    con = sqlite3.connect(str(db_path))
    con.execute("UPDATE sessions SET session_key = NULL WHERE id = ?", (sid_b,))
    con.commit()
    con.close()

    seen: list[str] = []

    def fake_ws_submit(
        home_dir: Path, target_sid: str, content: str, *, timeout: float = 8.0, request_id: str = ""
    ) -> tuple[bool, str]:
        seen.append(request_id)
        return True, "admission-1"

    monkeypatch.setattr(delivery, "submit_via_gateway_ws", fake_ws_submit)
    monkeypatch.setattr(delivery, "mount_dormant_local_session", lambda *a, **kw: (False, "no_inproc"))

    token = "retry-token-stable"
    first = json.loads(
        send_fn(
            {"target": sid_b, "message": "same body", "request_id": token},
            session_id=sid_a,
        )
    )
    assert first["ok"] is True, first
    assert first["request_id"] == token
    assert seen == [token]

    # The process-local dedup window must not swallow a genuine retry of the
    # same operation: the gateway's admission key is what collapses it.
    delivery._RECENT_SENDS.clear()
    seen.clear()
    again = json.loads(
        send_fn(
            {"target": sid_b, "message": "same body", "request_id": token},
            session_id=sid_a,
        )
    )
    assert again["ok"] is True, again
    assert again["request_id"] == token
    assert seen == [token], "the second call minted a new id instead of reusing the caller's"

    seen.clear()
    other = json.loads(
        send_fn(
            {"target": sid_b, "message": "different body", "request_id": "retry-token-other"},
            session_id=sid_a,
        )
    )
    assert other["ok"] is True, other
    assert other["request_id"] == "retry-token-other"
    assert seen == ["retry-token-other"]


def test_a_second_active_title_outside_the_window_is_not_unique(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """B07: title uniqueness is false when the other match sits past the raw scan."""
    hermes_intercom = _load_plugin()
    home = tmp_path / "hermes_home"
    home.mkdir()
    db_path = home / "state.db"
    _create_state_db(db_path)
    sid_a, _sid_b = _seed_two_sessions(db_path)
    recent = _insert_session(
        db_path,
        id="local-recent" + "1" * 54,
        title="Shared title",
        message_count=4,
        last_activity_at=9000.0,
        session_key="agent:main:local:dm:" + "local-recent" + "1" * 54,
    )
    hidden = "local-hidden" + "2" * 54
    con = sqlite3.connect(str(db_path))
    con.execute(
        "INSERT INTO sessions (id, source, title, message_count, last_activity_at, session_key)"
        " VALUES (?,?,?,?,?,?)",
        (hidden, "local", "Shared title", 3, 1.0, f"agent:main:local:dm:{hidden}"),
    )
    con.executemany(
        "INSERT INTO sessions (id, source, title, message_count, last_activity_at, session_key)"
        " VALUES (?,?,?,?,?,?)",
        [
            (
                f"local-n{i:062d}",
                "local",
                f"noise {i}",
                1,
                1000 + i,
                f"agent:main:local:dm:local-n{i:062d}",
            )
            for i in range(10050)
        ],
    )
    con.commit()
    con.close()

    monkeypatch.setenv("HERMES_HOME", str(home))
    ctx = RecordingContext(home)
    hermes_intercom.register(ctx)
    res = json.loads(
        ctx.tools["session_send"]["handler"](
            {"target": "Shared title", "message": "which one"}, session_id=sid_a
        )
    )
    assert res["ok"] is False, res
    assert res["error"] == "ambiguous_target", res
    ids = {c["session_id"] for c in res["candidates"]}
    assert recent in ids and hidden in ids, ids


