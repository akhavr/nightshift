"""OpenHands agent adapter.

Runs OpenHands via openhands-cli, translates JSON events to nightshift markers.
Uses litellm under the hood for multi-provider LLM support. Configured via
LLM_API_KEY, LLM_MODEL, LLM_BASE_URL environment variables.
"""

import json
import logging
import select
import subprocess
import threading
import time
from pathlib import Path
from typing import Iterator, Optional

from core.protocols import AgentEvent, AgentEventType

log = logging.getLogger(__name__)

READ_TIMEOUT_S = 10.0
STALL_TIMEOUT_S = 300.0
PROCESS_TERMINATE_TIMEOUT_S = 10
DEFAULT_MAX_TURNS = 50
STDERR_JOIN_TIMEOUT_S = 5
LOG_TRUNCATE_CHARS = 200
OBSERVATION_TRUNCATE_CHARS = 500


class OpenHandsAgent:
    def __init__(
        self,
        command: str = "openhands-cli",
        stall_timeout_s: float = STALL_TIMEOUT_S,
        extra_args: list[str] | None = None,
    ):
        self.command = command
        self.stall_timeout_s = stall_timeout_s
        self.extra_args = extra_args or []
        self._pid: int | None = None
        self._process: subprocess.Popen | None = None
        self._last_event: float = 0
        self._stderr_thread: threading.Thread | None = None

    def start(self, prompt: str, workspace: Path, max_turns: int = DEFAULT_MAX_TURNS) -> None:
        cmd = [
            self.command, "run",
            "--prompt", prompt,
            "--workspace", str(workspace),
            "--max-turns", str(max_turns),
            "--output-format", "json",
            *self.extra_args,
        ]
        self._process = subprocess.Popen(
            cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True,
            cwd=str(workspace), bufsize=1,
        )
        self._pid = self._process.pid
        self._last_event = time.monotonic()
        self._stderr_thread = threading.Thread(
            target=self._drain_stderr, daemon=True,
        )
        self._stderr_thread.start()

    def stream_events(self) -> Iterator[AgentEvent]:
        if not self._process:
            return
        stdout = self._process.stdout
        while True:
            if self._process.poll() is not None:
                for line in stdout:
                    ev = self._parse(line.rstrip("\n"))
                    if ev:
                        yield ev
                yield AgentEvent(type=AgentEventType.PROCESS_EXIT)
                return

            ready, _, _ = select.select([stdout], [], [], READ_TIMEOUT_S)
            if ready:
                line = stdout.readline()
                if not line:
                    yield AgentEvent(type=AgentEventType.PROCESS_EXIT)
                    return
                self._last_event = time.monotonic()
                ev = self._parse(line.rstrip("\n"))
                if ev:
                    yield ev
            else:
                elapsed = time.monotonic() - self._last_event
                if self.stall_timeout_s > 0 and elapsed > self.stall_timeout_s:
                    yield AgentEvent(
                        type=AgentEventType.STALL,
                        content=f"No output for {elapsed:.0f}s",
                    )
                    return

    def send_input(self, text: str) -> None:
        raise RuntimeError(
            "send_input() not supported for OpenHands. "
            "Use terminate() + start() with the answer as prompt."
        )

    def is_alive(self) -> bool:
        return self._process is not None and self._process.poll() is None

    def terminate(self) -> None:
        if self._process and self._process.poll() is None:
            self._process.terminate()
            try:
                self._process.wait(timeout=PROCESS_TERMINATE_TIMEOUT_S)
            except Exception as e:
                log.warning(f"Terminate wait failed, killing: {e}")
                self._process.kill()
                self._process.wait()
        if self._stderr_thread:
            self._stderr_thread.join(timeout=STDERR_JOIN_TIMEOUT_S)
            self._stderr_thread = None
        self._process = None
        self._pid = None

    def _drain_stderr(self) -> None:
        """Read stderr in a background thread and log each line."""
        proc = self._process
        if not proc or not proc.stderr:
            return
        for line in proc.stderr:
            stripped = line.rstrip("\n")
            if stripped:
                log.warning("openhands stderr: %s", stripped)

    @property
    def pid(self) -> int | None:
        return self._pid

    def _parse(self, raw: str) -> Optional[AgentEvent]:
        """Parse a JSON event line from openhands-cli into an AgentEvent."""
        if not raw.strip():
            return None
        try:
            ev = json.loads(raw)
        except json.JSONDecodeError:
            log.warning(f"Failed to parse openhands output: {raw[:LOG_TRUNCATE_CHARS]}")
            return None

        event_type = ev.get("type", "")

        if event_type == "action":
            return self._parse_action(ev, raw)

        if event_type == "observation":
            return self._parse_observation(ev, raw)

        if event_type == "status":
            return self._parse_status(ev, raw)

        if event_type == "error":
            error_msg = ev.get("message", str(ev))
            return AgentEvent(
                type=AgentEventType.SYSTEM,
                content=f"error: {error_msg}",
                raw=raw,
            )

        return AgentEvent(type=AgentEventType.UNKNOWN, raw=raw)

    def _parse_action(self, ev: dict, raw: str) -> Optional[AgentEvent]:
        """Parse an OpenHands action event."""
        action = ev.get("action", "")
        message = ev.get("message", "")

        if action == "message":
            return AgentEvent(
                type=AgentEventType.TEXT, content=message, raw=raw,
            )

        if action in ("run", "write", "read", "browse"):
            content = f"{action}: {message}" if message else action
            return AgentEvent(
                type=AgentEventType.TOOL_CALL, content=content, raw=raw,
            )

        if action == "finish":
            return AgentEvent(
                type=AgentEventType.TEXT, content="@@DONE@@", raw=raw,
            )

        return AgentEvent(
            type=AgentEventType.SYSTEM, content=f"action:{action}", raw=raw,
        )

    def _parse_observation(self, ev: dict, raw: str) -> Optional[AgentEvent]:
        """Parse an OpenHands observation event."""
        content = ev.get("content", "")
        return AgentEvent(
            type=AgentEventType.TOOL_RESULT,
            content=content[:OBSERVATION_TRUNCATE_CHARS],
            raw=raw,
        )

    def _parse_status(self, ev: dict, raw: str) -> Optional[AgentEvent]:
        """Parse an OpenHands status event."""
        status = ev.get("status", "")
        message = ev.get("message", "")

        if status == "complete":
            return AgentEvent(
                type=AgentEventType.TEXT, content="@@DONE@@", raw=raw,
            )

        return AgentEvent(
            type=AgentEventType.SYSTEM,
            content=f"status:{status} {message}".strip(),
            raw=raw,
        )
