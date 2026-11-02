"""Tool handlers, settings, rate/loop guards, and dormant-session mounting for hermes-intercom."""
from __future__ import annotations

import asyncio
import functools
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
from collections.abc import Mapping
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Callable

from . import store

logger = logging.getLogger(__name__)

_LOCK = threading.RLock()
# bucket_key = (home_str, target_key) -> [(ts_monotonic, sender_session_id, digest, send_token)]
_RECENT_SENDS: dict[tuple[str, str], list[tuple[float, str, str, str]]] = {}
# (home_str, session_id) -> (inbound hop, sender max_hops, ts_monotonic)
_TURN_HOP: dict[tuple[str, str], tuple[int, int, float]] = {}

# Bounds for the inbound-hop memory so long-running Gateway processes never leak (W213a).
_TURN_HOP_MAX = 1024
_TURN_HOP_TTL = 3600.0

# One caller with a short window must never evict another caller's 600s history (W212b).
_SEND_BUCKET_MIN_HORIZON = 600.0

# Own pool for gateway discovery: ``asyncio.run()`` waits for the *default* executor's
# ``shutdown_default_executor()``, so a slow ``to_thread`` worker can hang the whole call (W211a).
_DISCOVERY_EXECUTOR = ThreadPoolExecutor(max_workers=4, thread_name_prefix="hermes-intercom-discover")

_AUDIT_MAX_BYTES = 2 * 1024 * 1024
_INTERCOM_TAG_STRIP_RE = re.compile(r"\[intercom[^\]]*\]", re.IGNORECASE)

# Errors that prove ``prompt.submit`` never reached the gateway (or was provably refused), so the
# dedup reservation can be rolled back safely. Anything else may still land in the target queue.
_DETERMINISTIC_WS_ERROR_PREFIXES = (
    "pre_submit:",
    "submit_refused:",
    "resume_error:",
    "gateway_not_ready:",
    "core_unavailable:",
    "missing_args",
    "slash_command_forbidden",
)


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


def _json(payload: dict[str, Any]) -> str:
    """Serialize a tool result: never emits NaN/Infinity, never mangles Unicode."""
    try:
        return json.dumps(payload, ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError):
        # Degraded but non-raising path for exotic values coming back from the store.
        return json.dumps(payload, ensure_ascii=False, default=str, allow_nan=True)


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


def _exc_label(exc: BaseException) -> str:
    if isinstance(exc, asyncio.TimeoutError):
        return "TimeoutError"
    text = str(exc).strip()
    return text or exc.__class__.__name__


def _run_coro_sync(coro_fn: Callable[[], Any], *, timeout: float = 8.0) -> tuple[bool, str]:
    """Run a zero-arg coroutine factory to completion within one overall ``timeout`` budget.

    Taking a factory (not a coroutine object) means a failure before the worker thread starts can
    never leak an unawaited coroutine, and ``asyncio.wait_for`` is created *inside* the fresh loop so
    a failure during loop creation leaves no coroutine object behind at all (W211c).
    """
    try:
        timeout = max(0.1, float(timeout))
    except (TypeError, ValueError):
        timeout = 8.0

    def _execute() -> tuple[bool, str]:
        async def _entry() -> tuple[bool, str]:
            return await asyncio.wait_for(coro_fn(), timeout=timeout)

        return asyncio.run(_entry())

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


async def _discover_via_executor(discover_fn: Callable[..., Any], home: Path, budget: float) -> Any:
    """Run ``discover_gateway_endpoint`` off the loop on the shared pool instead of ``to_thread``."""
    loop = asyncio.get_running_loop()
    call = functools.partial(discover_fn, home, timeout=max(0.2, float(budget)))
    return await asyncio.wait_for(
        loop.run_in_executor(_DISCOVERY_EXECUTOR, call),
        timeout=max(0.5, float(budget) + 0.5),
    )


def _discovery_error(discovery: Any) -> str:
    """Empty string when the discovered endpoint is usable, else a deterministic reason.

    ``draining`` with ``reason_code == "restart_wait"`` still exposes a live endpoint, so a gateway
    restart window must not fail the delivery (W214b).
    """
    state = str(getattr(discovery, "state", "") or "")
    if getattr(discovery, "endpoint", None) is None:
        return f"gateway_not_ready:{state or 'unknown'}"
    if state == "ready":
        return ""
    if state == "draining" and str(getattr(discovery, "reason_code", "") or "") == "restart_wait":
        return ""
    return f"gateway_not_ready:{state or 'unknown'}"


class _HomeOverride:
    """Bind ``HERMES_HOME`` for the gateway coroutine so tickets are minted for ``home`` (W214a)."""

    def __init__(self, home: Path) -> None:
        self._home = home
        self._reset: Callable[[Any], Any] | None = None
        self._token: Any = None

    def __enter__(self) -> "_HomeOverride":
        try:
            mod = importlib.import_module("hermes_constants")
            setter = getattr(mod, "set_hermes_home_override", None)
            resetter = getattr(mod, "reset_hermes_home_override", None)
            if callable(setter) and callable(resetter):
                self._token = setter(str(self._home))
                self._reset = resetter
        except Exception:
            self._reset = None
            logger.debug("hermes-intercom: HERMES_HOME override unavailable", exc_info=True)
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> bool:
        if self._reset is not None:
            try:
                self._reset(self._token)
            except Exception:
                logger.debug("hermes-intercom: HERMES_HOME override reset failed", exc_info=True)
        return False


async def _detach_session(client: Any, session_id: str, subscription_id: str, remaining: float) -> None:
    """Best-effort detach with its own tiny budget; it can never discard a real receipt (W211b)."""
    budget = max(0.1, min(float(remaining), 1.5))
    try:
        await asyncio.wait_for(
            client.rpc("session.detach", session_id=session_id, subscription_id=subscription_id),
            timeout=budget,
        )
    except BaseException:  # noqa: BLE001 - a slow/failed detach must not undo a good receipt
        logger.debug("hermes-intercom: session.detach failed (ignored)", exc_info=True)


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
        with _HomeOverride(home):
            try:
                discover_fn = _load_core_attr(("hermes_cli", "gateway_runtime"), "discover_gateway_endpoint")
                connect_gw = _load_core_attr(("hermes_cli", "gateway_client"), "connect_gateway")
            except Exception as exc:  # noqa: BLE001 - absent core is a plain refusal
                return False, f"core_unavailable:{_exc_label(exc)}"

            discovery = await _discover_via_executor(discover_fn, home, min(_rem(), 5.0))
            problem = _discovery_error(discovery)
            if problem:
                return False, problem

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
                    await _detach_session(client, resumed_sid, sub_id, _rem())
                return True, resumed_sid

    return _run_coro_sync(_mount_coro, timeout=timeout)


def submit_via_gateway_ws(
    home: Path, target_session_id: str, content: str, *, timeout: float = 8.0
) -> tuple[bool, str]:
    """Deliver an attributed intercom card over the local Gateway WebSocket from an external process.

    Returns ``(False, "pre_submit:<label>")`` whenever ``prompt.submit`` was never sent, so callers can
    safely roll back a dedup reservation. Once a submit was attempted, errors stay opaque (except
    provable refusals) because the prompt may already sit in the target's queue (W212c).
    """
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
        submit_started = False
        admission_id = ""
        resumed_sid = ""
        sub_id = ""
        receipt: Any = None

        with _HomeOverride(home):
            try:
                discover_fn = _load_core_attr(("hermes_cli", "gateway_runtime"), "discover_gateway_endpoint")
                connect_gw = _load_core_attr(("hermes_cli", "gateway_client"), "connect_gateway")
            except Exception as exc:  # noqa: BLE001 - nothing was submitted yet
                return False, f"pre_submit:core_unavailable:{_exc_label(exc)}"

            try:
                discovery = await _discover_via_executor(discover_fn, home, min(_rem(), 5.0))
            except (KeyboardInterrupt, SystemExit):
                raise
            except BaseException as exc:  # noqa: BLE001 - nothing was submitted yet
                return False, f"pre_submit:{_exc_label(exc)}"
            problem = _discovery_error(discovery)
            if problem:
                return False, f"pre_submit:{problem}"

            try:
                async with connect_gw(endpoint=discovery.endpoint) as client:
                    try:
                        resumed = await asyncio.wait_for(
                            client.rpc("session.resume", session_id=target_session_id, omit_messages=True),
                            timeout=_rem(),
                        )
                        if not isinstance(resumed, dict) or resumed.get("error"):
                            return False, f"pre_submit:resume_error:{resumed}"
                        resumed_sid = str(resumed.get("session_id") or target_session_id)
                        sub_id = str(resumed.get("subscription_id") or "")
                        # From here on the prompt may reach the target queue.
                        submit_started = True
                        receipt = await asyncio.wait_for(
                            client.rpc(
                                "prompt.submit",
                                session_id=resumed_sid,
                                input_id=uuid.uuid4().hex,
                                text=content,
                            ),
                            timeout=_rem(),
                        )
                        if isinstance(receipt, dict) and receipt.get("admission_id"):
                            admission_id = str(receipt["admission_id"])
                    finally:
                        if sub_id:
                            await _detach_session(client, resumed_sid, sub_id, _rem())
            except (KeyboardInterrupt, SystemExit):
                raise
            except BaseException as exc:  # noqa: BLE001 - an admission receipt beats any transport error
                if admission_id:
                    return True, admission_id
                label = _exc_label(exc)
                return False, label if submit_started else f"pre_submit:{label}"

        if admission_id:
            return True, admission_id
        return False, f"submit_refused:{receipt}"

    return _run_coro_sync(_submit_coro, timeout=timeout)


def _ws_error_in_flight(ws_err: str) -> bool:
    """True when a WS failure may still have queued the prompt, so its dedup entry must stay."""
    label = (ws_err or "").strip()
    if not label:
        return False
    return not label.startswith(_DETERMINISTIC_WS_ERROR_PREFIXES)


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
    hop, sender_max_hops = _parse_intercom_tag(text)
    home_key = str(store.resolve_home(ctx)) if ctx is not None else ""
    key = (home_key, sid)
    now_mono = time.monotonic()
    with _LOCK:
        _evict_stale_turn_hops(now_mono)
        if hop > 0:
            _remember_turn_hop(key, hop, sender_max_hops, now_mono)
            if home_key:
                _TURN_HOP.pop(("", sid), None)
        else:
            _TURN_HOP.pop(key, None)
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
    return _json(
        {
            "ok": True,
            "current_session_id": str(session_id or ""),
            "current_session_key": caller_key,
            "count": len(sessions),
            "sessions": sessions,
        }
    )


def _evict_stale_send_buckets(now_mono: float, keep_win: float) -> None:
    """Drop buckets with no entry inside the longest guard window (caller holds ``_LOCK``)."""
    horizon = max(float(keep_win), _SEND_BUCKET_MIN_HORIZON)
    stale: list[tuple[str, str]] = []
    for key, entries in _RECENT_SENDS.items():
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


def _recent_sends_for(
    bucket_key: tuple[str, str], now_mono: float, keep_win: float
) -> list[tuple[float, str, str, str]]:
    """Fresh history for one bucket, normalized to 4-tuples (caller holds ``_LOCK``)."""
    history: list[tuple[float, str, str, str]] = []
    for entry in _RECENT_SENDS.get(bucket_key, []):
        try:
            ts = float(entry[0])
            sender = str(entry[1])
            digest = str(entry[2])
        except (TypeError, ValueError, IndexError):
            continue
        token = str(entry[3]) if len(entry) > 3 else ""
        if (now_mono - ts) < keep_win:
            history.append((ts, sender, digest, token))
    return history


def _inject(ctx: Any, payload: str, target_key: str) -> bool:
    """Call ``ctx.inject_message`` so a host exception can never escape the tool (W212a)."""
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
    if any(value is not None and not isinstance(value, str) for value in (raw_target, raw_message, raw_label)):
        return _json(
            {
                "ok": False,
                "error": "invalid_arguments",
                "message": "target, message, and sender_label must be strings",
            }
        )

    target_arg = _sanitize_utf8(raw_target or "").strip()
    message_body = _sanitize_utf8(raw_message or "").strip()
    expect_reply = _parse_bool(params.get("expect_reply", False), False)
    sender_label_override = _sanitize_label_override(_sanitize_utf8(raw_label or ""))

    if not target_arg:
        return _json({"ok": False, "error": "missing_target", "message": "target is required"})
    if not message_body:
        return _json({"ok": False, "error": "empty_message", "message": "message must not be empty"})
    if ctx is None or not hasattr(ctx, "inject_message"):
        return _json(
            {
                "ok": False,
                "error": "plugin_context_unavailable",
                "message": "Plugin context does not provide inject_message.",
            }
        )

    cfg = read_settings(ctx)
    home = store.resolve_home(ctx)
    ns = store.profile_namespace(ctx)
    caller_key = store.calling_session_key()
    caller_sid = str(session_id or "").strip()
    if not caller_sid and caller_key and ":local:dm:local-" in caller_key:
        # Recover the caller id from its gateway key so the hop guard still applies (W204).
        caller_sid = caller_key.rsplit(":", 1)[-1].strip()

    resolved, candidates = store.resolve_target(
        home,
        target_arg,
        ns=ns,
        caller_session_id=caller_sid,
        caller_session_key=caller_key,
    )
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
                "hint": "Call session_list first, then pass an exact session_id or unique title substring.",
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
    db_hop = store.latest_user_message_hop(home, caller_sid) if caller_sid else 0
    current_hop = max(mem_hop, db_hop)
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
    # Unique per send: two sends inside one 15.6ms Windows clock tick must not alias (W211).
    send_token = uuid.uuid4().hex
    wall_now = time.time()
    burst_win = cfg["burst_window_seconds"]
    dedup_win = cfg["dedup_window_seconds"]
    max_per_win = cfg["max_messages_per_window"]
    bucket_key = (home_key, target_key)
    keep_win = max(burst_win, dedup_win)

    with _LOCK:
        _evict_stale_send_buckets(now_mono, keep_win)
        history = _recent_sends_for(bucket_key, now_mono, keep_win)
        if dedup_win > 0 and any(
            sender == caller_sid and d == digest and (now_mono - ts) < dedup_win
            for ts, sender, d, _tok in history
        ):
            return _json(
                {
                    "ok": False,
                    "error": "duplicate_message",
                    "message": f"Identical message already sent to {resolved['session_id']} within {dedup_win}s.",
                }
            )
        burst_count = sum(1 for ts, _sender, _d, _tok in history if (now_mono - ts) < burst_win)
        if burst_count >= max_per_win:
            return _json(
                {
                    "ok": False,
                    "error": "rate_limited",
                    "message": f"Rate limit reached: {max_per_win} messages to {resolved['session_id']} within {burst_win}s.",
                }
            )
        history.append((now_mono, caller_sid, digest, send_token))
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

    # One overall budget shared by mount + WS submit so a dormant wake cannot double the wait (W212d).
    overall_deadline = time.monotonic() + float(cfg["mount_timeout_seconds"])
    mount_timeout = float(cfg["mount_timeout_seconds"])

    def _budget() -> float:
        return max(0.2, min(mount_timeout, overall_deadline - time.monotonic()))

    mount_sid = resolved.get("root_session_id") or resolved["session_id"]
    dormant_local = ":local:dm:" in target_key
    routed = resolved.get("routed", True)
    routed = True if routed is None else bool(routed)

    mounted_dormant = False
    delivery_method = "inject_message"

    if not routed and dormant_local and cfg["auto_mount_dormant"]:
        # Unrouted :local:dm: keys are a false positive for core: inject_message returns True
        # synchronously and then drops the payload, so mount before injecting (W202).
        mounted_ok, _info = mount_dormant_local_session(home, mount_sid, timeout=_budget())
        if mounted_ok:
            mounted_dormant = True

    accepted = _inject(ctx, payload, target_key)

    if not accepted and not mounted_dormant and cfg["auto_mount_dormant"] and dormant_local:
        # Routed but not mounted in authority.sessions right now: core returns False synchronously.
        mounted_ok, _info = mount_dormant_local_session(home, mount_sid, timeout=_budget())
        if mounted_ok:
            mounted_dormant = True
            accepted = _inject(ctx, payload, target_key)

    ws_err = ""
    if not accepted and _is_gateway_injection_allowed(ctx, home) and dormant_local:
        ws_ok, ws_err = submit_via_gateway_ws(home, mount_sid, payload, timeout=_budget())
        if ws_ok:
            accepted = True
            delivery_method = "gateway_ws"

    if not accepted:
        # A timed-out/unproven WS submit may still land in the target queue: keep the dedup entry so
        # an immediate retry cannot double-deliver. Only deterministic refusals are rolled back.
        if not _ws_error_in_flight(ws_err):
            with _LOCK:
                remaining = [
                    entry
                    for entry in _RECENT_SENDS.get(bucket_key, [])
                    if not (len(entry) > 3 and entry[3] == send_token)
                ]
                if remaining:
                    _RECENT_SENDS[bucket_key] = remaining
                else:
                    _RECENT_SENDS.pop(bucket_key, None)
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
        return _json(result)

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

    return _json(
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
        }
    )
