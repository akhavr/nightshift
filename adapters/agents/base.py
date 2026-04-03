"""Shared base class for headless (fire-and-forget) agent adapters.

Owns process lifecycle: launch, stream with select, stall detection,
terminate. Subclasses implement start() and _parse().
"""

import logging
import select
import subprocess
import time
from typing import Iterator, Optional

from core.protocols import AgentEvent, AgentEventType

log = logging.getLogger(__name__)

READ_TIMEOUT_S = 10.0
STALL_TIMEOUT_S = 300.0
PROCESS_TERMINATE_TIMEOUT_S = 10
TOOL_RESULT_PREVIEW_LEN = 500


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

    def __init__(
        self,
        command: str,
        stall_timeout_s: float = STALL_TIMEOUT_S,
        extra_args: list[str] | None = None,
    ):
        self.command = command
        self.stall_timeout_s = stall_timeout_s
        self.extra_args = extra_args or []
        self._pid: int | None = None
        self._process: subprocess.Popen | None = None
        self._last_event: float = 0
        self._session_id: str | None = None

    def stream_events(self) -> Iterator[AgentEvent]:
        if not self._process:
            return
        self._before_stream()
        stdout = self._process.stdout
        while True:
            yield from self._drain_extra()

            if self._process.poll() is not None:
                for line in stdout:
                    ev = self._parse(line.rstrip("\n"))
                    if ev:
                        yield ev
                    yield from self._drain_extra()
                self._on_process_exit()
                yield AgentEvent(type=AgentEventType.PROCESS_EXIT)
                return

            ready, _, _ = select.select([stdout], [], [], READ_TIMEOUT_S)
            if ready:
                line = stdout.readline()
                if not line:
                    self._on_process_exit()
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
