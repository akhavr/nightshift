"""Claude Code adapter — PTY-based, pure Python."""

import json
import logging
import os
import pty
import select
import subprocess
import time
from pathlib import Path
from typing import Iterator, Optional

from core.protocols import CodingAgent, AgentEvent, AgentEventType

log = logging.getLogger(__name__)

READ_TIMEOUT_S = 10.0
STALL_TIMEOUT_S = 300.0


class ClaudeCodeAgent:
    def __init__(
        self,
        command: str = "claude",
        stall_timeout_s: float = STALL_TIMEOUT_S,
        extra_args: list[str] | None = None,
    ):
        self.command = command
        self.stall_timeout_s = stall_timeout_s
        self.extra_args = extra_args or []
        self._pid: int | None = None
        self._master_fd: int | None = None
        self._process: subprocess.Popen | None = None
        self._last_event: float = 0

    def start(self, prompt: str, workspace: Path, max_turns: int = 50) -> None:
        master_fd, slave_fd = pty.openpty()
        self._master_fd = master_fd
        self._process = subprocess.Popen(
            [self.command, "--dangerously-skip-permissions",
             "--output-format", "stream-json",
             "--max-turns", str(max_turns),
             *self.extra_args, "-p", prompt],
            stdin=slave_fd, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, text=True,
            cwd=str(workspace), bufsize=1,
        )
        os.close(slave_fd)
        self._pid = self._process.pid
        self._last_event = time.monotonic()

    def stream_events(self) -> Iterator[AgentEvent]:
        if not self._process:
            return
        stdout = self._process.stdout
        while True:
            if self._process.poll() is not None:
                for line in stdout:
                    ev = self._parse(line.rstrip("\n"))
                    if ev: yield ev
                yield AgentEvent(type=AgentEventType.PROCESS_EXIT)
                return

            ready, _, _ = select.select([stdout], [], [], READ_TIMEOUT_S)
            if ready:
                line = stdout.readline()
                if not line:
                    yield AgentEvent(type=AgentEventType.PROCESS_EXIT); return
                self._last_event = time.monotonic()
                ev = self._parse(line.rstrip("\n"))
                if ev: yield ev
            else:
                elapsed = time.monotonic() - self._last_event
                if self.stall_timeout_s > 0 and elapsed > self.stall_timeout_s:
                    yield AgentEvent(type=AgentEventType.STALL,
                                     content=f"No output for {elapsed:.0f}s")
                    return

    def send_input(self, text: str) -> None:
        if self._master_fd is None:
            raise RuntimeError("No live process")
        os.write(self._master_fd, (text + "\n").encode())

    def is_alive(self) -> bool:
        return self._process is not None and self._process.poll() is None

    def terminate(self) -> None:
        if self._process and self._process.poll() is None:
            self._process.terminate()
            try: self._process.wait(timeout=10)
            except Exception: self._process.kill(); self._process.wait()
        if self._master_fd is not None:
            try: os.close(self._master_fd)
            except OSError: pass
            self._master_fd = None
        self._process = None; self._pid = None

    @property
    def pid(self) -> int | None:
        return self._pid

    def _parse(self, raw: str) -> Optional[AgentEvent]:
        if not raw.strip(): return None
        try: ev = json.loads(raw)
        except json.JSONDecodeError: return None
        t = ev.get("type", "")
        # OQ-2: These field names are assumed. Verify against real output.
        if t == "assistant":
            return AgentEvent(type=AgentEventType.TEXT, content=ev.get("content",""), raw=raw)
        elif t == "tool_use":
            return AgentEvent(type=AgentEventType.TOOL_CALL,
                              content=f"{ev.get('tool','?')}: {str(ev.get('input',''))[:300]}", raw=raw)
        elif t == "tool_result":
            return AgentEvent(type=AgentEventType.TOOL_RESULT,
                              content=str(ev.get("content",""))[:200], raw=raw)
        elif t == "system":
            return AgentEvent(type=AgentEventType.SYSTEM, content=ev.get("message",""), raw=raw)
        return AgentEvent(type=AgentEventType.UNKNOWN, raw=raw)
