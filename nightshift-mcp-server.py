#!/usr/bin/env python3
"""Nightshift MCP signal server — JSON-RPC 2.0 over stdio.

Exposes nightshift_done, nightshift_checkpoint, and nightshift_question tools
via the Model Context Protocol (MCP). Agents call these tools to signal
lifecycle events instead of emitting text markers.
"""

import json
import sys

PROTOCOL_VERSION = "2024-11-05"
SERVER_NAME = "nightshift-signals"
SERVER_VERSION = "1.0.0"

TOOLS = [
    {
        "name": "nightshift_done",
        "description": "Signal that the task is complete",
        "inputSchema": {
            "type": "object",
            "properties": {
                "summary": {
                    "type": "string",
                    "description": "Brief summary of what was accomplished",
                },
            },
            "required": ["summary"],
        },
    },
    {
        "name": "nightshift_checkpoint",
        "description": "Signal a checkpoint with progress description",
        "inputSchema": {
            "type": "object",
            "properties": {
                "description": {
                    "type": "string",
                    "description": "Description of current progress",
                },
            },
            "required": ["description"],
        },
    },
    {
        "name": "nightshift_question",
        "description": "Ask a question and wait for an answer",
        "inputSchema": {
            "type": "object",
            "properties": {
                "question": {
                    "type": "string",
                    "description": "The question to ask",
                },
            },
            "required": ["question"],
        },
    },
]


def handle_initialize(req_id):
    """Handle initialize request."""
    return {
        "jsonrpc": "2.0",
        "id": req_id,
        "result": {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {"tools": {}},
            "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
        },
    }


def handle_tools_list(req_id):
    """Handle tools/list request."""
    return {
        "jsonrpc": "2.0",
        "id": req_id,
        "result": {"tools": TOOLS},
    }


def handle_tools_call(req_id, params):
    """Handle tools/call request."""
    tool_name = params.get("name", "")
    arguments = params.get("arguments", {})
    print(f"[nightshift-signal] {tool_name}: {arguments}", file=sys.stderr)
    return {
        "jsonrpc": "2.0",
        "id": req_id,
        "result": {
            "content": [
                {
                    "type": "text",
                    "text": f"Signal received: {tool_name}",
                },
            ],
        },
    }


def handle_unknown(req_id, method):
    """Handle unknown methods with JSON-RPC error -32601."""
    return {
        "jsonrpc": "2.0",
        "id": req_id,
        "error": {
            "code": -32601,
            "message": f"Method not found: {method}",
        },
    }


HANDLERS = {
    "initialize": lambda req_id, _params: handle_initialize(req_id),
    "tools/list": lambda req_id, _params: handle_tools_list(req_id),
    "tools/call": lambda req_id, params: handle_tools_call(req_id, params),
}

# Notifications (no id, no response)
NOTIFICATIONS = {"notifications/initialized"}


def main():
    """Read JSON-RPC messages from stdin, dispatch, write responses to stdout."""
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError as exc:
            print(
                f"[nightshift-signal] JSON parse error: {exc}",
                file=sys.stderr,
            )
            continue

        method = msg.get("method", "")
        req_id = msg.get("id")
        params = msg.get("params", {})

        # Notifications have no id — no response expected
        if req_id is None or method in NOTIFICATIONS:
            continue

        handler = HANDLERS.get(method)
        if handler:
            response = handler(req_id, params)
        else:
            response = handle_unknown(req_id, method)

        sys.stdout.write(json.dumps(response) + "\n")
        sys.stdout.flush()


if __name__ == "__main__":
    main()
