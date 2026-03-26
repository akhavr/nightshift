"""Tests for nightshift issue CLI passthrough command."""

import sys
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from host.cli import cmd_issue, _build_parser


# ── helpers ──────────────────────────────────────────────────────────────────


def _make_args(**kwargs):
    """Create a simple namespace-like args object."""
    args = MagicMock()
    for k, v in kwargs.items():
        setattr(args, k, v)
    return args


def _make_workflow_file(repo: Path) -> Path:
    """Create a minimal WORKFLOW.md and return its path."""
    wf = repo / "WORKFLOW.md"
    wf.write_text(
        "---\n"
        "agent:\n  kind: claude-code\n"
        "tracker:\n  kind: git-bug\n"
        "workspace:\n  kind: worktree\n  base_branch: main\n  root: .worktrees\n"
        "---\nPrompt\n"
    )
    return wf


# ── cmd_issue ────────────────────────────────────────────────────────────────


def test_cmd_issue_passes_args_to_tracker(tmp_path, capsys):
    """issue passes all args literally to tracker.run_raw()."""
    repo = tmp_path / "repo"
    repo.mkdir()
    wf = _make_workflow_file(repo)

    mock_tracker = MagicMock()
    mock_tracker.run_raw.return_value = "bug abc123 open: Fix login"

    with patch("host.cli.repo_root", return_value=repo), \
         patch("host.cli.get_tracker_with_fallback", return_value=mock_tracker):
        cmd_issue(_make_args(
            tracker_args=["bug", "show", "abc123"],
            workflow=str(wf),
        ))

    mock_tracker.run_raw.assert_called_once_with("bug", "show", "abc123")
    out = capsys.readouterr().out
    assert "bug abc123 open: Fix login" in out


def test_cmd_issue_push_args(tmp_path, capsys):
    """issue passes push to tracker.run_raw()."""
    repo = tmp_path / "repo"
    repo.mkdir()
    wf = _make_workflow_file(repo)

    mock_tracker = MagicMock()
    mock_tracker.run_raw.return_value = ""

    with patch("host.cli.repo_root", return_value=repo), \
         patch("host.cli.get_tracker_with_fallback", return_value=mock_tracker):
        cmd_issue(_make_args(
            tracker_args=["push"],
            workflow=str(wf),
        ))

    mock_tracker.run_raw.assert_called_once_with("push")


def test_cmd_issue_empty_output(tmp_path, capsys):
    """issue with empty output prints nothing."""
    repo = tmp_path / "repo"
    repo.mkdir()
    wf = _make_workflow_file(repo)

    mock_tracker = MagicMock()
    mock_tracker.run_raw.return_value = ""

    with patch("host.cli.repo_root", return_value=repo), \
         patch("host.cli.get_tracker_with_fallback", return_value=mock_tracker):
        cmd_issue(_make_args(
            tracker_args=["bug", "show", "nonexistent"],
            workflow=str(wf),
        ))

    out = capsys.readouterr().out
    assert out == ""


def test_cmd_issue_not_implemented_exits(tmp_path, capsys):
    """issue exits with error when tracker doesn't support run_raw."""
    repo = tmp_path / "repo"
    repo.mkdir()
    wf = _make_workflow_file(repo)

    mock_tracker = MagicMock()
    mock_tracker.run_raw.side_effect = NotImplementedError("not supported")

    with patch("host.cli.repo_root", return_value=repo), \
         patch("host.cli.get_tracker_with_fallback", return_value=mock_tracker), \
         pytest.raises(SystemExit) as exc_info:
        cmd_issue(_make_args(
            tracker_args=["bug", "show", "abc"],
            workflow=str(wf),
        ))

    assert exc_info.value.code == 1
    err = capsys.readouterr().err
    assert "does not support raw CLI passthrough" in err


def test_cmd_issue_no_args(tmp_path, capsys):
    """issue with no tracker args passes empty args to run_raw."""
    repo = tmp_path / "repo"
    repo.mkdir()
    wf = _make_workflow_file(repo)

    mock_tracker = MagicMock()
    mock_tracker.run_raw.return_value = "usage: git-bug ..."

    with patch("host.cli.repo_root", return_value=repo), \
         patch("host.cli.get_tracker_with_fallback", return_value=mock_tracker):
        cmd_issue(_make_args(
            tracker_args=[],
            workflow=str(wf),
        ))

    mock_tracker.run_raw.assert_called_once_with()
    out = capsys.readouterr().out
    assert "usage: git-bug" in out


# ── Parser integration ───────────────────────────────────────────────────────


def test_parser_issue_captures_remainder():
    """The issue subcommand captures all remaining args."""
    parser = _build_parser()
    args = parser.parse_args(["issue", "bug", "show", "abc123"])
    assert args.cmd == "issue"
    assert args.tracker_args == ["bug", "show", "abc123"]


def test_parser_issue_no_args():
    """The issue subcommand works with no additional args."""
    parser = _build_parser()
    args = parser.parse_args(["issue"])
    assert args.cmd == "issue"
    assert args.tracker_args == []


def test_parser_issue_with_flags():
    """The issue subcommand passes flags literally (no argparse interpretation)."""
    parser = _build_parser()
    args = parser.parse_args(["issue", "bug", "-f", "json"])
    assert args.tracker_args == ["bug", "-f", "json"]


# ── GitBugTracker.run_raw ────────────────────────────────────────────────────


def test_git_bug_run_raw_delegates_to_run():
    """GitBugTracker.run_raw delegates to _run with the same args."""
    from adapters.trackers.git_bug import GitBugTracker

    tracker = GitBugTracker(repo_dir="/tmp")
    with patch.object(tracker, "_run", return_value="output") as mock_run:
        result = tracker.run_raw("bug", "show", "abc123")

    mock_run.assert_called_once_with("bug", "show", "abc123")
    assert result == "output"


def test_git_bug_run_raw_push():
    """GitBugTracker.run_raw passes push through _run."""
    from adapters.trackers.git_bug import GitBugTracker

    tracker = GitBugTracker(repo_dir="/tmp")
    with patch.object(tracker, "_run", return_value="") as mock_run:
        tracker.run_raw("push")

    mock_run.assert_called_once_with("push")


# ── StaticTracker.run_raw ────────────────────────────────────────────────────


def test_static_tracker_run_raw_raises():
    """StaticTracker.run_raw raises NotImplementedError."""
    from adapters.trackers.static import StaticTracker

    tracker = StaticTracker.__new__(StaticTracker)
    with pytest.raises(NotImplementedError):
        tracker.run_raw("anything")
