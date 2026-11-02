"""hermes-intercom — cross-session discovery and messaging for Hermes Agent."""
from __future__ import annotations

import logging
from pathlib import Path

logger = logging.getLogger(__name__)


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
    ctx.register_hook("pre_llm_call", delivery.on_pre_llm_call)

    try:
        skills_dir = Path(__file__).parent / "skills"
        if skills_dir.is_dir():
            for child in sorted(skills_dir.iterdir()):
                skill_md = child / "SKILL.md"
                if child.is_dir() and skill_md.exists():
                    ctx.register_skill(child.name, skill_md)
    except Exception:
        logger.debug("hermes-intercom: skill bundling skipped", exc_info=True)
