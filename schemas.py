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
        "`agent:<namespace>:local:dm:<session_id>`; such a session must be mounted in the gateway "
        "authority before a send is accepted."
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
        "or unique title substring). Wakes an idle target session as a new turn or queues into the target's "
        "durable FIFO if busy. Renders as an attributed agent card in Hermes Desktop. Includes multi-hop "
        "loop protection so two sessions can exchange a bounded reply without infinite ping-pong."
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
        },
        "required": ["target", "message"],
    },
}
