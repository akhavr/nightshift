"""OpenAI Codex CLI adapter.

Headless mode: fire-and-forget with full-auto approval.
Inside Docker containers (detected via /.dockerenv), uses
--dangerously-bypass-approvals-and-sandbox since the container IS the sandbox.
On the host, uses --full-auto for standard sandboxed execution.
"""

import json
import logging
import subprocess
import time
from pathlib import Path
from typing import Optional

from adapters.agents.base import HeadlessAgentBase
from core.protocols import AgentEvent, AgentEventType

log = logging.getLogger(__name__)

DOCKERENV_PATH = Path("/.dockerenv")


def _in_docker() -> bool:
    """Detect if running inside a Docker container."""
    return DOCKERENV_PATH.exists()


class CodexAgent(HeadlessAgentBase):
    AUTH_FAILURE_PATTERNS = (
        "invalid api key",
        "incorrect api key",
        "authentication_error",
        "unauthorized",
        "error code: 401",
        "error code: 429",
    )

    def __init__(
        self,
        command: str = "codex",
        stall_timeout_s: float = 300.0,
        extra_args: list[str] | None = None,
    ):
        super().__init__(command, stall_timeout_s, extra_args)

    def start(self, prompt: str, workspace: Path, max_turns: int = 50) -> None:
        if _in_docker():
            approval_flag = "--dangerously-bypass-approvals-and-sandbox"
        else:
            approval_flag = "--full-auto"

        cmd = [
            self.command, approval_flag,
            *self.extra_args, prompt,
        ]

        self._process = subprocess.Popen(
            cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, text=True,
            cwd=str(workspace), bufsize=1,
        )
        self._pid = self._process.pid
        self._last_event = time.monotonic()

    def _parse(self, raw: str) -> Optional[AgentEvent]:
        """Parse a single output line into an AgentEvent."""
        stripped = raw.strip()
        if not stripped:
            return None

        try:
            ev = json.loads(stripped)
        except json.JSONDecodeError:
            return AgentEvent(type=AgentEventType.TEXT, content=raw, raw=raw)

        kind = ev.get("type", "")

        if kind == "error":
            content = str(ev.get("message", raw))
            if self._is_auth_failure(content):
                return AgentEvent(
                    type=AgentEventType.AUTH_FAILURE,
                    content=content, raw=raw,
                )
            return AgentEvent(
                type=AgentEventType.SYSTEM, content=content, raw=raw,
            )

        if kind == "message":
            content = str(ev.get("content", ""))
            return AgentEvent(
                type=AgentEventType.TEXT, content=content, raw=raw,
            )

        return AgentEvent(type=AgentEventType.TEXT, content=raw, raw=raw)
