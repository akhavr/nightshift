"""Claude Code adapter.

OQ-1 RESOLVED: -p mode is fire-and-forget (exits after responding).
stdin follow-up does NOT work. Multi-turn Q&A uses --resume <session_id>
to restart with full conversation context preserved.
"""

import json
import logging
import subprocess
import time
from pathlib import Path
from typing import Iterator, Optional

from adapters.agents.base import HeadlessAgentBase, TOOL_RESULT_PREVIEW_LEN
from core.constants import (
    MCP_CONFIG_CONTAINER_PATH,
    MCP_SIGNAL_SERVER_PREFIX,
    PROMPT_FILE_NAME,
    PROMPT_FILE_THRESHOLD,
)
from core.protocols import AgentEvent, AgentEventType

log = logging.getLogger(__name__)

TOOL_INPUT_PREVIEW_LEN = 300


class ClaudeCodeAgent(HeadlessAgentBase):
    AUTH_FAILURE_PATTERNS = (
        "invalid_api_key",
        "authentication_error",
        "authorization_error",
        "invalid x-api-key",
        "permission_error",
        "api key is invalid",
        "token has expired",
        "expired token",
        "unauthorized",
        "could not authenticate",
    )

    def __init__(
        self,
        command: str = "claude",
        stall_timeout_s: float = 300.0,
        extra_args: list[str] | None = None,
        session_dir: Path | None = None,
        signal_method: str = "auto",
    ):
        super().__init__(command, stall_timeout_s, extra_args, signal_method=signal_method)
        self._extra_events: list[AgentEvent] = []
        self._session_dir = session_dir or Path("/session")
        self._prompt_file: Path | None = None

    def start(self, prompt: str, workspace: Path, max_turns: int = 50) -> None:
        self._store_start_params(prompt, workspace, max_turns)
        cmd = [
            self.command, "--dangerously-skip-permissions",
            "--verbose", "--output-format", "stream-json",
            "--max-turns", str(max_turns),
        ]
        # Resume previous session to preserve conversation context (OQ-1)
        if self._session_id:
            cmd += ["--resume", self._session_id]
        if Path(MCP_CONFIG_CONTAINER_PATH).exists():
            cmd += ["--mcp-config", MCP_CONFIG_CONTAINER_PATH]

        # Large prompts use file-based passing to avoid OS arg limit
        prompt_arg = self._prepare_prompt_arg(prompt)
        cmd += [*self.extra_args, "-p", prompt_arg]

        self._process = subprocess.Popen(
            cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, text=True,
            cwd=str(workspace), bufsize=1,
        )
        self._pid = self._process.pid
        self._last_event = time.monotonic()

    def _prepare_prompt_arg(self, prompt: str) -> str:
        """Prepare prompt argument, using file if prompt exceeds threshold."""
        if len(prompt) > PROMPT_FILE_THRESHOLD and self._session_dir.exists():
            self._prompt_file = self._session_dir / PROMPT_FILE_NAME
            self._prompt_file.write_text(prompt)
            log.info(f"Prompt ({len(prompt)} bytes) written to {self._prompt_file}")
            return f"@{self._prompt_file}"
        return prompt

    def _on_process_exit(self) -> None:
        """Clean up prompt file if it was used."""
        if self._prompt_file:
            if self._prompt_file.exists():
                try:
                    self._prompt_file.unlink()
                    log.debug(f"Cleaned up prompt file: {self._prompt_file}")
                except OSError as e:
                    log.warning(f"Failed to clean up prompt file {self._prompt_file}: {e}")
            self._prompt_file = None

    def _before_stream(self) -> None:
        self._extra_events.clear()

    def _drain_extra(self) -> Iterator[AgentEvent]:
        while self._extra_events:
            yield self._extra_events.pop(0)

    def _parse_user_event(self, ev: dict, raw: str) -> Optional[AgentEvent]:
        """Parse a 'user' event containing tool results."""
        msg = ev.get("message", {})
        content_parts = msg.get("content", []) if isinstance(msg, dict) else []
        for part in content_parts:
            if isinstance(part, dict) and part.get("type") == "tool_result":
                result_content = part.get("content", "")
                if isinstance(result_content, list):
                    result_content = " ".join(
                        p.get("text", "") for p in result_content
                        if isinstance(p, dict) and p.get("type") == "text"
                    )
                return AgentEvent(
                    type=AgentEventType.TOOL_RESULT,
                    content=str(result_content)[:TOOL_RESULT_PREVIEW_LEN], raw=raw)
        return None

    def _parse(self, raw: str) -> Optional[AgentEvent]:
        """Parse a stream-json line into an AgentEvent."""
        if not raw.strip(): return None
        try: ev = json.loads(raw)
        except json.JSONDecodeError: return None
        t = ev.get("type", "")

        if t == "system" and ev.get("subtype") == "init":
            sid = ev.get("session_id")
            if sid:
                self._session_id = sid
                log.info(f"Session ID: {sid}")
            return AgentEvent(type=AgentEventType.SYSTEM,
                              content="init", raw=raw)

        if t == "assistant":
            if ev.get("error") == "authentication_failed":
                msg = ev.get("message", {})
                text = ""
                if isinstance(msg, dict):
                    for part in msg.get("content", []):
                        if isinstance(part, dict) and part.get("type") == "text":
                            text = part.get("text", "")
                            break
                return AgentEvent(type=AgentEventType.AUTH_FAILURE,
                                  content=text or "authentication_failed", raw=raw)
            msg = ev.get("message", {})
            content_parts = msg.get("content", []) if isinstance(msg, dict) else []
            events = list(self._parse_assistant_content(content_parts, raw))
            if events:
                self._extra_events.extend(events[1:])
                return events[0]
            return None

        if t == "user":
            return self._parse_user_event(ev, raw)

        if t == "result":
            result_text = ev.get("result", "")
            if self._is_auth_failure(result_text):
                return AgentEvent(type=AgentEventType.AUTH_FAILURE,
                                  content=result_text, raw=raw)
            if ev.get("subtype") == "success" and not ev.get("is_error"):
                done_events = self._build_done_signal_events(raw)
                if done_events:
                    self._extra_events.extend(done_events)
            metadata = {}
            usage_obj = ev.get("usage", {})
            cost = ev.get("total_cost_usd", ev.get("cost_usd", 0.0))
            in_tok = usage_obj.get("input_tokens", 0) if isinstance(usage_obj, dict) else 0
            out_tok = usage_obj.get("output_tokens", 0) if isinstance(usage_obj, dict) else 0
            # Extract model from modelUsage keys, fall back to top-level model
            model = ev.get("model", "")
            if not model:
                model_usage = ev.get("modelUsage", {})
                if isinstance(model_usage, dict) and model_usage:
                    model = next(iter(model_usage))
            if cost or in_tok or out_tok:
                metadata["usage"] = {
                    "input_tokens": in_tok,
                    "output_tokens": out_tok,
                    "cost_usd": cost,
                    "model": model,
                }
            return AgentEvent(type=AgentEventType.SYSTEM,
                              content=result_text, metadata=metadata, raw=raw)

        if t == "error":
            error_msg = ev.get("error", {})
            error_text = error_msg.get("message", "") if isinstance(error_msg, dict) else str(error_msg)
            if self._is_auth_failure(error_text):
                return AgentEvent(type=AgentEventType.AUTH_FAILURE,
                                  content=error_text, raw=raw)
            return AgentEvent(type=AgentEventType.SYSTEM,
                              content=f"error: {error_text}", raw=raw)

        if t == "rate_limit_event":
            return None

        if t == "system":
            msg_text = ev.get("message", "")
            if self._is_auth_failure(msg_text):
                return AgentEvent(type=AgentEventType.AUTH_FAILURE,
                                  content=msg_text, raw=raw)
            return AgentEvent(type=AgentEventType.SYSTEM,
                              content=msg_text, raw=raw)

        return AgentEvent(type=AgentEventType.UNKNOWN, raw=raw)

    def _parse_assistant_content(
        self, content_parts: list, raw: str,
    ) -> Iterator[AgentEvent]:
        """Parse the content array from an assistant message.

        A single assistant event can contain multiple content items:
        text, tool_use, and thinking blocks. We yield separate AgentEvents
        for each so the session runner can handle them independently.
        """
        for part in content_parts:
            if not isinstance(part, dict):
                continue
            pt = part.get("type", "")
            if pt == "text":
                text = part.get("text", "")
                if text:
                    yield AgentEvent(
                        type=AgentEventType.TEXT, content=text, raw=raw)
            elif pt == "tool_use":
                name = part.get("name", "?")
                if name.startswith(MCP_SIGNAL_SERVER_PREFIX):
                    signal = name[len(MCP_SIGNAL_SERVER_PREFIX):]
                    inp_data = part.get("input", {})
                    if signal == "nightshift_done":
                        done_events = self._build_done_signal_events(raw)
                        if done_events:
                            yield done_events[0]
                            yield from done_events[1:]
                    elif signal == "nightshift_checkpoint":
                        desc = inp_data.get("description", "") if isinstance(inp_data, dict) else ""
                        yield AgentEvent(
                            type=AgentEventType.TEXT,
                            content=f"@@CHECKPOINT@@ {desc}", raw=raw)
                    elif signal == "nightshift_question":
                        question = inp_data.get("question", "") if isinstance(inp_data, dict) else ""
                        yield AgentEvent(
                            type=AgentEventType.TEXT,
                            content=f"@@QUESTION@@ {question}", raw=raw)
                        yield AgentEvent(
                            type=AgentEventType.TEXT,
                            content="@@WAITING@@", raw=raw)
                    else:
                        inp = str(inp_data)[:TOOL_INPUT_PREVIEW_LEN]
                        yield AgentEvent(
                            type=AgentEventType.TOOL_CALL,
                            content=f"{name}: {inp}", raw=raw)
                else:
                    inp = str(part.get("input", ""))[:TOOL_INPUT_PREVIEW_LEN]
                    yield AgentEvent(
                        type=AgentEventType.TOOL_CALL,
                        content=f"{name}: {inp}", raw=raw)
            elif pt == "thinking":
                pass  # internal reasoning, not surfaced
