"""Tier B real Hermes PluginManager + tool registry integration smoke tests."""
from __future__ import annotations

import json
import os
import shutil
import sqlite3
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _hermes_repo() -> Path:
    """Host checkout: HERMES_AGENT_REPO, then the per-user install, then the POSIX default."""
    candidates = [
        os.environ.get("HERMES_AGENT_REPO") or "",
        str(Path(os.environ.get("LOCALAPPDATA", "")) / "hermes" / "hermes-agent"),
        str(Path.home() / ".hermes" / "hermes-agent"),
    ]
    for raw in candidates:
        if raw and Path(raw).is_dir():
            return Path(raw)
    return Path(candidates[0] or ".")


HERMES_REPO = _hermes_repo()


def _seed_disposable_home(home: Path, *, allow_injection: bool) -> tuple[str, str]:
    (home / "plugins").mkdir(parents=True, exist_ok=True)
    plugin_dst = home / "plugins" / "hermes-intercom"
    shutil.copytree(
        ROOT,
        plugin_dst,
        ignore=shutil.ignore_patterns(".git", "__pycache__", ".pytest_cache", "tests"),
    )

    allow_str = "true" if allow_injection else "false"
    (home / "config.yaml").write_text(
        f"""plugins:
  enabled:
    - hermes-intercom
  entries:
    hermes-intercom:
      allow_gateway_injection: {allow_str}
""",
        encoding="utf-8",
    )

    db_path = home / "state.db"
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
    con.execute("CREATE TABLE gateway_routing (session_key TEXT PRIMARY KEY, entry_json TEXT)")
    con.execute(
        "CREATE TABLE messages (id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT, role TEXT, content TEXT, timestamp REAL)"
    )

    sid_a = "local-1111111111111111111111111111111111111111111111111111111111111111"
    sid_b = "local-2222222222222222222222222222222222222222222222222222222222222222"
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
                5,
                "E:/Projekty/one",
                "Sender Thread One",
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
                8,
                "E:/Projekty/two",
                "Recipient Thread Two",
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


@pytest.mark.skipif(
    not (HERMES_REPO / "hermes_cli").is_dir(),
    reason="Hermes core checkout not present",
)
def test_real_plugin_manager_and_registry_dispatch_positive_and_negative(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    if str(HERMES_REPO) not in sys.path:
        sys.path.insert(0, str(HERMES_REPO))

    from hermes_cli.plugins import PluginManager
    from tools.registry import registry

    # 1. Positive control: allow_gateway_injection: true + published gateway injector
    home_ok = tmp_path / "home_ok"
    sid_a, sid_b = _seed_disposable_home(home_ok, allow_injection=True)
    monkeypatch.setenv("HERMES_HOME", str(home_ok))

    injected_Payloads: list[dict] = []

    class FakeGatewayOwner:
        pass

    owner = FakeGatewayOwner()

    def fake_injector(**kwargs) -> bool:
        injected_Payloads.append(kwargs)
        return True

    mgr = PluginManager(scope_key=str(home_ok))
    mgr.set_gateway_message_injector(owner, fake_injector)
    try:
        mgr.discover_and_load()
        assert "hermes-intercom" in mgr._plugins
        loaded = mgr._plugins["hermes-intercom"]
        assert not loaded.error, f"Plugin failed to load: {loaded.error}"
        assert any("hermes-intercom" in k for k in mgr._plugin_skills)

        list_raw = registry.dispatch("session_list", {}, session_id=sid_a)
        list_res = json.loads(list_raw)
        assert list_res["ok"] is True
        assert list_res["count"] == 2

        send_raw = registry.dispatch(
            "session_send",
            {"target": "Recipient Thread Two", "message": "elo z prawdziwego rejestru xD", "expect_reply": True},
            session_id=sid_a,
        )
        send_res = json.loads(send_raw)
        assert send_res["ok"] is True
        assert send_res["target_session_id"] == sid_b
        assert len(injected_Payloads) == 1
        assert injected_Payloads[0]["session_key"] == f"agent:main:local:dm:{sid_b}"
        assert "elo z prawdziwego rejestru xD" in injected_Payloads[0]["content"]
    finally:
        mgr.unload()
        mgr.clear_gateway_message_injector(owner)

    # 2. Negative control: allow_gateway_injection: false must fail closed
    home_deny = tmp_path / "home_deny"
    sid_a2, sid_b2 = _seed_disposable_home(home_deny, allow_injection=False)
    monkeypatch.setenv("HERMES_HOME", str(home_deny))

    mgr_deny = PluginManager(scope_key=str(home_deny))
    mgr_deny.set_gateway_message_injector(owner, fake_injector)
    try:
        mgr_deny.discover_and_load()
        deny_raw = registry.dispatch(
            "session_send",
            {"target": sid_b2, "message": "should be denied"},
            session_id=sid_a2,
        )
        deny_res = json.loads(deny_raw)
        assert deny_res["ok"] is False
        assert deny_res["error"] == "injection_refused"
        assert len(injected_Payloads) == 1  # unchanged
    finally:
        mgr_deny.unload()
        mgr_deny.clear_gateway_message_injector(owner)
