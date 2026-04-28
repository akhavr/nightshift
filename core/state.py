"""Atomic session state with file locking."""

import fcntl
import json
import logging
from contextlib import contextmanager
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Generator

from core.protocols import UsageData
from core.state_machine import SessionStateMachine, STATES

logger = logging.getLogger(__name__)

STATE_DEFAULTS = {
    "status": "starting",
    "step": 0,
    "orphan_resumes": 0,
    "overload_resumes": 0,
    "auth_retries": 0,
    "completed_at": "",
    "checkpoints": [],
    "human_answers": [],
    "usage": {
        "input_tokens": 0,
        "output_tokens": 0,
        "cost_usd": 0.0,
        "model": "",
    },
}

STATE_SCHEMA = {
    "status": {"type": str, "allowed": STATES},
    "step": {"type": int, "min": 0},
    "orphan_resumes": {"type": int, "min": 0},
    "overload_resumes": {"type": int, "min": 0},
    "auth_retries": {"type": int, "min": 0},
    "completed_at": {"type": str},
    "checkpoints": {"type": list},
    "human_answers": {"type": list},
    "usage": {"type": dict},
}


def _is_non_negative_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _validate_usage(usage: dict) -> dict:
    """Validate usage fields and normalize numeric values."""
    validated = dict(usage)

    for field_name in ("input_tokens", "output_tokens"):
        if field_name in validated and not _is_non_negative_int(validated[field_name]):
            raise ValueError(
                f"Invalid usage.{field_name}: {validated[field_name]!r}"
            )
        if field_name in validated:
            validated[field_name] = int(validated[field_name])

    if "cost_usd" in validated:
        cost_usd = validated["cost_usd"]
        if isinstance(cost_usd, bool) or not isinstance(cost_usd, (int, float)):
            raise ValueError(f"Invalid usage.cost_usd: {cost_usd!r}")
        if cost_usd < 0:
            raise ValueError(f"Invalid usage.cost_usd: {cost_usd!r}")
        validated["cost_usd"] = float(cost_usd)

    if "model" in validated and not isinstance(validated["model"], str):
        validated["model"] = str(validated["model"])

    return validated


def _validate_checkpoint(entry: object) -> tuple[dict | None, str | None]:
    """Validate a checkpoint entry and return a normalized copy or a warning."""
    if not isinstance(entry, dict):
        return None, f"invalid checkpoint entry type: {type(entry).__name__}"

    required_fields = ("step", "description", "timestamp", "commit")
    for field_name in required_fields:
        if field_name not in entry:
            return None, f"checkpoint missing {field_name}"

    step = entry["step"]
    if not _is_non_negative_int(step):
        return None, f"checkpoint has invalid step: {step!r}"

    for field_name in ("description", "timestamp", "commit"):
        if not isinstance(entry[field_name], str):
            return None, f"checkpoint has invalid {field_name}: {entry[field_name]!r}"

    return dict(entry), None


def _validate_state(
    data: dict,
    *,
    max_orphan_resumes: int | None = None,
) -> tuple[dict, list[str]]:
    """Validate and partially normalize state.json data.

    Unknown fields are preserved for forward compatibility.
    """
    state = dict(data)
    warnings: list[str] = []

    if "status" in state:
        status = state["status"]
        if not isinstance(status, str) or status not in STATES:
            warnings.append(f"invalid status {status!r}; defaulting to {STATE_DEFAULTS['status']!r}")
            state["status"] = STATE_DEFAULTS["status"]

    for field_name in ("step", "overload_resumes", "auth_retries"):
        if field_name in state:
            value = state[field_name]
            if not _is_non_negative_int(value):
                warnings.append(
                    f"invalid {field_name} {value!r}; defaulting to {STATE_DEFAULTS[field_name]!r}"
                )
                state[field_name] = STATE_DEFAULTS[field_name]

    if "orphan_resumes" in state:
        orphan_resumes = state["orphan_resumes"]
        if not _is_non_negative_int(orphan_resumes):
            warnings.append(
                f"invalid orphan_resumes {orphan_resumes!r}; defaulting to {STATE_DEFAULTS['orphan_resumes']!r}"
            )
            state["orphan_resumes"] = STATE_DEFAULTS["orphan_resumes"]
        elif max_orphan_resumes is not None and orphan_resumes > max_orphan_resumes:
            warnings.append(
                f"orphan_resumes {orphan_resumes} exceeds max {max_orphan_resumes}; clamping"
            )
            state["orphan_resumes"] = max_orphan_resumes

    if "completed_at" in state and not isinstance(state["completed_at"], str):
        warnings.append(
            f"invalid completed_at {state['completed_at']!r}; defaulting to {STATE_DEFAULTS['completed_at']!r}"
        )
        state["completed_at"] = STATE_DEFAULTS["completed_at"]

    if "checkpoints" in state:
        checkpoints = state["checkpoints"]
        if not isinstance(checkpoints, list):
            warnings.append(
                f"invalid checkpoints {type(checkpoints).__name__}; defaulting to []"
            )
            state["checkpoints"] = []
        else:
            validated_checkpoints: list[dict] = []
            for idx, checkpoint in enumerate(checkpoints):
                validated_checkpoint, warning = _validate_checkpoint(checkpoint)
                if warning is not None:
                    warnings.append(f"checkpoints[{idx}]: {warning}")
                    continue
                validated_checkpoints.append(validated_checkpoint)
            state["checkpoints"] = validated_checkpoints

    if "human_answers" in state and not isinstance(state["human_answers"], list):
        warnings.append(
            f"invalid human_answers {type(state['human_answers']).__name__}; defaulting to []"
        )
        state["human_answers"] = []

    if "usage" in state:
        usage = state["usage"]
        if not isinstance(usage, dict):
            raise ValueError(f"usage must be an object, got {type(usage).__name__}")
        state["usage"] = _validate_usage(usage)

    return state, warnings


@contextmanager
def state_lock(session_dir: Path) -> Generator[None, None, None]:
    """Acquire exclusive lock on state.json for the duration of the context.

    Uses a separate .lock file to avoid issues with atomic rename.
    The lock is advisory (processes must cooperate by using this function).
    """
    lock_file = session_dir / "state.json.lock"
    # Ensure lock file exists
    lock_file.touch(exist_ok=True)
    fd = lock_file.open("r")
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        fd.close()

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
    overload_resumes: int = 0
    auth_retries: int = 0
    completed_at: str = ""
    checkpoints: list[Checkpoint] = field(default_factory=list)
    human_answers: list[QAExchange] = field(default_factory=list)
    usage: UsageData = field(default_factory=UsageData)

    def __post_init__(self):
        if not self.started_at:
            self.started_at = datetime.now(timezone.utc).isoformat()
        self.checkpoints = [Checkpoint(**c) if isinstance(c, dict) else c for c in self.checkpoints]
        self.human_answers = [QAExchange(**q) if isinstance(q, dict) else q for q in self.human_answers]
        if isinstance(self.usage, dict):
            self.usage = UsageData(**self.usage)


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
        self.conversation_log.touch(exist_ok=True)
        # Create the file if needed, but do not refresh mtime on existing logs.
        if not self.raw_output_log.exists():
            self.raw_output_log.touch()
        self._ssm: SessionStateMachine | None = None

    def load_state(self) -> SessionState:
        with open(self.state_file) as f:
            st = SessionState(**json.load(f))
        if self._ssm is None:
            self._ssm = SessionStateMachine(initial_state=st.status)
            self._register_logging_hooks()
        return st

    def _register_logging_hooks(self) -> None:
        """Register hooks to log all state transitions."""
        for state in STATES:
            self._ssm.register_hook(state, "enter", self._log_transition)

    def _log_transition(self, ctx: dict) -> None:
        """Log a state transition."""
        logger.info(
            "session state: %s -> %s", ctx["from_state"], ctx["to_state"]
        )

    @property
    def status(self) -> str:
        if self._ssm is None:
            return self.load_state().status
        return self._ssm.state

    def update_status(self, s: str):
        with state_lock(self.session_dir):
            if self._ssm is None:
                self.load_state()  # initializes _ssm
            self._ssm.transition(s)  # validates transition
            st = self.load_state()
            st.status = self._ssm.state
            self._write(st)

    def mark_completed(self):
        """Set completed_at timestamp to indicate a normal session completion."""
        with state_lock(self.session_dir):
            st = self.load_state()
            st.completed_at = datetime.now(timezone.utc).isoformat()
            self._write(st)

    def mark_done(self, status: str):
        """Atomically set status and completed_at in a single load-modify-write cycle.

        This prevents race conditions where a concurrent reader (e.g., the watcher)
        could read state between separate update_status() and mark_completed() calls
        and overwrite one of the changes.

        Status transition is validated through the SSM before writing.
        """
        with state_lock(self.session_dir):
            if self._ssm is None:
                self.load_state()  # initializes _ssm
            self._ssm.transition(status)  # validates transition
            st = self.load_state()
            st.status = self._ssm.state
            st.completed_at = datetime.now(timezone.utc).isoformat()
            self._write(st)

    def increment_step(self) -> int:
        with state_lock(self.session_dir):
            st = self.load_state()
            st.step += 1
            self._write(st)
            return st.step

    def increment_overload_resumes(self) -> int:
        with state_lock(self.session_dir):
            st = self.load_state()
            st.overload_resumes += 1
            self._write(st)
            return st.overload_resumes

    def reset_overload_resumes(self):
        with state_lock(self.session_dir):
            st = self.load_state()
            st.overload_resumes = 0
            self._write(st)

    def add_checkpoint(self, desc: str, step: int, commit: str = "none"):
        with state_lock(self.session_dir):
            st = self.load_state()
            st.checkpoints.append(Checkpoint(
                step=step, description=desc,
                timestamp=datetime.now(timezone.utc).isoformat(), commit=commit))
            self._write(st)

    def update_usage(self, input_tokens: int, output_tokens: int,
                     cost_usd: float, model: str = ""):
        """Accumulate token usage from an agent result event."""
        with state_lock(self.session_dir):
            st = self.load_state()
            st.usage.input_tokens += input_tokens
            st.usage.output_tokens += output_tokens
            st.usage.cost_usd += cost_usd
            if model:
                st.usage.model = model
            self._write(st)

    def add_qa(self, q: str, a: str):
        with state_lock(self.session_dir):
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
