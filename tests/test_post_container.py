"""Tests for host/launch.py _post_container — posting proof-of-work after container exits."""

import json
from pathlib import Path
from unittest.mock import MagicMock, call, patch

import pytest

# Import after sys.path manipulation
import sys
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from host.launch import _post_container


@pytest.fixture
def session_dir(tmp_path):
    sd = tmp_path / "session"
    sd.mkdir()
    return sd


@pytest.fixture
def mock_config():
    config = MagicMock()
    config.workspace.base_branch = "main"
    config.agent.kind = "claude-code"
    return config


def write_state(session_dir, status="waiting:review", checkpoints=None, human_answers=None):
    state = {
        "issue_id": "test-001",
        "branch": "agent/test-001",
        "status": status,
        "step": 3,
        "started_at": "2026-01-01T00:00:00+00:00",
        "checkpoints": checkpoints or [],
        "human_answers": human_answers or [],
    }
    (session_dir / "state.json").write_text(json.dumps(state))


def _mock_git_run(cmd, status_stdout="", diff_stdout="", fsck_stdout="", commit_returncode=0):
    """Return a subprocess result tailored to the git command under test."""
    result = MagicMock(returncode=0, stdout="", stderr="")
    if cmd[:3] == ["git", "status", "--porcelain"]:
        result.stdout = status_stdout
    elif len(cmd) >= 4 and cmd[:2] == ["git", "--git-dir"] and cmd[3] == "fsck":
        result.stdout = fsck_stdout
    elif cmd[:2] == ["git", "diff"]:
        result.stdout = diff_stdout
    elif cmd[:2] == ["git", "add"]:
        result.stdout = ""
    elif cmd[:2] == ["git", "commit"]:
        result.returncode = commit_returncode
    return result


def test_posts_comment_on_waiting_review(session_dir, mock_config, tmp_path):
    """_post_container should post proof-of-work when status is waiting:review."""
    write_state(session_dir, status="waiting:review", checkpoints=[
        {"step": 1, "description": "Implemented feature X", "timestamp": "t1", "commit": "abc"},
        {"step": 2, "description": "Added tests", "timestamp": "t2", "commit": "def"},
    ])

    mock_tracker = MagicMock()

    with patch("host.launch.get_tracker_with_fallback", return_value=mock_tracker), \
         patch("subprocess.run") as mock_run:
        mock_run.side_effect = lambda cmd, **kwargs: _mock_git_run(
            cmd,
            status_stdout="",
            diff_stdout=" file.py | 10 ++++\n 1 file changed",
        )

        _post_container(session_dir, mock_config, tmp_path, "test-001")

    # Verify comment was posted
    mock_tracker.add_comment.assert_called_once()
    comment_body = mock_tracker.add_comment.call_args[0][1]
    assert "Work complete" in comment_body
    assert "Implemented feature X" in comment_body
    assert "Added tests" in comment_body
    assert "nightshift accept/reject/revise" in comment_body

    # Verify label and sync
    mock_tracker.add_label.assert_called_with("test-001", "needs-review")
    mock_tracker.sync.assert_called_once()


def test_skips_when_not_waiting_review(session_dir, mock_config, tmp_path):
    """_post_container should do nothing if status is not waiting:review."""
    write_state(session_dir, status="working")

    with patch("host.launch.get_tracker_with_fallback") as mock_create:
        _post_container(session_dir, mock_config, tmp_path, "test-001")

    mock_create.assert_not_called()


def test_skips_when_no_state_file(session_dir, mock_config, tmp_path):
    """_post_container should do nothing if state.json doesn't exist."""
    with patch("host.launch.get_tracker_with_fallback") as mock_create:
        _post_container(session_dir, mock_config, tmp_path, "test-001")

    mock_create.assert_not_called()


def test_includes_diff_stat(session_dir, mock_config, tmp_path):
    """Proof-of-work should include git diff --stat output."""
    write_state(session_dir)

    mock_tracker = MagicMock()
    diff_output = " foo.py | 5 +++++\n bar.py | 3 +++\n 2 files changed, 8 insertions(+)"

    with patch("host.launch.get_tracker_with_fallback", return_value=mock_tracker), \
         patch("subprocess.run") as mock_run:
        mock_run.side_effect = lambda cmd, **kwargs: _mock_git_run(
            cmd,
            status_stdout="",
            diff_stdout=diff_output,
        )

        _post_container(session_dir, mock_config, tmp_path, "test-001")

    comment_body = mock_tracker.add_comment.call_args[0][1]
    assert "foo.py" in comment_body
    assert "bar.py" in comment_body


def test_handles_no_checkpoints(session_dir, mock_config, tmp_path):
    """Should post even with zero checkpoints."""
    write_state(session_dir, checkpoints=[])

    mock_tracker = MagicMock()

    with patch("host.launch.get_tracker_with_fallback", return_value=mock_tracker), \
         patch("subprocess.run") as mock_run:
        mock_run.side_effect = lambda cmd, **kwargs: _mock_git_run(
            cmd,
            status_stdout="",
        )

        _post_container(session_dir, mock_config, tmp_path, "test-001")

    comment_body = mock_tracker.add_comment.call_args[0][1]
    assert "No checkpoints recorded" in comment_body


def test_handles_tracker_error(session_dir, mock_config, tmp_path, capsys):
    """Should not crash if tracker fails."""
    write_state(session_dir)

    with patch("host.launch.get_tracker_with_fallback", side_effect=Exception("tracker broke")):
        _post_container(session_dir, mock_config, tmp_path, "test-001")

    captured = capsys.readouterr()
    assert "Failed to post review summary" in captured.err


def _write_state_with_usage(session_dir, status="waiting:review", step="coder",
                            usage=None):
    """Helper to write state.json with usage data."""
    state = {
        "issue_id": "test-001",
        "branch": "agent/test-001",
        "status": status,
        "step": 3,
        "started_at": "2026-01-01T00:00:00+00:00",
        "checkpoints": [],
        "human_answers": [],
        "usage": usage or {
            "input_tokens": 1000,
            "output_tokens": 500,
            "cost_usd": 0.05,
            "model": "claude-sonnet-4-20250514",
        },
    }
    (session_dir / "state.json").write_text(json.dumps(state))


def test_review_session_logs_usage(session_dir, mock_config, tmp_path):
    """Usage entry with step=review is appended for review sessions."""
    _write_state_with_usage(session_dir, status="waiting:review")

    with patch("host.launch.get_tracker_with_fallback", return_value=MagicMock()), \
         patch("subprocess.run") as mock_run:
        mock_run.side_effect = lambda cmd, **kwargs: _mock_git_run(
            cmd,
            status_stdout="",
        )
        _post_container(session_dir, mock_config, tmp_path, "test-001", step="review")

    usage_file = tmp_path / ".nightshift" / "usage.jsonl"
    assert usage_file.exists()
    entry = json.loads(usage_file.read_text().strip())
    assert entry["step"] == "review"
    assert entry["input_tokens"] == 1000
    assert entry["cost_usd"] == 0.05


def test_failed_session_logs_usage(session_dir, mock_config, tmp_path):
    """Usage is logged for suspended:too-complex and suspended:auth-failure."""
    for status in ("suspended:too-complex", "suspended:auth-failure"):
        # Clean up usage file between iterations
        usage_file = tmp_path / ".nightshift" / "usage.jsonl"
        if usage_file.exists():
            usage_file.unlink()

        _write_state_with_usage(session_dir, status=status)

        with patch("host.launch.subprocess.run") as mock_run:
            mock_run.side_effect = lambda cmd, **kwargs: _mock_git_run(
                cmd,
                status_stdout="",
            )
            _post_container(session_dir, mock_config, tmp_path, "test-001")

        assert usage_file.exists(), f"Usage not logged for {status}"
        entry = json.loads(usage_file.read_text().strip())
        assert entry["input_tokens"] == 1000


def test_no_double_logging(session_dir, mock_config, tmp_path):
    """Duplicate session_id is not appended twice."""
    _write_state_with_usage(session_dir, status="waiting:review")

    with patch("host.launch.get_tracker_with_fallback", return_value=MagicMock()), \
         patch("subprocess.run") as mock_run:
        mock_run.side_effect = lambda cmd, **kwargs: _mock_git_run(
            cmd,
            status_stdout="",
        )
        _post_container(session_dir, mock_config, tmp_path, "test-001")
        _post_container(session_dir, mock_config, tmp_path, "test-001")

    usage_file = tmp_path / ".nightshift" / "usage.jsonl"
    lines = [l for l in usage_file.read_text().strip().split("\n") if l]
    assert len(lines) == 1, f"Expected 1 entry, got {len(lines)}"


def test_uncommitted_changes_auto_committed(session_dir, mock_config, tmp_path):
    """Dirty worktree changes should be staged and committed before review."""
    write_state(session_dir, status="waiting:review")

    mock_tracker = MagicMock()
    dirty_status = " M file.py\n?? new_file.py\n"
    diff_output = " file.py | 2 ++\n new_file.py | 1 +\n 2 files changed, 3 insertions(+)"

    with patch("host.launch.get_tracker_with_fallback", return_value=mock_tracker), \
         patch("subprocess.run") as mock_run:
        mock_run.side_effect = lambda cmd, **kwargs: _mock_git_run(
            cmd,
            status_stdout=dirty_status,
            diff_stdout=diff_output,
        )

        _post_container(session_dir, mock_config, tmp_path, "test-001")

    commands = [call.args[0] for call in mock_run.call_args_list]
    assert ["git", "status", "--porcelain"] in commands
    assert ["git", "add", "-A"] in commands
    assert any(cmd[:2] == ["git", "commit"] for cmd in commands)


def test_clean_worktree_no_auto_commit(session_dir, mock_config, tmp_path):
    """A clean worktree should not trigger the auto-commit guardrail."""
    write_state(session_dir, status="waiting:review")

    mock_tracker = MagicMock()
    diff_output = " file.py | 1 +\n 1 file changed"

    with patch("host.launch.get_tracker_with_fallback", return_value=mock_tracker), \
         patch("subprocess.run") as mock_run:
        mock_run.side_effect = lambda cmd, **kwargs: _mock_git_run(
            cmd,
            status_stdout="",
            diff_stdout=diff_output,
        )

        _post_container(session_dir, mock_config, tmp_path, "test-001")

    commands = [call.args[0] for call in mock_run.call_args_list]
    assert ["git", "status", "--porcelain"] in commands
    assert not any(cmd[:2] == ["git", "add"] for cmd in commands)
    assert not any(cmd[:2] == ["git", "commit"] for cmd in commands)
