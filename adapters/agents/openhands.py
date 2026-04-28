"""OpenHands agent adapter.

Headless JSON mode: fire-and-forget with --json output.
Events separated by "--JSON Event--" lines. Multi-turn Q&A
uses --resume <conversation_id> to restart with context preserved.
"""

import json
import logging
import re
import subprocess
import time
from pathlib import Path
from typing import Optional

from adapters.agents.base import HeadlessAgentBase, TOOL_RESULT_PREVIEW_LEN
from core.protocols import AgentEvent, AgentEventType

log = logging.getLogger(__name__)

EVENT_SEPARATOR = "--JSON Event--"
CONVERSATION_ID_RE = re.compile(r"Conversation ID:\s*(\S+)")
INPUT_TOKEN_KEYS = ("input_tokens", "prompt_tokens", "input")
OUTPUT_TOKEN_KEYS = ("output_tokens", "completion_tokens", "output")
COST_KEYS = ("cost_usd", "total_cost_usd", "cost", "response_cost")


class OpenHandsAgent(HeadlessAgentBase):
    # Patterns indicating LLM API authentication/authorization failures.
    # Checked against ObservationEvent content when is_error=true.
    # Note: "429", "rate limit", "ratelimiterror" are handled as transient errors
    # with retry in HeadlessAgentBase._maybe_retry_transient()
    AUTH_FAILURE_PATTERNS = (
        "error code: 401",
        "error code: 404",
        "invalid api key",
        "incorrect api key",
        "authentication_error",
        "authenticationerror",
        "authorization_error",
        "unauthorized",
        "model not found",
        "connection error",
        "litellm.",
    )

    def __init__(
        self,
        command: str = "openhands",
        stall_timeout_s: float = 300.0,
        extra_args: list[str] | None = None,
        signal_method: str = "auto",
    ):
        super().__init__(command, stall_timeout_s, extra_args, signal_method=signal_method)

    def _done_signal_event(self, raw: str, metadata: dict | None = None) -> AgentEvent | None:
        """Build the configured completion signal event."""
        if self.signal_method in ("file", "auto"):
            self._write_done_signal_file()
        if self.signal_method == "file":
            return None
        if metadata is None:
            metadata = {}
        if self.signal_method == "mcp":
            return AgentEvent(type=AgentEventType.DONE, metadata=metadata, raw=raw)
        return AgentEvent(
            type=AgentEventType.TEXT,
            content="@@DONE@@",
            metadata=metadata,
            raw=raw,
        )

    def start(self, prompt: str, workspace: Path, max_turns: int = 50) -> None:
        self._store_start_params(prompt, workspace, max_turns)
        cmd = [
            self.command, "--headless", "--json", "--always-approve",
            "--override-with-envs",
            *self.extra_args,
            "-t", prompt,
        ]
        if self._session_id:
            cmd += ["--resume", self._session_id]

        self._process = subprocess.Popen(
            cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True,
            cwd=str(workspace), bufsize=1,
        )
        self._pid = self._process.pid
        self._last_event = time.monotonic()

    def _on_process_exit(self) -> None:
        """Extract conversation ID from stderr for resume support."""
        self._extract_session_id()

    def _extract_session_id(self) -> None:
        """Read stderr and extract conversation ID if present."""
        if not self._process or not self._process.stderr:
            return
        try:
            stderr_text = self._process.stderr.read()
            if stderr_text:
                match = CONVERSATION_ID_RE.search(stderr_text)
                if match:
                    self._session_id = match.group(1)
                    log.info(f"Session ID: {self._session_id}")
        except Exception as e:
            log.warning(f"Failed to read stderr for session ID: {e}")

    def _parse(self, raw: str) -> Optional[AgentEvent]:
        """Parse a JSON event line into an AgentEvent."""
        stripped = raw.strip()
        if not stripped or stripped == EVENT_SEPARATOR:
            return None

        try:
            ev = json.loads(stripped)
        except json.JSONDecodeError as e:
            log.debug("Failed to parse OpenHands JSON event as JSON: %s", e)
            return AgentEvent(type=AgentEventType.TEXT, content=raw, raw=raw)
        if not isinstance(ev, dict):
            return AgentEvent(type=AgentEventType.TEXT, content=raw, raw=raw)
        return self._parse_event(ev, raw)

    def _parse_event(self, ev: dict, raw: str) -> AgentEvent:
        """Dispatch a decoded OpenHands event."""
        kind = ev.get("kind", "")
        metadata = self._metadata(ev)

        if kind == "ActionEvent":
            action = ev.get("action")
            if isinstance(action, dict):
                return self._parse_unified_action(action, raw, metadata)
            return self._parse_action(ev, raw, metadata)

        if kind == "ObservationEvent":
            observation = ev.get("observation")
            if isinstance(observation, dict):
                return self._parse_unified_observation(ev, observation, raw, metadata)
            return self._parse_observation(ev, raw, metadata)

        if kind == "MessageEvent":
            content = ev.get("content", "")
            return AgentEvent(
                type=AgentEventType.TEXT,
                content=str(content),
                metadata=metadata,
                raw=raw,
            )

        if kind == "LLMCompletionLogEvent":
            return AgentEvent(
                type=AgentEventType.SYSTEM,
                content="llm_completion_log",
                metadata=metadata,
                raw=raw,
            )

        # reasoning_content on unknown event kinds
        reasoning = ev.get("reasoning_content", "")
        if reasoning:
            return AgentEvent(
                type=AgentEventType.TEXT,
                content=f"@@LOG@@ {reasoning}",
                metadata=metadata,
                raw=raw,
            )

        # Unknown kind
        return AgentEvent(
            type=AgentEventType.TEXT, content=raw, metadata=metadata, raw=raw)

    def _parse_unified_action(
        self, action: dict, raw: str, metadata: dict,
    ) -> AgentEvent:
        """Parse a unified ActionEvent payload into an AgentEvent."""
        action_kind = str(action.get("kind", ""))

        if action_kind == "FinishAction":
            return self._done_signal_event(raw, metadata)

        content = self._action_preview(action_kind, action)
        return AgentEvent(
            type=AgentEventType.TOOL_CALL,
            content=content,
            metadata=metadata,
            raw=raw,
        )

    def _parse_observation(self, ev: dict, raw: str, metadata: dict) -> AgentEvent:
        """Parse an ObservationEvent into the appropriate AgentEvent."""
        content = str(ev.get("content", ""))[:TOOL_RESULT_PREVIEW_LEN]
        if ev.get("is_error") and self._is_auth_failure(content):
            return AgentEvent(
                type=AgentEventType.AUTH_FAILURE,
                content=content,
                metadata=metadata,
                raw=raw,
            )
        return AgentEvent(
            type=AgentEventType.TOOL_RESULT,
            content=content,
            metadata=metadata,
            raw=raw,
        )

    def _parse_unified_observation(
        self, ev: dict, observation: dict, raw: str, metadata: dict,
    ) -> AgentEvent:
        """Parse a unified ObservationEvent payload into an AgentEvent."""
        content = self._observation_preview(observation)[:TOOL_RESULT_PREVIEW_LEN]
        is_error = bool(ev.get("is_error") or observation.get("is_error"))
        if is_error and self._is_auth_failure(content):
            return AgentEvent(
                type=AgentEventType.AUTH_FAILURE,
                content=content,
                metadata=metadata,
                raw=raw,
            )
        return AgentEvent(
            type=AgentEventType.TOOL_RESULT,
            content=content,
            metadata=metadata,
            raw=raw,
        )

    def _parse_action(self, ev: dict, raw: str, metadata: dict) -> AgentEvent:
        """Parse an ActionEvent into the appropriate AgentEvent."""
        action_type = ev.get("action_type", "")

        if action_type == "FinishAction":
            return self._done_signal_event(raw, metadata)

        if action_type == "FileEditorAction":
            summary = ev.get("summary", "file edit")
            return AgentEvent(
                type=AgentEventType.TEXT,
                content=f"@@CHECKPOINT@@ {summary}",
                metadata=metadata,
                raw=raw,
            )

        if action_type == "TerminalAction":
            command = ev.get("command", "")
            return AgentEvent(
                type=AgentEventType.TOOL_CALL,
                content=f"TerminalAction: {command}",
                metadata=metadata,
                raw=raw,
            )

        # reasoning_content on non-marker action types
        reasoning = ev.get("reasoning_content", "")
        if reasoning:
            return AgentEvent(
                type=AgentEventType.TEXT,
                content=f"@@LOG@@ {reasoning}",
                metadata=metadata,
                raw=raw,
            )

        # Unknown action type
        return AgentEvent(
            type=AgentEventType.TEXT,
            content=f"{action_type}: {json.dumps(ev)[:200]}",
            metadata=metadata,
            raw=raw,
        )

    def _action_preview(self, action_type: str, action: dict) -> str:
        """Build a readable preview for a unified action payload."""
        if action_type == "TerminalAction":
            command = action.get("command", "")
            return f"TerminalAction: {command}"

        if action_type == "FileEditorAction":
            summary = action.get("summary") or action.get("path") or "file edit"
            return f"FileEditorAction: {summary}"

        if action_type:
            details = json.dumps(action)[:200]
            return f"{action_type}: {details}"

        return json.dumps(action)[:200]

    def _observation_preview(self, observation: dict) -> str:
        """Build a readable preview for a unified observation payload."""
        content = observation.get("content", "")
        if isinstance(content, list):
            pieces: list[str] = []
            for part in content:
                if isinstance(part, dict) and part.get("type") == "text":
                    pieces.append(str(part.get("text", "")))
            content = " ".join(piece for piece in pieces if piece)
        return str(content)

    def _metadata(self, ev: dict) -> dict:
        usage = self._extract_usage(ev)
        return {"usage": usage} if usage else {}

    def _extract_usage(self, ev: dict) -> dict | None:
        """Extract per-event usage from OpenHands or LiteLLM payload shapes."""
        if ev.get("kind") == "LLMCompletionLogEvent":
            usage = self._extract_completion_log_usage(ev)
            if usage:
                return usage

        usage = self._extract_usage_payload(ev, ev)
        if usage:
            return usage

        metrics = ev.get("metrics") or ev.get("llm_metrics")
        if isinstance(metrics, dict):
            usage_obj = metrics.get("accumulated_token_usage")
            return self._extract_usage_payload(metrics, usage_obj)
        return None

    def _extract_completion_log_usage(self, ev: dict) -> dict | None:
        log_data = ev.get("log_data", "")
        if not isinstance(log_data, str) or not log_data:
            return None
        try:
            payload = json.loads(log_data)
        except json.JSONDecodeError as e:
            log.warning("Failed to parse OpenHands LLM log usage: %s", e)
            return None
        if not isinstance(payload, dict):
            log.warning("OpenHands LLM log usage was not an object: %r", payload)
            return None
        usage = self._extract_usage_payload(payload, payload.get("usage_summary"))
        if usage:
            usage["model"] = usage["model"] or ev.get("model_name", "")
            return usage
        response = payload.get("response", {})
        if not isinstance(response, dict):
            return None
        return self._extract_usage_payload(payload, response.get("usage", {}))

    def _extract_usage_payload(
        self, container: dict, usage_obj: object,
    ) -> dict | None:
        nested = container.get("usage") or container.get("tokens")
        if isinstance(nested, dict):
            usage_obj = nested
        if not isinstance(usage_obj, dict):
            usage_obj = container.get("usage") or container.get("tokens") or {}
        if not isinstance(usage_obj, dict):
            return None
        input_tokens = self._first_int(usage_obj, INPUT_TOKEN_KEYS)
        output_tokens = self._first_int(usage_obj, OUTPUT_TOKEN_KEYS)
        cost_usd = self._first_float(container, COST_KEYS)
        if cost_usd == 0.0:
            cost_usd = self._first_float(usage_obj, COST_KEYS)
        model = usage_obj.get("model", container.get("model", ""))
        if not model:
            model = container.get("model_name", "")
        if input_tokens or output_tokens or cost_usd:
            return {
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "cost_usd": cost_usd,
                "model": model,
            }
        return None

    @staticmethod
    def _first_int(data: dict, keys: tuple[str, ...]) -> int:
        for key in keys:
            value = data.get(key)
            if value is not None:
                try:
                    return int(value or 0)
                except (TypeError, ValueError) as e:
                    log.warning(
                        "Invalid OpenHands token value for %s=%r: %s",
                        key, value, e,
                    )
        return 0

    @staticmethod
    def _first_float(data: dict, keys: tuple[str, ...]) -> float:
        for key in keys:
            value = data.get(key)
            if value is not None:
                try:
                    return float(value or 0.0)
                except (TypeError, ValueError) as e:
                    log.warning(
                        "Invalid OpenHands cost value for %s=%r: %s",
                        key, value, e,
                    )
        return 0.0
