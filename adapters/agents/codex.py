"""OpenAI Codex app-server adapter (sketch).

Uses JSON-RPC over stdio: initialize → thread/start → turn/start → stream.
See: https://developers.openai.com/codex/app-server/
"""

from pathlib import Path
from typing import Iterator
from core.protocols import CodingAgent, AgentEvent


class CodexAgent:
    def start(self, prompt: str, workspace: Path, max_turns: int = 50) -> None:
        raise NotImplementedError("Codex adapter not yet implemented")

    def stream_events(self) -> Iterator[AgentEvent]:
        raise NotImplementedError

    def send_input(self, text: str) -> None:
        # Send continuation turn/start on the existing thread
        raise NotImplementedError

    def is_alive(self) -> bool:
        raise NotImplementedError

    def terminate(self) -> None:
        raise NotImplementedError

    @property
    def pid(self) -> int | None:
        return None
