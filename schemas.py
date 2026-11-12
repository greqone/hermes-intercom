"""Tool JSON schemas for hermes-intercom."""
from __future__ import annotations

SESSION_LIST_SCHEMA: dict = {
    "name": "session_list",
    "description": (
        "List active or recent Hermes sessions in this profile (Desktop, CLI, TUI, Discord, Telegram) "
        "that can be messaged with session_send, ordered by most recent activity. Each entry returns "
        "session_id, root_session_id, title, source, model, cwd, session_key, message_count, "
        "last_activity_at, addressable, routed, and is_self. "
        "`addressable` is true when the entry has a gateway session_key (explicit, from the routing "
        "table, or synthesized). `routed` is true only when a real route exists — an explicit "
        "session_key or a gateway_routing row — and is false when the key was merely synthesized as "
        "`agent:<namespace>:local:dm:<session_id>`; an unverified route is refused. "
        "A route does not guarantee that a dormant target is currently injectable. In a shared "
        "gateway, results may include other users' chat titles; restrict the intercom toolset to owner-only platforms."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "Optional case-insensitive substring filter on title, session_id, source, or cwd.",
            },
            "source": {
                "type": "string",
                "description": "Optional exact source filter (e.g. 'local', 'gui', 'desktop', 'discord', 'telegram').",
            },
            "limit": {
                "type": "integer",
                "description": "Maximum number of sessions to return (default 20, max 100).",
                "default": 20,
            },
            "include_cron": {
                "type": "boolean",
                "description": "Include automated cron sessions (default false).",
                "default": False,
            },
            "include_archived": {
                "type": "boolean",
                "description": "Include archived or hidden sessions (default false).",
                "default": False,
            },
        },
        "required": [],
    },
}

SESSION_SEND_SCHEMA: dict = {
    "name": "session_send",
    "description": (
        "Send a message to another Hermes session in the same profile (by session_id, prefix, session_key, "
        "or unique title substring). Uses only the public ctx.inject_message API. "
        "Returns scheduled when the host accepts scheduling, not proof of execution or durable "
        "delivery. Refuses CLI/unknown sender surfaces and unverified routes. It does not mount "
        "dormant targets or bypass an injection refusal. Shows an attributed native notice in "
        "Hermes Desktop and enforces hop, duplicate and rate guards. In a shared gateway it can "
        "start turns in other users' chats; restrict the intercom toolset to owner-only platforms."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "target": {
                "type": "string",
                "description": "Target session_id (full or >=6 char prefix), session_key, or unique title substring.",
            },
            "message": {
                "type": "string",
                "description": "Message body to deliver to the target session.",
            },
            "expect_reply": {
                "type": "boolean",
                "description": (
                    "When true, marks the card as mode=reply and instructs the target session how to "
                    "reply back via session_send. When false (default), marks the card as mode=fyi."
                ),
                "default": False,
            },
            "sender_label": {
                "type": "string",
                "description": "Optional short sender label override (defaults to this session's title and short ID).",
            },
            "request_id": {
                "type": "string",
                "description": (
                    "Optional retry identity, 1-1024 characters. While retained, reuse the request_id, "
                    "body, expect_reply and exact target_session_id from the first result. A retained "
                    "accepted operation replays its outcome without another injection. A changed body "
                    "or unrelated sender/target conflicts. State is process-local and bounded to 4096 "
                    "entries; completed entries are evictable under pressure. After eviction or restart, "
                    "check target history before resending. This is not an exactly-once guarantee. "
                    "Omit this only for genuinely new work."
                ),
            },
        },
        "required": ["target", "message"],
    },
}
