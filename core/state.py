"""Atomic session state."""

import json
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

RECENT_CONVERSATION_DEFAULT = 50
CONVERSATION_PREVIEW_LEN = 500


@dataclass
class Checkpoint:
    step: int
    description: str
    timestamp: str
    commit: str


@dataclass
class QAExchange:
    question: str
    answer: str


@dataclass
class SessionState:
    issue_id: str
    branch: str
    status: str = "starting"
    step: int = 0
    started_at: str = ""
    orphan_resumes: int = 0
    completed_at: str = ""
    checkpoints: list[Checkpoint] = field(default_factory=list)
    human_answers: list[QAExchange] = field(default_factory=list)

    def __post_init__(self):
        if not self.started_at:
            self.started_at = datetime.now(timezone.utc).isoformat()
        self.checkpoints = [Checkpoint(**c) if isinstance(c, dict) else c for c in self.checkpoints]
        self.human_answers = [QAExchange(**q) if isinstance(q, dict) else q for q in self.human_answers]


class StateManager:
    def __init__(self, session_dir: str | Path):
        self.session_dir = Path(session_dir)
        self.state_file = self.session_dir / "state.json"
        self.conversation_log = self.session_dir / "conversation.jsonl"
        self.raw_output_log = self.session_dir / "raw-output.log"
        self.resume_prompt_file = self.session_dir / "resume-prompt.md"
        self.waiting_signal = self.session_dir / "waiting.json"
        self.answer_file = self.session_dir / "answer.txt"
        self.session_dir.mkdir(parents=True, exist_ok=True)

    def load_state(self) -> SessionState:
        with open(self.state_file) as f: return SessionState(**json.load(f))

    def update_status(self, s: str):
        st = self.load_state(); st.status = s; self._write(st)

    def mark_completed(self):
        """Set completed_at timestamp to indicate a normal session completion."""
        st = self.load_state()
        st.completed_at = datetime.now(timezone.utc).isoformat()
        self._write(st)

    def increment_step(self) -> int:
        st = self.load_state(); st.step += 1; self._write(st); return st.step

    def add_checkpoint(self, desc: str, step: int, commit: str = "none"):
        st = self.load_state()
        st.checkpoints.append(Checkpoint(
            step=step, description=desc,
            timestamp=datetime.now(timezone.utc).isoformat(), commit=commit))
        self._write(st)

    def add_qa(self, q: str, a: str):
        st = self.load_state()
        st.human_answers.append(QAExchange(question=q, answer=a))
        self._write(st)

    def append_conversation(self, role: str, content: str):
        with open(self.conversation_log, "a") as f:
            f.write(json.dumps({"role": role, "content": content,
                                "timestamp": datetime.now(timezone.utc).isoformat()}) + "\n")

    def append_raw(self, line: str):
        with open(self.raw_output_log, "a") as f: f.write(line + "\n")

    def get_recent_conversation(self, n: int = RECENT_CONVERSATION_DEFAULT) -> str:
        if not self.conversation_log.exists(): return ""
        lines = self.conversation_log.read_text().strip().splitlines()
        parts = []
        for l in lines[-n:]:
            try:
                e = json.loads(l); parts.append(f"[{e['role']}]: {e['content'][:CONVERSATION_PREVIEW_LEN]}")
            except Exception: continue
        return "\n".join(parts)

    def write_resume_prompt(self, c: str): self.resume_prompt_file.write_text(c)
    def read_resume_prompt(self) -> Optional[str]:
        return self.resume_prompt_file.read_text() if self.resume_prompt_file.exists() else None

    def signal_waiting(self, question: str):
        self.waiting_signal.write_text(json.dumps({
            "question": question, "issue_id": self.load_state().issue_id,
            "timestamp": datetime.now(timezone.utc).isoformat()}))

    def clear_waiting(self): self.waiting_signal.unlink(missing_ok=True)

    def check_answer(self) -> Optional[str]:
        if self.answer_file.exists():
            a = self.answer_file.read_text().strip()
            self.answer_file.unlink(); self.clear_waiting(); return a
        return None

    def _write(self, st: SessionState):
        tmp = self.state_file.with_suffix(".tmp")
        with open(tmp, "w") as f: json.dump(asdict(st), f, indent=2)
        tmp.rename(self.state_file)
