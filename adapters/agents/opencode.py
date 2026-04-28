"""OpenCode CLI adapter.

Headless JSON mode: fire-and-forget with --format json output.
Events are JSONL with `type` field (text, tool_use, step_finish, error).
Multi-turn uses `opencode run --session ID "prompt"`.
"""

import json
import logging
import subprocess
import time
from pathlib import Path
from typing import Optional

from adapters.agents.base import HeadlessAgentBase, TOOL_RESULT_PREVIEW_LEN
from core.protocols import AgentEvent, AgentEventType

log = logging.getLogger(__name__)

TOOL_INPUT_PREVIEW_LEN = 300


class OpenCodeAgent(HeadlessAgentBase):
    # Patterns indicating authentication/authorization failures.
    # Note: "rate limit", "rate_limit_exceeded" are handled as transient errors
    # with retry in HeadlessAgentBase._maybe_retry_transient()
    AUTH_FAILURE_PATTERNS = (
        "invalid_api_key",
        "authentication_error",
        "authorization_error",
        "invalid x-api-key",
        "api key is invalid",
        "unauthorized",
        "could not authenticate",
        "status 401",
        "status 403",
        "insufficient_quota",
    )

    def __init__(
        self,
        command: str = "opencode",
        stall_timeout_s: float = 300.0,
        extra_args: list[str] | None = None,
        signal_method: str = "auto",
    ):
        super().__init__(command, stall_timeout_s, extra_args, signal_method=signal_method)

    def start(self, prompt: str, workspace: Path, max_turns: int = 50) -> None:
        self._store_start_params(prompt, workspace, max_turns)
        cmd = [
            self.command, "run",
            "--format", "json",
            "--dangerously-skip-permissions",
        ]
        if self._session_id:
            cmd += ["--session", self._session_id]
        cmd += [*self.extra_args, prompt]

        self._process = subprocess.Popen(
            cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, text=True,
            cwd=str(workspace), bufsize=1,
        )
        self._pid = self._process.pid
        self._last_event = time.monotonic()

    def _parse(self, raw: str) -> Optional[AgentEvent]:
        """Parse a JSONL event line from opencode run."""
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

        # Extract session ID from any event that has it
        session_id = ev.get("sessionID") or ev.get("session_id")
        if session_id and not self._session_id:
            self._session_id = session_id
            log.info("Session ID: %s", session_id)

        if event_type == "text":
            text = ev.get("text", "") or ev.get("content", "")
            if not text:
                return None
            return AgentEvent(
                type=AgentEventType.TEXT,
                content=text,
                raw=raw,
            )

        if event_type == "tool_use":
            # OpenCode wraps tool info in part.tool / part.state.input
            part = ev.get("part", {})
            name = part.get("tool") or ev.get("name", ev.get("tool", "?"))
            state = part.get("state", {})
            inp = state.get("input") or ev.get("input", ev.get("arguments", {}))
            inp_str = str(inp)[:TOOL_INPUT_PREVIEW_LEN]
            return AgentEvent(
                type=AgentEventType.TOOL_CALL,
                content=f"{name}: {inp_str}",
                raw=raw,
            )

        if event_type == "tool_result":
            result = ev.get("result", ev.get("output", ""))
            return AgentEvent(
                type=AgentEventType.TOOL_RESULT,
                content=str(result)[:TOOL_RESULT_PREVIEW_LEN],
                raw=raw,
            )

        if event_type == "step_finish":
            reason = ev.get("reason", "")
            metadata = {}
            # Extract usage from step_finish.part.tokens or step_finish.tokens
            part = ev.get("part", {})
            tokens = part.get("tokens") if isinstance(part, dict) else None
            if tokens is None:
                tokens = ev.get("tokens", {})
            if isinstance(tokens, dict):
                metadata["usage"] = {
                    "input_tokens": tokens.get("input_tokens", tokens.get("input", 0)),
                    "output_tokens": tokens.get("output_tokens", tokens.get("output", 0)),
                    "cost_usd": tokens.get("cost_usd", tokens.get("cost", 0.0)),
                    "model": tokens.get("model", ev.get("model", "")),
                }
            # Note: reason='stop' just means current step finished without tool
            # calls, NOT that the agent completed its task. True completion is
            # signaled by process exit or file signals (/session/signal/done).
            return AgentEvent(
                type=AgentEventType.SYSTEM,
                content=f"step_finish:{reason}",
                raw=raw,
                metadata=metadata,
            )

        if event_type == "error":
            msg = ev.get("message", ev.get("error", ""))
            if isinstance(msg, dict):
                msg = msg.get("message", str(msg))
            if self._is_auth_failure(str(msg)):
                return AgentEvent(
                    type=AgentEventType.AUTH_FAILURE,
                    content=str(msg),
                    raw=raw,
                )
            return AgentEvent(
                type=AgentEventType.SYSTEM,
                content=f"error: {msg}",
                raw=raw,
            )

        if event_type == "session":
            # Session started event
            return AgentEvent(
                type=AgentEventType.SYSTEM,
                content=f"session:{ev.get('sessionID', '')}",
                raw=raw,
            )

        return AgentEvent(type=AgentEventType.UNKNOWN, raw=raw)
