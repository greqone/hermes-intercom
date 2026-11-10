"""Read-only SQLite session store discovery, target resolution, and hop parsing."""
from __future__ import annotations

import json
import math
import os
import re
import sqlite3
import urllib.parse
from pathlib import Path
from typing import Any

# Minimum accepted session_id prefix length for target resolution.
MIN_ID_PREFIX_LEN = 6

# Characters that must never reach a Desktop agent card sender label: quotes/brackets,
# CR/LF/TAB and control chars, lone UTF-16 surrogates (which cannot be UTF-8 encoded), and
# bidi/zero-width formatting characters that can visually reorder a rendered card.
_SENDER_STRIP_RE = re.compile(
    r"['\"\[\]\r\n\t\x00-\x1f\x7f]"
    r"|[\ud800-\udfff\u200b-\u200f\u202a-\u202e\u2060-\u2069\ufeff]"
)

# Cards/tags are parsed only at the START of the text (never inside a user-supplied body).
INTERCOM_CARD_PREFIX_RE = re.compile(
    r"^\s*\[Message from agent '[^'\[\]\r\n]{1,64}'\]\s+"
    r"\[intercom\s+from=([^\s\]]+)\s+hop=(\d+)/(\d+)\s+mode=(reply|fyi)\]"
)
BARE_INTERCOM_TAG_RE = re.compile(
    r"^\s*\[intercom\s+from=([^\s\]]+)\s+hop=(\d+)/(\d+)\s+mode=(reply|fyi)\]"
)
# A queued prompt can be prepended in front of the card, so the card may start a later line.
_INTERCOM_CARD_LINE_RE = re.compile(
    r"^\s*\[Message from agent '[^'\[\]\r\n]{1,64}'\]\s+"
    r"\[intercom\s+from=([^\s\]]+)\s+hop=(\d+)/(\d+)\s+mode=(reply|fyi)\]",
    re.MULTILINE,
)
# Backwards-compatible alias for the primary (full card) pattern.
INTERCOM_TAG_RE = INTERCOM_CARD_PREFIX_RE

# Columns optional in ``sessions``; a partial/older ``state.db`` projects them as ``NULL AS <col>``.
_SESSION_OPTIONAL_COLUMNS = (
    "source",
    "model",
    "parent_session_id",
    "started_at",
    "ended_at",
    "end_reason",
    "message_count",
    "cwd",
    "title",
    "archived",
    "hidden",
    "session_key",
    "last_activity_at",
)

# End reasons that mark a session segment as a superseded compression ancestor.
_COMPRESSED_END_REASONS = frozenset({"compression", "compressed"})
# A row ended for any of these keeps the key the live conversation now owns. Naming it delivers
# into the new conversation while the result still quotes the old id (R5-S01).
_RESET_END_REASONS = frozenset({"session_reset", "idle", "daily"})



class StoreReadError(RuntimeError):
    """state.db exists but could not be read. Distinct from an empty result."""


def resolve_home(ctx: Any = None) -> Path:
    """Resolve the Hermes home directory for this call.

    Order: explicit ``ctx.home_path`` -> the host manager's home (``ctx._manager.home_path``,
    which is where ``PluginContext`` actually keeps the profile) -> ``hermes_constants.get_hermes_home()``
    (which already honors the thread/async ContextVar override *and* ``HERMES_HOME``) -> a stripped and
    expanded ``HERMES_HOME`` -> ``~/.hermes``.
    """
    if ctx is not None:
        home = getattr(ctx, "home_path", None)
        if home is None:
            manager = getattr(ctx, "_manager", None)
            home = getattr(manager, "home_path", None)
        if home is not None:
            text = str(home).strip()
            if text:
                return Path(os.path.expanduser(os.path.expandvars(text))).resolve()
    try:
        from hermes_constants import get_hermes_home

        core_home = get_hermes_home()
        if core_home is not None and str(core_home).strip():
            return Path(os.path.expanduser(os.path.expandvars(str(core_home).strip()))).resolve()
    except Exception:
        pass
    env_home = (os.environ.get("HERMES_HOME") or "").strip()
    if env_home:
        return Path(os.path.expanduser(os.path.expandvars(env_home))).resolve()
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


def sanitize_utf8_text(text: str) -> str:
    """Return ``text`` with any lone UTF-16 surrogate replaced so it can be UTF-8 encoded.

    ``hashlib.sha256``, ``json.dumps`` and SQLite all raise ``UnicodeEncodeError`` on a lone
    surrogate (a real risk when text arrives from a Desktop/CLI paste), so every string that
    leaves the plugin passes through here first.
    """
    if not isinstance(text, str):
        text = "" if text is None else str(text)
    try:
        return text.encode("utf-8", "replace").decode("utf-8")
    except (UnicodeEncodeError, UnicodeDecodeError):  # pragma: no cover - defensive
        return text.encode("utf-8", "replace").decode("utf-8", "replace")


def _safe_int(val: Any, default: int = 0) -> int:
    """Coerce a SQLite cell to int; text such as ``'false'``/``'many'`` yields ``default``."""
    if isinstance(val, bool):
        return int(val)
    if isinstance(val, float) and not math.isfinite(val):
        return default
    try:
        return int(val)
    except (TypeError, ValueError, OverflowError):
        return default


def _safe_float(val: Any, default: float = 0.0) -> float:
    """Coerce a SQLite cell to a finite float; NaN/inf/text yields ``default``."""
    if isinstance(val, bool):
        return default
    try:
        parsed = float(val)
    except (TypeError, ValueError, OverflowError):
        return default
    if not math.isfinite(parsed):
        return default
    return parsed


def _row_get(row: Any, key: str, default: Any = None) -> Any:
    """Read one column from a ``sqlite3.Row`` (or plain tuple) without ever raising."""
    try:
        return row[key]
    except (IndexError, KeyError, TypeError):
        return default


def _column_names(con: sqlite3.Connection, table: str) -> list[str]:
    """Column names of ``table`` via ``PRAGMA table_info``; empty when the table is absent."""
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", table or ""):
        return []
    try:
        rows = con.execute(f"PRAGMA table_info({table})").fetchall()
    except sqlite3.DatabaseError:
        # A file that is not a database is a read failure, not a table that happens to
        # have no columns. Callers turn this into store_unreadable instead of "no sessions".
        raise
    except sqlite3.Error:
        return []
    names: list[str] = []
    for row in rows:
        name = _row_get(row, "name")
        if name is None:
            name = _row_get(row, 1)
        text = str(name or "").strip()
        if text:
            names.append(text)
    return names


def _projection(columns: set[str], wanted: tuple[str, ...]) -> str:
    """Build a SELECT list aliasing absent columns as ``NULL`` so older schemas never fail."""
    return ", ".join(
        f"{col} AS {col}" if col in columns else f"NULL AS {col}" for col in wanted
    )


def _sqlite_uri(db_path: Path) -> str:
    """Percent-encoded ``file:`` URI for a read-only open of ``state.db``.

    Encoding keeps ``#``, ``%`` and ``?`` inside directory names from breaking the URI.

    A UNC path keeps its leading slashes (``resolve()`` would drop them on POSIX) and uses the
    four-slash form SQLite opens on Windows. The five-slash form fails against a real share.
    """
    raw_posix = db_path.as_posix()
    is_unc = raw_posix.startswith("//") or str(db_path).startswith("\\\\")
    if is_unc:
        # ``resolve()`` on POSIX collapses ``//server/share`` to ``/server/share``. Keep the UNC form,
        # and keep the four slashes: on Windows SQLite the five-slash form fails to open a real
        # ``\\localhost\c$`` share while four slashes opens it (measured 2026-11-09).
        posix = raw_posix if raw_posix.startswith("//") else "//" + str(db_path).lstrip("\\").replace("\\", "/")
        # ``file:`` plus ``////server`` is the four-slash form. ``as_posix()`` only keeps two, and
        # ``file://server`` does not open a Windows share.
        encoded = urllib.parse.quote("//" + posix, safe="/:")
    else:
        try:
            resolved = db_path.resolve()
        except OSError:
            resolved = db_path
        posix = resolved.as_posix()
        encoded = urllib.parse.quote(posix, safe="/:")
    return f"file:{encoded}?mode=ro"


def _connect_ro(db_path: Path) -> sqlite3.Connection:
    """Open state.db read-only through :func:`_sqlite_uri`."""
    con = sqlite3.connect(_sqlite_uri(db_path), uri=True, timeout=5.0)
    con.row_factory = sqlite3.Row
    return con


def _load_routing_map(
    con: sqlite3.Connection, home: Path | None = None
) -> tuple[dict[str, str], dict[str, dict[str, Any]]]:
    """Return (session_id -> session_key, session_key -> parsed_entry).

    Column presence is discovered through ``PRAGMA table_info`` *before* the query is built, so a
    minimal ``gateway_routing`` (``session_key``/``entry_json``) and a full one (``scope``,
    ``updated_at``) both work on the first attempt - a failed query would otherwise burn two 5s
    busy timeouts against a locked database.

    When ``home`` is given, rows carrying a non-empty ``scope`` that differs from ``<home>/sessions``
    are ignored (they belong to another profile's store).
    """
    sid_to_key: dict[str, str] = {}
    key_to_entry: dict[str, dict[str, Any]] = {}

    expected_scope = ""
    if home is not None:
        try:
            expected_scope = str((Path(home) / "sessions").resolve())
        except OSError:
            expected_scope = ""

    columns = _column_names(con, "gateway_routing")
    if not columns:
        return sid_to_key, key_to_entry
    colset = {c.lower() for c in columns}
    if "session_key" not in colset:
        return sid_to_key, key_to_entry

    has_entry = "entry_json" in colset
    has_scope = "scope" in colset
    has_updated = "updated_at" in colset

    wanted: list[str] = ["session_key"]
    if has_entry:
        wanted.append("entry_json")
    if has_scope:
        wanted.append("scope")
    if has_updated:
        wanted.append("updated_at")

    where = ""
    params: tuple[Any, ...] = ()
    if has_scope and expected_scope:
        # NOCASE keeps a Windows drive-letter/UNC case drift from hiding this profile's rows; the
        # authoritative comparison is the normcase/normpath check below.
        where = " WHERE (scope IS NULL OR scope = '' OR scope = ? COLLATE NOCASE)"
        params = (expected_scope,)
    order = " ORDER BY COALESCE(updated_at, 0) DESC" if has_updated else ""
    sql = f"SELECT {', '.join(wanted)} FROM gateway_routing{where}{order} LIMIT 10000"
    try:
        rows = con.execute(sql, params).fetchall()
    except sqlite3.Error:
        return sid_to_key, key_to_entry

    norm_expected = os.path.normcase(os.path.normpath(expected_scope)) if expected_scope else ""
    for r in rows:
        skey = str(_row_get(r, "session_key") or "").strip()
        if not skey:
            continue
        if expected_scope:
            row_scope = str(_row_get(r, "scope") or "").strip()
            if row_scope:
                try:
                    norm_row = os.path.normcase(os.path.normpath(row_scope))
                except (OSError, ValueError):
                    norm_row = row_scope
                if norm_row != norm_expected:
                    continue
        raw_json = _row_get(r, "entry_json") if has_entry else None
        if isinstance(raw_json, (bytes, bytearray)):
            try:
                raw_json = bytes(raw_json).decode("utf-8", "replace")
            except (UnicodeDecodeError, ValueError):
                raw_json = None
        if not isinstance(raw_json, str):
            # An untyped column can hold an int/float/blob; json.loads would raise TypeError.
            entry: Any = {}
        else:
            try:
                entry = json.loads(raw_json or "{}")
            except (sqlite3.Error, ValueError, TypeError):
                entry = {}
        if isinstance(entry, dict):
            key_to_entry.setdefault(skey, entry)
            sid = str(entry.get("session_id") or "")
            if sid:
                sid_to_key.setdefault(sid, skey)
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
    _max_limit: int = 100,
    _with_internal: bool = False,
) -> list[dict[str, Any]]:
    """List addressable sessions from a read-only ``state.db``.

    The query is bounded (``LIMIT max(500, min(max_limit*5, 10000))``) so a 50k-row store never
    materializes every row, and every optional column is projected as ``NULL`` when absent.
    ``_with_internal`` adds the private ``_archived``/``_cron``/``_active`` markers used by
    :func:`resolve_target`; they never reach tool output.
    """
    db_path = Path(home) / "state.db"
    if not db_path.is_file():
        return []

    max_limit = max(1, _safe_int(_max_limit, 100))
    limit = max(1, min(_safe_int(limit, 20), max_limit))
    sql_limit = max(500, min(max_limit * 5, 10000))
    q_norm = (query or "").strip().lower()
    src_norm = (source or "").strip().lower()

    con: sqlite3.Connection | None = None
    rows: list[Any] = []
    sid_to_key: dict[str, str] = {}
    try:
        con = _connect_ro(db_path)
        columns = _column_names(con, "sessions")
        if not columns:
            return []
        colset = {c.lower() for c in columns}
        if "id" not in colset:
            return []
        sid_to_key, _key_to_entry = _load_routing_map(con, home)
        select_list = "id, " + _projection(colset, _SESSION_OPTIONAL_COLUMNS)
        order = ""
        if "last_activity_at" in colset and "started_at" in colset:
            order = " ORDER BY COALESCE(last_activity_at, started_at, 0) DESC"
        elif "last_activity_at" in colset:
            order = " ORDER BY COALESCE(last_activity_at, 0) DESC"
        elif "started_at" in colset:
            order = " ORDER BY COALESCE(started_at, 0) DESC"
        # F10: subagents and cron rows used to fill the window before any filtering, so a busy
        # profile returned an empty list. Exclude them in SQL; the Python pass stays as a backstop.
        where_parts: list[str] = []
        if "source" in colset:
            excluded = "'subagent'" if include_cron else "'subagent', 'cron'"
            where_parts.append(f"lower(COALESCE(source, '')) NOT IN ({excluded})")
        if not include_cron:
            where_parts.append("lower(id) NOT LIKE 'cron\\_%' ESCAPE '\\'")
        if "title" in colset:
            where_parts.append("COALESCE(title, '') NOT LIKE 'Subagent:%'")
        if "end_reason" in colset:
            # S-R05: a profile full of compression ancestors used to fill the window, so the
            # older live session was read and then discarded. Exclude them before the LIMIT.
            quoted = ", ".join(f"'{reason}'" for reason in sorted(_COMPRESSED_END_REASONS))
            where_parts.append(f"lower(COALESCE(end_reason, '')) NOT IN ({quoted})")
        if not include_archived:
            if "archived" in colset:
                where_parts.append("COALESCE(archived, 0) = 0")
            if "hidden" in colset:
                where_parts.append("COALESCE(hidden, 0) = 0")
        where_sql = (" WHERE " + " AND ".join(where_parts)) if where_parts else ""
        rows = con.execute(
            f"SELECT {select_list} FROM sessions{where_sql}{order} LIMIT ?", (sql_limit,)
        ).fetchall()
    except sqlite3.Error as exc:
        raise StoreReadError(str(exc)) from exc
    finally:
        if con is not None:
            con.close()

    by_route: dict[str, dict[str, Any]] = {}
    route_rank: dict[str, tuple[bool, float]] = {}
    for r in rows:
        sid = str(_row_get(r, "id") or "")
        if not sid:
            continue
        title = str(_row_get(r, "title") or "")
        row_source = str(_row_get(r, "source") or "")
        if row_source.strip().lower() == "subagent" or title.strip().startswith("Subagent:"):
            # Subagents are private work of a parent turn, never an intercom target.
            continue
        if str(_row_get(r, "end_reason") or "").strip().lower() in _COMPRESSED_END_REASONS:
            # Superseded compression segment: still reachable through resolve_target's alias tier.
            continue
        archived = _safe_int(_row_get(r, "archived"), 0)
        hidden = _safe_int(_row_get(r, "hidden"), 0)
        if not include_archived and (archived != 0 or hidden != 0):
            continue
        is_cron = row_source.strip().lower() == "cron" or sid.startswith("cron_")
        if not include_cron and is_cron:
            continue
        msg_count = _safe_int(_row_get(r, "message_count"), 0)
        if not include_cron and msg_count == 0 and not title:
            continue

        explicit_key = str(_row_get(r, "session_key") or "").strip() or sid_to_key.get(sid, "")
        # ``routed`` tells delivery whether a real gateway route exists. A merely synthesized
        # ``:local:dm:`` key can pass core's admissibility check and be dropped asynchronously, so a
        # dormant mount must happen before injecting.
        routed = bool(explicit_key)
        skey = explicit_key
        if not skey and sid.startswith("local-"):
            skey = f"agent:{ns}:local:dm:{sid}"

        root_sid = sid
        if skey and ":local:dm:local-" in skey:
            root_sid = skey.rsplit(":", 1)[-1]

        ended_at = _row_get(r, "ended_at")
        item_active = ended_at is None or ended_at == ""
        last_active = _safe_float(_row_get(r, "last_activity_at"), 0.0)
        if last_active <= 0.0:
            last_active = _safe_float(_row_get(r, "started_at"), 0.0)
        is_self = bool(
            (caller_session_id and caller_session_id in (sid, root_sid))
            or (caller_session_key and skey and skey == caller_session_key)
        )

        item: dict[str, Any] = {
            "session_id": sid,
            "root_session_id": root_sid,
            "title": title,
            "source": row_source,
            "model": str(_row_get(r, "model") or ""),
            "cwd": str(_row_get(r, "cwd") or ""),
            "session_key": skey,
            "message_count": msg_count,
            "last_activity_at": last_active,
            "addressable": bool(skey),
            "routed": routed,
            "is_self": is_self,
            "_archived": archived != 0 or hidden != 0,
            "_cron": is_cron,
            "_active": item_active,
        }

        dedup_key = skey or sid
        rank = (item_active, last_active)
        if dedup_key not in by_route or rank >= route_rank.get(dedup_key, (False, 0.0)):
            by_route[dedup_key] = item
            route_rank[dedup_key] = rank

    items = sorted(by_route.values(), key=lambda x: x["last_activity_at"], reverse=True)

    filtered: list[dict[str, Any]] = []
    for item in items:
        if src_norm and item["source"].strip().lower() != src_norm:
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

    if _with_internal:
        return filtered
    return [{k: v for k, v in item.items() if not k.startswith("_")} for item in filtered]


def _root_from_key(skey: str) -> str:
    """Session id embedded in a ``...:local:dm:<id>`` gateway key ("" when absent)."""
    text = str(skey or "")
    if ":local:dm:" not in text:
        return ""
    return text.rsplit(":", 1)[-1]


def _parent_session_id(con: sqlite3.Connection, sid: str) -> str:
    """``parent_session_id`` for ``sid`` ("" when unknown), never raising."""
    if not sid:
        return ""
    try:
        colset = {c.lower() for c in _column_names(con, "sessions")}
        if "id" not in colset or "parent_session_id" not in colset:
            return ""
        row = con.execute(
            "SELECT parent_session_id FROM sessions WHERE id = ? LIMIT 1", (sid,)
        ).fetchone()
    except sqlite3.Error:
        return ""
    if row is None:
        return ""
    return str(_row_get(row, "parent_session_id") or "").strip()


def _is_branch_edge(model_config: Any, ancestor_id: str) -> bool:
    """True when ``model_config`` records a branch/reset/delegate edge off ``ancestor_id``.

    Compression continuations carry none of these markers, so a descendant without one stays a
    candidate. A descendant with one is a different conversation and must not inherit the reply.
    """
    if not isinstance(model_config, str) or not model_config.strip():
        return False
    try:
        parsed = json.loads(model_config)
    except (ValueError, TypeError):
        return False
    if not isinstance(parsed, dict):
        return False
    markers = ("_branched_from", "_reset_from", "_delegate_from")
    return any(str(parsed.get(key) or "").strip().lower() == ancestor_id.lower() for key in markers)


def _same_title(left: str, right: str) -> bool:
    """Unicode-aware equality. ``casefold`` folds Ż→ż and ß→ss; SQLite ``lower()`` folds neither."""
    return left.casefold() == right.casefold()


def _ancestor_descendants(
    home: Path, needle_low: str, all_sessions: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Live descendants of ``needle_low``, with branch/reset/delegate edges removed.

    Empty when the id is not an ancestor. More than one entry means the lineage is ambiguous and
    the caller must say so instead of guessing.
    """
    if not needle_low or not all_sessions:
        return []
    db_path = Path(home) / "state.db"
    if not db_path.is_file():
        return []

    chain: list[str] = []
    ancestor_key = ""
    branched: set[str] = set()
    con: sqlite3.Connection | None = None
    try:
        con = _connect_ro(db_path)
        colset = {c.lower() for c in _column_names(con, "sessions")}
        if "id" not in colset:
            return []
        has_key = "session_key" in colset
        has_parent = "parent_session_id" in colset
        has_config = "model_config" in colset
        wanted = ["id"] + (["session_key"] if has_key else []) + (
            ["parent_session_id"] if has_parent else []
        ) + (["model_config"] if has_config else [])
        row = con.execute(
            f"SELECT {', '.join(wanted)} FROM sessions WHERE lower(id) = ? LIMIT 1", (needle_low,)
        ).fetchone()
        if row is None:
            return []
        ancestor_id = str(_row_get(row, "id") or "")
        if not ancestor_id:
            return []
        ancestor_key = str(_row_get(row, "session_key") or "").strip()
        chain = [ancestor_id.lower()]
        frontier = [ancestor_id]
        for _ in range(5):
            if not has_parent or not frontier:
                break
            marks = ",".join("?" for _ in frontier)
            child_rows = con.execute(
                f"SELECT {', '.join(wanted)} FROM sessions WHERE lower(parent_session_id) IN ({marks})",
                [item.lower() for item in frontier],
            ).fetchall()
            next_frontier: list[str] = []
            for child in child_rows:
                cid = str(_row_get(child, "id") or "")
                if not cid or cid.lower() in chain:
                    continue
                parent_id = str(_row_get(child, "parent_session_id") or ancestor_id)
                if _is_branch_edge(_row_get(child, "model_config"), parent_id):
                    branched.add(cid.lower())
                    continue
                chain.append(cid.lower())
                next_frontier.append(cid)
            frontier = next_frontier
    except sqlite3.Error:
        return []
    finally:
        if con is not None:
            con.close()

    by_sid = {s["session_id"].lower(): s for s in all_sessions}
    candidates: list[dict[str, Any]] = []
    seen: set[str] = set()
    for cid in chain:
        found = by_sid.get(cid)
        if found is not None and cid not in seen:
            candidates.append(found)
            seen.add(cid)
    for s in all_sessions:
        sid_low = s["session_id"].lower()
        if sid_low in seen:
            continue
        if ancestor_key and s["session_key"] == ancestor_key:
            candidates.append(s)
            seen.add(sid_low)
        elif s["root_session_id"].lower() == needle_low:
            candidates.append(s)
            seen.add(sid_low)

    candidates = [c for c in candidates if c["session_id"].lower() != needle_low]
    candidates = [c for c in candidates if c["session_id"].lower() not in branched]
    live = [
        c for c in candidates if not c.get("_archived") and not c.get("_cron") and c.get("_active", True)
    ]
    # An archived or cron descendant is not a conversation to deliver into. Falling back to it
    # made a compressed ancestor resolve to a session the user had already closed (Finding C).
    return live


def _resolve_ancestor_alias(
    home: Path, needle_low: str, all_sessions: list[dict[str, Any]]
) -> dict[str, Any] | None:
    """The single live continuation of an ended segment, or None when there is not exactly one."""
    pool = _ancestor_descendants(home, needle_low, all_sessions)
    if len(pool) == 1:
        return pool[0]
    return None




def _row_to_session(r, sid_to_key, *, ns, caller_session_id, caller_session_key):
    """One sessions row as a resolvable item. None for subagents and compression ancestors."""
    sid = str(_row_get(r, "id") or "")
    if not sid:
        return None
    title = str(_row_get(r, "title") or "")
    row_source = str(_row_get(r, "source") or "")
    if row_source.strip().lower() == "subagent" or title.strip().startswith("Subagent:"):
        return None
    if str(_row_get(r, "end_reason") or "").strip().lower() in _COMPRESSED_END_REASONS:
        return None
    explicit_key = str(_row_get(r, "session_key") or "").strip() or sid_to_key.get(sid, "")
    routed = bool(explicit_key)
    skey = explicit_key or (f"agent:{ns}:local:dm:{sid}" if sid.startswith("local-") else "")
    root_sid = skey.rsplit(":", 1)[-1] if skey and ":local:dm:local-" in skey else sid
    ended_at = _row_get(r, "ended_at")
    last_active = _safe_float(_row_get(r, "last_activity_at"), 0.0) or _safe_float(_row_get(r, "started_at"), 0.0)
    return {
        "session_id": sid,
        "root_session_id": root_sid,
        "title": title,
        "source": row_source,
        "model": str(_row_get(r, "model") or ""),
        "cwd": str(_row_get(r, "cwd") or ""),
        "session_key": skey,
        "message_count": _safe_int(_row_get(r, "message_count"), 0),
        "last_activity_at": last_active,
        "addressable": bool(skey),
        "routed": routed,
        "is_self": bool(
            (caller_session_id and caller_session_id in (sid, root_sid))
            or (caller_session_key and skey and skey == caller_session_key)
        ),
        "_archived": _safe_int(_row_get(r, "archived"), 0) != 0 or _safe_int(_row_get(r, "hidden"), 0) != 0,
        "_cron": row_source.strip().lower() == "cron" or sid.startswith("cron_"),
        "_active": ended_at is None or ended_at == "",
        "_end_reason": str(_row_get(r, "end_reason") or "").strip().lower(),
    }


def lookup_exact_session(home: Path, needle: str, *, ns: str = "main",
                         caller_session_id: str = "", caller_session_key: str = "") -> dict[str, Any] | None:
    """Resolve one exact session id or session_key with a single indexed read.

    ``query_sessions`` only scans the most recent rows, so an old session would otherwise be
    unreachable by the exact id its owner still has. Returns None for anything that is not an
    exact match, leaving prefix and title resolution to the bounded scan.
    """
    db_path = Path(home) / "state.db"
    text = (needle or "").strip()
    if not text or not db_path.is_file():
        return None
    con: sqlite3.Connection | None = None
    try:
        con = _connect_ro(db_path)
        colset = {c.lower() for c in _column_names(con, "sessions")}
        if "id" not in colset:
            return None
        select_list = "id, " + _projection(colset, _SESSION_OPTIONAL_COLUMNS)
        clauses = ["lower(id) = ?"]
        params: list[Any] = [text.lower()]
        if "session_key" in colset:
            clauses.append("lower(COALESCE(session_key, '')) = ?")
            params.append(text.lower())
        order_terms: list[str] = []
        if "ended_at" in colset:
            order_terms.append("(ended_at IS NULL OR ended_at = '') DESC")
        activity = [col for col in ("last_activity_at", "started_at") if col in colset]
        if activity:
            order_terms.append(f"COALESCE({', '.join(activity)}, 0) DESC")
        order_sql = (" ORDER BY " + ", ".join(order_terms)) if order_terms else ""
        row = con.execute(
            f"SELECT {select_list} FROM sessions WHERE {' OR '.join(clauses)}{order_sql} LIMIT 1",
            tuple(params),
        ).fetchone()
        if row is None:
            return None
        sid_to_key, _entries = _load_routing_map(con, home)
        built = _row_to_session(
            row, sid_to_key, ns=ns, caller_session_id=caller_session_id,
            caller_session_key=caller_session_key,
        )
    except sqlite3.Error:
        return None
    finally:
        if con is not None:
            con.close()
    return built


def _store_looks_like_id(needle_low: str) -> bool:
    """True only for the id shapes this plugin resolves: ``local-``, ``cron_`` or a hex token.

    A bare word such as a foreign title must not take the prefix path, because that path compares
    bytes and skips casefolding.
    """
    if needle_low.startswith(("local-", "cron_")):
        return True
    return bool(re.fullmatch(r"[0-9a-f-]{16,}", needle_low))


def _scan_title_and_prefix(
    home: Path, needle: str, needle_low: str, *, ns: str, caller_session_id: str, caller_session_key: str
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Every active-or-not row whose id prefix or title matches ``needle``.

    ``query_sessions`` stops at 10000 raw rows, so a second match past that window
    would make the first look unique. This reads the whole table but only keeps
    the rows that actually match, which for a title or a >=6-char prefix is small.
    """
    db_path = Path(home) / "state.db"
    if not needle or not db_path.is_file():
        return [], []
    want_prefix = len(needle_low) >= MIN_ID_PREFIX_LEN
    con: sqlite3.Connection | None = None
    prefix_hits: list[dict[str, Any]] = []
    title_hits: list[dict[str, Any]] = []
    seen_prefix: set[str] = set()
    seen_title: set[str] = set()
    try:
        con = _connect_ro(db_path)
        colset = {c.lower() for c in _column_names(con, "sessions")}
        if "id" not in colset:
            return [], []
        sid_to_key, _entries = _load_routing_map(con, home)
        select_list = "id, " + _projection(colset, _SESSION_OPTIONAL_COLUMNS)
        clauses: list[str] = []
        params: list[Any] = []
        if want_prefix and _store_looks_like_id(needle_low):
            escaped = needle_low.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            clauses.append("lower(id) LIKE ? ESCAPE '\\'")
            params.append(escaped + "%")
            if "session_key" in colset:
                # root_session_id is the trailing segment of a ``:local:dm:`` key, so a prefix of it
                # never matched a row whose own id differs (S04).
                clauses.append("lower(COALESCE(session_key, '')) LIKE ? ESCAPE '\\'")
                params.append("%:local:dm:" + escaped + "%")
        if "title" in colset:
            # No SQL predicate: SQLite lower() does not casefold Ż or ß, so the filter used to drop
            # the uppercase title before Python ever ranked it (S02). A prefix predicate is worse:
            # a title such as ``local-topic`` never matches ``lower(id) LIKE 'local-topic%'``, so
            # the row was discarded before the title fold ran. Title matching reads the rows.
            title_scan = True
            clauses = []
            params = []
        else:
            title_scan = False
        if not clauses and not title_scan:
            return [], []
        where_sql = (" WHERE " + " OR ".join(clauses)) if clauses else ""
        rows = con.execute(
            f"SELECT {select_list} FROM sessions{where_sql}", tuple(params)
        ).fetchall()
        for row in rows:
            built = _row_to_session(
                row, sid_to_key, ns=ns, caller_session_id=caller_session_id,
                caller_session_key=caller_session_key,
            )
            if built is None:
                continue
            sid = built["session_id"]
            if want_prefix and (
                sid.lower().startswith(needle_low) or built["root_session_id"].lower().startswith(needle_low)
            ) and sid not in seen_prefix:
                prefix_hits.append(built)
                seen_prefix.add(sid)
            title = built["title"]
            if title and needle.casefold() in title.casefold() and sid not in seen_title:
                title_hits.append(built)
                seen_title.add(sid)
    except sqlite3.Error as exc:
        raise StoreReadError(str(exc)) from exc
    finally:
        if con is not None:
            con.close()
    return prefix_hits, title_hits


def resolve_target(
    home: Path,
    target: str,
    *,
    ns: str = "main",
    caller_session_id: str = "",
    caller_session_key: str = "",
) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
    """Resolve a target string to one session (or an ambiguity list).

    Tiers, in order: exact session_id/root_session_id/session_key -> compressed ancestor alias ->
    unique ``>=MIN_ID_PREFIX_LEN`` id prefix -> title (exact, then substring). Prefix and title
    matches come from the whole table, not the recency window, so a second match past that
    window stays visible. An id prefix is never beaten by a title, so a chat titled ``local-ab``
    cannot hijack the prefix ``local-ab...``, and an archived chat with the same title never
    blocks the active one.
    """
    needle = (target or "").strip()
    if not needle:
        return None, []
    needle_low = needle.lower()

    direct = lookup_exact_session(
        home, needle, ns=ns, caller_session_id=caller_session_id,
        caller_session_key=caller_session_key,
    )
    if direct is not None and needle_low in (
        direct["session_id"].lower(), direct["root_session_id"].lower(), direct["session_key"].lower()
    ):
        # A row ended by session_reset keeps the key the live conversation now owns. Returning it
        # makes the host dispatcher deliver into that new conversation while this result names the
        # old id (S-R01). The ended id is not a target.
        if str(direct.get("_end_reason") or "").strip().lower() in _RESET_END_REASONS:
            return None, []
        return direct, []

    # Rank over a much wider window than the public session_list cap so the compression
    # alias can still see the live continuation. Prefix and title resolution do not use
    # this window: a second match past it would otherwise look unique.
    all_sessions = query_sessions(
        home,
        ns=ns,
        caller_session_id=caller_session_id,
        caller_session_key=caller_session_key,
        limit=5000,
        include_cron=True,
        include_archived=True,
        _max_limit=5000,
        _with_internal=True,
    )

    # Tier 1 - exact session id / root id / gateway session key.
    exact_id = [
        s
        for s in all_sessions
        if needle_low in (s["session_id"].lower(), s["root_session_id"].lower(), s["session_key"].lower())
    ]
    if exact_id:
        if len(exact_id) == 1:
            return exact_id[0], []
        active_exact = [
            s for s in exact_id if not s.get("_archived") and not s.get("_cron") and s.get("_active", True)
        ]
        if len(active_exact) == 1:
            return active_exact[0], []
        return None, (active_exact or exact_id)[:8]

    # Tier 1b - an ended/compressed segment id resolves to its live continuation. Several live
    # descendants and no lineage to separate them is ambiguity, not a miss (S01).
    alias = _resolve_ancestor_alias(home, needle_low, all_sessions)
    if alias is not None:
        return alias, []
    descendants = _ancestor_descendants(home, needle_low, all_sessions)
    if len(descendants) > 1:
        return None, descendants[:8]

    prefix_hits, title_hits = _scan_title_and_prefix(
        home, needle, needle_low, ns=ns, caller_session_id=caller_session_id,
        caller_session_key=caller_session_key,
    )

    # Tier 2 - unique id/root-id prefix of at least MIN_ID_PREFIX_LEN characters.
    if prefix_hits:
        if len(prefix_hits) == 1:
            return prefix_hits[0], []
        narrowed = [s for s in prefix_hits if not s.get("_archived") and not s.get("_cron")]
        # S-R03: an ended reset row and the live child that keeps its route are one conversation.
        # Two live rows that share a prefix stay ambiguous.
        by_key: dict[str, list[dict[str, Any]]] = {}
        for item in narrowed:
            by_key.setdefault(item.get("session_key") or item["session_id"], []).append(item)
        collapsed = []
        for group in by_key.values():
            live = [item for item in group if item.get("_active", True)]
            ended = [item for item in group if not item.get("_active", True)]
            if len(live) == 1 and ended:
                collapsed.append(live[0])
            else:
                collapsed.extend(group)
        if len(collapsed) == 1:
            return collapsed[0], []
        return None, (collapsed or prefix_hits)[:8]

    # Tier 3 - title: exact title first, then substring, active before archived, never cron.
    non_cron = [s for s in title_hits if not s.get("_cron")]
    for exact in (True, False):
        pool = [
            s
            for s in non_cron
            if s["title"]
            and (
                _same_title(s["title"].strip(), needle)
                if exact
                else needle.casefold() in s["title"].casefold()
            )
        ]
        if not pool:
            continue
        if len(pool) == 1:
            return pool[0], []
        preferred = [s for s in pool if not s.get("_archived") and s.get("_active", True)]
        if len(preferred) == 1:
            return preferred[0], []
        return None, (preferred or pool)[:8]

    return None, []


def lookup_sender_info(
    home: Path,
    session_id: str,
    *,
    ns: str = "main",
    caller_session_key: str = "",
) -> dict[str, str]:
    """Return {"session_id", "root_session_id", "title", "short_id"} for the calling session."""
    sid = (session_id or "").strip()
    if not sid and caller_session_key and ":local:dm:local-" in caller_session_key:
        sid = caller_session_key.rsplit(":", 1)[-1]

    title = ""
    root_sid = sid
    db_path = Path(home) / "state.db"
    if sid and db_path.is_file():
        con: sqlite3.Connection | None = None
        try:
            con = _connect_ro(db_path)
            colset = {c.lower() for c in _column_names(con, "sessions")}
            if "id" in colset:
                row = con.execute(
                    f"SELECT {_projection(colset, ('id', 'title', 'session_key', 'parent_session_id'))} "
                    "FROM sessions WHERE id = ? LIMIT 1",
                    (sid,),
                ).fetchone()
                if row is not None:
                    title = str(_row_get(row, "title") or "")
                    key_root = _root_from_key(
                        str(_row_get(row, "session_key") or "") or caller_session_key
                    )
                    if key_root:
                        root_sid = key_root
                    else:
                        # Compression continuation: climb to the top segment (five hops max).
                        top = sid
                        current = str(_row_get(row, "parent_session_id") or "").strip()
                        for _ in range(5):
                            if not current or current == top:
                                break
                            top = current
                            parent = con.execute(
                                f"SELECT {_projection(colset, ('id', 'parent_session_id'))} "
                                "FROM sessions WHERE id = ? LIMIT 1",
                                (current,),
                            ).fetchone()
                            if parent is None:
                                break
                            current = str(_row_get(parent, "parent_session_id") or "").strip()
                        root_sid = top
        except sqlite3.Error:
            title = ""
        finally:
            if con is not None:
                con.close()

    short_id = sid[:14] if sid.startswith("local-") else (sid[:16] if sid else "unknown")
    return {
        "session_id": sid or "unknown",
        "root_session_id": root_sid or sid or "unknown",
        "title": title,
        "short_id": short_id,
    }


def parse_intercom_tag(text: str | None) -> tuple[int, int]:
    """Return ``(hop, sender_max_hops)`` for the inbound intercom card in ``text``.

    The card must be the first thing in the message, or — when a queued prompt was prepended in
    front of it — start its own line. The first card found is the envelope. A later card is the body
    quoting one, so it never raises the hop or lowers the sender's budget.

    The line fallback used to run ``^\\s*`` under ``re.MULTILINE``, which re-scans the whole tail at
    every blank line. The walk below is one pass.
    """
    if not text or not isinstance(text, str):
        return 0, 0
    m = INTERCOM_CARD_PREFIX_RE.match(text)
    if m is None:
        m = BARE_INTERCOM_TAG_RE.match(text)
    if m is not None:
        return _tag_ints(m)
    for line in text.split("\n"):
        stripped = line.lstrip()
        if not stripped:
            continue
        if stripped.startswith("[Message from agent "):
            found = _INTERCOM_CARD_LINE_RE.match(stripped)
            return _tag_ints(found) if found is not None else (0, 0)
        # Only a host queued-prompt marker may precede the card. Real text means the card is a quote.
        if not stripped.startswith("[Queued prompt"):
            return 0, 0
    return 0, 0


def _tag_ints(match: re.Match[str] | None) -> tuple[int, int]:
    if match is None:
        return 0, 0
    try:
        return max(0, int(match.group(2))), max(0, int(match.group(3)))
    except (TypeError, ValueError):
        return 0, 0


def parse_hop_from_text(text: str | None) -> int:
    """Read the hop counter from an intercom card (never from a tag quoted inside a body)."""
    return parse_intercom_tag(text)[0]


def latest_user_message_tag(home: Path, session_id: str) -> tuple[int, int]:
    """Fallback tag reader: ``(hop, sender_max_hops)`` of the latest user message.

    ``(-1, 0)`` means the store could not be read, which is not an untagged turn.

    Right after context compression the caller's ``session_id`` is a fresh continuation segment
    with no messages yet, so up to five ``parent_session_id`` hops are checked.
    """
    sid = str(session_id or "").strip()
    if not sid:
        return 0, 0
    db_path = Path(home) / "state.db"
    if not db_path.is_file():
        return 0, 0
    con: sqlite3.Connection | None = None
    try:
        con = _connect_ro(db_path)
        msg_cols = {c.lower() for c in _column_names(con, "messages")}
        if "content" not in msg_cols or "session_id" not in msg_cols:
            return 0, 0
        has_role = "role" in msg_cols
        order_col = "id" if "id" in msg_cols else ("timestamp" if "timestamp" in msg_cols else "rowid")
        current = sid
        seen: set[str] = set()
        for _ in range(6):  # this segment, then up to five parent segments
            if not current or current in seen:
                break
            seen.add(current)
            where = "session_id = ?" + (" AND role = 'user'" if has_role else "")
            # A pure compression summary is not a turn, so it must not hide the parent's card
            # (S-R02). A carrier that still holds a real user ask is a turn: the host marks it
            # with _compressed_summary_has_user_turn. Skipping every flagged row would drop that ask.
            if "_compressed_summary" in msg_cols and "_compressed_summary_has_user_turn" in msg_cols:
                where += (
                    " AND (COALESCE(_compressed_summary, 0) = 0"
                    " OR COALESCE(_compressed_summary_has_user_turn, 0) != 0)"
                )
            # A rewind leaves the withdrawn row in place with active=0. Reading it hides the card
            # the host still shows (R5-S02). A store without the column keeps the old query.
            if "active" in msg_cols:
                where += " AND COALESCE(active, 1) = 1"
            row = con.execute(
                f"SELECT content FROM messages WHERE {where} ORDER BY {order_col} DESC LIMIT 1",
                (current,),
            ).fetchone()
            if row is not None:
                # A real user turn (tagged or not) is authoritative for this segment.
                return parse_intercom_tag(str(_row_get(row, "content") or ""))
            current = _parent_session_id(con, current)
    except sqlite3.Error:
        return -1, 0
    finally:
        if con is not None:
            con.close()
    return 0, 0


def latest_user_message_hop(home: Path, session_id: str) -> int:
    """Hop of the latest user message; 0 when the store is unreadable or the turn is untagged."""
    hop, _max_hops = latest_user_message_tag(home, session_id)
    return max(0, hop)


def sanitize_card_sender(raw_label: str) -> str:
    """Format a sender name guaranteed to match Desktop's AGENT_MESSAGE_RE.

    Strips quotes, brackets, CR/LF/TAB and control chars, lone surrogates and bidi controls,
    collapses whitespace, falls back to "Hermes Session", and caps the result at 60 chars.
    """
    cleaned = sanitize_utf8_text(str(raw_label or ""))
    cleaned = _SENDER_STRIP_RE.sub(" ", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    if not cleaned:
        cleaned = "Hermes Session"
    if len(cleaned) > 60:
        cleaned = cleaned[:59].rstrip() + "\u2026"
    return cleaned


# The host expands these anywhere in the card, including the sender label
# (agent/context_references.py). Built-ins are file/folder/git/url/diff/staged.
# A registered provider is any @word:value the built-in pattern did not claim.
_DIRECTIVE_RE = re.compile(
    r"@+(file|folder|diff|git|url):|@+(diff|staged)\b|@+([a-zA-Z][a-zA-Z0-9_-]*):",
    re.IGNORECASE,
)


def neutralize_directives(text: str) -> str:
    """Break ``@file:`` and registered ``@kind:`` so the target session cannot expand them.

    The readable value stays. Only the ``@`` that makes it a directive is removed.
    """
    return _DIRECTIVE_RE.sub(lambda m: (m.group(1) or m.group(2) or m.group(3) or "ref") + ":", text)


def format_intercom_message(
    *,
    sender_card_name: str,
    sender_session_id: str,
    hop: int,
    max_hops: int,
    expect_reply: bool,
    message: str,
) -> str:
    card_name = neutralize_directives(sanitize_card_sender(sender_card_name))
    safe_sid = re.sub(
        r"[\s\[\]]+", "", sanitize_utf8_text(str(sender_session_id or "unknown"))
    ) or "unknown"
    safe_body = neutralize_directives(sanitize_utf8_text(str(message or "")))
    mode = "reply" if expect_reply else "fyi"
    tag = f"[intercom from={safe_sid} hop={hop}/{max_hops} mode={mode}]"
    if expect_reply and hop < max_hops:
        hint = (
            f"Reply requested by session `{safe_sid}` (hop {hop}/{max_hops}). "
            f"When done, send your concise reply with `session_send(target=\"{safe_sid}\", message=\"...\")`."
        )
    elif expect_reply and hop >= max_hops:
        hint = (
            f"Message from session `{safe_sid}` (final autonomous hop {hop}/{max_hops} - "
            f"do not call `session_send` back automatically)."
        )
    else:
        hint = (
            f"FYI from session `{safe_sid}` (hop {hop}/{max_hops}). "
            f"No automatic reply needed unless asked."
        )
    return f"[Message from agent '{card_name}'] {tag}\n{hint}\n\n{safe_body}"
