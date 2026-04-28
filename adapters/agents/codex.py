"""OpenAI Codex CLI adapter.

Headless JSONL mode: fire-and-forget with --json output.
Events keyed by `type` field (thread.started, item.completed, turn.completed, etc.).
Multi-turn uses `codex exec resume <thread_id> "prompt"`.
"""

import json
import logging
import re
import subprocess
import time
from pathlib import Path
from typing import Optional

from typing import Iterator

from adapters.agents.base import HeadlessAgentBase, TOOL_RESULT_PREVIEW_LEN
from core.constants import MCP_SIGNAL_SERVER
from core.protocols import AgentEvent, AgentEventType

log = logging.getLogger(__name__)

DOCKERENV_PATH = Path("/.dockerenv")


def _in_docker() -> bool:
    """Detect if running inside a Docker container."""
    return DOCKERENV_PATH.exists()


class CodexAgent(HeadlessAgentBase):
    # Patterns indicating authentication/authorization failures.
    # Note: "status 429", "rate limit" are handled as transient errors
    # with retry in HeadlessAgentBase._maybe_retry_transient()
    AUTH_FAILURE_PATTERNS = (
        "status 401",
        "unauthorized",
        "invalid api key",
        "incorrect api key",
        "authentication_error",
        "insufficient_quota",
        "missing authentication",
    )

    def __init__(
        self,
        command: str = "codex",
        stall_timeout_s: float = 300.0,
        extra_args: list[str] | None = None,
        signal_method: str = "auto",
    ):
        super().__init__(command, stall_timeout_s, extra_args, signal_method=signal_method)
        self._extra_events: list[AgentEvent] = []
        # Buffer @@DONE@@ until turn.completed arrives with usage data
        self._pending_done_raw: str | None = None

    def _before_stream(self) -> None:
        self._extra_events.clear()
        self._pending_done_raw = None

    def _drain_extra(self) -> Iterator[AgentEvent]:
        while self._extra_events:
            yield self._extra_events.pop(0)

    def start(self, prompt: str, workspace: Path, max_turns: int = 50) -> None:
        self._store_start_params(prompt, workspace, max_turns)
        if self._session_id:
            cmd = [
                self.command, "exec", "resume",
                "--json",
                "--dangerously-bypass-approvals-and-sandbox" if _in_docker() else "--full-auto",
                *self.extra_args,
                self._session_id, prompt,
            ]
        else:
            cmd = [
                self.command, "exec",
                "--json",
                "--dangerously-bypass-approvals-and-sandbox" if _in_docker() else "--full-auto",
                *self.extra_args,
                prompt,
            ]

        self._process = subprocess.Popen(
            cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, text=True,
            cwd=str(workspace), bufsize=1,
        )
        self._pid = self._process.pid
        self._last_event = time.monotonic()

    def _parse(self, raw: str) -> Optional[AgentEvent]:
        """Parse a JSONL event line from codex exec."""
        stripped = raw.strip()
        if not stripped:
            return None

        try:
            ev = json.loads(stripped)
        except json.JSONDecodeError:
            return AgentEvent(type=AgentEventType.TEXT, content=raw, raw=raw)

        if not isinstance(ev, dict):
            return None

        event_type = ev.get("type", "")

        if event_type == "thread.started":
            thread_id = ev.get("thread_id", "")
            if thread_id:
                self._session_id = thread_id
                log.info("Session ID: %s", thread_id)
            return AgentEvent(
                type=AgentEventType.SYSTEM,
                content=f"thread:{thread_id}",
                raw=raw,
            )

        if event_type == "turn.started":
            return None

        if event_type in ("item.started", "item.completed"):
            return self._parse_item(ev, raw)

        if event_type == "turn.completed":
            metadata = {}
            usage = ev.get("usage")
            if isinstance(usage, dict):
                metadata["usage"] = {
                    "input_tokens": usage.get("input_tokens", 0),
                    "output_tokens": usage.get("output_tokens", 0),
                    "cost_usd": usage.get("cost_usd", 0.0),
                    "model": usage.get("model", ev.get("model", "")),
                }
            # Use buffered raw if @@DONE@@ was emitted via MCP/text marker
            event_raw = self._pending_done_raw if self._pending_done_raw else raw
            self._pending_done_raw = None
            if self.signal_method == "file":
                self._write_done_signal_file()
                return None
            if self.signal_method == "mcp":
                return AgentEvent(
                    type=AgentEventType.DONE,
                    raw=event_raw,
                    metadata=metadata,
                )
            if self.signal_method == "auto":
                self._write_done_signal_file()
            return AgentEvent(
                type=AgentEventType.TEXT,
                content="@@DONE@@",
                raw=event_raw,
                metadata=metadata,
            )

        if event_type == "turn.failed":
            error = ev.get("error", {})
            msg = error.get("message", "") if isinstance(error, dict) else str(error)
            if self._is_auth_failure(msg):
                return AgentEvent(
                    type=AgentEventType.AUTH_FAILURE,
                    content=msg,
                    raw=raw,
                )
            return AgentEvent(
                type=AgentEventType.SYSTEM,
                content=f"turn.failed: {msg}",
                raw=raw,
            )

        if event_type == "error":
            msg = ev.get("message", "")
            if self._is_auth_failure(msg):
                return AgentEvent(
                    type=AgentEventType.AUTH_FAILURE,
                    content=msg,
                    raw=raw,
                )
            # Detect provider overload at retry limit (5/5)
            if self._is_overload_exhausted(msg):
                return AgentEvent(
                    type=AgentEventType.PROVIDER_OVERLOAD,
                    content=msg,
                    raw=raw,
                )
            return AgentEvent(
                type=AgentEventType.SYSTEM,
                content=f"error: {msg}",
                raw=raw,
            )

        return AgentEvent(type=AgentEventType.UNKNOWN, raw=raw)

    def _parse_item(self, ev: dict, raw: str) -> Optional[AgentEvent]:
        """Parse an item.started or item.completed event."""
        item = ev.get("item", {})
        if not isinstance(item, dict):
            return None

        item_type = item.get("type", "")
        status = item.get("status", "")

        if item_type == "agent_message":
            text = item.get("text", "")
            if not text:
                return None
            # Fallback: detect text markers if MCP tools weren't used (REQ-028)
            marker_event = self._parse_text_markers(text, raw)
            if marker_event:
                return marker_event
            # If @@DONE@@ was buffered (pending_done_raw set), don't emit the text
            if self._pending_done_raw:
                return None
            return AgentEvent(
                type=AgentEventType.TEXT,
                content=text,
                raw=raw,
            )

        if item_type == "command_execution":
            command = item.get("command", "")
            if status == "in_progress":
                return AgentEvent(
                    type=AgentEventType.TOOL_CALL,
                    content=command[:TOOL_RESULT_PREVIEW_LEN],
                    raw=raw,
                )
            output = item.get("aggregated_output", "")
            exit_code = item.get("exit_code")
            content = f"exit={exit_code}: {output}" if output else f"exit={exit_code}"
            return AgentEvent(
                type=AgentEventType.TOOL_RESULT,
                content=content[:TOOL_RESULT_PREVIEW_LEN],
                raw=raw,
            )

        if item_type == "mcp_tool_call":
            server = item.get("server", "")
            if server == MCP_SIGNAL_SERVER:
                return self._parse_mcp_signal(item, raw)
            return AgentEvent(
                type=AgentEventType.SYSTEM,
                content=f"item:mcp_tool_call:{server}",
                raw=raw,
            )

        return AgentEvent(type=AgentEventType.SYSTEM, content=f"item:{item_type}", raw=raw)

    def _parse_mcp_signal(self, item: dict, raw: str) -> Optional[AgentEvent]:
        """Parse a nightshift-signals MCP tool call into a marker event."""
        tool = item.get("tool", "")
        args = item.get("arguments", {})
        if not isinstance(args, dict):
            args = {}

        if tool == "nightshift_done":
            if self.signal_method in ("file", "auto"):
                self._write_done_signal_file()
            if self.signal_method == "file":
                return None
            # Buffer @@DONE@@ until turn.completed arrives with usage data
            self._pending_done_raw = raw
            return None

        if tool == "nightshift_checkpoint":
            desc = args.get("description", "")
            return AgentEvent(
                type=AgentEventType.TEXT,
                content=f"@@CHECKPOINT@@ {desc}",
                raw=raw,
            )

        if tool == "nightshift_question":
            question = args.get("question", "")
            self._extra_events.append(
                AgentEvent(type=AgentEventType.TEXT, content="@@WAITING@@", raw=raw)
            )
            return AgentEvent(
                type=AgentEventType.TEXT,
                content=f"@@QUESTION@@ {question}",
                raw=raw,
            )

        return AgentEvent(
            type=AgentEventType.SYSTEM,
            content=f"mcp_signal:{tool}",
            raw=raw,
        )

    def _is_overload_exhausted(self, text: str) -> bool:
        """Detect provider overload at retry limit (5/5 reconnect with high demand)."""
        # Pattern: "Reconnecting... 5/5 (high demand...)"
        if "5/5" not in text:
            return False
        return self._is_transient_error(text)

    def _parse_text_markers(self, text: str, raw: str) -> Optional[AgentEvent]:
        """Fallback: detect signal markers in agent_message text.

        This handles the case where the agent prints markers as text instead of
        using MCP tools. SessionRunner also detects markers, but explicit
        adapter-level detection ensures consistent behavior per REQ-028.
        """
        # @@DONE@@ - task completion (buffer until turn.completed for usage data)
        if "@@DONE@@" in text:
            self._pending_done_raw = raw
            # If the same message also includes a reviewer verdict, preserve the
            # text so SessionRunner can log it before turn.completed arrives.
            if re.search(r"@nightshift\s+(approve|revise)\b", text):
                content = text.replace("@@DONE@@", "").rstrip()
                return AgentEvent(
                    type=AgentEventType.TEXT,
                    content=content,
                    raw=raw,
                )
            # Otherwise buffer the whole message and wait for turn.completed so
            # usage data stays attached to the emitted @@DONE@@ event.
            return None

        # @@CHECKPOINT@@ <description>
        match = re.search(r"@@CHECKPOINT@@\s*(.*?)(?:@@|$)", text)
        if match:
            desc = match.group(1).strip()
            return AgentEvent(
                type=AgentEventType.TEXT,
                content=f"@@CHECKPOINT@@ {desc}",
                raw=raw,
            )

        # @@QUESTION@@ <question> - also queue @@WAITING@@
        match = re.search(r"@@QUESTION@@\s*(.*?)(?:@@|$)", text)
        if match:
            question = match.group(1).strip()
            self._extra_events.append(
                AgentEvent(type=AgentEventType.TEXT, content="@@WAITING@@", raw=raw)
            )
            return AgentEvent(
                type=AgentEventType.TEXT,
                content=f"@@QUESTION@@ {question}",
                raw=raw,
            )

        return None

    def _on_process_exit(self) -> None:
        """Emit buffered @@DONE@@ if stream ends before turn.completed."""
        if self._pending_done_raw:
            if self.signal_method in ("file", "auto"):
                self._write_done_signal_file()
            if self.signal_method == "mcp":
                self._extra_events.append(
                    AgentEvent(
                        type=AgentEventType.DONE,
                        raw=self._pending_done_raw,
                    )
                )
            elif self.signal_method != "file":
                self._extra_events.append(
                    AgentEvent(
                        type=AgentEventType.TEXT,
                        content="@@DONE@@",
                        raw=self._pending_done_raw,
                    )
                )
            self._pending_done_raw = None
