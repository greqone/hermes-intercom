"""hermes-intercom — cross-session discovery and messaging for Hermes Agent."""
from __future__ import annotations

import inspect
import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


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
    try:
        import yaml

        parsed = yaml.safe_load("\n".join(lines[1:end]))
    except Exception:
        logger.debug("hermes-intercom: SKILL.md frontmatter unparsable", exc_info=True)
        return {}
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
