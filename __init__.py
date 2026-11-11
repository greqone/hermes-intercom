"""hermes-intercom — cross-session discovery and messaging for Hermes Agent."""
from __future__ import annotations

import inspect
import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


def _frontmatter_scalar(raw: str) -> Any:
    """One unquoted YAML scalar or an inline ``[a, b]`` list. Nested blocks are not scalars."""
    text = raw.strip()
    if not text or text in {">", "|", ">-", "|-"}:
        return None
    if len(text) >= 2 and text[0] == text[-1] and text[0] in {"'", '"'}:
        return text[1:-1]
    if text.startswith("[") and text.endswith("]"):
        parts = [part.strip().strip("'\"") for part in text[1:-1].split(",")]
        return [part for part in parts if part]
    if text in {"true", "false"}:
        return text == "true"
    if text in {"null", "~"}:
        return None
    return text


def _frontmatter_stdlib(block: str) -> dict[str, Any]:
    """Top-level ``key: value`` pairs when PyYAML is not installed.

    A skill's name and description are flat scalars. Nested maps (``metadata:``) need PyYAML;
    without it those keys are omitted rather than registered as an empty description.
    """
    parsed: dict[str, Any] = {}
    for line in block.splitlines():
        if not line.strip() or line[:1].isspace() or line.lstrip().startswith("#"):
            continue
        key, sep, raw = line.partition(":")
        if not sep or not key.strip():
            continue
        value = _frontmatter_scalar(raw)
        if value is None and raw.strip() in {"", ">", "|", ">-", "|-"}:
            continue
        parsed[key.strip()] = value
    return parsed


def _read_skill_frontmatter(skill_md: Path) -> dict[str, Any]:
    """Parse the YAML frontmatter block at the top of ``SKILL.md``; ``{}`` when absent/invalid."""
    try:
        text = skill_md.read_text(encoding="utf-8-sig")
    except (OSError, UnicodeError):
        logger.debug("hermes-intercom: SKILL.md unreadable", exc_info=True)
        return {}
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}
    end = None
    for idx in range(1, len(lines)):
        if lines[idx].strip() == "---":
            end = idx
            break
    if end is None:
        return {}
    block = "\n".join(lines[1:end])
    try:
        import yaml

        parsed = yaml.safe_load(block)
    except Exception:
        # PyYAML is not a declared dependency. Flat skill fields still have to register.
        logger.debug("hermes-intercom: SKILL.md frontmatter fell back to stdlib", exc_info=True)
        parsed = _frontmatter_stdlib(block)
    return parsed if isinstance(parsed, dict) else {}


def _register_skill(ctx: Any, name: str, path: Path, description: str, frontmatter: dict[str, Any]) -> None:
    """Pass skill metadata when the host supports it, else fall back to ``(name, path)``."""
    register = ctx.register_skill
    try:
        signature: Any = inspect.signature(register)
    except (TypeError, ValueError):
        signature = None

    supports_metadata = True
    if signature is not None:
        params = signature.parameters
        if not any(param.kind is inspect.Parameter.VAR_KEYWORD for param in params.values()):
            supports_metadata = {"description", "frontmatter"} <= set(params)

    if supports_metadata:
        try:
            register(name, path, description=description, frontmatter=frontmatter)
            return
        except TypeError:
            # Minimal test doubles / older hosts only accept (name, path).
            logger.debug("hermes-intercom: register_skill rejected metadata kwargs", exc_info=True)
    register(name, path)


def register(ctx) -> None:
    """Register hermes-intercom tools, hooks, and bundled skill."""
    from . import delivery, schemas

    common = {"toolset": "intercom"}
    ctx.register_tool(
        name="session_list",
        schema=schemas.SESSION_LIST_SCHEMA,
        handler=lambda args, **kw: delivery.tool_session_list(args, ctx=ctx, **kw),
        description=schemas.SESSION_LIST_SCHEMA["description"],
        emoji="📡",
        **common,
    )
    ctx.register_tool(
        name="session_send",
        schema=schemas.SESSION_SEND_SCHEMA,
        handler=lambda args, **kw: delivery.tool_session_send(args, ctx=ctx, **kw),
        description=schemas.SESSION_SEND_SCHEMA["description"],
        emoji="📨",
        **common,
    )
    ctx.register_hook("pre_llm_call", lambda **kw: delivery.on_pre_llm_call(ctx=ctx, **kw))
    try:
        ctx.on_unload(delivery.shutdown_discovery_executor)
    except Exception:
        logger.debug("hermes-intercom: host has no on_unload", exc_info=True)

    try:
        skills_dir = Path(__file__).parent / "skills"
        if skills_dir.is_dir():
            for child in sorted(skills_dir.iterdir()):
                skill_md = child / "SKILL.md"
                if not (child.is_dir() and skill_md.exists()):
                    continue
                frontmatter = _read_skill_frontmatter(skill_md)
                description = str(frontmatter.get("description") or "")
                _register_skill(ctx, child.name, skill_md, description, frontmatter)
    except Exception:
        logger.debug("hermes-intercom: skill bundling skipped", exc_info=True)
