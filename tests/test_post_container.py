"""Tests for host/launch.py _post_container — posting proof-of-work after container exits."""

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

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


def test_posts_comment_on_waiting_review(session_dir, mock_config, tmp_path):
    """_post_container should post proof-of-work when status is waiting:review."""
    write_state(session_dir, status="waiting:review", checkpoints=[
        {"step": 1, "description": "Implemented feature X", "timestamp": "t1", "commit": "abc"},
        {"step": 2, "description": "Added tests", "timestamp": "t2", "commit": "def"},
    ])

    mock_tracker = MagicMock()

    with patch("host.launch.create_tracker", return_value=mock_tracker), \
         patch("subprocess.run") as mock_run:
        # Mock git diff --stat
        mock_run.return_value = MagicMock(returncode=0, stdout=" file.py | 10 ++++\n 1 file changed")

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

    with patch("host.launch.create_tracker") as mock_create:
        _post_container(session_dir, mock_config, tmp_path, "test-001")

    mock_create.assert_not_called()


def test_skips_when_no_state_file(session_dir, mock_config, tmp_path):
    """_post_container should do nothing if state.json doesn't exist."""
    with patch("host.launch.create_tracker") as mock_create:
        _post_container(session_dir, mock_config, tmp_path, "test-001")

    mock_create.assert_not_called()


def test_includes_diff_stat(session_dir, mock_config, tmp_path):
    """Proof-of-work should include git diff --stat output."""
    write_state(session_dir)

    mock_tracker = MagicMock()
    diff_output = " foo.py | 5 +++++\n bar.py | 3 +++\n 2 files changed, 8 insertions(+)"

    with patch("host.launch.create_tracker", return_value=mock_tracker), \
         patch("subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=0, stdout=diff_output)

        _post_container(session_dir, mock_config, tmp_path, "test-001")

    comment_body = mock_tracker.add_comment.call_args[0][1]
    assert "foo.py" in comment_body
    assert "bar.py" in comment_body


def test_handles_no_checkpoints(session_dir, mock_config, tmp_path):
    """Should post even with zero checkpoints."""
    write_state(session_dir, checkpoints=[])

    mock_tracker = MagicMock()

    with patch("host.launch.create_tracker", return_value=mock_tracker), \
         patch("subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=0, stdout="")

        _post_container(session_dir, mock_config, tmp_path, "test-001")

    comment_body = mock_tracker.add_comment.call_args[0][1]
    assert "No checkpoints recorded" in comment_body


def test_handles_tracker_error(session_dir, mock_config, tmp_path, capsys):
    """Should not crash if tracker fails."""
    write_state(session_dir)

    with patch("host.launch.create_tracker", side_effect=Exception("tracker broke")):
        _post_container(session_dir, mock_config, tmp_path, "test-001")

    captured = capsys.readouterr()
    assert "Failed to post review summary" in captured.err
