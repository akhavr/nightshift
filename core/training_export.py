"""Extract training data from session logs for finetuning.

Pairs coder session traces with review verdicts to produce
(prompt, agent_output, review_feedback) tuples suitable for
supervised finetuning of cheaper coding agents.
"""

import json
import logging
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Optional

from host.constants import REVIEW_SESSION_PREFIX

log = logging.getLogger(__name__)

# Conversation roles that represent agent work product
_AGENT_ROLES = frozenset({"assistant", "tool_call"})
# Roles representing prompts/instructions given to the agent
_PROMPT_ROLES = frozenset({"user", "system"})


@dataclass
class TrainingExample:
    """One training example: a coder session paired with its review verdict."""
    session_id: str
    issue_id: str
    issue_title: str
    prompt: str
    agent_output: str
    review_verdict: Optional[str]  # "approve" or "revise" or None
    review_feedback: str
    coder_agent_kind: str
    reviewer_agent_kind: str
    timestamp: str  # coder session started_at


def _read_jsonl(path: Path) -> list[dict]:
    """Read a JSONL file, skipping malformed lines."""
    if not path.exists():
        return []
    entries = []
    for line in path.read_text().strip().splitlines():
        try:
            entries.append(json.loads(line))
        except json.JSONDecodeError as e:
            log.debug("Skipping malformed JSONL line in %s: %s", path, e)
    return entries


def _extract_prompt(entries: list[dict]) -> str:
    """Extract the initial prompt from conversation entries."""
    parts = []
    for entry in entries:
        role = entry.get("role", "")
        if role in _PROMPT_ROLES:
            parts.append(entry.get("content", ""))
        elif role in _AGENT_ROLES:
            break  # stop at first agent response
    return "\n".join(parts).strip()


def _extract_agent_output(entries: list[dict]) -> str:
    """Extract all agent output from conversation entries."""
    parts = []
    for entry in entries:
        role = entry.get("role", "")
        if role in _AGENT_ROLES:
            parts.append(entry.get("content", ""))
    return "\n".join(parts).strip()


def _extract_review_verdict(entries: list[dict]) -> Optional[str]:
    """Extract approve/revise verdict from review conversation."""
    for entry in reversed(entries):
        content = entry.get("content", "").lower()
        if "@nightshift" in content:
            if "approve" in content:
                return "approve"
            if "revise" in content:
                return "revise"
    return None


def _extract_review_feedback(entries: list[dict]) -> str:
    """Extract reviewer feedback text from review conversation."""
    parts = []
    for entry in entries:
        role = entry.get("role", "")
        if role in _AGENT_ROLES:
            parts.append(entry.get("content", ""))
    return "\n".join(parts).strip()


def _read_state(session_dir: Path) -> Optional[dict]:
    """Read state.json from a session directory."""
    state_file = session_dir / "state.json"
    if not state_file.exists():
        return None
    try:
        return json.loads(state_file.read_text())
    except (json.JSONDecodeError, OSError) as e:
        log.warning("Failed to read state.json in %s: %s", session_dir, e)
        return None


def _read_agent_kind(session_dir: Path) -> str:
    """Infer agent kind from session directory.

    Checks for workflow config hints in the session dir.
    Falls back to 'unknown'.
    """
    # The container doesn't persist agent.kind directly in state.json,
    # but the raw-output.log format differs by agent. Check for known patterns.
    raw_log = session_dir / "raw-output.log"
    if raw_log.exists():
        try:
            head = raw_log.read_text()[:2000]
            if '"type":' in head and '"subtype":' in head:
                return "claude-code"
            if "--JSON Event--" in head:
                return "openhands"
        except OSError as e:
            log.debug("Could not read raw-output.log in %s: %s", session_dir, e)
    return "unknown"


def extract_training_data(sessions_dir: Path,
                          verdict_filter: Optional[str] = None,
                          ) -> list[TrainingExample]:
    """Extract training examples from all completed session pairs.

    Args:
        sessions_dir: Path to .nightshift/sessions/
        verdict_filter: If set, only include examples with this verdict
            ("approve" or "revise"). None includes all.

    Returns:
        List of TrainingExample objects, one per coder session that has
        a matching review session with a verdict.
    """
    if not sessions_dir.exists():
        return []

    examples = []
    for session_dir in sorted(sessions_dir.iterdir()):
        if not session_dir.is_dir():
            continue
        sid = session_dir.name

        # Skip review sessions — we process them via the coder session
        if sid.startswith(REVIEW_SESSION_PREFIX):
            continue

        state = _read_state(session_dir)
        if state is None:
            continue

        # Find matching review session
        review_sid = f"{REVIEW_SESSION_PREFIX}{sid}"
        review_dir = sessions_dir / review_sid
        if not review_dir.is_dir():
            log.debug("No review session for %s, skipping", sid)
            continue

        review_state = _read_state(review_dir)
        if review_state is None:
            continue

        # Read conversation logs
        coder_entries = _read_jsonl(session_dir / "conversation.jsonl")
        review_entries = _read_jsonl(review_dir / "conversation.jsonl")

        if not coder_entries:
            log.debug("Empty coder conversation for %s, skipping", sid)
            continue

        verdict = _extract_review_verdict(review_entries)
        if verdict is None:
            log.debug("No verdict in review session for %s, skipping", sid)
            continue

        if verdict_filter and verdict != verdict_filter:
            continue

        # Read issue title from state or issue.json
        issue_title = state.get("issue_title", "")
        if not issue_title:
            issue_file = session_dir / "issue.json"
            if issue_file.exists():
                try:
                    issue_data = json.loads(issue_file.read_text())
                    issue_title = issue_data.get("title", "")
                except (json.JSONDecodeError, OSError) as e:
                    log.debug("Could not read issue.json for %s: %s", sid, e)

        example = TrainingExample(
            session_id=sid,
            issue_id=state.get("issue_id", ""),
            issue_title=issue_title,
            prompt=_extract_prompt(coder_entries),
            agent_output=_extract_agent_output(coder_entries),
            review_verdict=verdict,
            review_feedback=_extract_review_feedback(review_entries),
            coder_agent_kind=_read_agent_kind(session_dir),
            reviewer_agent_kind=_read_agent_kind(review_dir),
            timestamp=state.get("started_at", ""),
        )
        examples.append(example)

    return examples


def export_jsonl(examples: list[TrainingExample], output_path: Path) -> int:
    """Write training examples to a JSONL file.

    Returns the number of examples written.
    """
    with open(output_path, "w") as f:
        for ex in examples:
            f.write(json.dumps(asdict(ex)) + "\n")
    return len(examples)
