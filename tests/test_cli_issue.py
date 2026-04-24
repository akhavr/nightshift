"""Tests for nightshift issue CLI passthrough command."""

import json
import sys
from dataclasses import asdict, replace
from pathlib import Path
from unittest.mock import call
from unittest.mock import patch, MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.protocols import TrackerIssue
from host.constants import SHORT_ID_LEN
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


def _make_issue(issue_id: str = "abc123") -> TrackerIssue:
    return TrackerIssue(
        id=issue_id,
        identifier=issue_id,
        title="Fix lock contention",
        body="Avoid raw CLI calls when GraphQL methods are available.",
        status="open",
        labels=["nightshift"],
    )


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
            tracker_args=["bug", "show", "abc123", "--format", "raw"],
            workflow=str(wf),
        ))

    mock_tracker.run_raw.assert_called_once_with("bug", "show", "abc123", "--format", "raw")
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
            tracker_args=["bug", "show", "nonexistent", "--format", "raw"],
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
            tracker_args=["bug", "show", "abc", "--format", "raw"],
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


def test_cmd_issue_bug_new_uses_create_issue_when_available(tmp_path, capsys):
    """GraphQL-backed trackers should avoid run_raw for bug creation."""
    repo = tmp_path / "repo"
    repo.mkdir()
    wf = _make_workflow_file(repo)

    class CreateIssueTracker:
        def __init__(self):
            self.create_issue = MagicMock(return_value="abc123")
            self.run_raw = MagicMock(return_value="")

    mock_tracker = CreateIssueTracker()

    with patch("host.cli.repo_root", return_value=repo), \
         patch("host.cli.get_tracker_with_fallback", return_value=mock_tracker):
        cmd_issue(_make_args(
            tracker_args=["bug", "new", "-t", "Fix lock", "-m", "body text"],
            workflow=str(wf),
        ))

    mock_tracker.create_issue.assert_called_once_with("Fix lock", "body text")
    mock_tracker.run_raw.assert_not_called()
    out = capsys.readouterr().out
    assert "abc123" in out


def test_bug_ls_uses_list_issues(tmp_path, capsys):
    repo = tmp_path / "repo"
    repo.mkdir()
    wf = _make_workflow_file(repo)

    mock_tracker = MagicMock()
    mock_tracker.list_issues.return_value = [
        _make_issue("abc123"),
        replace(_make_issue("def456"), status="closed"),
    ]

    with patch("host.cli.repo_root", return_value=repo), \
         patch("host.cli.get_tracker_with_fallback", return_value=mock_tracker):
        cmd_issue(_make_args(
            tracker_args=["bug", "ls"],
            workflow=str(wf),
        ))

    mock_tracker.list_issues.assert_called_once_with()
    mock_tracker.run_raw.assert_not_called()
    out = capsys.readouterr().out
    assert "abc123" in out
    assert "def456" in out


def test_bug_ls_tty_output_human_readable(tmp_path, capsys):
    repo = tmp_path / "repo"
    repo.mkdir()
    wf = _make_workflow_file(repo)

    mock_tracker = MagicMock()
    mock_tracker.list_issues.return_value = [
        _make_issue("75038569f8c8"),
        replace(_make_issue("7cc81110abcd"), status="closed", title="Watcher auto-start"),
    ]

    with patch("host.cli.repo_root", return_value=repo), \
         patch("host.cli.get_tracker_with_fallback", return_value=mock_tracker), \
         patch("host.cli._is_tty", return_value=True):
        cmd_issue(_make_args(
            tracker_args=["bug", "ls"],
            workflow=str(wf),
        ))

    assert capsys.readouterr().out == (
        f"{'75038569f8c8'[:SHORT_ID_LEN]} open   Fix lock contention\n"
        f"{'7cc81110abcd'[:SHORT_ID_LEN]} closed Watcher auto-start\n"
    )


def test_bug_ls_pipe_output_json(tmp_path, capsys):
    repo = tmp_path / "repo"
    repo.mkdir()
    wf = _make_workflow_file(repo)

    mock_tracker = MagicMock()
    mock_tracker.list_issues.return_value = [_make_issue("75038569f8c8")]

    with patch("host.cli.repo_root", return_value=repo), \
         patch("host.cli.get_tracker_with_fallback", return_value=mock_tracker), \
         patch("host.cli._is_tty", return_value=False):
        cmd_issue(_make_args(
            tracker_args=["bug", "ls"],
            workflow=str(wf),
        ))

    expected = json.dumps([asdict(_make_issue("75038569f8c8"))], indent=2)
    assert capsys.readouterr().out == f"{expected}\n"


def test_bug_ls_with_status_filter(tmp_path, capsys):
    repo = tmp_path / "repo"
    repo.mkdir()
    wf = _make_workflow_file(repo)

    mock_tracker = MagicMock()
    mock_tracker.list_issues.return_value = [_make_issue("abc123")]

    with patch("host.cli.repo_root", return_value=repo), \
         patch("host.cli.get_tracker_with_fallback", return_value=mock_tracker):
        cmd_issue(_make_args(
            tracker_args=["bug", "ls", "-s", "open"],
            workflow=str(wf),
        ))

    mock_tracker.list_issues.assert_called_once_with(status="open")
    mock_tracker.run_raw.assert_not_called()
    assert "abc123" in capsys.readouterr().out


def test_bug_with_status_filter(tmp_path, capsys):
    repo = tmp_path / "repo"
    repo.mkdir()
    wf = _make_workflow_file(repo)

    mock_tracker = MagicMock()
    mock_tracker.list_issues.return_value = [_make_issue("abc123")]

    with patch("host.cli.repo_root", return_value=repo), \
         patch("host.cli.get_tracker_with_fallback", return_value=mock_tracker):
        cmd_issue(_make_args(
            tracker_args=["bug", "-s", "open"],
            workflow=str(wf),
        ))

    mock_tracker.list_issues.assert_called_once_with(status="open")
    mock_tracker.run_raw.assert_not_called()
    assert "abc123" in capsys.readouterr().out


def test_bug_show_uses_get_issue(tmp_path, capsys):
    repo = tmp_path / "repo"
    repo.mkdir()
    wf = _make_workflow_file(repo)

    mock_tracker = MagicMock()
    mock_tracker.get_issue.return_value = _make_issue("abc123")

    with patch("host.cli.repo_root", return_value=repo), \
         patch("host.cli.get_tracker_with_fallback", return_value=mock_tracker):
        cmd_issue(_make_args(
            tracker_args=["bug", "show", "abc123"],
            workflow=str(wf),
        ))

    mock_tracker.get_issue.assert_called_once_with("abc123")
    mock_tracker.run_raw.assert_not_called()
    assert "Fix lock contention" in capsys.readouterr().out


def test_bug_show_tty_output_human_readable(tmp_path, capsys):
    repo = tmp_path / "repo"
    repo.mkdir()
    wf = _make_workflow_file(repo)

    mock_tracker = MagicMock()
    mock_tracker.get_issue.return_value = _make_issue("75038569f8c8")

    with patch("host.cli.repo_root", return_value=repo), \
         patch("host.cli.get_tracker_with_fallback", return_value=mock_tracker), \
         patch("host.cli._is_tty", return_value=True):
        cmd_issue(_make_args(
            tracker_args=["bug", "show", "75038569f8c8"],
            workflow=str(wf),
        ))

    assert capsys.readouterr().out == (
        f"{'75038569f8c8'[:SHORT_ID_LEN]} open   Fix lock contention\n"
    )


def test_bug_label_uses_methods(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    wf = _make_workflow_file(repo)

    mock_tracker = MagicMock()

    with patch("host.cli.repo_root", return_value=repo), \
         patch("host.cli.get_tracker_with_fallback", return_value=mock_tracker):
        cmd_issue(_make_args(
            tracker_args=["bug", "label", "new", "abc123", "upstream"],
            workflow=str(wf),
        ))
        cmd_issue(_make_args(
            tracker_args=["bug", "label", "rm", "abc123", "upstream"],
            workflow=str(wf),
        ))

    mock_tracker.add_label.assert_called_once_with("abc123", "upstream")
    mock_tracker.remove_label.assert_called_once_with("abc123", "upstream")
    mock_tracker.run_raw.assert_not_called()


def test_bug_status_uses_set_status(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    wf = _make_workflow_file(repo)

    mock_tracker = MagicMock()

    with patch("host.cli.repo_root", return_value=repo), \
         patch("host.cli.get_tracker_with_fallback", return_value=mock_tracker):
        cmd_issue(_make_args(
            tracker_args=["bug", "status", "open", "abc123"],
            workflow=str(wf),
        ))
        cmd_issue(_make_args(
            tracker_args=["bug", "status", "close", "abc123"],
            workflow=str(wf),
        ))

    assert mock_tracker.set_status.call_args_list == [
        call("abc123", "open"),
        call("abc123", "closed"),
    ]
    mock_tracker.run_raw.assert_not_called()


def test_bug_comment_uses_add_comment(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    wf = _make_workflow_file(repo)

    mock_tracker = MagicMock()

    with patch("host.cli.repo_root", return_value=repo), \
         patch("host.cli.get_tracker_with_fallback", return_value=mock_tracker):
        cmd_issue(_make_args(
            tracker_args=["bug", "comment", "new", "abc123", "hello from cli"],
            workflow=str(wf),
        ))

    mock_tracker.add_comment.assert_called_once_with("abc123", "hello from cli")
    mock_tracker.run_raw.assert_not_called()


def test_bug_new_non_interactive_uses_api(tmp_path, capsys):
    """bug new with --non-interactive should use create_issue, not run_raw."""
    repo = tmp_path / "repo"
    repo.mkdir()
    wf = _make_workflow_file(repo)

    class CreateIssueTracker:
        def __init__(self):
            self.create_issue = MagicMock(return_value="abc123")
            self.run_raw = MagicMock(return_value="")

    mock_tracker = CreateIssueTracker()

    with patch("host.cli.repo_root", return_value=repo), \
         patch("host.cli.get_tracker_with_fallback", return_value=mock_tracker):
        cmd_issue(_make_args(
            tracker_args=["bug", "new", "-t", "Fix bug", "-m", "body", "--non-interactive"],
            workflow=str(wf),
        ))

    mock_tracker.create_issue.assert_called_once_with("Fix bug", "body")
    mock_tracker.run_raw.assert_not_called()


def test_bug_comment_with_m_flag_uses_api(tmp_path):
    """bug comment new with -m flag should use add_comment, not run_raw."""
    repo = tmp_path / "repo"
    repo.mkdir()
    wf = _make_workflow_file(repo)

    mock_tracker = MagicMock()

    with patch("host.cli.repo_root", return_value=repo), \
         patch("host.cli.get_tracker_with_fallback", return_value=mock_tracker):
        cmd_issue(_make_args(
            tracker_args=["bug", "comment", "new", "abc123", "-m", "hello from cli"],
            workflow=str(wf),
        ))

    mock_tracker.add_comment.assert_called_once_with("abc123", "hello from cli")
    mock_tracker.run_raw.assert_not_called()


def test_bug_label_filter_uses_api(tmp_path, capsys):
    """bug ls -l <label> should use list_issues and filter client-side, not run_raw."""
    repo = tmp_path / "repo"
    repo.mkdir()
    wf = _make_workflow_file(repo)

    mock_tracker = MagicMock()
    mock_tracker.list_issues.return_value = [
        _make_issue("abc123"),
        replace(_make_issue("def456"), labels=["other"]),
    ]

    with patch("host.cli.repo_root", return_value=repo), \
         patch("host.cli.get_tracker_with_fallback", return_value=mock_tracker):
        cmd_issue(_make_args(
            tracker_args=["bug", "ls", "-l", "nightshift"],
            workflow=str(wf),
        ))

    mock_tracker.list_issues.assert_called_once_with(status=None)
    mock_tracker.run_raw.assert_not_called()
    out = capsys.readouterr().out
    assert "abc123" in out
    assert "def456" not in out


def test_bug_label_filter_short_form(tmp_path, capsys):
    """bug -l <label> (without ls) should use list_issues with client-side filter."""
    repo = tmp_path / "repo"
    repo.mkdir()
    wf = _make_workflow_file(repo)

    mock_tracker = MagicMock()
    mock_tracker.list_issues.return_value = [
        _make_issue("abc123"),
        replace(_make_issue("def456"), labels=["other"]),
    ]

    with patch("host.cli.repo_root", return_value=repo), \
         patch("host.cli.get_tracker_with_fallback", return_value=mock_tracker):
        cmd_issue(_make_args(
            tracker_args=["bug", "-l", "nightshift"],
            workflow=str(wf),
        ))

    mock_tracker.list_issues.assert_called_once_with(status=None)
    mock_tracker.run_raw.assert_not_called()
    out = capsys.readouterr().out
    assert "abc123" in out
    assert "def456" not in out


def test_bug_all_status_flag_uses_api(tmp_path, capsys):
    """bug ls -a should list all issues (no status filter), not run_raw."""
    repo = tmp_path / "repo"
    repo.mkdir()
    wf = _make_workflow_file(repo)

    mock_tracker = MagicMock()
    mock_tracker.list_issues.return_value = [
        _make_issue("abc123"),
        replace(_make_issue("def456"), status="closed"),
    ]

    with patch("host.cli.repo_root", return_value=repo), \
         patch("host.cli.get_tracker_with_fallback", return_value=mock_tracker):
        cmd_issue(_make_args(
            tracker_args=["bug", "ls", "-a"],
            workflow=str(wf),
        ))

    mock_tracker.list_issues.assert_called_once_with(status=None)
    mock_tracker.run_raw.assert_not_called()
    out = capsys.readouterr().out
    assert "abc123" in out
    assert "def456" in out


def test_bug_all_status_short_form(tmp_path, capsys):
    """bug -a (without ls) should list all issues."""
    repo = tmp_path / "repo"
    repo.mkdir()
    wf = _make_workflow_file(repo)

    mock_tracker = MagicMock()
    mock_tracker.list_issues.return_value = [_make_issue("abc123")]

    with patch("host.cli.repo_root", return_value=repo), \
         patch("host.cli.get_tracker_with_fallback", return_value=mock_tracker):
        cmd_issue(_make_args(
            tracker_args=["bug", "-a"],
            workflow=str(wf),
        ))

    mock_tracker.list_issues.assert_called_once_with(status=None)
    mock_tracker.run_raw.assert_not_called()


def test_bug_combined_status_and_label_filter(tmp_path, capsys):
    """bug ls -s open -l nightshift should filter by both status and label."""
    repo = tmp_path / "repo"
    repo.mkdir()
    wf = _make_workflow_file(repo)

    mock_tracker = MagicMock()
    mock_tracker.list_issues.return_value = [
        _make_issue("abc123"),
        replace(_make_issue("def456"), labels=["other"]),
    ]

    with patch("host.cli.repo_root", return_value=repo), \
         patch("host.cli.get_tracker_with_fallback", return_value=mock_tracker):
        cmd_issue(_make_args(
            tracker_args=["bug", "ls", "-s", "open", "-l", "nightshift"],
            workflow=str(wf),
        ))

    mock_tracker.list_issues.assert_called_once_with(status="open")
    mock_tracker.run_raw.assert_not_called()
    out = capsys.readouterr().out
    assert "abc123" in out
    assert "def456" not in out


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
