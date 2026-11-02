"""Tool handlers, settings, rate/loop guards, and dormant-session mounting for hermes-intercom."""
from __future__ import annotations

import asyncio
import hashlib
import importlib
import json
import logging
import math
import os
import re
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Callable

from . import store

logger = logging.getLogger(__name__)

_LOCK = threading.RLock()
# bucket_key = (home_str, target_key) -> [(ts_monotonic, sender_session_id, digest)]
_RECENT_SENDS: dict[tuple[str, str], list[tuple[float, str, str]]] = {}
# (home_str, session_id) -> inbound hop count for the current turn
_TURN_HOP: dict[tuple[str, str], int] = {}

_AUDIT_MAX_BYTES = 2 * 1024 * 1024
_INTERCOM_TAG_STRIP_RE = re.compile(r"\[intercom[^\]]*\]", re.IGNORECASE)


def _load_core_attr(mod_parts: tuple[str, ...], attr: str) -> Any:
    mod = importlib.import_module(".".join(mod_parts))
    return getattr(mod, attr)


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
        "auto_mount_dormant": _parse_bool(_read_setting(ctx, "auto_mount_dormant", True), True),
        "mount_timeout_seconds": _pos_float(
            _read_setting(ctx, "mount_timeout_seconds", 8.0), 8.0, minimum=1.0, maximum=60.0
        ),
    }


def _is_gateway_injection_allowed(ctx: Any, home: Path) -> bool:
    """Fail-closed check whether plugins.entries.<plugin_id>.allow_gateway_injection is True."""
    explicit = getattr(ctx, "allow_gateway_injection", None)
    if isinstance(explicit, bool):
        return explicit
    plugin_id = getattr(ctx, "plugin_id", "hermes-intercom") or "hermes-intercom"
    cfg_path = home / "config.yaml"
    if not cfg_path.is_file():
        return False
    try:
        import yaml
        cfg = yaml.safe_load(cfg_path.read_text(encoding="utf-8-sig")) or {}
        entries = ((cfg.get("plugins") or {}).get("entries") or {}) if isinstance(cfg, dict) else {}
        entry = entries.get(plugin_id) if isinstance(entries, dict) else None
        return isinstance(entry, dict) and entry.get("allow_gateway_injection") is True
    except Exception:
        return False


def _append_audit(home: Path, record: dict[str, Any]) -> None:
    """Best-effort append to $HERMES_HOME/cache/hermes-intercom/audit.jsonl (never inside plugins/)."""
    try:
        cache_dir = home / "cache" / "hermes-intercom"
        cache_dir.mkdir(parents=True, exist_ok=True)
        audit_file = cache_dir / "audit.jsonl"
        data = (json.dumps(record, ensure_ascii=False) + "\n").encode("utf-8")
        with _LOCK:
            try:
                if audit_file.is_file() and audit_file.stat().st_size > _AUDIT_MAX_BYTES:
                    os.replace(audit_file, cache_dir / "audit.jsonl.1")
            except OSError:
                logger.debug("hermes-intercom: audit rotation skipped", exc_info=True)
            fd = os.open(audit_file, os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o600)
            try:
                os.write(fd, data)
            finally:
                os.close(fd)
    except Exception:
        logger.debug("hermes-intercom: audit write skipped", exc_info=True)


def _exc_label(exc: BaseException) -> str:
    if isinstance(exc, asyncio.TimeoutError):
        return "TimeoutError"
    text = str(exc).strip()
    return text or exc.__class__.__name__


def _run_coro_sync(coro_fn: Callable[[], Any], *, timeout: float = 8.0) -> tuple[bool, str]:
    """Run a zero-arg coroutine factory to completion within one overall ``timeout`` budget.

    Taking a factory (not a coroutine object) means a failure before the worker thread starts can
    never leak an unawaited coroutine.
    """
    try:
        timeout = max(0.1, float(timeout))
    except (TypeError, ValueError):
        timeout = 8.0

    def _execute() -> tuple[bool, str]:
        return asyncio.run(asyncio.wait_for(coro_fn(), timeout=timeout))

    try:
        try:
            asyncio.get_running_loop()
            running_loop = True
        except RuntimeError:
            running_loop = False

        if not running_loop:
            return _execute()

        result_box: list[tuple[bool, str]] = []
        err_box: list[BaseException] = []

        def _runner() -> None:
            try:
                result_box.append(_execute())
            except BaseException as exc:  # noqa: BLE001 - reported through err_box
                err_box.append(exc)

        thread = threading.Thread(target=_runner, daemon=True)
        thread.start()
        thread.join(timeout=timeout + 2.0)
        if err_box:
            exc = err_box[0]
            if isinstance(exc, (KeyboardInterrupt, SystemExit)):
                raise exc
            logger.debug("hermes-intercom: gateway coroutine failed: %s", exc)
            return False, _exc_label(exc)
        if not result_box:
            return False, "gateway_thread_timeout"
        return result_box[0]
    except (KeyboardInterrupt, SystemExit):
        raise
    except BaseException as exc:  # noqa: BLE001 - delivery must never crash the agent loop
        logger.debug("hermes-intercom: gateway coroutine failed: %s", exc)
        return False, _exc_label(exc)


def mount_dormant_local_session(home: Path, target_session_id: str, *, timeout: float = 8.0) -> tuple[bool, str]:
    """Mount a dormant local session into the live gateway authority via session.resume + session.detach."""
    if not target_session_id:
        return False, "missing_target_session_id"
    try:
        timeout = max(0.1, float(timeout))
    except (TypeError, ValueError):
        timeout = 8.0
    deadline = time.monotonic() + timeout

    def _rem() -> float:
        return max(0.1, deadline - time.monotonic())

    async def _mount_coro() -> tuple[bool, str]:
        discover_fn = _load_core_attr(("hermes_cli", "gateway_runtime"), "discover_gateway_endpoint")
        connect_gw = _load_core_attr(("hermes_cli", "gateway_client"), "connect_gateway")

        discovery = await asyncio.to_thread(discover_fn, home, timeout=min(_rem(), 5.0))
        if discovery.state != "ready" or discovery.endpoint is None:
            return False, f"gateway_not_ready:{discovery.state}"
        async with connect_gw(endpoint=discovery.endpoint) as client:
            resumed = await asyncio.wait_for(
                client.rpc("session.resume", session_id=target_session_id, omit_messages=True),
                timeout=_rem(),
            )
            if not isinstance(resumed, dict) or resumed.get("error"):
                return False, f"resume_error:{resumed}"
            resumed_sid = str(resumed.get("session_id") or target_session_id)
            sub_id = str(resumed.get("subscription_id") or "")
            if sub_id:
                try:
                    await asyncio.wait_for(
                        client.rpc("session.detach", session_id=resumed_sid, subscription_id=sub_id),
                        timeout=_rem(),
                    )
                except Exception:
                    # A detach hiccup must not undo an already successful resume.
                    logger.debug("hermes-intercom: session.detach failed after resume", exc_info=True)
            return True, resumed_sid

    return _run_coro_sync(_mount_coro, timeout=timeout)


def submit_via_gateway_ws(
    home: Path, target_session_id: str, content: str, *, timeout: float = 8.0
) -> tuple[bool, str]:
    """Deliver an attributed intercom card over the local Gateway WebSocket from an external process."""
    if not target_session_id or not content:
        return False, "missing_args"
    if content.lstrip().startswith("/"):
        return False, "slash_command_forbidden"
    try:
        timeout = max(0.1, float(timeout))
    except (TypeError, ValueError):
        timeout = 8.0
    deadline = time.monotonic() + timeout

    def _rem() -> float:
        return max(0.1, deadline - time.monotonic())

    async def _submit_coro() -> tuple[bool, str]:
        discover_fn = _load_core_attr(("hermes_cli", "gateway_runtime"), "discover_gateway_endpoint")
        connect_gw = _load_core_attr(("hermes_cli", "gateway_client"), "connect_gateway")

        discovery = await asyncio.to_thread(discover_fn, home, timeout=min(_rem(), 5.0))
        if discovery.state != "ready" or discovery.endpoint is None:
            return False, f"gateway_not_ready:{discovery.state}"
        async with connect_gw(endpoint=discovery.endpoint) as client:
            resumed = await asyncio.wait_for(
                client.rpc("session.resume", session_id=target_session_id, omit_messages=True),
                timeout=_rem(),
            )
            if not isinstance(resumed, dict) or resumed.get("error"):
                return False, f"resume_error:{resumed}"
            resumed_sid = str(resumed.get("session_id") or target_session_id)
            sub_id = str(resumed.get("subscription_id") or "")
            try:
                receipt = await asyncio.wait_for(
                    client.rpc(
                        "prompt.submit",
                        session_id=resumed_sid,
                        input_id=uuid.uuid4().hex,
                        text=content,
                    ),
                    timeout=_rem(),
                )
            finally:
                if sub_id:
                    try:
                        await asyncio.wait_for(
                            client.rpc("session.detach", session_id=resumed_sid, subscription_id=sub_id),
                            timeout=_rem(),
                        )
                    except Exception:
                        logger.debug("hermes-intercom: session.detach failed after submit", exc_info=True)
            if isinstance(receipt, dict) and receipt.get("admission_id"):
                return True, str(receipt["admission_id"])
            return False, f"submit_refused:{receipt}"

    return _run_coro_sync(_submit_coro, timeout=timeout)


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


def _parse_hop(text: str) -> int:
    """Hop advertised by the inbound card, clamped to >= 0 and never raising."""
    if not text:
        return 0
    try:
        return max(0, int(store.parse_hop_from_text(text)))
    except Exception:
        return 0


def _sanitize_label_override(raw: str) -> str:
    """Strip intercom tag syntax from a caller-supplied sender label so it cannot forge hop tags."""
    cleaned = _INTERCOM_TAG_STRIP_RE.sub(" ", str(raw or ""))
    return re.sub(r"\s+", " ", cleaned).strip()


def on_pre_llm_call(session_id: str = "", user_message: Any = None, *, ctx: Any = None, **_: Any) -> None:
    sid = str(session_id or "").strip()
    if not sid:
        return None
    text = _message_text(user_message)
    hop = _parse_hop(text)
    home_key = str(store.resolve_home(ctx)) if ctx is not None else ""
    key = (home_key, sid)
    with _LOCK:
        if hop > 0:
            _TURN_HOP[key] = hop
        else:
            _TURN_HOP.pop(key, None)
    return None


def tool_session_list(args: dict[str, Any] | None, *, ctx: Any = None, session_id: str = "", **_: Any) -> str:
    params = dict(args or {})
    home = store.resolve_home(ctx)
    ns = store.profile_namespace(ctx)
    caller_key = store.calling_session_key()

    sessions = store.query_sessions(
        home,
        ns=ns,
        caller_session_id=str(session_id or ""),
        caller_session_key=caller_key,
        query=str(params.get("query") or ""),
        source=str(params.get("source") or ""),
        limit=_pos_int(params.get("limit"), 20, minimum=1, maximum=100),
        include_cron=_parse_bool(params.get("include_cron", False), False),
        include_archived=_parse_bool(params.get("include_archived", False), False),
    )
    return json.dumps(
        {
            "ok": True,
            "current_session_id": str(session_id or ""),
            "current_session_key": caller_key,
            "count": len(sessions),
            "sessions": sessions,
        },
        ensure_ascii=False,
    )


def _evict_stale_send_buckets(now_mono: float, keep_win: float) -> None:
    """Drop buckets with no entry inside the longest guard window (caller holds ``_LOCK``)."""
    stale = [
        key
        for key, entries in _RECENT_SENDS.items()
        if not any((now_mono - ts) < keep_win for ts, _sender, _d in entries)
    ]
    for key in stale:
        _RECENT_SENDS.pop(key, None)


def tool_session_send(args: dict[str, Any] | None, *, ctx: Any = None, session_id: str = "", **_: Any) -> str:
    params = dict(args or {})
    target_arg = str(params.get("target") or "").strip()
    message_body = str(params.get("message") or "").strip()
    expect_reply = _parse_bool(params.get("expect_reply", False), False)
    sender_label_override = _sanitize_label_override(str(params.get("sender_label") or ""))

    if not target_arg:
        return json.dumps({"ok": False, "error": "missing_target", "message": "target is required"})
    if not message_body:
        return json.dumps({"ok": False, "error": "empty_message", "message": "message must not be empty"})
    if ctx is None or not hasattr(ctx, "inject_message"):
        return json.dumps({"ok": False, "error": "plugin_context_unavailable"})

    cfg = read_settings(ctx)
    home = store.resolve_home(ctx)
    ns = store.profile_namespace(ctx)
    caller_key = store.calling_session_key()
    caller_sid = str(session_id or "").strip()

    resolved, candidates = store.resolve_target(
        home,
        target_arg,
        ns=ns,
        caller_session_id=caller_sid,
        caller_session_key=caller_key,
    )
    if resolved is None:
        return json.dumps(
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
                "hint": "Call session_list first, then pass an exact session_id or unique title substring.",
            },
            ensure_ascii=False,
        )

    if resolved["is_self"]:
        return json.dumps(
            {
                "ok": False,
                "error": "target_is_self",
                "message": "Cannot send an intercom message to the calling session itself.",
                "target_session_id": resolved["session_id"],
            },
            ensure_ascii=False,
        )

    target_key = resolved["session_key"]
    if not target_key:
        return json.dumps(
            {
                "ok": False,
                "error": "target_not_addressable",
                "message": "Target session has no gateway session_key.",
                "target_session_id": resolved["session_id"],
            },
            ensure_ascii=False,
        )

    home_key = str(home)
    with _LOCK:
        if caller_sid:
            mem_hop = _TURN_HOP.get((home_key, caller_sid), _TURN_HOP.get(("", caller_sid), 0))
        else:
            mem_hop = 0
    db_hop = store.latest_user_message_hop(home, caller_sid) if caller_sid else 0
    current_hop = max(mem_hop, db_hop)
    next_hop = current_hop + 1
    max_hops = cfg["max_hops"]

    if next_hop > max_hops:
        return json.dumps(
            {
                "ok": False,
                "error": "max_hops_exceeded",
                "hop": next_hop,
                "max_hops": max_hops,
                "message": (
                    f"Intercom loop guard: hop {next_hop} exceeds max_hops={max_hops}. "
                    "Stop automatic back-and-forth and report to the user."
                ),
            },
            ensure_ascii=False,
        )

    digest = hashlib.sha256(f"{int(expect_reply)}:{message_body}".encode("utf-8")).hexdigest()[:16]
    wall_now = time.time()
    now_mono = time.monotonic()
    burst_win = cfg["burst_window_seconds"]
    dedup_win = cfg["dedup_window_seconds"]
    max_per_win = cfg["max_messages_per_window"]
    bucket_key = (home_key, target_key)
    keep_win = max(burst_win, dedup_win)

    with _LOCK:
        _evict_stale_send_buckets(now_mono, keep_win)
        history = [
            (ts, sender, d)
            for ts, sender, d in _RECENT_SENDS.get(bucket_key, [])
            if (now_mono - ts) < keep_win
        ]
        if dedup_win > 0 and any(
            sender == caller_sid and d == digest and (now_mono - ts) < dedup_win
            for ts, sender, d in history
        ):
            return json.dumps(
                {
                    "ok": False,
                    "error": "duplicate_message",
                    "message": f"Identical message already sent to {resolved['session_id']} within {dedup_win}s.",
                },
                ensure_ascii=False,
            )
        burst_count = sum(1 for ts, _sender, _d in history if (now_mono - ts) < burst_win)
        if burst_count >= max_per_win:
            return json.dumps(
                {
                    "ok": False,
                    "error": "rate_limited",
                    "message": f"Rate limit reached: {max_per_win} messages to {resolved['session_id']} within {burst_win}s.",
                },
                ensure_ascii=False,
            )
        history.append((now_mono, caller_sid, digest))
        _RECENT_SENDS[bucket_key] = history

    sender_info = store.lookup_sender_info(home, caller_sid, ns=ns, caller_session_key=caller_key)
    if sender_label_override:
        card_label = sender_label_override
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

    mounted_dormant = False
    delivery_method = "inject_message"
    accepted = bool(ctx.inject_message(payload, session_key=target_key))
    mount_sid = resolved["root_session_id"] or resolved["session_id"]
    if not accepted and cfg["auto_mount_dormant"] and ":local:dm:" in target_key:
        mounted_ok, _ = mount_dormant_local_session(
            home, mount_sid, timeout=cfg["mount_timeout_seconds"]
        )
        if mounted_ok:
            mounted_dormant = True
            accepted = bool(ctx.inject_message(payload, session_key=target_key))
    ws_err = ""
    if not accepted and _is_gateway_injection_allowed(ctx, home) and ":local:dm:" in target_key:
        ws_ok, ws_err = submit_via_gateway_ws(
            home, mount_sid, payload, timeout=cfg["mount_timeout_seconds"]
        )
        if ws_ok:
            accepted = True
            delivery_method = "gateway_ws"

    if not accepted:
        # A timed-out WS submit may still land in the target queue: keep the dedup entry so an
        # immediate retry cannot double-deliver. Only deterministic refusals are rolled back.
        in_flight = ws_err in ("gateway_thread_timeout", "TimeoutError")
        if not in_flight:
            with _LOCK:
                _RECENT_SENDS[bucket_key] = [
                    (ts, sender, d)
                    for ts, sender, d in _RECENT_SENDS.get(bucket_key, [])
                    if not (ts == now_mono and sender == caller_sid and d == digest)
                ]
        result: dict[str, Any] = {
            "ok": False,
            "error": "injection_refused",
            "target_session_id": resolved["session_id"],
            "target_session_key": target_key,
            "mounted_dormant": mounted_dormant,
            "message": (
                "Gateway refused message injection. Ensure "
                "plugins.entries.hermes-intercom.allow_gateway_injection: true is set in config.yaml "
                "and the target session belongs to this profile."
            ),
        }
        if ws_err:
            result["ws_error"] = ws_err
        return json.dumps(result, ensure_ascii=False)

    audit_row = {
        "ts": wall_now,
        "from_session_id": sender_info["session_id"],
        "to_session_id": resolved["session_id"],
        "to_session_key": target_key,
        "hop": next_hop,
        "max_hops": max_hops,
        "expect_reply": expect_reply,
        "mounted_dormant": mounted_dormant,
        "delivery_method": delivery_method,
        "digest": digest,
    }
    _append_audit(home, audit_row)

    return json.dumps(
        {
            "ok": True,
            "status": "queued",
            "delivery_method": delivery_method,
            "from_session_id": sender_info["session_id"],
            "target_session_id": resolved["session_id"],
            "target_title": resolved["title"],
            "target_session_key": target_key,
            "hop": next_hop,
            "max_hops": max_hops,
            "expect_reply": expect_reply,
            "mounted_dormant": mounted_dormant,
            "note": (
                "Accepted into the target session's durable FIFO queue. "
                "Runs immediately if the target is idle, or after its current turn if busy."
            ),
        },
        ensure_ascii=False,
    )
