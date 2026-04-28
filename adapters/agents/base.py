"""Shared base class for headless (fire-and-forget) agent adapters.

Owns process lifecycle: launch, stream with select, stall detection,
terminate. Subclasses implement start() and _parse().
"""

import logging
import select
import subprocess
import time
from pathlib import Path
from typing import Iterator, Optional

from core.protocols import AgentEvent, AgentEventType

log = logging.getLogger(__name__)

READ_TIMEOUT_S = 10.0
STALL_TIMEOUT_S = 300.0
PROCESS_TERMINATE_TIMEOUT_S = 10
TOOL_RESULT_PREVIEW_LEN = 500

# Transient error retry configuration
TRANSIENT_RETRY_DELAYS = [30, 60, 120]
TRANSIENT_ERROR_PATTERNS = (
    "500", "502", "503", "504", "429",
    "rate limit", "usage limit", "overloaded", "service unavailable", "high demand",
)

# Provider overload retry configuration (longer delays since provider is under load)
OVERLOAD_RETRY_DELAYS = [60, 120, 300]


class HeadlessAgentBase:
    """Base for agents that run as a subprocess in fire-and-forget mode.

    Provides: stream_events (select loop + stall detection), terminate,
    is_alive, pid, send_input (raises). Subclasses must implement
    start() and _parse().
    """

    # Subclasses define their own patterns for auth/rate-limit detection.
    AUTH_FAILURE_PATTERNS: tuple[str, ...] = ()

    @classmethod
    def _is_auth_failure(cls, text: str) -> bool:
        """Check if text indicates an authentication/authorization failure."""
        lower = text.lower()
        return any(pattern in lower for pattern in cls.AUTH_FAILURE_PATTERNS)

    @classmethod
    def _is_transient_error(cls, text: str) -> bool:
        """Check if text indicates a transient/retryable error (500s, rate limits)."""
        lower = text.lower()
        return any(pattern in lower for pattern in TRANSIENT_ERROR_PATTERNS)

    def __init__(
        self,
        command: str,
        stall_timeout_s: float = STALL_TIMEOUT_S,
        extra_args: list[str] | None = None,
        signal_method: str = "auto",
    ):
        self.command = command
        self.stall_timeout_s = stall_timeout_s
        self.extra_args = extra_args or []
        self.signal_method = signal_method
        self._pid: int | None = None
        self._process: subprocess.Popen | None = None
        self._last_event: float = 0
        self._session_id: str | None = None
        # Transient error retry state
        self._transient_retry_count: int = 0
        # Provider overload retry state (separate from transient)
        self._overload_retry_count: int = 0
        # Stored for restart capability
        self._last_prompt: str | None = None
        self._last_workspace: Path | None = None
        self._last_max_turns: int = 50

    def stream_events(self) -> Iterator[AgentEvent]:
        while True:  # Outer loop for transient error retries
            if not self._process:
                return
            self._before_stream()
            stdout = self._process.stdout
            restart_needed = False

            while True:  # Inner loop for streaming
                yield from self._drain_extra()

                if self._process.poll() is not None:
                    for line in stdout:
                        ev = self._parse(line.rstrip("\n"))
                        if ev:
                            handled = self._maybe_retry_transient(ev)
                            if handled:
                                restart_needed = True
                                break
                            yield ev
                        yield from self._drain_extra()
                    if restart_needed:
                        break
                    self._on_process_exit()
                    yield from self._drain_extra()
                    yield AgentEvent(type=AgentEventType.PROCESS_EXIT)
                    return

                ready, _, _ = select.select([stdout], [], [], READ_TIMEOUT_S)
                if ready:
                    line = stdout.readline()
                    if not line:
                        self._on_process_exit()
                        yield from self._drain_extra()
                        yield AgentEvent(type=AgentEventType.PROCESS_EXIT)
                        return
                    self._last_event = time.monotonic()
                    ev = self._parse(line.rstrip("\n"))
                    if ev:
                        handled = self._maybe_retry_transient(ev)
                        if handled:
                            restart_needed = True
                            break
                        yield ev
                else:
                    elapsed = time.monotonic() - self._last_event
                    if self.stall_timeout_s > 0 and elapsed > self.stall_timeout_s:
                        yield AgentEvent(
                            type=AgentEventType.STALL,
                            content=f"No output for {elapsed:.0f}s",
                        )
                        return

            if not restart_needed:
                return
            # Outer loop continues after restart

    def _maybe_retry_transient(self, ev: AgentEvent) -> bool:
        """Handle transient/overload error retry. Returns True if retry was triggered."""
        # Handle PROVIDER_OVERLOAD events with longer delays
        if ev.type == AgentEventType.PROVIDER_OVERLOAD:
            return self._retry_overload(ev)

        # Handle transient AUTH_FAILURE events (500s, rate limits)
        if ev.type != AgentEventType.AUTH_FAILURE:
            return False
        if not self._is_transient_error(ev.content):
            return False

        self._transient_retry_count += 1
        if self._transient_retry_count <= len(TRANSIENT_RETRY_DELAYS):
            delay = TRANSIENT_RETRY_DELAYS[self._transient_retry_count - 1]
            log.warning(
                f"Transient error (attempt {self._transient_retry_count}/{len(TRANSIENT_RETRY_DELAYS)}): "
                f"{ev.content[:100]}... retrying in {delay}s"
            )
            time.sleep(delay)
            self._restart()
            return True

        # Max retries exceeded, reset counter and let the event through
        log.warning(f"Transient error retry limit exceeded, yielding AUTH_FAILURE: {ev.content[:100]}...")
        self._transient_retry_count = 0
        return False

    def _retry_overload(self, ev: AgentEvent) -> bool:
        """Handle PROVIDER_OVERLOAD retry with longer delays. Returns True if retry was triggered."""
        self._overload_retry_count += 1
        if self._overload_retry_count <= len(OVERLOAD_RETRY_DELAYS):
            delay = OVERLOAD_RETRY_DELAYS[self._overload_retry_count - 1]
            log.warning(
                f"Provider overload (attempt {self._overload_retry_count}/{len(OVERLOAD_RETRY_DELAYS)}): "
                f"{ev.content[:100]}... retrying in {delay}s"
            )
            time.sleep(delay)
            self._restart()
            return True

        # Max retries exceeded, reset counter and let the event through
        log.warning(f"Provider overload retry limit exceeded, yielding PROVIDER_OVERLOAD: {ev.content[:100]}...")
        self._overload_retry_count = 0
        return False

    def send_input(self, text: str) -> None:
        raise RuntimeError(
            "send_input() not supported in headless mode. "
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
                log.warning(f"Process did not terminate cleanly: {e}")
                self._process.kill()
                self._process.wait()
        self._process = None
        self._pid = None
        # _session_id preserved for resume

    @property
    def pid(self) -> int | None:
        return self._pid

    def _parse(self, raw: str) -> Optional[AgentEvent]:
        """Parse a single output line into an AgentEvent. Subclasses must implement."""
        raise NotImplementedError

    def _before_stream(self) -> None:
        """Hook called at the start of stream_events(). Override for setup."""

    def _drain_extra(self) -> Iterator[AgentEvent]:
        """Yield extra events accumulated during parsing. Override in subclasses."""
        return iter(())

    def _on_process_exit(self) -> None:
        """Hook called before PROCESS_EXIT event. Override for cleanup."""

    def _store_start_params(self, prompt: str, workspace: Path, max_turns: int) -> None:
        """Store start parameters for potential restart. Call at the start of start()."""
        self._last_prompt = prompt
        self._last_workspace = workspace
        self._last_max_turns = max_turns

    def _write_done_signal_file(self) -> None:
        """Write the file-based completion signal expected by SessionRunner."""
        done_path = Path("/session/signal/done")
        try:
            done_path.parent.mkdir(parents=True, exist_ok=True)
            done_path.touch(exist_ok=True)
        except OSError as e:
            log.warning(f"Failed to write done signal file {done_path}: {e}")

    def _build_done_signal_events(
        self,
        raw: str,
        metadata: dict | None = None,
    ) -> list[AgentEvent]:
        """Build completion events for the configured signal method."""
        if self.signal_method in ("file", "auto"):
            self._write_done_signal_file()

        if metadata is None:
            metadata = {}

        events: list[AgentEvent] = []
        if self.signal_method in ("text", "auto"):
            events.append(
                AgentEvent(
                    type=AgentEventType.TEXT,
                    content="@@DONE@@",
                    metadata=metadata,
                    raw=raw,
                )
            )
        if self.signal_method in ("mcp", "auto"):
            events.append(
                AgentEvent(
                    type=AgentEventType.DONE,
                    metadata=metadata,
                    raw=raw,
                )
            )
        return events

    def _restart(self) -> None:
        """Terminate and restart the agent with previously stored parameters."""
        if self._last_prompt is None or self._last_workspace is None:
            raise RuntimeError("Cannot restart: no stored start parameters")
        self.terminate()
        self.start(self._last_prompt, self._last_workspace, self._last_max_turns)

    def start(self, prompt: str, workspace: Path, max_turns: int = 50) -> None:
        """Start the agent. Subclasses must implement and call _store_start_params()."""
        raise NotImplementedError
