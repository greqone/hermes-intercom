"""Public-API delivery, discovery, and rate/loop guards for hermes-intercom."""
from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import re
import sqlite3
import threading
import time
import uuid
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from . import store

logger = logging.getLogger(__name__)

_LOCK = threading.RLock()
# bucket_key = (home_str, target_key) -> [(ts_monotonic, sender_session_id, digest, send_token)]
_RECENT_SENDS: dict[tuple[str, str], list[tuple[float, str, str, str]]] = {}
# (home_str, session_id) -> (inbound hop, sender max_hops, ts_monotonic)
_TURN_HOP: dict[tuple[str, str], tuple[int, int, float]] = {}

_MESSAGE_MAX_CHARS = 32 * 1024

# One operation, keyed by request_id, so a retry replays the first outcome instead of delivering
# again. ``payload_hash`` is the body as accepted; a different body under the same id conflicts.
class _OperationsView(dict):
    """Reads an operation by its request id even when the store keys it as (home, id).

    Writes still use the real key, so a test that plants a bare id keeps doing that.
    """

    def __contains__(self, key: object) -> bool:
        if dict.__contains__(self, key):
            return True
        return _operation_record(str(key)) is not None

    def __getitem__(self, key: object) -> dict:
        if dict.__contains__(self, key):
            return dict.__getitem__(self, key)
        found = _operation_record(str(key))
        if found is None:
            raise KeyError(key)
        return found


_OPERATIONS: _OperationsView = _OperationsView()
_OPERATION_MAX = 4096

# One bucket per target. A long-lived process that talks to a new target every time must not
# keep them all (R4-D06). The oldest buckets go first.
_RECENT_SENDS_MAX_BUCKETS = 4096

# Bounds for the inbound-hop memory so long-running Gateway processes never leak (W213a).
_TURN_HOP_MAX = 1024
_TURN_HOP_TTL = 3600.0

# One caller with a short window must never evict another caller's 600s history (W212b).
_SEND_BUCKET_MIN_HORIZON = 600.0

# Per-target retention horizon for rate and duplicate guards.
_BUCKET_HORIZON: dict[tuple[str, str], float] = {}

_AUDIT_MAX_BYTES = 2 * 1024 * 1024
_INTERCOM_TAG_STRIP_RE = re.compile(r"\[intercom[^\]]*\]", re.IGNORECASE)

def _pos_int(val: Any, default: int, *, minimum: int = 0, maximum: int = 10000) -> int:
    if isinstance(val, bool):
        return default
    if isinstance(val, float) and not math.isfinite(val):
        return default
    try:
        parsed = int(val)
    except (TypeError, ValueError, OverflowError):
        return default
    if parsed < minimum:
        return default
    return min(parsed, maximum)


def _pos_float(val: Any, default: float, *, minimum: float = 0.5, maximum: float = 120.0) -> float:
    if isinstance(val, bool):
        return default
    try:
        parsed = float(val)
    except (TypeError, ValueError, OverflowError):
        return default
    if not math.isfinite(parsed):
        return default
    if parsed < minimum:
        return default
    return min(parsed, maximum)


def _parse_bool(val: Any, default: bool) -> bool:
    """Strictly parse a boolean setting; anything ambiguous falls back to ``default``."""
    if isinstance(val, bool):
        return val
    if isinstance(val, str):
        normalized = val.strip().lower()
        if normalized in ("true", "1", "yes", "on"):
            return True
        if normalized in ("false", "0", "no", "off"):
            return False
        return default
    if isinstance(val, int):
        if val in (0, 1):
            return bool(val)
        return default
    return default


def _read_setting(ctx: Any, key: str, default: Any) -> Any:
    if ctx is None or not hasattr(ctx, "get_config"):
        return default
    try:
        val = ctx.get_config(key, default)
        return default if val is None else val
    except Exception:
        return default


def read_settings(ctx: Any = None) -> dict[str, Any]:
    return {
        "max_hops": _pos_int(_read_setting(ctx, "max_hops", 3), 3, minimum=1, maximum=10),
        "dedup_window_seconds": _pos_int(_read_setting(ctx, "dedup_window_seconds", 120), 120, minimum=0, maximum=3600),
        "burst_window_seconds": _pos_int(_read_setting(ctx, "burst_window_seconds", 600), 600, minimum=1, maximum=86400),
        "max_messages_per_window": _pos_int(
            _read_setting(ctx, "max_messages_per_window", 10), 10, minimum=1, maximum=200
        ),
    }


def _json(payload: dict[str, Any]) -> str:
    """Serialize a tool result: never emits NaN/Infinity, never mangles Unicode."""
    try:
        return json.dumps(payload, ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError):
        # JSON's default callback never sees NaN/Infinity: normalize nested numbers too.
        def clean(value: Any) -> Any:
            if isinstance(value, float) and not math.isfinite(value):
                return None
            if isinstance(value, dict):
                return {str(key): clean(item) for key, item in value.items()}
            if isinstance(value, (tuple, list)):
                return [clean(item) for item in value]
            return value
        return json.dumps(clean(payload), ensure_ascii=False, default=str, allow_nan=False)


def _sanitize_utf8(text: Any) -> str:
    """Replace lone UTF-16 surrogates so hashing/formatting/audit writes never raise (W210)."""
    raw = "" if text is None else str(text)
    sanitizer = getattr(store, "sanitize_utf8_text", None)
    if callable(sanitizer):
        try:
            cleaned = sanitizer(raw)
            if isinstance(cleaned, str):
                return cleaned
        except Exception:
            logger.debug("hermes-intercom: store.sanitize_utf8_text failed", exc_info=True)
    try:
        return raw.encode("utf-8", "replace").decode("utf-8")
    except (UnicodeError, AttributeError):
        return raw


def _append_audit(home: Path, record: dict[str, Any]) -> None:
    """Best-effort append to $HERMES_HOME/cache/hermes-intercom/audit.jsonl (never inside plugins/)."""
    try:
        cache_dir = home / "cache" / "hermes-intercom"
        cache_dir.mkdir(parents=True, exist_ok=True)
        audit_file = cache_dir / "audit.jsonl"
        row = dict(record)
        # Stamp at write time, not when the turn started (W215c).
        row["ts"] = time.time()
        data = (json.dumps(row, ensure_ascii=False, allow_nan=False) + "\n").encode("utf-8")
        # O_BINARY stops Windows from rewriting "\n" as "\r\n" inside the JSONL stream (W215c).
        flags = os.O_APPEND | os.O_CREAT | os.O_WRONLY | getattr(os, "O_BINARY", 0)
        with _LOCK:
            try:
                if audit_file.is_file() and audit_file.stat().st_size > _AUDIT_MAX_BYTES:
                    os.replace(audit_file, cache_dir / "audit.jsonl.1")
            except OSError:
                logger.debug("hermes-intercom: audit rotation skipped", exc_info=True)
            fd = os.open(audit_file, flags, 0o600)
            try:
                os.write(fd, data)
            finally:
                os.close(fd)
    except Exception:
        logger.debug("hermes-intercom: audit write skipped", exc_info=True)


def _message_text(user_message: Any) -> str:
    """Flatten a plain or multimodal user message into searchable text."""
    if isinstance(user_message, str):
        return user_message
    if isinstance(user_message, list):
        parts: list[str] = []
        for part in user_message:
            if isinstance(part, str):
                parts.append(part)
                continue
            if isinstance(part, dict):
                for field in ("text", "content"):
                    value = part.get(field)
                    if isinstance(value, str) and value:
                        parts.append(value)
                        break
        return "\n".join(parts)
    if isinstance(user_message, dict):
        for field in ("text", "content"):
            value = user_message.get(field)
            if isinstance(value, str):
                return value
    return ""


def _same_operation_sender(record: dict[str, Any], caller_sid: str, home: Path) -> bool:
    original = record.get("from_session_id") or (record.get("result") or {}).get("from_session_id") or ""
    return bool(original) and (original == caller_sid or store.compression_line(home, caller_sid, original))


def _same_operation_target(record: dict[str, Any], target_arg: str, resolved_id: str = "",
                          home: Any = None) -> bool:
    """The retry names the same conversation the first call resolved.

    The text the caller typed counts only together with the session it resolves to. A title that
    happens to equal another session's id must not replay the wrong one. A later session counts
    only when the whole line back to the stored one is compression, with no reset in it.
    """
    result = record.get("result") or {}
    session_id = (record.get("target_session_id") or result.get("target_session_id") or "").lower()
    same = resolved_id.lower() == session_id or (
        home is not None and bool(session_id)
        and store.compression_line(home, resolved_id, session_id)
    )
    if not session_id or not same:
        return False
    return True


def _operation_is_live(record: dict[str, Any], token: str = "") -> bool:
    """Active or uncertain calls cannot be evicted and silently delivered twice."""
    return bool(record.get("_inflight") or not record.get("result"))


def _operation_record(request_id: str) -> dict[str, Any] | None:
    """The stored operation for a request id, whether its key is the bare id or (home, id)."""
    direct = _OPERATIONS.get(request_id)
    if isinstance(direct, dict):
        return direct
    for key, record in _OPERATIONS.items():
        if isinstance(key, tuple) and len(key) > 1 and key[1] == request_id and isinstance(record, dict):
            return record
    return None


def _make_room_for_operation() -> bool:
    """Drop finished operations until one slot is free. Live ones stay. Returns False when
    every slot holds work that is still in doubt, so the caller must refuse instead."""
    while len(_OPERATIONS) >= _OPERATION_MAX:
        finished = next((token for token, record in _OPERATIONS.items() if not _operation_is_live(record, token)), None)
        if finished is None:
            return False
        _OPERATIONS.pop(finished, None)
    return True


def _parse_hop(text: str) -> int:
    """Hop advertised by the inbound card, clamped to >= 0 and never raising."""
    if not text:
        return 0
    try:
        return max(0, int(store.parse_hop_from_text(text)))
    except Exception:
        return 0


def _parse_intercom_tag(text: str) -> tuple[int, int]:
    """Return ``(hop, sender_max_hops)`` from the inbound card, tolerating older stores."""
    if not text:
        return 0, 0
    parser = getattr(store, "parse_intercom_tag", None)
    if callable(parser):
        try:
            parsed = parser(text)
            if isinstance(parsed, (tuple, list)) and len(parsed) >= 2:
                return max(0, int(parsed[0] or 0)), max(0, int(parsed[1] or 0))
            if isinstance(parsed, int) and not isinstance(parsed, bool):
                return max(0, parsed), 0
        except Exception:
            logger.debug("hermes-intercom: store.parse_intercom_tag failed", exc_info=True)
    return _parse_hop(text), 0


def _sanitize_label_override(raw: str) -> str:
    """Strip intercom tag syntax from a caller-supplied sender label so it cannot forge hop tags."""
    cleaned = _INTERCOM_TAG_STRIP_RE.sub(" ", str(raw or ""))
    return re.sub(r"\s+", " ", cleaned).strip()


def _evict_stale_turn_hops(now_mono: float) -> None:
    """Drop expired or malformed hop-memory entries (caller holds ``_LOCK``) (W213a)."""
    for key, entry in list(_TURN_HOP.items()):
        if not (isinstance(entry, tuple) and len(entry) >= 3):
            _TURN_HOP.pop(key, None)
            continue
        try:
            fresh = (now_mono - float(entry[2])) < _TURN_HOP_TTL
        except (TypeError, ValueError, OverflowError):
            fresh = False
        if not fresh:
            _TURN_HOP.pop(key, None)


def _remember_turn_hop(key: tuple[str, str], hop: int, sender_max_hops: int, now_mono: float) -> None:
    """Record an inbound hop, keeping ``_TURN_HOP`` bounded (caller holds ``_LOCK``)."""
    _TURN_HOP[key] = (max(0, int(hop)), max(0, int(sender_max_hops)), float(now_mono))
    if len(_TURN_HOP) > _TURN_HOP_MAX:
        oldest_first = sorted(_TURN_HOP.items(), key=lambda kv: kv[1][2])
        for stale_key, _entry in oldest_first[: len(_TURN_HOP) - _TURN_HOP_MAX]:
            _TURN_HOP.pop(stale_key, None)


def _hop_parts(entry: Any) -> tuple[int, int]:
    """Read ``(hop, sender_max_hops)`` from a hop-memory entry, tolerating legacy ints."""
    if isinstance(entry, tuple) and len(entry) >= 2:
        try:
            return max(0, int(entry[0] or 0)), max(0, int(entry[1] or 0))
        except (TypeError, ValueError, OverflowError):
            return 0, 0
    if isinstance(entry, int) and not isinstance(entry, bool):
        return max(0, entry), 0
    return 0, 0


def on_pre_llm_call(session_id: str = "", user_message: Any = None, *, ctx: Any = None, **_: Any) -> None:
    sid = str(session_id or "").strip()
    if not sid:
        return None
    text = _message_text(user_message)
    # Missing input and pure synthetic summaries cannot establish a fresh human turn.
    if not text.strip() or (
        text.lstrip().startswith("[CONTEXT COMPACTION — REFERENCE ONLY]")
        and store._unwrap_carrier(text) == text
    ):
        return None
    hop, sender_max_hops = _parse_intercom_tag(text)
    home_key = str(store.resolve_home(ctx)) if ctx is not None else ""
    key = (home_key, sid)
    now_mono = time.monotonic()
    with _LOCK:
        _evict_stale_turn_hops(now_mono)
        # Zero is an observed current turn, not a cache miss. The DB can lag its flush.
        _remember_turn_hop(key, hop, sender_max_hops, now_mono)
        if home_key:
            _TURN_HOP.pop(("", sid), None)
    return None


def tool_session_list(args: dict[str, Any] | None, *, ctx: Any = None, session_id: str = "", **_: Any) -> str:
    if args is not None and not isinstance(args, Mapping):
        return _json({"ok": False, "error": "invalid_arguments", "message": "arguments must be a JSON object"})
    params = dict(args or {})

    query_arg = params.get("query")
    source_arg = params.get("source")
    if (query_arg is not None and not isinstance(query_arg, str)) or (
        source_arg is not None and not isinstance(source_arg, str)
    ):
        return _json({"ok": False, "error": "invalid_arguments", "message": "query and source must be strings"})

    home = store.resolve_home(ctx)
    ns = store.profile_namespace(ctx)
    caller_key = store.calling_session_key()

    try:
        sessions = store.query_sessions(
            home,
            ns=ns,
        caller_session_id=str(session_id or ""),
        caller_session_key=caller_key,
        query=str(query_arg or ""),
        source=str(source_arg or ""),
        limit=_pos_int(params.get("limit"), 20, minimum=1, maximum=100),
        include_cron=_parse_bool(params.get("include_cron", False), False),
        include_archived=_parse_bool(params.get("include_archived", False), False),
        )
    except store.StoreReadError:
        return _json({
            "ok": False,
            "error": "store_unreadable",
            "message": "state.db exists but could not be read. This is not an empty session list.",
        })
    return _json(
        {
            "ok": True,
            "current_session_id": str(session_id or ""),
            "current_session_key": caller_key,
            "count": len(sessions),
            "sessions": sessions,
        }
    )


def _remember_bucket_horizon(bucket_key: tuple[str, str], keep_win: float) -> None:
    """Retain this target for its own window, and never less than the 600s floor (caller holds ``_LOCK``)."""
    horizon = max(float(keep_win), _SEND_BUCKET_MIN_HORIZON)
    if horizon > _BUCKET_HORIZON.get(bucket_key, 0.0):
        _BUCKET_HORIZON[bucket_key] = horizon


def _evict_stale_send_buckets(now_mono: float) -> None:
    """Drop a bucket only after its own horizon, not after the caller's (caller holds ``_LOCK``)."""
    stale: list[tuple[str, str]] = []
    for key, entries in _RECENT_SENDS.items():
        horizon = _BUCKET_HORIZON.get(key, _SEND_BUCKET_MIN_HORIZON)
        fresh = False
        for entry in entries:
            if len(entry) <= 0:
                continue
            try:
                if (now_mono - float(entry[0])) < horizon:
                    fresh = True
                    break
            except (TypeError, ValueError):
                continue
        if not fresh:
            stale.append(key)
    for key in stale:
        _RECENT_SENDS.pop(key, None)
        _BUCKET_HORIZON.pop(key, None)


def _recent_sends_for(
    bucket_key: tuple[str, str], now_mono: float, keep_win: float
) -> tuple[list[tuple[float, str, str, str]], list[tuple[float, str, str, str]]]:
    """``(visible, retained)`` for one bucket (caller holds ``_LOCK``).

    ``visible`` is what this call's window may judge. ``retained`` keeps every entry still
    inside the bucket's own horizon, so a shorter window cannot erase it (R4-D02).
    """
    horizon = max(float(keep_win), _BUCKET_HORIZON.get(bucket_key, _SEND_BUCKET_MIN_HORIZON))
    visible: list[tuple[float, str, str, str]] = []
    retained: list[tuple[float, str, str, str]] = []
    for entry in _RECENT_SENDS.get(bucket_key, []):
        try:
            ts = float(entry[0])
            sender = str(entry[1])
            digest = str(entry[2])
        except (TypeError, ValueError, IndexError):
            continue
        token = str(entry[3]) if len(entry) > 3 else ""
        age = now_mono - ts
        if age >= horizon:
            continue
        row = (ts, sender, digest, token)
        retained.append(row)
        if age < keep_win:
            visible.append(row)
    return visible, retained


def _rollback_reservation(bucket_key: tuple[str, str], request_id: str) -> None:
    """Release only this unsent operation's history; caller holds ``_LOCK``."""
    remaining = [
        entry for entry in _RECENT_SENDS.get(bucket_key, [])
        if not (len(entry) > 3 and entry[3] == request_id)
    ]
    if remaining:
        _RECENT_SENDS[bucket_key] = remaining
    else:
        _RECENT_SENDS.pop(bucket_key, None)
        _BUCKET_HORIZON.pop(bucket_key, None)


def _classic_cli_host(ctx: Any) -> bool:
    """Public surface check; unknown/older contexts fail closed.

    The public API cannot distinguish classic CLI from gateway-backed CLI turns.
    Both are conservatively refused until core guarantees target-aware injection.
    """
    try:
        surface = ctx.session_surface()
        return not isinstance(surface, str) or surface.strip().lower() in {"", "cli"}
    except Exception:
        return True


def _inject(ctx: Any, payload: str, target_key: str) -> bool:
    """Deliver ``payload`` to ``target_key``.

    A classic CLI host cannot honor ``session_key`` (it swallows the text into the
    sender's own REPL and still returns True), so that call is refused here instead
    of being reported as queued. Any other host exception stays inside the tool
    (W212a).
    """
    if _classic_cli_host(ctx):
        logger.debug("hermes-intercom: refusing inject_message from a classic CLI host")
        return False
    try:
        return bool(ctx.inject_message(payload, session_key=target_key))
    except Exception:
        logger.debug("hermes-intercom: ctx.inject_message raised", exc_info=True)
        return False


def tool_session_send(args: dict[str, Any] | None, *, ctx: Any = None, session_id: str = "", **_: Any) -> str:
    if args is not None and not isinstance(args, Mapping):
        return _json({"ok": False, "error": "invalid_arguments", "message": "arguments must be a JSON object"})
    params = dict(args or {})

    raw_target = params.get("target")
    raw_message = params.get("message")
    raw_label = params.get("sender_label")
    raw_request = params.get("request_id")
    if any(value is not None and not isinstance(value, str) for value in (raw_target, raw_message, raw_label, raw_request)):
        return _json(
            {
                "ok": False,
                "error": "invalid_arguments",
                "message": "target, message, sender_label, and request_id must be strings",
            }
        )

    target_arg = _sanitize_utf8(raw_target or "").strip()
    message_body = _sanitize_utf8(raw_message or "")
    expect_reply = _parse_bool(params.get("expect_reply", False), False)
    sender_label_override = _sanitize_label_override(_sanitize_utf8(raw_label or ""))

    if not target_arg:
        return _json({"ok": False, "error": "missing_target", "message": "target is required"})
    if not message_body.strip():
        return _json({"ok": False, "error": "empty_message", "message": "message must not be empty"})
    if len(message_body) > _MESSAGE_MAX_CHARS:
        return _json({
            "ok": False,
            "error": "message_too_large",
            "message": f"message exceeds {_MESSAGE_MAX_CHARS} characters.",
            "max_chars": _MESSAGE_MAX_CHARS,
        })
    if ctx is None or not hasattr(ctx, "inject_message"):
        return _json(
            {
                "ok": False,
                "error": "plugin_context_unavailable",
                "message": "Plugin context does not provide inject_message.",
            }
        )

    if _classic_cli_host(ctx):
        return _json({
            "ok": False, "error": "surface_cannot_route",
            "message": "Cross-session sends require a known non-CLI session surface; this host cannot safely route the target.",
        })

    cfg = read_settings(ctx)
    home = store.resolve_home(ctx)
    ns = store.profile_namespace(ctx)
    caller_key = store.calling_session_key()
    caller_sid = str(session_id or "").strip()
    if not caller_sid and caller_key and ":local:dm:local-" in caller_key:
        # Recover the caller id from its gateway key so the hop guard still applies (W204).
        caller_sid = caller_key.rsplit(":", 1)[-1].strip()

    # A retry names work that already happened. Look it up before the hop guard and before the
    # target is resolved again: compression rotates the physical session id, and a later turn can
    # raise the hop, but neither changes the operation that was already accepted (R5-D02).
    supplied_request = raw_request or ""
    if supplied_request and (len(supplied_request) > 1024 or any(
        ch.isspace() or ord(ch) < 32 or 0xD800 <= ord(ch) <= 0xDFFF
        for ch in supplied_request
    )):
        return _json({
            "ok": False,
            "error": "invalid_request_id",
            "message": "request_id must be 1-1024 characters with no whitespace.",
        })

    try:
        resolved, candidates = store.resolve_target(
        home,
        target_arg,
        ns=ns,
        caller_session_id=caller_sid,
        caller_session_key=caller_key,
        )
    except store.StoreReadError:
        return _json({
            "ok": False,
            "error": "store_unreadable",
            "message": "state.db exists but could not be read, so the target was not resolved.",
        })
    if resolved is None:
        return _json(
            {
                "ok": False,
                "error": "ambiguous_target" if candidates else "target_not_found",
                "target": target_arg,
                "candidates": [
                    {
                        "session_id": c["session_id"],
                        "title": c["title"],
                        "source": c["source"],
                        "last_activity_at": c["last_activity_at"],
                    }
                    for c in candidates
                ],
                "hint": (
                    "Call session_list first, then pass an exact session_id or unique title substring. "
                    "An original ID prefix may stop resolving after compression. For a retained retry, "
                    "use target_session_id from the first result with the same request_id and body; "
                    "do not create a new request to recover an uncertain delivery."
                ),
            }
        )

    if resolved["is_self"]:
        return _json(
            {
                "ok": False,
                "error": "target_is_self",
                "message": "Cannot send an intercom message to the calling session itself.",
                "target_session_id": resolved["session_id"],
            }
        )

    if supplied_request:
        home_key = str(home)
        with _LOCK:
            prior_early = _OPERATIONS.get((home_key, supplied_request))
        if (
            prior_early is not None
            and prior_early.get("delivered")
            and prior_early.get("body") == message_body
            and bool(prior_early.get("expect_reply")) == bool(expect_reply)
            and _same_operation_sender(prior_early, caller_sid, home)
            and _same_operation_target(prior_early, target_arg, resolved["session_id"], home)
        ):
            return _json({**prior_early["result"], "replayed": True})

    target_key = resolved["session_key"]
    if not target_key:
        return _json(
            {
                "ok": False,
                "error": "target_not_addressable",
                "message": "Target session has no gateway session_key.",
                "target_session_id": resolved["session_id"],
            }
        )

    home_key = str(home)
    now_mono = time.monotonic()
    with _LOCK:
        _evict_stale_turn_hops(now_mono)
        entry = None
        if caller_sid:
            entry = _TURN_HOP.get((home_key, caller_sid))
            if entry is None:
                entry = _TURN_HOP.get(("", caller_sid))
    mem_hop, sender_max_hops = _hop_parts(entry)
    # The in-memory pair dies with the process. The stored card still carries both numbers,
    # so a restart must not silently raise a budget the sender already lowered.
    db_hop, db_max_hops = (
        store.latest_user_message_tag(home, caller_sid)
        if caller_sid and entry is None else (0, 0)
    )
    if db_hop < 0:
        return _json({
            "ok": False,
            "error": "hop_state_unreadable",
            "message": "The session store could not be read, so the hop guard cannot be applied.",
        })
    current_hop = max(mem_hop, db_hop)
    if db_max_hops > 0:
        sender_max_hops = min(sender_max_hops or db_max_hops, db_max_hops)
    next_hop = current_hop + 1
    max_hops = cfg["max_hops"]
    if sender_max_hops > 0:
        # An inbound card may lower this profile's loop budget, never raise it (W204).
        max_hops = min(max_hops, sender_max_hops)

    if next_hop > max_hops:
        return _json(
            {
                "ok": False,
                "error": "max_hops_exceeded",
                "hop": next_hop,
                "max_hops": max_hops,
                "message": (
                    f"Intercom loop guard: hop {next_hop} exceeds max_hops={max_hops}. "
                    "Stop automatic back-and-forth and report to the user."
                ),
            }
        )

    digest = hashlib.sha256(f"{int(expect_reply)}:{message_body}".encode("utf-8")).hexdigest()[:16]
    # A caller-supplied ID identifies one retained operation in this process.
    supplied_request = raw_request or ""
    if supplied_request and (len(supplied_request) > 1024 or any(
        ch.isspace() or ord(ch) < 32 or 0xD800 <= ord(ch) <= 0xDFFF
        for ch in supplied_request
    )):
        return _json({
            "ok": False,
            "error": "invalid_request_id",
            "message": "request_id must be 1-1024 characters with no whitespace.",
        })
    send_token = (home_key, supplied_request) if supplied_request else (home_key, uuid.uuid4().hex)
    public_request = supplied_request or send_token[1]
    identity = "\n".join((
        str(resolved["session_id"]), target_key, caller_sid, str(bool(expect_reply)), message_body,
    ))
    payload_hash = hashlib.sha256(identity.encode("utf-8")).hexdigest()
    wall_now = time.time()
    burst_win = cfg["burst_window_seconds"]
    dedup_win = cfg["dedup_window_seconds"]
    max_per_win = cfg["max_messages_per_window"]
    bucket_key = (home_key, target_key)
    keep_win = max(burst_win, dedup_win)

    with _LOCK:
        prior = _OPERATIONS.get(send_token)
        same_retry = prior is not None and (
            prior.get("body") == message_body
            and bool(prior.get("expect_reply")) == bool(expect_reply)
            and _same_operation_sender(prior, caller_sid, home)
            and _same_operation_target(prior, target_arg, resolved["session_id"], home)
        )
        if prior is not None and prior.get("payload_hash") != payload_hash and not same_retry:
            return _json({
                "ok": False,
                "error": "request_conflict",
                "request_id": public_request,
                "message": "request_id was already used with a different message.",
            })
        if prior is not None and prior.get("delivered"):
            return _json({**prior["result"], "replayed": True})
        if prior is not None:
            return _json({
                "ok": False, "error": "request_in_flight", "request_id": public_request,
                "message": "This request_id is already being delivered. Retry after it finishes.",
            })
        if prior is None:
            # Capacity is decided before the claim. Evicting the oldest entry here used to drop an
            # unknown or still-running submit, and the cap only ran after success, so a run of
            # unknown results grew without bound (R5-D01).
            if not _make_room_for_operation():
                return _json({
                    "ok": False,
                    "error": "operation_capacity",
                    "request_id": public_request,
                    "message": "Too many deliveries are still unconfirmed. Retry once one of them settles.",
                })
            # Claimed before delivery so a second caller with the same id waits on this one
            # instead of delivering beside it.
            _OPERATIONS[send_token] = {
                "payload_hash": payload_hash, "delivered": False, "result": {},
                "claimed_at": time.monotonic(), "home": home_key,
                "body": message_body, "expect_reply": bool(expect_reply),
                "from_session_id": caller_sid, "target_session_id": resolved["session_id"],
            }
        _remember_bucket_horizon(bucket_key, keep_win)
        _evict_stale_send_buckets(now_mono)
        history, retained = _recent_sends_for(bucket_key, now_mono, keep_win)
        if dedup_win > 0 and any(
            sender == caller_sid and d == digest and (now_mono - ts) < dedup_win and tok != public_request
            for ts, sender, d, tok in history
        ):
            if prior is None:
                _OPERATIONS.pop(send_token, None)
            if bucket_key not in _RECENT_SENDS:
                _BUCKET_HORIZON.pop(bucket_key, None)
            return _json(
                {
                    "ok": False,
                    "error": "duplicate_message",
                    "message": f"Identical message already sent to {resolved['session_id']} within {dedup_win}s.",
                }
            )
        # Burst counts operations, not attempts: a retry of one request_id must not spend a second
        # slot, and it must still be deliverable when the window is already full (D04).
        seen_tokens = {
            tok for ts, _sender, _d, tok in history if tok and (now_mono - ts) < burst_win
        }
        if send_token[1] not in seen_tokens and len(seen_tokens) >= max_per_win:
            if prior is None:
                _OPERATIONS.pop(send_token, None)
            if bucket_key not in _RECENT_SENDS:
                _BUCKET_HORIZON.pop(bucket_key, None)
            return _json(
                {
                    "ok": False,
                    "error": "rate_limited",
                    "message": f"Rate limit reached: {max_per_win} messages to {resolved['session_id']} within {burst_win}s.",
                }
            )
        if not any(tok == public_request for _ts, _sender, _d, tok in retained):
            retained.append((now_mono, caller_sid, digest, public_request))
        _RECENT_SENDS[bucket_key] = retained
        while len(_RECENT_SENDS) > _RECENT_SENDS_MAX_BUCKETS:
            oldest = min(
                _RECENT_SENDS, key=lambda key: _RECENT_SENDS[key][-1][0] if _RECENT_SENDS[key] else 0.0
            )
            _RECENT_SENDS.pop(oldest, None)
            _BUCKET_HORIZON.pop(oldest, None)

    sender_info = store.lookup_sender_info(home, caller_sid, ns=ns, caller_session_key=caller_key)
    real_short = sender_info["short_id"]
    if sender_label_override:
        room = max(0, 60 - len(f" ({real_short})") - 1) if real_short else 60
        card_label = sender_label_override[:room].rstrip()
        if real_short:
            card_label = f"{card_label} ({real_short})"
    elif sender_info["title"]:
        title_short = sender_info["title"][:38].strip()
        card_label = f"{title_short} ({sender_info['short_id']})"
    else:
        card_label = sender_info["short_id"]

    payload = store.format_intercom_message(
        sender_card_name=card_label,
        sender_session_id=sender_info["session_id"],
        hop=next_hop,
        max_hops=max_hops,
        expect_reply=expect_reply,
        message=message_body,
    )
    routed = resolved.get("routed", True)
    routed = True if routed is None else bool(routed)
    # R4-D05: a session_key column is metadata. It is unverified only when gateway_routing
    # exists and does not name this session. No routing table at all keeps the old path,
    # because a store without that index cannot prove the key is stale.
    if routed:
        db_path = Path(home) / "state.db"
        if db_path.is_file():
            try:
                con = store._connect_ro(db_path)
                try:
                    has_index = bool(store._column_names(con, "gateway_routing"))
                    sid_to_key, key_to_entry = store._load_routing_map(con, home) if has_index else ({}, {})
                finally:
                    con.close()
            except sqlite3.Error:
                # The claim was taken and nothing was sent. Leaving it makes every later retry
                # say request_in_flight forever (R5-D03). Release it and say the store failed.
                with _LOCK:
                    held = _OPERATIONS.get(send_token)
                    if held and not held.get("delivered"):
                        _OPERATIONS.pop(send_token, None)
                        _rollback_reservation(bucket_key, public_request)
                    elif held is not None:
                        held.pop("_inflight", None)
                return _json({
                    "ok": False,
                    "error": "store_unavailable",
                    "request_id": public_request,
                    "message": "The session store could not be read after the send was claimed. Nothing was delivered. Retry.",
                })
            if has_index:
                # An empty index used to count as a verified route, because the check only ran
                # when one of the maps had rows. A stored session_key with nothing behind it is
                # metadata, not a route (R5-D04).
                route_key = resolved.get("session_key") or ""
                owner = str((key_to_entry.get(route_key) or {}).get("session_id") or "")
                routed = bool(owner) and (
                    owner == resolved["session_id"]
                    or store.compression_line(home, resolved["session_id"], owner)
                )

    injectable = routed
    if not injectable:
        with _LOCK:
            held = _OPERATIONS.get(send_token)
            if held and not held.get("delivered"):
                _OPERATIONS.pop(send_token, None)
                _rollback_reservation(bucket_key, public_request)
            elif held is not None:
                held.pop("_inflight", None)
        return _json({
            "ok": False,
            "error": "route_unverified",
            "request_id": public_request,
            "target_session_id": resolved["session_id"],
            "message": "No confirmed route exists for this session. The stored session_key is metadata, not a route.",
        })
    accepted = _inject(ctx, payload, target_key)
    if not accepted:
        with _LOCK:
            _OPERATIONS.pop(send_token, None)
            _rollback_reservation(bucket_key, public_request)
        return _json({
            "ok": False, "error": "injection_refused", "request_id": public_request,
            "target_session_id": resolved["session_id"], "target_session_key": target_key,
            "message": "The public injection API refused this target. Check the host's injection permission and open the target session. Intercom does not mount dormant sessions or bypass the host's refusal.",
        })

    audit_row = {
        "ts": wall_now,
        "from_session_id": sender_info["session_id"],
        "to_session_id": resolved["session_id"],
        "to_session_key": target_key,
        "hop": next_hop,
        "max_hops": max_hops,
        "expect_reply": expect_reply,
        "mounted_dormant": False,
        "delivery_method": "inject_message",
        "digest": digest,
        "request_id": public_request,
        "admission_id": "",
    }
    _append_audit(home, audit_row)

    result = {
        "ok": True,
        "status": "scheduled",
        "delivery_method": "inject_message",
        "from_session_id": sender_info["session_id"],
        "target_session_id": resolved["session_id"],
        "target_title": resolved["title"],
        "target_session_key": target_key,
        "hop": next_hop,
        "max_hops": max_hops,
        "expect_reply": expect_reply,
        "mounted_dormant": False,
        "request_id": public_request,
        "admission_id": "",
        "admission_status": "",
        "outcome": "",
        "reference": "",
        "note": (
            "The host scheduled this turn. inject_message confirms scheduling, not a durable "
            "admission, so a later refusal is reported by the gateway log rather than here."
        ),
    }
    with _LOCK:
        held = _OPERATIONS.get(send_token)
        if held is not None:
            held.pop("_inflight", None)
        _OPERATIONS[send_token] = {
            **(held or {}),
            "payload_hash": (held or {}).get("payload_hash", payload_hash),
            "delivered": True,
            "delivery_method": "inject_message",
            "envelope": payload,
            "body": message_body,
            "expect_reply": bool(expect_reply),
            "target_text": target_arg,
            "home": home_key,
            "claimed_at": (held.get("claimed_at") if held else None) or time.monotonic(),
            "result": result,
        }
    return _json(result)
