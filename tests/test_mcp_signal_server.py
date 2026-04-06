"""Tests for nightshift MCP signal server (JSON-RPC 2.0 stdio)."""

import json
import subprocess
import sys
from pathlib import Path

import pytest

SERVER_PATH = Path(__file__).resolve().parent.parent / "nightshift-mcp-server.py"


def _send_request(proc, method, params=None, req_id=1):
    """Send a JSON-RPC request and return the parsed response."""
    msg = {"jsonrpc": "2.0", "method": method, "id": req_id}
    if params is not None:
        msg["params"] = params
    line = json.dumps(msg) + "\n"
    proc.stdin.write(line)
    proc.stdin.flush()
    raw = proc.stdout.readline()
    return json.loads(raw)


def _send_notification(proc, method, params=None):
    """Send a JSON-RPC notification (no id) and return None."""
    msg = {"jsonrpc": "2.0", "method": method}
    if params is not None:
        msg["params"] = params
    line = json.dumps(msg) + "\n"
    proc.stdin.write(line)
    proc.stdin.flush()
    # Notifications produce no response; send a known request to flush.


@pytest.fixture
def server():
    """Start the MCP server as a subprocess."""
    proc = subprocess.Popen(
        [sys.executable, str(SERVER_PATH)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    yield proc
    proc.terminate()
    proc.wait(timeout=5)


def test_initialize_returns_protocol_version(server):
    resp = _send_request(server, "initialize", {
        "protocolVersion": "2024-11-05",
        "capabilities": {},
        "clientInfo": {"name": "test", "version": "0.1"},
    })
    assert resp["jsonrpc"] == "2.0"
    assert resp["result"]["protocolVersion"] == "2024-11-05"
    assert resp["result"]["serverInfo"]["name"] == "nightshift-signals"


def test_tools_list_returns_three_tools(server):
    # Must initialize first
    _send_request(server, "initialize", {
        "protocolVersion": "2024-11-05",
        "capabilities": {},
        "clientInfo": {"name": "test", "version": "0.1"},
    })
    resp = _send_request(server, "tools/list", {}, req_id=2)
    tools = resp["result"]["tools"]
    assert len(tools) == 3
    names = {t["name"] for t in tools}
    assert names == {"nightshift_done", "nightshift_checkpoint", "nightshift_question"}


def test_tools_call_done_returns_signal(server):
    _send_request(server, "initialize", {
        "protocolVersion": "2024-11-05",
        "capabilities": {},
        "clientInfo": {"name": "test", "version": "0.1"},
    })
    resp = _send_request(server, "tools/call", {
        "name": "nightshift_done",
        "arguments": {"summary": "Task complete"},
    }, req_id=2)
    content = resp["result"]["content"]
    assert len(content) == 1
    assert content[0]["type"] == "text"
    assert "Signal received: nightshift_done" in content[0]["text"]


def test_tools_call_checkpoint_returns_signal(server):
    _send_request(server, "initialize", {
        "protocolVersion": "2024-11-05",
        "capabilities": {},
        "clientInfo": {"name": "test", "version": "0.1"},
    })
    resp = _send_request(server, "tools/call", {
        "name": "nightshift_checkpoint",
        "arguments": {"description": "Halfway done"},
    }, req_id=2)
    content = resp["result"]["content"]
    assert len(content) == 1
    assert "Signal received: nightshift_checkpoint" in content[0]["text"]


def test_tools_call_question_returns_signal(server):
    _send_request(server, "initialize", {
        "protocolVersion": "2024-11-05",
        "capabilities": {},
        "clientInfo": {"name": "test", "version": "0.1"},
    })
    resp = _send_request(server, "tools/call", {
        "name": "nightshift_question",
        "arguments": {"question": "What port?"},
    }, req_id=2)
    content = resp["result"]["content"]
    assert len(content) == 1
    assert "Signal received: nightshift_question" in content[0]["text"]


def test_unknown_method_returns_error(server):
    _send_request(server, "initialize", {
        "protocolVersion": "2024-11-05",
        "capabilities": {},
        "clientInfo": {"name": "test", "version": "0.1"},
    })
    resp = _send_request(server, "unknown/method", {}, req_id=2)
    assert "error" in resp
    assert resp["error"]["code"] == -32601


def test_notifications_initialized_returns_none(server):
    """notifications/initialized is a notification — no response expected."""
    _send_request(server, "initialize", {
        "protocolVersion": "2024-11-05",
        "capabilities": {},
        "clientInfo": {"name": "test", "version": "0.1"},
    })
    # Send the notification (no id field)
    _send_notification(server, "notifications/initialized")
    # Verify server is still alive by sending a real request after
    resp = _send_request(server, "tools/list", {}, req_id=3)
    assert "result" in resp
    assert len(resp["result"]["tools"]) == 3
