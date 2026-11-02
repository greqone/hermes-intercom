"""Tool JSON schemas for hermes-intercom."""
from __future__ import annotations

SESSION_LIST_SCHEMA: dict = {
    "name": "session_list",
    "description": (
        "List active or recent Hermes sessions in this profile (Desktop, CLI, TUI, Discord, Telegram) "
        "that can be messaged with session_send. Returns session_id, title, source, cwd, model, "
        "session_key, message_count, addressable, and is_self, ordered by most recent activity."
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
                    "When true, instructs the target session how to reply back to this session via session_send. "
                    "When false (default), marks the message as FYI unless the message asks a question."
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
