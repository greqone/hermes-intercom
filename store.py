"""Read-only SQLite session store discovery, target resolution, and hop parsing."""
from __future__ import annotations

import json
import os
import re
import sqlite3
from pathlib import Path
from typing import Any

INTERCOM_TAG_RE = re.compile(
    r"\[intercom\s+from=([^\s\]]+)\s+hop=(\d+)/(\d+)\s+mode=(reply|fyi)\]"
)


def resolve_home(ctx: Any = None) -> Path:
    if ctx is not None:
        home = getattr(ctx, "home_path", None)
        if home:
            return Path(home).resolve()
        mgr = getattr(ctx, "_manager", None)
        if mgr is not None and getattr(mgr, "home_path", None):
            return Path(mgr.home_path).resolve()
    env_home = os.environ.get("HERMES_HOME")
    if env_home:
        return Path(env_home).resolve()
    try:
        from hermes_constants import get_hermes_home
        return Path(get_hermes_home()).resolve()
    except Exception:
        return (Path.home() / ".hermes").resolve()


def profile_namespace(ctx: Any = None) -> str:
    name = "default"
    if ctx is not None and hasattr(ctx, "profile_name"):
        try:
            name = str(ctx.profile_name or "default")
        except Exception:
            name = "default"
    if name in ("", "default"):
        return "main"
    if name == "main":
        return "main~"
    return name


def calling_session_key() -> str:
    try:
        from gateway.session_context import get_session_env
        return get_session_env("HERMES_SESSION_KEY", "") or ""
    except Exception:
        return ""


def _connect_ro(db_path: Path) -> sqlite3.Connection:
    uri = f"file:{db_path.as_posix()}?mode=ro"
    con = sqlite3.connect(uri, uri=True, timeout=5.0)
    con.row_factory = sqlite3.Row
    return con


def _load_routing_map(con: sqlite3.Connection) -> tuple[dict[str, str], dict[str, dict[str, Any]]]:
    """Return (session_id -> session_key, session_key -> parsed_entry)."""
    sid_to_key: dict[str, str] = {}
    key_to_entry: dict[str, dict[str, Any]] = {}
    try:
        rows = con.execute("SELECT session_key, entry_json FROM gateway_routing").fetchall()
    except sqlite3.Error:
        return sid_to_key, key_to_entry
    for r in rows:
        skey = r["session_key"] or ""
        if not skey:
            continue
        try:
            entry = json.loads(r["entry_json"] or "{}")
        except Exception:
            entry = {}
        if isinstance(entry, dict):
            key_to_entry[skey] = entry
            sid = str(entry.get("session_id") or "")
            if sid:
                sid_to_key[sid] = skey
    return sid_to_key, key_to_entry


def query_sessions(
    home: Path,
    *,
    ns: str = "main",
    caller_session_id: str = "",
    caller_session_key: str = "",
    query: str = "",
    source: str = "",
    limit: int = 20,
    include_cron: bool = False,
    include_archived: bool = False,
) -> list[dict[str, Any]]:
    db_path = home / "state.db"
    if not db_path.is_file():
        return []

    limit = max(1, min(int(limit or 20), 100))
    q_norm = (query or "").strip().lower()
    src_norm = (source or "").strip().lower()

    con = _connect_ro(db_path)
    try:
        sid_to_key, _ = _load_routing_map(con)
        sql = """
            SELECT
                id, source, model, parent_session_id, started_at, ended_at,
                end_reason, message_count, cwd, title, archived, hidden,
                session_key, last_activity_at
            FROM sessions
            WHERE COALESCE(end_reason, '') != 'compressed'
              AND (parent_session_id IS NULL OR session_key IS NOT NULL)
        """
        rows = con.execute(sql).fetchall()
    finally:
        con.close()

    by_route: dict[str, dict[str, Any]] = {}
    for r in rows:
        sid = str(r["id"] or "")
        if not sid:
            continue
        title = str(r["title"] or "")
        if r["parent_session_id"] and not r["session_key"] and title.startswith("Subagent:"):
            continue
        if not include_archived and (int(r["archived"] or 0) != 0 or int(r["hidden"] or 0) != 0):
            continue
        row_source = str(r["source"] or "")
        if not include_cron and (row_source.lower() == "cron" or sid.startswith("cron_")):
            continue
        msg_count = int(r["message_count"] or 0)
        if not include_cron and msg_count == 0 and not title:
            continue

        skey = str(r["session_key"] or "") or sid_to_key.get(sid, "")
        if not skey and sid.startswith("local-"):
            skey = f"agent:{ns}:local:dm:{sid}"

        root_sid = sid
        if skey and ":local:dm:local-" in skey:
            root_sid = skey.rsplit(":", 1)[-1]

        last_active = float(r["last_activity_at"] or r["started_at"] or 0.0)
        is_self = bool(
            (caller_session_id and caller_session_id in (sid, root_sid))
            or (caller_session_key and skey and skey == caller_session_key)
        )

        item = {
            "session_id": sid,
            "root_session_id": root_sid,
            "title": title,
            "source": row_source,
            "model": str(r["model"] or ""),
            "cwd": str(r["cwd"] or ""),
            "session_key": skey,
            "message_count": msg_count,
            "last_activity_at": last_active,
            "addressable": bool(skey),
            "is_self": is_self,
        }

        dedup_key = skey or sid
        existing = by_route.get(dedup_key)
        if existing is None or item["last_activity_at"] >= existing["last_activity_at"]:
            by_route[dedup_key] = item

    items = list(by_route.values())
    items.sort(key=lambda x: x["last_activity_at"], reverse=True)

    filtered: list[dict[str, Any]] = []
    for item in items:
        if src_norm and item["source"].lower() != src_norm:
            continue
        if q_norm:
            haystack = " ".join(
                [
                    item["session_id"],
                    item["root_session_id"],
                    item["title"],
                    item["source"],
                    item["cwd"],
                    item["session_key"],
                ]
            ).lower()
            if q_norm not in haystack:
                continue
        filtered.append(item)
        if len(filtered) >= limit:
            break

    return filtered


def resolve_target(
    home: Path,
    target: str,
    *,
    ns: str = "main",
    caller_session_id: str = "",
    caller_session_key: str = "",
) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
    """Resolve target by exact session_id/root_session_id/session_key, >=6-char ID prefix, or unique title substring."""
    needle = (target or "").strip()
    if not needle:
        return None, []
    needle_low = needle.lower()

    all_sessions = query_sessions(
        home,
        ns=ns,
        caller_session_id=caller_session_id,
        caller_session_key=caller_session_key,
        limit=100,
        include_cron=True,
        include_archived=True,
    )

    exact = [
        s
        for s in all_sessions
        if needle_low
        in (
            s["session_id"].lower(),
            s["root_session_id"].lower(),
            s["session_key"].lower(),
            s["title"].strip().lower(),
        )
        and needle_low != ""
    ]
    if len(exact) == 1:
        return exact[0], []
    if len(exact) > 1:
        active_exact = [s for s in exact if s["source"].lower() != "cron"]
        if len(active_exact) == 1:
            return active_exact[0], []
        return None, exact[:8]

    if len(needle_low) >= 6:
        prefix_hits = [
            s
            for s in all_sessions
            if s["session_id"].lower().startswith(needle_low)
            or s["root_session_id"].lower().startswith(needle_low)
        ]
        if len(prefix_hits) == 1:
            return prefix_hits[0], []
        if len(prefix_hits) > 1:
            return None, prefix_hits[:8]

    active_sessions = [s for s in all_sessions if s["source"].lower() != "cron"]
    title_hits = [s for s in active_sessions if s["title"] and needle_low in s["title"].lower()]
    if len(title_hits) == 1:
        return title_hits[0], []
    if len(title_hits) > 1:
        return None, title_hits[:8]

    return None, []


def lookup_sender_info(
    home: Path,
    session_id: str,
    *,
    ns: str = "main",
    caller_session_key: str = "",
) -> dict[str, str]:
    """Return {"session_id", "title", "short_id"} for the calling session."""
    sid = (session_id or "").strip()
    if not sid and caller_session_key and ":local:dm:local-" in caller_session_key:
        sid = caller_session_key.rsplit(":", 1)[-1]

    title = ""
    db_path = home / "state.db"
    if sid and db_path.is_file():
        con = _connect_ro(db_path)
        try:
            row = con.execute("SELECT title FROM sessions WHERE id = ? LIMIT 1", (sid,)).fetchone()
            if row and row["title"]:
                title = str(row["title"])
        except sqlite3.Error:
            pass
        finally:
            con.close()

    short_id = sid[:14] if sid.startswith("local-") else (sid[:16] if sid else "unknown")
    return {"session_id": sid or "unknown", "title": title, "short_id": short_id}


def parse_hop_from_text(text: str | None) -> int:
    if not text or not isinstance(text, str):
        return 0
    m = INTERCOM_TAG_RE.search(text)
    if not m:
        return 0
    try:
        return max(0, int(m.group(2)))
    except ValueError:
        return 0


def latest_user_message_hop(home: Path, session_id: str) -> int:
    """Fallback hop reader: check the latest role='user' message in state.db for session_id."""
    if not session_id:
        return 0
    db_path = home / "state.db"
    if not db_path.is_file():
        return 0
    con = _connect_ro(db_path)
    try:
        row = con.execute(
            "SELECT content FROM messages WHERE session_id = ? AND role = 'user' ORDER BY id DESC LIMIT 1",
            (session_id,),
        ).fetchone()
        if row and row["content"]:
            return parse_hop_from_text(str(row["content"]))
    except sqlite3.Error:
        return 0
    finally:
        con.close()
    return 0


def sanitize_card_sender(raw_label: str) -> str:
    """Format a sender name guaranteed to match Desktop's AGENT_MESSAGE_RE (no single quotes, 1..64 chars)."""
    cleaned = re.sub(r"['\r\n\t]+", " ", str(raw_label or "")).strip()
    cleaned = re.sub(r"\s+", " ", cleaned)
    if not cleaned:
        cleaned = "Hermes Session"
    if len(cleaned) > 60:
        cleaned = cleaned[:59].rstrip() + "…"
    return cleaned


def format_intercom_message(
    *,
    sender_card_name: str,
    sender_session_id: str,
    hop: int,
    max_hops: int,
    expect_reply: bool,
    message: str,
) -> str:
    card_name = sanitize_card_sender(sender_card_name)
    mode = "reply" if expect_reply else "fyi"
    tag = f"[intercom from={sender_session_id} hop={hop}/{max_hops} mode={mode}]"
    if expect_reply and hop < max_hops:
        hint = (
            f"Reply requested by session `{sender_session_id}` (hop {hop}/{max_hops}). "
            f"When done, send your concise reply with `session_send(target=\"{sender_session_id}\", message=\"...\")`."
        )
    elif expect_reply and hop >= max_hops:
        hint = (
            f"Message from session `{sender_session_id}` (final autonomous hop {hop}/{max_hops} — "
            f"do not call `session_send` back automatically)."
        )
    else:
        hint = (
            f"FYI from session `{sender_session_id}` (hop {hop}/{max_hops}). "
            f"No automatic reply needed unless asked."
        )
    return f"[Message from agent '{card_name}'] {tag}\n{hint}\n\n{message.strip()}"
