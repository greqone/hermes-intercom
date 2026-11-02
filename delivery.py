"""Tool handlers, settings, rate/loop guards, and dormant-session mounting for hermes-intercom."""
from __future__ import annotations

import asyncio
import hashlib
import importlib
import json
import logging
import threading
import time
import uuid
from pathlib import Path
from typing import Any

from . import store

logger = logging.getLogger(__name__)

_LOCK = threading.RLock()
_RECENT_SENDS: dict[tuple[str, str], list[tuple[float, str]]] = {}  # (home_str, target_key) -> [(ts, sha256_prefix)]
_TURN_HOP: dict[str, int] = {}  # session_id -> inbound hop count


def _load_core_attr(mod_parts: tuple[str, ...], attr: str) -> Any:
    mod = importlib.import_module(".".join(mod_parts))
    return getattr(mod, attr)


def _pos_int(val: Any, default: int, *, minimum: int = 0, maximum: int = 10000) -> int:
    try:
        parsed = int(val)
    except (TypeError, ValueError):
        return default
    if parsed < minimum:
        return default
    return min(parsed, maximum)


def _pos_float(val: Any, default: float, *, minimum: float = 0.5, maximum: float = 120.0) -> float:
    try:
        parsed = float(val)
    except (TypeError, ValueError):
        return default
    if parsed < minimum:
        return default
    return min(parsed, maximum)


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
        "auto_mount_dormant": bool(_read_setting(ctx, "auto_mount_dormant", True)),
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
        line = json.dumps(record, ensure_ascii=False) + "\n"
        with _LOCK:
            with open(audit_file, "a", encoding="utf-8") as fh:
                fh.write(line)
    except Exception:
        logger.debug("hermes-intercom: audit write skipped", exc_info=True)


def _run_coro_sync(coro: Any, *, timeout: float = 8.0) -> tuple[bool, str]:
    try:
        try:
            asyncio.get_running_loop()
            running_loop = True
        except RuntimeError:
            running_loop = False

        if not running_loop:
            return asyncio.run(coro)

        result_box: list[tuple[bool, str]] = []
        err_box: list[BaseException] = []

        def _runner() -> None:
            try:
                result_box.append(asyncio.run(coro))
            except BaseException as exc:
                err_box.append(exc)

        t = threading.Thread(target=_runner, daemon=True)
        t.start()
        t.join(timeout=timeout + 2.0)
        if err_box:
            raise err_box[0]
        return result_box[0] if result_box else (False, "gateway_thread_timeout")
    except Exception as exc:
        logger.debug("hermes-intercom: gateway coroutine failed: %s", exc)
        return False, str(exc)


def mount_dormant_local_session(home: Path, target_session_id: str, *, timeout: float = 8.0) -> tuple[bool, str]:
    """Mount a dormant local session into the live gateway authority via session.resume + session.detach."""
    if not target_session_id:
        return False, "missing_target_session_id"

    async def _mount_coro() -> tuple[bool, str]:
        discover_fn = _load_core_attr(("hermes_cli", "gateway_runtime"), "discover_gateway_endpoint")
        connect_gw = _load_core_attr(("hermes_cli", "gateway_client"), "connect_gateway")

        discovery = await asyncio.to_thread(discover_fn, home, timeout=min(timeout, 5.0))
        if discovery.state != "ready" or discovery.endpoint is None:
            return False, f"gateway_not_ready:{discovery.state}"
        async with connect_gw(endpoint=discovery.endpoint) as client:
            resumed = await asyncio.wait_for(
                client.rpc("session.resume", session_id=target_session_id, omit_messages=True),
                timeout=timeout,
            )
            if not isinstance(resumed, dict) or resumed.get("error"):
                return False, f"resume_error:{resumed}"
            resumed_sid = str(resumed.get("session_id") or target_session_id)
            sub_id = str(resumed.get("subscription_id") or "")
            if sub_id:
                await asyncio.wait_for(
                    client.rpc("session.detach", session_id=resumed_sid, subscription_id=sub_id),
                    timeout=timeout,
                )
            return True, resumed_sid

    return _run_coro_sync(_mount_coro(), timeout=timeout)


def submit_via_gateway_ws(
    home: Path, target_session_id: str, content: str, *, timeout: float = 8.0
) -> tuple[bool, str]:
    """Deliver an attributed intercom card over the local Gateway WebSocket from an external process."""
    if not target_session_id or not content:
        return False, "missing_args"
    if content.lstrip().startswith("/"):
        return False, "slash_command_forbidden"

    async def _submit_coro() -> tuple[bool, str]:
        discover_fn = _load_core_attr(("hermes_cli", "gateway_runtime"), "discover_gateway_endpoint")
        connect_gw = _load_core_attr(("hermes_cli", "gateway_client"), "connect_gateway")

        discovery = await asyncio.to_thread(discover_fn, home, timeout=min(timeout, 5.0))
        if discovery.state != "ready" or discovery.endpoint is None:
            return False, f"gateway_not_ready:{discovery.state}"
        async with connect_gw(endpoint=discovery.endpoint) as client:
            resumed = await asyncio.wait_for(
                client.rpc("session.resume", session_id=target_session_id, omit_messages=True),
                timeout=timeout,
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
                    timeout=timeout,
                )
            finally:
                if sub_id:
                    try:
                        await asyncio.wait_for(
                            client.rpc("session.detach", session_id=resumed_sid, subscription_id=sub_id),
                            timeout=timeout,
                        )
                    except Exception:
                        pass
            if isinstance(receipt, dict) and receipt.get("admission_id"):
                return True, str(receipt["admission_id"])
            return False, f"submit_refused:{receipt}"

    return _run_coro_sync(_submit_coro(), timeout=timeout)


def on_pre_llm_call(session_id: str = "", user_message: Any = None, **_: Any) -> None:
    sid = str(session_id or "").strip()
    if not sid:
        return None
    text = user_message if isinstance(user_message, str) else ""
    hop = store.parse_hop_from_text(text)
    with _LOCK:
        if hop > 0:
            _TURN_HOP[sid] = hop
        else:
            _TURN_HOP.pop(sid, None)
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
        include_cron=bool(params.get("include_cron", False)),
        include_archived=bool(params.get("include_archived", False)),
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


def tool_session_send(args: dict[str, Any] | None, *, ctx: Any = None, session_id: str = "", **_: Any) -> str:
    params = dict(args or {})
    target_arg = str(params.get("target") or "").strip()
    message_body = str(params.get("message") or "").strip()
    expect_reply = bool(params.get("expect_reply", False))
    sender_label_override = str(params.get("sender_label") or "").strip()

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

    with _LOCK:
        mem_hop = _TURN_HOP.get(caller_sid, 0) if caller_sid else 0
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

    digest = hashlib.sha256(message_body.encode("utf-8")).hexdigest()[:16]
    now = time.time()
    burst_win = cfg["burst_window_seconds"]
    dedup_win = cfg["dedup_window_seconds"]
    max_per_win = cfg["max_messages_per_window"]
    bucket_key = (str(home), target_key)

    with _LOCK:
        history = [(ts, d) for ts, d in _RECENT_SENDS.get(bucket_key, []) if now - ts < max(burst_win, dedup_win)]
        if dedup_win > 0 and any(d == digest and (now - ts) < dedup_win for ts, d in history):
            return json.dumps(
                {
                    "ok": False,
                    "error": "duplicate_message",
                    "message": f"Identical message already sent to {resolved['session_id']} within {dedup_win}s.",
                },
                ensure_ascii=False,
            )
        burst_count = sum(1 for ts, _ in history if (now - ts) < burst_win)
        if burst_count >= max_per_win:
            return json.dumps(
                {
                    "ok": False,
                    "error": "rate_limited",
                    "message": f"Rate limit reached: {max_per_win} messages to {resolved['session_id']} within {burst_win}s.",
                },
                ensure_ascii=False,
            )
        history.append((now, digest))
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
    if not accepted and cfg["auto_mount_dormant"] and ":local:dm:" in target_key:
        mount_sid = resolved["root_session_id"] or resolved["session_id"]
        mounted_ok, _ = mount_dormant_local_session(
            home, mount_sid, timeout=cfg["mount_timeout_seconds"]
        )
        if mounted_ok:
            mounted_dormant = True
            accepted = bool(ctx.inject_message(payload, session_key=target_key))
        if not accepted and _is_gateway_injection_allowed(ctx, home):
            ws_ok, _ = submit_via_gateway_ws(
                home, mount_sid, payload, timeout=cfg["mount_timeout_seconds"]
            )
            if ws_ok:
                accepted = True
                delivery_method = "gateway_ws"

    if not accepted:
        with _LOCK:
            _RECENT_SENDS[bucket_key] = [
                (ts, d) for ts, d in _RECENT_SENDS.get(bucket_key, []) if not (ts == now and d == digest)
            ]
        return json.dumps(
            {
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
            },
            ensure_ascii=False,
        )

    audit_row = {
        "ts": now,
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
