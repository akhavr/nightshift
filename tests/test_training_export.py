"""Tests for core/training_export.py and the export-training-data CLI command."""

import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.training_export import (
    extract_training_data,
    export_jsonl,
    TrainingExample,
    _extract_prompt,
    _extract_agent_output,
    _extract_review_verdict,
    _extract_review_feedback,
    _read_agent_kind,
    _read_jsonl,
)
from host.cli import cmd_export_training_data


# ── helpers ──────────────────────────────────────────────────────────────────


def _write_jsonl(path: Path, entries: list[dict]):
    """Write a list of dicts as JSONL."""
    with open(path, "w") as f:
        for entry in entries:
            f.write(json.dumps(entry) + "\n")


def _make_coder_session(sessions_dir: Path, sid: str, issue_id: str = "issue-abc",
                        issue_title: str = "Fix the bug",
                        conversation: list[dict] | None = None,
                        raw_log: str = '{"type":"assistant","subtype":"text"}'):
    """Create a minimal coder session directory."""
    d = sessions_dir / sid
    d.mkdir(parents=True, exist_ok=True)

    state = {
        "issue_id": issue_id,
        "branch": f"agent/{sid}",
        "status": "waiting:review",
        "step": 3,
        "started_at": "2025-01-15T10:00:00Z",
        "issue_title": issue_title,
        "checkpoints": [],
        "human_answers": [],
    }
    (d / "state.json").write_text(json.dumps(state))

    if conversation is None:
        conversation = [
            {"role": "user", "content": "Fix the bug in parser.py", "timestamp": "2025-01-15T10:00:01Z"},
            {"role": "system", "content": "You are a coding agent.", "timestamp": "2025-01-15T10:00:02Z"},
            {"role": "assistant", "content": "I'll fix the bug.", "timestamp": "2025-01-15T10:00:03Z"},
            {"role": "tool_call", "content": "edit parser.py line 10", "timestamp": "2025-01-15T10:00:04Z"},
            {"role": "assistant", "content": "@@DONE@@", "timestamp": "2025-01-15T10:00:05Z"},
        ]
    _write_jsonl(d / "conversation.jsonl", conversation)

    if raw_log:
        (d / "raw-output.log").write_text(raw_log)

    return d


def _make_review_session(sessions_dir: Path, sid: str,
                         verdict: str = "approve",
                         conversation: list[dict] | None = None,
                         raw_log: str = '{"type":"assistant","subtype":"text"}'):
    """Create a minimal review session directory."""
    review_sid = f"review-{sid}"
    d = sessions_dir / review_sid
    d.mkdir(parents=True, exist_ok=True)

    state = {
        "issue_id": f"issue-{sid}",
        "branch": f"review/{sid}",
        "status": "waiting:review",
        "step": 1,
        "started_at": "2025-01-15T11:00:00Z",
        "checkpoints": [],
        "human_answers": [],
    }
    (d / "state.json").write_text(json.dumps(state))

    if conversation is None:
        if verdict == "approve":
            conversation = [
                {"role": "user", "content": "Review the diff.", "timestamp": "2025-01-15T11:00:01Z"},
                {"role": "assistant", "content": "LGTM. @nightshift approve", "timestamp": "2025-01-15T11:00:02Z"},
            ]
        else:
            conversation = [
                {"role": "user", "content": "Review the diff.", "timestamp": "2025-01-15T11:00:01Z"},
                {"role": "assistant", "content": "Missing test coverage.\n@nightshift revise", "timestamp": "2025-01-15T11:00:02Z"},
            ]
    _write_jsonl(d / "conversation.jsonl", conversation)

    if raw_log:
        (d / "raw-output.log").write_text(raw_log)

    return d


def _make_args(**kwargs):
    """Create a simple args object."""
    args = MagicMock()
    for k, v in kwargs.items():
        setattr(args, k, v)
    return args


# ── _extract_prompt ─────────────────────────────────────────────────────────


class TestExtractPrompt:
    def test_extracts_user_and_system_before_agent(self):
        entries = [
            {"role": "user", "content": "Fix bug"},
            {"role": "system", "content": "You are an agent"},
            {"role": "assistant", "content": "OK"},
        ]
        result = _extract_prompt(entries)
        assert "Fix bug" in result
        assert "You are an agent" in result
        assert "OK" not in result

    def test_empty_entries(self):
        assert _extract_prompt([]) == ""

    def test_no_agent_output(self):
        entries = [{"role": "user", "content": "Hello"}]
        assert _extract_prompt(entries) == "Hello"


# ── _extract_agent_output ──────────────────────────────────────────────────


class TestExtractAgentOutput:
    def test_extracts_assistant_and_tool_calls(self):
        entries = [
            {"role": "user", "content": "Fix bug"},
            {"role": "assistant", "content": "I'll fix it"},
            {"role": "tool_call", "content": "edit file.py"},
            {"role": "tool_result", "content": "ok"},
            {"role": "assistant", "content": "Done"},
        ]
        result = _extract_agent_output(entries)
        assert "I'll fix it" in result
        assert "edit file.py" in result
        assert "Done" in result
        assert "Fix bug" not in result
        assert "ok" not in result  # tool_result is not agent output

    def test_empty_entries(self):
        assert _extract_agent_output([]) == ""


# ── _extract_review_verdict ────────────────────────────────────────────────


class TestExtractReviewVerdict:
    def test_approve(self):
        entries = [
            {"role": "assistant", "content": "LGTM\n@nightshift approve"},
        ]
        assert _extract_review_verdict(entries) == "approve"

    def test_revise(self):
        entries = [
            {"role": "assistant", "content": "Issues found\n@nightshift revise"},
        ]
        assert _extract_review_verdict(entries) == "revise"

    def test_no_verdict(self):
        entries = [
            {"role": "assistant", "content": "Looking at the code..."},
        ]
        assert _extract_review_verdict(entries) is None

    def test_last_verdict_wins(self):
        entries = [
            {"role": "assistant", "content": "@nightshift revise"},
            {"role": "assistant", "content": "Actually @nightshift approve"},
        ]
        assert _extract_review_verdict(entries) == "approve"

    def test_empty(self):
        assert _extract_review_verdict([]) is None


# ── _read_agent_kind ───────────────────────────────────────────────────────


class TestReadAgentKind:
    def test_claude_code(self, tmp_path):
        d = tmp_path / "session"
        d.mkdir()
        (d / "raw-output.log").write_text('{"type":"assistant","subtype":"text","text":"hello"}')
        assert _read_agent_kind(d) == "claude-code"

    def test_openhands(self, tmp_path):
        d = tmp_path / "session"
        d.mkdir()
        (d / "raw-output.log").write_text('--JSON Event--\n{"action":"message"}')
        assert _read_agent_kind(d) == "openhands"

    def test_unknown(self, tmp_path):
        d = tmp_path / "session"
        d.mkdir()
        assert _read_agent_kind(d) == "unknown"

    def test_unknown_format(self, tmp_path):
        d = tmp_path / "session"
        d.mkdir()
        (d / "raw-output.log").write_text("some random log output")
        assert _read_agent_kind(d) == "unknown"


# ── _read_jsonl ────────────────────────────────────────────────────────────


class TestReadJsonl:
    def test_reads_valid_entries(self, tmp_path):
        path = tmp_path / "data.jsonl"
        _write_jsonl(path, [{"a": 1}, {"b": 2}])
        result = _read_jsonl(path)
        assert len(result) == 2
        assert result[0] == {"a": 1}

    def test_skips_malformed_lines(self, tmp_path):
        path = tmp_path / "data.jsonl"
        path.write_text('{"a": 1}\nnot json\n{"b": 2}\n')
        result = _read_jsonl(path)
        assert len(result) == 2

    def test_missing_file(self, tmp_path):
        result = _read_jsonl(tmp_path / "nope.jsonl")
        assert result == []

    def test_empty_file(self, tmp_path):
        path = tmp_path / "data.jsonl"
        path.write_text("")
        result = _read_jsonl(path)
        assert result == []


# ── extract_training_data (integration) ────────────────────────────────────


class TestExtractTrainingData:
    def test_basic_pair(self, tmp_path):
        sd = tmp_path / "sessions"
        sd.mkdir()
        _make_coder_session(sd, "abc123456789")
        _make_review_session(sd, "abc123456789", verdict="approve")

        examples = extract_training_data(sd)
        assert len(examples) == 1
        ex = examples[0]
        assert ex.session_id == "abc123456789"
        assert ex.review_verdict == "approve"
        assert "Fix the bug" in ex.issue_title
        assert "Fix the bug in parser.py" in ex.prompt
        assert "I'll fix the bug" in ex.agent_output
        assert ex.coder_agent_kind == "claude-code"

    def test_revise_verdict(self, tmp_path):
        sd = tmp_path / "sessions"
        sd.mkdir()
        _make_coder_session(sd, "def123456789")
        _make_review_session(sd, "def123456789", verdict="revise")

        examples = extract_training_data(sd)
        assert len(examples) == 1
        assert examples[0].review_verdict == "revise"
        assert "Missing test coverage" in examples[0].review_feedback

    def test_verdict_filter_approve(self, tmp_path):
        sd = tmp_path / "sessions"
        sd.mkdir()
        _make_coder_session(sd, "aaa123456789")
        _make_review_session(sd, "aaa123456789", verdict="approve")
        _make_coder_session(sd, "bbb123456789")
        _make_review_session(sd, "bbb123456789", verdict="revise")

        examples = extract_training_data(sd, verdict_filter="approve")
        assert len(examples) == 1
        assert examples[0].session_id == "aaa123456789"

    def test_verdict_filter_revise(self, tmp_path):
        sd = tmp_path / "sessions"
        sd.mkdir()
        _make_coder_session(sd, "aaa123456789")
        _make_review_session(sd, "aaa123456789", verdict="approve")
        _make_coder_session(sd, "bbb123456789")
        _make_review_session(sd, "bbb123456789", verdict="revise")

        examples = extract_training_data(sd, verdict_filter="revise")
        assert len(examples) == 1
        assert examples[0].session_id == "bbb123456789"

    def test_no_review_session_skipped(self, tmp_path):
        sd = tmp_path / "sessions"
        sd.mkdir()
        _make_coder_session(sd, "abc123456789")
        # No review session
        assert extract_training_data(sd) == []

    def test_no_verdict_skipped(self, tmp_path):
        sd = tmp_path / "sessions"
        sd.mkdir()
        _make_coder_session(sd, "abc123456789")
        _make_review_session(sd, "abc123456789", conversation=[
            {"role": "assistant", "content": "Looking at code..."},
        ])
        assert extract_training_data(sd) == []

    def test_empty_coder_conversation_skipped(self, tmp_path):
        sd = tmp_path / "sessions"
        sd.mkdir()
        _make_coder_session(sd, "abc123456789", conversation=[])
        _make_review_session(sd, "abc123456789", verdict="approve")
        assert extract_training_data(sd) == []

    def test_no_sessions_dir(self, tmp_path):
        assert extract_training_data(tmp_path / "nope") == []

    def test_issue_title_from_issue_json(self, tmp_path):
        sd = tmp_path / "sessions"
        sd.mkdir()
        coder_dir = _make_coder_session(sd, "abc123456789", issue_title="")
        # Clear issue_title from state.json
        state = json.loads((coder_dir / "state.json").read_text())
        state["issue_title"] = ""
        (coder_dir / "state.json").write_text(json.dumps(state))
        # Write issue.json
        (coder_dir / "issue.json").write_text(json.dumps({"title": "From issue.json"}))
        _make_review_session(sd, "abc123456789", verdict="approve")

        examples = extract_training_data(sd)
        assert len(examples) == 1
        assert examples[0].issue_title == "From issue.json"

    def test_multiple_sessions(self, tmp_path):
        sd = tmp_path / "sessions"
        sd.mkdir()
        for i, sid in enumerate(["aaa123456789", "bbb123456789", "ccc123456789"]):
            _make_coder_session(sd, sid)
            verdict = "approve" if i % 2 == 0 else "revise"
            _make_review_session(sd, sid, verdict=verdict)

        examples = extract_training_data(sd)
        assert len(examples) == 3

    def test_openhands_agent_detected(self, tmp_path):
        sd = tmp_path / "sessions"
        sd.mkdir()
        _make_coder_session(sd, "abc123456789",
                            raw_log="--JSON Event--\n{\"action\":\"message\"}")
        _make_review_session(sd, "abc123456789", verdict="approve")

        examples = extract_training_data(sd)
        assert examples[0].coder_agent_kind == "openhands"


# ── export_jsonl ───────────────────────────────────────────────────────────


class TestExportJsonl:
    def test_writes_jsonl(self, tmp_path):
        examples = [
            TrainingExample(
                session_id="abc",
                issue_id="issue-1",
                issue_title="Fix bug",
                prompt="Fix it",
                agent_output="Fixed",
                review_verdict="approve",
                review_feedback="LGTM",
                coder_agent_kind="claude-code",
                reviewer_agent_kind="claude-code",
                timestamp="2025-01-15T10:00:00Z",
            ),
        ]
        output = tmp_path / "out.jsonl"
        count = export_jsonl(examples, output)
        assert count == 1

        lines = output.read_text().strip().splitlines()
        assert len(lines) == 1
        data = json.loads(lines[0])
        assert data["session_id"] == "abc"
        assert data["review_verdict"] == "approve"

    def test_empty_list(self, tmp_path):
        output = tmp_path / "out.jsonl"
        count = export_jsonl([], output)
        assert count == 0
        assert output.read_text() == ""


# ── cmd_export_training_data ───────────────────────────────────────────────


class TestCmdExportTrainingData:
    @patch("host.cli.sessions_dir")
    def test_no_sessions(self, mock_sd, tmp_path, capsys):
        mock_sd.return_value = tmp_path / "nope"
        args = _make_args(output="out.jsonl", verdict=None)
        with pytest.raises(SystemExit):
            cmd_export_training_data(args)

    @patch("host.cli.sessions_dir")
    def test_no_examples(self, mock_sd, tmp_path, capsys):
        sd = tmp_path / "sessions"
        sd.mkdir()
        mock_sd.return_value = sd
        args = _make_args(output=str(tmp_path / "out.jsonl"), verdict=None)
        cmd_export_training_data(args)
        captured = capsys.readouterr()
        assert "No training examples found" in captured.out

    @patch("host.cli.sessions_dir")
    def test_successful_export(self, mock_sd, tmp_path, capsys):
        sd = tmp_path / "sessions"
        sd.mkdir()
        _make_coder_session(sd, "abc123456789")
        _make_review_session(sd, "abc123456789", verdict="approve")
        mock_sd.return_value = sd

        output = tmp_path / "out.jsonl"
        args = _make_args(output=str(output), verdict=None)
        cmd_export_training_data(args)

        captured = capsys.readouterr()
        assert "Exported 1" in captured.out
        assert "Approved: 1" in captured.out
        assert output.exists()

    @patch("host.cli.sessions_dir")
    def test_verdict_filter(self, mock_sd, tmp_path, capsys):
        sd = tmp_path / "sessions"
        sd.mkdir()
        _make_coder_session(sd, "aaa123456789")
        _make_review_session(sd, "aaa123456789", verdict="approve")
        _make_coder_session(sd, "bbb123456789")
        _make_review_session(sd, "bbb123456789", verdict="revise")
        mock_sd.return_value = sd

        output = tmp_path / "out.jsonl"
        args = _make_args(output=str(output), verdict="revise")
        cmd_export_training_data(args)

        captured = capsys.readouterr()
        assert "Exported 1" in captured.out
        assert "Revisions: 1" in captured.out
