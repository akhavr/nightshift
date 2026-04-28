"""Tests for CLI commands: status, answer, history, init, revise, cleanup."""

import json
import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch, MagicMock, call

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from host.cli import (
    cmd_accept,
    cmd_reject,
    cmd_status,
    cmd_answer,
    cmd_review,
    cmd_resume,
    cmd_history,
    cmd_init,
    cmd_revise,
    cmd_cleanup,
    cmd_usage,
    cmd_blocked,
    _read_issue_title,
    _truncate_title,
    _format_history_line,
    _unblock_dependents,
)
from core.protocols import TrackerIssue


# ── helpers ──────────────────────────────────────────────────────────────────


def _make_args(**kwargs):
    """Create a simple namespace-like args object."""
    args = MagicMock()
    for k, v in kwargs.items():
        setattr(args, k, v)
    return args


def _clean_git_env():
    """Return env dict with GIT_DIR/GIT_WORK_TREE removed for isolated test repos."""
    env = os.environ.copy()
    env.pop("GIT_DIR", None)
    env.pop("GIT_WORK_TREE", None)
    return env


def _init_repo(tmp_path):
    """Create a git repo with an initial commit on main."""
    repo = tmp_path / "repo"
    repo.mkdir()
    env = _clean_git_env()
    run = lambda *args: subprocess.run(
        args, cwd=str(repo), capture_output=True, text=True, env=env,
    )
    run("git", "init")
    run("git", "config", "user.email", "test@test.com")
    run("git", "config", "user.name", "Test")
    (repo / "file.txt").write_text("initial\n")
    run("git", "add", ".")
    run("git", "commit", "-m", "initial")
    run("git", "checkout", "-b", "main")
    return repo, run


# ── cmd_status ───────────────────────────────────────────────────────────────


def test_cmd_status_no_sessions_dir(tmp_path, capsys):
    """When no sessions dir exists, prints 'No sessions.'"""
    repo = tmp_path / "repo"
    repo.mkdir()

    with patch("host.cli.repo_root", return_value=repo):
        cmd_status(_make_args())

    captured = capsys.readouterr()
    assert "No sessions." in captured.out


def test_cmd_status_multiple_sessions(tmp_path, capsys):
    """When sessions exist, prints a table with each session's info."""
    repo = tmp_path / "repo"
    repo.mkdir()
    sessions = repo / ".nightshift" / "sessions"

    # Create two session dirs with state files and issue data
    for sid, status, step, checkpoints in [
        ("aabbcc112233", "working", 3, ["cp1", "cp2"]),
        ("ddeeff445566", "waiting:review", 7, []),
    ]:
        sd = sessions / sid
        sd.mkdir(parents=True)
        (sd / "state.json").write_text(
            json.dumps({"status": status, "step": step, "checkpoints": checkpoints})
        )

    # Add issue.json for one session
    (sessions / "aabbcc112233" / "issue.json").write_text(
        json.dumps({"title": "Fix login bug", "body": "..."})
    )

    with patch("host.cli.repo_root", return_value=repo):
        cmd_status(_make_args())

    out = capsys.readouterr().out
    assert "aabbcc112233" in out
    assert "working" in out
    assert "ddeeff445566" in out
    assert "waiting:review" in out
    assert "TITLE" in out
    assert "Fix login bug" in out


def test_cmd_status_corrupt_state_json(tmp_path, capsys):
    """Corrupt state.json is handled gracefully — prints error indicator."""
    repo = tmp_path / "repo"
    repo.mkdir()
    sd = repo / ".nightshift" / "sessions" / "corrupt1234ab"
    sd.mkdir(parents=True)
    (sd / "state.json").write_text("{ this is not valid json !!!")

    with patch("host.cli.repo_root", return_value=repo):
        cmd_status(_make_args())

    out = capsys.readouterr().out
    assert "corrupt1234ab" in out
    assert "<error>" in out


def test_cmd_status_shows_title_from_state(tmp_path, capsys):
    """Title is read from state.json issue_title field when available."""
    repo = tmp_path / "repo"
    repo.mkdir()
    sd = repo / ".nightshift" / "sessions" / "statetitle123"
    sd.mkdir(parents=True)
    (sd / "state.json").write_text(json.dumps({
        "status": "working", "step": 1, "checkpoints": [],
        "issue_title": "Title from state",
    }))

    with patch("host.cli.repo_root", return_value=repo):
        cmd_status(_make_args())

    out = capsys.readouterr().out
    assert "Title from state" in out


def test_cmd_status_title_from_issue_json_fallback(tmp_path, capsys):
    """Title falls back to issue.json when state.json has no issue_title."""
    repo = tmp_path / "repo"
    repo.mkdir()
    sd = repo / ".nightshift" / "sessions" / "issuetitle12"
    sd.mkdir(parents=True)
    (sd / "state.json").write_text(json.dumps({
        "status": "working", "step": 1, "checkpoints": [],
    }))
    (sd / "issue.json").write_text(json.dumps({
        "title": "Title from issue.json", "body": "...",
    }))

    with patch("host.cli.repo_root", return_value=repo):
        cmd_status(_make_args())

    out = capsys.readouterr().out
    assert "Title from issue.json" in out


def test_cmd_status_long_title_truncated(tmp_path, capsys):
    """Long titles are truncated with an ellipsis character."""
    repo = tmp_path / "repo"
    repo.mkdir()
    sd = repo / ".nightshift" / "sessions" / "longtitle123"
    sd.mkdir(parents=True)
    long_title = "A" * 60
    (sd / "state.json").write_text(json.dumps({
        "status": "working", "step": 1, "checkpoints": [],
        "issue_title": long_title,
    }))

    with patch("host.cli.repo_root", return_value=repo):
        cmd_status(_make_args())

    out = capsys.readouterr().out
    # Should be truncated to 39 chars + ellipsis
    assert "\u2026" in out
    assert long_title not in out


# ── _read_issue_title ─────────────────────────────────────────────────────────


def test_read_issue_title_from_state(tmp_path):
    """_read_issue_title returns title from state.json when present."""
    sd = tmp_path / "session"
    sd.mkdir()
    (sd / "state.json").write_text(json.dumps({"issue_title": "Bug fix"}))
    assert _read_issue_title(sd) == "Bug fix"


def test_read_issue_title_fallback_to_issue_json(tmp_path):
    """_read_issue_title falls back to issue.json when state has no title."""
    sd = tmp_path / "session"
    sd.mkdir()
    (sd / "state.json").write_text(json.dumps({"status": "working"}))
    (sd / "issue.json").write_text(json.dumps({"title": "From issue"}))
    assert _read_issue_title(sd) == "From issue"


def test_read_issue_title_no_title_anywhere(tmp_path):
    """_read_issue_title returns empty string when no title is found."""
    sd = tmp_path / "session"
    sd.mkdir()
    (sd / "state.json").write_text(json.dumps({"status": "working"}))
    assert _read_issue_title(sd) == ""


def test_read_issue_title_corrupt_files(tmp_path):
    """_read_issue_title returns empty string on corrupt JSON files."""
    sd = tmp_path / "session"
    sd.mkdir()
    (sd / "state.json").write_text("not json!!!")
    (sd / "issue.json").write_text("also not json!!!")
    assert _read_issue_title(sd) == ""


# ── _truncate_title ───────────────────────────────────────────────────────────


def test_truncate_title_short():
    """Short titles are returned unchanged."""
    assert _truncate_title("Short title") == "Short title"


def test_truncate_title_exact_limit():
    """Titles exactly at the limit are returned unchanged."""
    title = "A" * 40
    assert _truncate_title(title) == title


def test_truncate_title_over_limit():
    """Titles over the limit are truncated with ellipsis."""
    title = "A" * 50
    result = _truncate_title(title)
    assert len(result) == 40
    assert result.endswith("\u2026")
    assert result == "A" * 39 + "\u2026"


# ── cmd_answer ───────────────────────────────────────────────────────────────


def test_cmd_review_launches_review(tmp_path):
    """review launches launch.py in review mode for waiting:review sessions."""
    repo = tmp_path / "repo"
    repo.mkdir()
    sd = repo / ".nightshift" / "sessions" / "cb5fde88faba"
    sd.mkdir(parents=True)
    (sd / "state.json").write_text(
        json.dumps({"status": "waiting:review", "step": 5, "checkpoints": []})
    )
    (repo / "REVIEW.md").write_text("review workflow\n")

    with (
        patch("host.cli.repo_root", return_value=repo),
        patch("host.cli.resolve_session", return_value="cb5fde88faba"),
        patch("subprocess.run") as run_mock,
    ):
        cmd_review(_make_args(issue_id="cb5fde88faba"))

    cmd = run_mock.call_args[0][0]
    assert cmd[0] == sys.executable
    assert Path(cmd[1]).name == "launch.py"
    assert cmd[2] == "cb5fde88faba"
    assert cmd[cmd.index("--workflow") + 1].endswith("REVIEW.md")
    assert cmd[cmd.index("--step") + 1] == "review"
    assert cmd[cmd.index("--coder-session") + 1] == "cb5fde88faba"


def test_cmd_review_rejects_non_waiting_session(tmp_path, capsys):
    """review rejects sessions that are not waiting for review."""
    repo = tmp_path / "repo"
    repo.mkdir()
    sd = repo / ".nightshift" / "sessions" / "cb5fde88faba"
    sd.mkdir(parents=True)
    (sd / "state.json").write_text(
        json.dumps({"status": "working", "step": 5, "checkpoints": []})
    )

    with (
        patch("host.cli.repo_root", return_value=repo),
        patch("host.cli.resolve_session", return_value="cb5fde88faba"),
        pytest.raises(SystemExit) as exc_info,
    ):
        cmd_review(_make_args(issue_id="cb5fde88faba"))

    assert exc_info.value.code == 1
    assert "waiting:review" in capsys.readouterr().err


def test_cmd_resume_review_session_strips_prefix(tmp_path):
    """resume resolves review sessions and passes bare issue ID to launch.py."""
    repo = tmp_path / "repo"
    repo.mkdir()
    with (
        patch("host.cli.repo_root", return_value=repo),
        patch("host.cli._resolve_workflow", return_value=Path("WORKFLOW.md")),
        patch("host.cli.resolve_session", return_value="review-47ca35f12345"),
        patch("subprocess.run") as run_mock,
    ):
        cmd_resume(_make_args(issue_id="review-47ca35f", workflow=None))

    cmd = run_mock.call_args[0][0]
    assert cmd[0] == sys.executable
    assert Path(cmd[1]).name == "launch.py"
    assert cmd[2] == "47ca35f12345"
    assert "--resume" in cmd


def test_cmd_resume_review_session_adds_step_flag(tmp_path):
    """resume adds review step and review workflow for review sessions."""
    repo = tmp_path / "repo"
    repo.mkdir()
    with (
        patch("host.cli.repo_root", return_value=repo),
        patch("host.cli._resolve_workflow", return_value=Path("CUSTOM.md")),
        patch("host.cli.resolve_session", return_value="review-47ca35f12345"),
        patch("subprocess.run") as run_mock,
    ):
        cmd_resume(_make_args(issue_id="review-47ca35f", workflow=None))

    cmd = run_mock.call_args[0][0]
    assert "--step" in cmd
    assert cmd[cmd.index("--step") + 1] == "review"
    assert "--workflow" in cmd
    assert cmd[cmd.index("--workflow") + 1].endswith("REVIEW.md")


def test_cmd_answer_writes_file(tmp_path, capsys):
    """answer writes the message to answer.txt in the correct session dir."""
    repo = tmp_path / "repo"
    repo.mkdir()
    sd = repo / ".nightshift" / "sessions" / "abc123456789"
    sd.mkdir(parents=True)

    with patch("host.cli.repo_root", return_value=repo), \
         patch("host.cli.resolve_session", return_value="abc123456789"):
        cmd_answer(_make_args(issue_id="abc123456789", message="The answer is 42"))

    answer_file = sd / "answer.txt"
    assert answer_file.exists()
    assert answer_file.read_text() == "The answer is 42"

    out = capsys.readouterr().out
    assert "abc123456789" in out


def test_cmd_answer_session_not_found(tmp_path, capsys):
    """answer prints an error to stderr when the session dir does not exist."""
    repo = tmp_path / "repo"
    repo.mkdir()
    # Sessions dir exists but the target session does not
    (repo / ".nightshift" / "sessions").mkdir(parents=True)

    with patch("host.cli.repo_root", return_value=repo), \
         patch("host.cli.resolve_session", return_value="nosession1234"):
        cmd_answer(_make_args(issue_id="nosession1234", message="hello"))

    err = capsys.readouterr().err
    assert "No session found" in err


# ── cmd_history ───────────────────────────────────────────────────────────────


def test_cmd_history_prints_entries(tmp_path, capsys):
    """history prints formatted entries from conversation.jsonl."""
    repo = tmp_path / "repo"
    repo.mkdir()
    sd = repo / ".nightshift" / "sessions" / "hist1234abcd"
    sd.mkdir(parents=True)

    entries = [
        {"timestamp": "2024-01-01T10:00:00Z", "role": "thought",
         "content": "I should check the code"},
        {"timestamp": "2024-01-01T10:01:00Z", "role": "checkpoint",
         "content": "Reviewed the codebase"},
    ]
    (sd / "conversation.jsonl").write_text(
        "\n".join(json.dumps(e) for e in entries)
    )

    with patch("host.cli.repo_root", return_value=repo), \
         patch("host.cli.resolve_session", return_value="hist1234abcd"):
        cmd_history(_make_args(issue_id="hist1234abcd", follow=False))

    out = capsys.readouterr().out
    assert "thought" in out
    assert "checkpoint" in out
    assert "I should check the code" in out
    assert "Reviewed the codebase" in out


def test_cmd_history_missing_file(tmp_path, capsys):
    """history prints 'No history.' to stderr when conversation.jsonl is absent."""
    repo = tmp_path / "repo"
    repo.mkdir()
    sd = repo / ".nightshift" / "sessions" / "nohist12abcd"
    sd.mkdir(parents=True)
    # No conversation.jsonl

    with patch("host.cli.repo_root", return_value=repo), \
         patch("host.cli.resolve_session", return_value="nohist12abcd"):
        cmd_history(_make_args(issue_id="nohist12abcd", follow=False))

    err = capsys.readouterr().err
    assert "No history." in err


def test_format_history_line_valid():
    """_format_history_line returns formatted string for valid JSON."""
    line = json.dumps({"timestamp": "2024-01-01T10:00:00Z", "role": "thought",
                       "content": "Hello world"})
    result = _format_history_line(line)
    assert result is not None
    assert "thought" in result
    assert "Hello world" in result


def test_format_history_line_invalid():
    """_format_history_line returns None for malformed input."""
    assert _format_history_line("not-json") is None
    assert _format_history_line("") is None


def test_cmd_history_follow_prints_new_lines(tmp_path, capsys):
    """history --follow prints new lines appended after initial read."""
    repo = tmp_path / "repo"
    repo.mkdir()
    sd = repo / ".nightshift" / "sessions" / "follow12abcd"
    sd.mkdir(parents=True)
    cf = sd / "conversation.jsonl"

    initial = {"timestamp": "2024-01-01T10:00:00Z", "role": "thought",
               "content": "Initial entry"}
    cf.write_text(json.dumps(initial) + "\n")

    new_entry = json.dumps({"timestamp": "2024-01-01T10:01:00Z",
                            "role": "checkpoint",
                            "content": "New entry from follow"}) + "\n"

    sleep_call_count = 0

    def fake_sleep(_duration):
        nonlocal sleep_call_count
        sleep_call_count += 1
        if sleep_call_count == 1:
            # Append a new line on the first sleep (no new data yet)
            with open(cf, "a") as fh:
                fh.write(new_entry)
        elif sleep_call_count >= 3:
            raise KeyboardInterrupt

    with patch("host.cli.repo_root", return_value=repo), \
         patch("host.cli.resolve_session", return_value="follow12abcd"), \
         patch("host.cli.time.sleep", side_effect=fake_sleep):
        cmd_history(_make_args(issue_id="follow12abcd", follow=True))

    out = capsys.readouterr().out
    assert "Initial entry" in out
    assert "New entry from follow" in out


def test_cmd_history_no_follow_by_default(tmp_path, capsys):
    """history without --follow exits after printing existing entries."""
    repo = tmp_path / "repo"
    repo.mkdir()
    sd = repo / ".nightshift" / "sessions" / "nofol123abcd"
    sd.mkdir(parents=True)

    entry = {"timestamp": "2024-01-01T10:00:00Z", "role": "thought",
             "content": "Just an entry"}
    (sd / "conversation.jsonl").write_text(json.dumps(entry) + "\n")

    with patch("host.cli.repo_root", return_value=repo), \
         patch("host.cli.resolve_session", return_value="nofol123abcd"):
        cmd_history(_make_args(issue_id="nofol123abcd", follow=False))

    out = capsys.readouterr().out
    assert "Just an entry" in out


# ── cmd_init ─────────────────────────────────────────────────────────────────


def test_cmd_init_creates_files(tmp_path, capsys):
    """init creates WORKFLOW.md, REVIEW.md, .env.example, and .nightshift/."""
    repo, run = _init_repo(tmp_path)

    with patch("host.cli.repo_root", return_value=repo):
        cmd_init(_make_args(force=False, workflow_path=None))

    assert (repo / "WORKFLOW.md").exists()
    assert (repo / "REVIEW.md").exists()
    assert (repo / ".env.example").exists()
    assert (repo / ".nightshift" / "sessions").is_dir()


def test_cmd_init_does_not_overwrite_without_force(tmp_path, capsys):
    """init skips existing files when --force is not set."""
    repo, run = _init_repo(tmp_path)
    (repo / "WORKFLOW.md").write_text("# original")

    with patch("host.cli.repo_root", return_value=repo):
        cmd_init(_make_args(force=False, workflow_path=None))

    # Original content should be preserved
    assert (repo / "WORKFLOW.md").read_text() == "# original"
    out = capsys.readouterr().out
    assert "already exists" in out


def test_cmd_init_overwrites_with_force(tmp_path, capsys):
    """init overwrites existing files when --force is set."""
    repo, run = _init_repo(tmp_path)
    (repo / "WORKFLOW.md").write_text("# original")

    with patch("host.cli.repo_root", return_value=repo):
        cmd_init(_make_args(force=True, workflow_path=None))

    content = (repo / "WORKFLOW.md").read_text()
    assert "# original" not in content
    # Should contain the default template content
    assert "agent:" in content


def test_cmd_init_updates_gitignore(tmp_path, capsys):
    """init adds .env, .worktrees/, .nightshift/ to .gitignore."""
    repo, run = _init_repo(tmp_path)

    with patch("host.cli.repo_root", return_value=repo):
        cmd_init(_make_args(force=False, workflow_path=None))

    gitignore = (repo / ".gitignore").read_text()
    assert ".env" in gitignore
    assert ".worktrees/" in gitignore
    assert ".nightshift/" in gitignore


def test_cmd_init_no_duplicate_gitignore_entries(tmp_path, capsys):
    """init doesn't add duplicate entries to .gitignore."""
    repo, run = _init_repo(tmp_path)
    (repo / ".gitignore").write_text(".env\n.worktrees/\n.nightshift/\n")

    with patch("host.cli.repo_root", return_value=repo):
        cmd_init(_make_args(force=False, workflow_path=None))

    gitignore_content = (repo / ".gitignore").read_text()
    out = capsys.readouterr().out
    assert "already has nightshift entries" in out
    # The entries should appear exactly once
    assert gitignore_content.count(".env\n") == 1


def test_cmd_init_installs_pre_commit_hook(tmp_path, capsys):
    """init installs pre-commit hook that rejects conflict markers."""
    repo, run = _init_repo(tmp_path)

    with patch("host.cli.repo_root", return_value=repo):
        cmd_init(_make_args(force=False, workflow_path=None))

    hook_path = repo / ".git" / "hooks" / "pre-commit"
    assert hook_path.exists()
    assert hook_path.stat().st_mode & 0o111  # executable
    content = hook_path.read_text()
    assert "conflict marker" in content.lower() or "<<<<<<<" in content


def test_cmd_init_does_not_overwrite_hook_without_force(tmp_path, capsys):
    """init skips existing pre-commit hook when --force is not set."""
    repo, run = _init_repo(tmp_path)
    hook_dir = repo / ".git" / "hooks"
    hook_dir.mkdir(parents=True, exist_ok=True)
    hook_path = hook_dir / "pre-commit"
    hook_path.write_text("#!/bin/bash\n# custom hook\n")

    with patch("host.cli.repo_root", return_value=repo):
        cmd_init(_make_args(force=False, workflow_path=None))

    # Original hook should be preserved
    assert "custom hook" in hook_path.read_text()
    out = capsys.readouterr().out
    assert "already exists" in out


def test_cmd_init_overwrites_hook_with_force(tmp_path, capsys):
    """init overwrites existing pre-commit hook when --force is set."""
    repo, run = _init_repo(tmp_path)
    hook_dir = repo / ".git" / "hooks"
    hook_dir.mkdir(parents=True, exist_ok=True)
    hook_path = hook_dir / "pre-commit"
    hook_path.write_text("#!/bin/bash\n# custom hook\n")

    with patch("host.cli.repo_root", return_value=repo):
        cmd_init(_make_args(force=True, workflow_path=None))

    # Hook should be overwritten with nightshift version
    content = hook_path.read_text()
    assert "custom hook" not in content
    assert "conflict marker" in content.lower() or "<<<<<<<" in content


def test_pre_commit_hook_rejects_conflict_markers(tmp_path):
    """The pre-commit hook should reject commits with conflict markers."""
    repo, _ = _init_repo(tmp_path)
    env = _clean_git_env()

    with patch("host.cli.repo_root", return_value=repo):
        cmd_init(_make_args(force=False, workflow_path=None))

    # Create a file with conflict markers
    conflict_file = repo / "conflict.txt"
    conflict_file.write_text("before\n<<<<<<< HEAD\nours\n=======\ntheirs\n>>>>>>> branch\nafter\n")
    subprocess.run(["git", "add", "conflict.txt"], cwd=str(repo), env=env)

    # Commit should fail due to hook
    result = subprocess.run(
        ["git", "commit", "-m", "should fail"],
        cwd=str(repo), capture_output=True, text=True, env=env
    )
    assert result.returncode != 0
    assert "conflict marker" in result.stderr.lower() or "conflict marker" in result.stdout.lower()


def test_pre_commit_hook_allows_clean_commits(tmp_path):
    """The pre-commit hook should allow commits without conflict markers."""
    repo, _ = _init_repo(tmp_path)
    env = _clean_git_env()

    with patch("host.cli.repo_root", return_value=repo):
        cmd_init(_make_args(force=False, workflow_path=None))

    # Create a clean file
    clean_file = repo / "clean.txt"
    clean_file.write_text("no conflicts here\n")
    subprocess.run(["git", "add", "clean.txt"], cwd=str(repo), env=env)

    # Commit should succeed
    result = subprocess.run(
        ["git", "commit", "-m", "should pass"],
        cwd=str(repo), capture_output=True, text=True, env=env
    )
    assert result.returncode == 0


# ── cmd_revise ────────────────────────────────────────────────────────────────


def test_cmd_revise_session_not_found(tmp_path, capsys):
    """revise exits with error when session directory does not exist."""
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".nightshift" / "sessions").mkdir(parents=True)

    with patch("host.cli.repo_root", return_value=repo), \
         patch("host.cli.resolve_session", return_value="nosession5678"), \
         pytest.raises(SystemExit) as exc_info:
        cmd_revise(_make_args(issue_id="nosession5678", workflow=None, message=None))

    assert exc_info.value.code == 1
    err = capsys.readouterr().err
    assert "No session found" in err


def test_cmd_revise_wrong_status(tmp_path, capsys):
    """revise exits with error when session status is not revisable."""
    repo = tmp_path / "repo"
    repo.mkdir()
    sd = repo / ".nightshift" / "sessions" / "done12345678"
    sd.mkdir(parents=True)
    (sd / "state.json").write_text(json.dumps({"status": "done", "step": 2}))

    with patch("host.cli.repo_root", return_value=repo), \
         patch("host.cli.resolve_session", return_value="done12345678"), \
         pytest.raises(SystemExit) as exc_info:
        cmd_revise(_make_args(issue_id="done12345678", workflow=None, message=None))

    assert exc_info.value.code == 1
    err = capsys.readouterr().err
    assert "not revisable" in err


def test_cmd_revise_success(tmp_path, capsys):
    """revise writes resume-prompt.md, updates status to working, calls launch.py."""
    repo, run = _init_repo(tmp_path)
    sd = repo / ".nightshift" / "sessions" / "review1234ab"
    sd.mkdir(parents=True)
    (sd / "state.json").write_text(
        json.dumps({"status": "waiting:review", "step": 5, "checkpoints": []})
    )

    # Minimal WORKFLOW.md
    (repo / "WORKFLOW.md").write_text(
        "---\n"
        "agent:\n  kind: claude-code\n"
        "tracker:\n  kind: git-bug\n"
        "workspace:\n  kind: worktree\n  base_branch: main\n  root: .worktrees\n"
        "---\nPrompt\n"
    )

    mock_comment = MagicMock()
    mock_comment.author = "reviewer"
    mock_comment.body = "Please add more tests"

    mock_tracker = MagicMock()
    mock_tracker.get_comments.return_value = [mock_comment]

    with patch("host.cli.repo_root", return_value=repo), \
         patch("host.cli.resolve_session", return_value="review1234ab"), \
         patch("host.cli.get_tracker_with_fallback", return_value=mock_tracker), \
         patch("subprocess.run") as mock_subproc:
        cmd_revise(_make_args(
            issue_id="review1234ab",
            workflow=str(repo / "WORKFLOW.md"),
            message=None,
        ))

    # resume-prompt.md should have been written
    resume_prompt = sd / "resume-prompt.md"
    assert resume_prompt.exists()
    assert "Please add more tests" in resume_prompt.read_text()

    # state should be updated to working
    state = json.loads((sd / "state.json").read_text())
    assert state["status"] == "working"

    # launch.py --resume should have been called
    launch_call = mock_subproc.call_args_list[-1]
    cmd_args = launch_call[0][0]
    assert "launch.py" in " ".join(str(c) for c in cmd_args)
    assert "--resume" in cmd_args


def test_cmd_revise_review_session_uses_review_resume_args(tmp_path):
    """revise relaunches review sessions with bare ID, review step, and REVIEW.md."""
    repo = tmp_path / "repo"
    repo.mkdir()
    sd = repo / ".nightshift" / "sessions" / "review-47ca35f12345"
    sd.mkdir(parents=True)
    (sd / "state.json").write_text(
        json.dumps({"status": "waiting:review", "step": 2, "checkpoints": []})
    )
    (repo / "REVIEW.md").write_text("review workflow\n")

    with (
        patch("host.cli.repo_root", return_value=repo),
        patch("host.cli.resolve_session", return_value="review-47ca35f12345"),
        patch("host.cli._resolve_workflow", return_value=repo / "WORKFLOW.md"),
        patch("host.cli._collect_review_feedback", return_value="Please fix this."),
        patch("subprocess.run") as mock_subproc,
    ):
        cmd_revise(_make_args(issue_id="review-47ca35f", workflow=None, message=None))

    cmd_args = mock_subproc.call_args[0][0]
    assert cmd_args[2] == "47ca35f12345"
    assert cmd_args[cmd_args.index("--step") + 1] == "review"
    assert cmd_args[cmd_args.index("--workflow") + 1].endswith("REVIEW.md")


def test_cmd_revise_accepts_waiting_human_review(tmp_path, capsys):
    """revise also accepts 'waiting:human-review' status."""
    repo, run = _init_repo(tmp_path)
    sd = repo / ".nightshift" / "sessions" / "humrev123456"
    sd.mkdir(parents=True)
    (sd / "state.json").write_text(
        json.dumps({"status": "waiting:human-review", "step": 3, "checkpoints": []})
    )

    (repo / "WORKFLOW.md").write_text(
        "---\n"
        "agent:\n  kind: claude-code\n"
        "tracker:\n  kind: git-bug\n"
        "workspace:\n  kind: worktree\n  base_branch: main\n  root: .worktrees\n"
        "---\nPrompt\n"
    )

    mock_comment = MagicMock()
    mock_comment.author = "reviewer"
    mock_comment.body = "Looks good, minor nit"

    mock_tracker = MagicMock()
    mock_tracker.get_comments.return_value = [mock_comment]

    with patch("host.cli.repo_root", return_value=repo), \
         patch("host.cli.resolve_session", return_value="humrev123456"), \
         patch("host.cli.get_tracker_with_fallback", return_value=mock_tracker), \
         patch("subprocess.run"):
        # Should not raise SystemExit
        cmd_revise(_make_args(
            issue_id="humrev123456",
            workflow=str(repo / "WORKFLOW.md"),
            message=None,
        ))

    state = json.loads((sd / "state.json").read_text())
    assert state["status"] == "working"


def test_cmd_revise_suspended_session(tmp_path):
    """revise accepts suspended sessions and relaunches with feedback."""
    repo, run = _init_repo(tmp_path)
    sd = repo / ".nightshift" / "sessions" / "suspend12345"
    sd.mkdir(parents=True)
    (sd / "state.json").write_text(
        json.dumps({"status": "suspended:max-resumes", "step": 4, "checkpoints": []})
    )

    (repo / "WORKFLOW.md").write_text(
        "---\n"
        "agent:\n  kind: claude-code\n"
        "tracker:\n  kind: git-bug\n"
        "workspace:\n  kind: worktree\n  base_branch: main\n  root: .worktrees\n"
        "---\nPrompt\n"
    )

    mock_tracker = MagicMock()
    mock_tracker.get_comments.return_value = []

    with patch("host.cli.repo_root", return_value=repo), \
         patch("host.cli.resolve_session", return_value="suspend12345"), \
         patch("host.cli.get_tracker_with_fallback", return_value=mock_tracker), \
         patch("subprocess.run") as mock_subproc:
        cmd_revise(_make_args(
            issue_id="suspend12345",
            workflow=str(repo / "WORKFLOW.md"),
            message="Resume with the new plan.",
        ))

    resume_prompt = sd / "resume-prompt.md"
    assert resume_prompt.exists()
    assert "Resume with the new plan." in resume_prompt.read_text()

    state = json.loads((sd / "state.json").read_text())
    assert state["status"] == "working"

    launch_call = mock_subproc.call_args_list[-1]
    cmd_args = launch_call[0][0]
    assert "--resume" in cmd_args


def test_cmd_revise_working_session_stops_and_relaunches(tmp_path, capsys):
    """revise on a working session stops the container, writes prompt, relaunches."""
    repo, run = _init_repo(tmp_path)
    sd = repo / ".nightshift" / "sessions" / "workrev12345"
    sd.mkdir(parents=True)
    (sd / "state.json").write_text(
        json.dumps({"status": "working", "step": 3, "checkpoints": []})
    )

    (repo / "WORKFLOW.md").write_text(
        "---\n"
        "agent:\n  kind: claude-code\n"
        "tracker:\n  kind: git-bug\n"
        "workspace:\n  kind: worktree\n  base_branch: main\n  root: .worktrees\n"
        "---\nPrompt\n"
    )

    with patch("host.cli.repo_root", return_value=repo), \
         patch("host.cli.resolve_session", return_value="workrev12345"), \
         patch("host.cli.docker_stop", return_value=True) as mock_stop, \
         patch("subprocess.run") as mock_subproc:
        cmd_revise(_make_args(
            issue_id="workrev12345",
            workflow=str(repo / "WORKFLOW.md"),
            message="Stop, the requirements changed. Use the new API.",
        ))

    # Container should have been stopped
    mock_stop.assert_called_once_with("nightshift-workrev12345")

    # resume-prompt.md should contain the mid-flight prompt
    resume_prompt = sd / "resume-prompt.md"
    assert resume_prompt.exists()
    content = resume_prompt.read_text()
    assert "Mid-flight Course Correction" in content
    assert "Use the new API" in content

    # state should remain working
    state = json.loads((sd / "state.json").read_text())
    assert state["status"] == "working"

    # launch.py --resume should have been called
    launch_call = mock_subproc.call_args_list[-1]
    cmd_args = launch_call[0][0]
    assert "--resume" in cmd_args


def test_cmd_revise_starting_session(tmp_path, capsys):
    """revise also works on 'starting' status sessions."""
    repo, run = _init_repo(tmp_path)
    sd = repo / ".nightshift" / "sessions" / "start1234567"
    sd.mkdir(parents=True)
    (sd / "state.json").write_text(
        json.dumps({"status": "starting", "step": 0, "checkpoints": []})
    )

    (repo / "WORKFLOW.md").write_text(
        "---\n"
        "agent:\n  kind: claude-code\n"
        "tracker:\n  kind: git-bug\n"
        "workspace:\n  kind: worktree\n  base_branch: main\n  root: .worktrees\n"
        "---\nPrompt\n"
    )

    with patch("host.cli.repo_root", return_value=repo), \
         patch("host.cli.resolve_session", return_value="start1234567"), \
         patch("host.cli.docker_stop", return_value=True), \
         patch("subprocess.run"):
        cmd_revise(_make_args(
            issue_id="start1234567",
            workflow=str(repo / "WORKFLOW.md"),
            message="Wrong issue data, use the updated spec.",
        ))

    state = json.loads((sd / "state.json").read_text())
    assert state["status"] == "working"
    assert "updated spec" in (sd / "resume-prompt.md").read_text()


def test_cmd_revise_working_requires_message(tmp_path, capsys):
    """revise on a working session requires an inline message."""
    repo = tmp_path / "repo"
    repo.mkdir()
    sd = repo / ".nightshift" / "sessions" / "nomsg1234567"
    sd.mkdir(parents=True)
    (sd / "state.json").write_text(
        json.dumps({"status": "working", "step": 1, "checkpoints": []})
    )

    with patch("host.cli.repo_root", return_value=repo), \
         patch("host.cli.resolve_session", return_value="nomsg1234567"), \
         pytest.raises(SystemExit) as exc_info:
        cmd_revise(_make_args(issue_id="nomsg1234567", workflow=None, message=None))

    assert exc_info.value.code == 1
    err = capsys.readouterr().err
    assert "message is required" in err


def test_cmd_revise_working_warns_on_stop_failure(tmp_path, capsys):
    """revise prints a warning if docker stop fails (container may not be running)."""
    repo, run = _init_repo(tmp_path)
    sd = repo / ".nightshift" / "sessions" / "stopfail1234"
    sd.mkdir(parents=True)
    (sd / "state.json").write_text(
        json.dumps({"status": "working", "step": 2, "checkpoints": []})
    )

    (repo / "WORKFLOW.md").write_text(
        "---\n"
        "agent:\n  kind: claude-code\n"
        "tracker:\n  kind: git-bug\n"
        "workspace:\n  kind: worktree\n  base_branch: main\n  root: .worktrees\n"
        "---\nPrompt\n"
    )

    with patch("host.cli.repo_root", return_value=repo), \
         patch("host.cli.resolve_session", return_value="stopfail1234"), \
         patch("host.cli.docker_stop", return_value=False), \
         patch("subprocess.run"):
        cmd_revise(_make_args(
            issue_id="stopfail1234",
            workflow=str(repo / "WORKFLOW.md"),
            message="Change direction please.",
        ))

    err = capsys.readouterr().err
    assert "may not be running" in err

    # Should still write prompt and proceed
    assert (sd / "resume-prompt.md").exists()


# ── cmd_cleanup ───────────────────────────────────────────────────────────────


def test_cmd_cleanup_removes_worktree_and_session(tmp_path, capsys):
    """cleanup removes worktree dir and session dir by default."""
    repo, run = _init_repo(tmp_path)

    # Create a minimal WORKFLOW.md
    (repo / "WORKFLOW.md").write_text(
        "---\n"
        "agent:\n  kind: claude-code\n"
        "tracker:\n  kind: git-bug\n"
        "workspace:\n  kind: worktree\n  base_branch: main\n  root: .worktrees\n"
        "---\nPrompt\n"
    )

    # Create session dir and a fake worktree dir
    sd = repo / ".nightshift" / "sessions" / "cleanup12345"
    sd.mkdir(parents=True)
    (sd / "state.json").write_text(json.dumps({"status": "working"}))

    wt = repo / ".worktrees" / "agent-cleanup12345"
    wt.mkdir(parents=True)

    with patch("host.cli.repo_root", return_value=repo), \
         patch("host.cli.resolve_session", return_value="cleanup12345"), \
         patch("host.session_utils.subprocess.run") as mock_git:
        # simulate successful git worktree remove
        mock_git.return_value = MagicMock(returncode=0)
        cmd_cleanup(_make_args(issue_id="cleanup12345", keep_session=False, workflow=None))

    # Session dir should be gone
    assert not sd.exists()
    out = capsys.readouterr().out
    assert "cleanup12345" in out


def test_cmd_cleanup_keep_session(tmp_path, capsys):
    """cleanup with --keep-session preserves the session directory."""
    repo, run = _init_repo(tmp_path)

    (repo / "WORKFLOW.md").write_text(
        "---\n"
        "agent:\n  kind: claude-code\n"
        "tracker:\n  kind: git-bug\n"
        "workspace:\n  kind: worktree\n  base_branch: main\n  root: .worktrees\n"
        "---\nPrompt\n"
    )

    sd = repo / ".nightshift" / "sessions" / "keepses12345"
    sd.mkdir(parents=True)
    (sd / "state.json").write_text(json.dumps({"status": "waiting:review"}))

    with patch("host.cli.repo_root", return_value=repo), \
         patch("host.cli.resolve_session", return_value="keepses12345"), \
         patch("host.session_utils.subprocess.run") as mock_git:
        mock_git.return_value = MagicMock(returncode=0)
        cmd_cleanup(_make_args(issue_id="keepses12345", keep_session=True, workflow=None))

    # Session dir should still be present
    assert sd.exists()
    out = capsys.readouterr().out
    assert "keepses12345" in out


# ── cmd_usage ───────────────────────────────────────────────────────────────


def test_cmd_usage_no_file(tmp_path, capsys):
    """When usage.jsonl does not exist, prints informative message."""
    repo = tmp_path / "repo"
    repo.mkdir()
    with patch("host.cli.repo_root", return_value=repo):
        cmd_usage(_make_args(issue_id=None, since=None, until=None, daily=False, all_projects=False))
    out = capsys.readouterr().out
    assert "No usage data found" in out


def test_cmd_usage_basic_output(tmp_path, capsys):
    """Displays entries and totals from usage.jsonl."""
    repo = tmp_path / "repo"
    ns = repo / ".nightshift"
    ns.mkdir(parents=True)
    entries = [
        {"session_id": "abc123", "issue_id": "issue1", "input_tokens": 10000,
         "output_tokens": 3000, "cost_usd": 0.15, "model": "claude-sonnet-4-6",
         "step": "coder"},
        {"session_id": "def456", "issue_id": "issue2", "input_tokens": 20000,
         "output_tokens": 5000, "cost_usd": 0.25, "model": "claude-sonnet-4-6",
         "step": "coder"},
    ]
    (ns / "usage.jsonl").write_text(
        "\n".join(json.dumps(e) for e in entries) + "\n")

    with patch("host.cli.repo_root", return_value=repo):
        cmd_usage(_make_args(issue_id=None, since=None, until=None, daily=False, all_projects=False))
    out = capsys.readouterr().out
    assert "abc123" in out
    assert "def456" in out
    assert "$0.15" in out
    assert "$0.25" in out
    assert "TOTAL" in out
    assert "2 session(s)" in out


def test_cmd_usage_filter_by_issue_id(tmp_path, capsys):
    """Filters entries by issue_id prefix match."""
    repo = tmp_path / "repo"
    ns = repo / ".nightshift"
    ns.mkdir(parents=True)
    entries = [
        {"session_id": "s1", "issue_id": "abc-123", "input_tokens": 10000,
         "output_tokens": 3000, "cost_usd": 0.15, "model": "m", "step": "coder"},
        {"session_id": "s2", "issue_id": "xyz-999", "input_tokens": 20000,
         "output_tokens": 5000, "cost_usd": 0.25, "model": "m", "step": "coder"},
    ]
    (ns / "usage.jsonl").write_text(
        "\n".join(json.dumps(e) for e in entries) + "\n")

    with patch("host.cli.repo_root", return_value=repo):
        cmd_usage(_make_args(issue_id="abc", since=None, until=None, daily=False, all_projects=False))
    out = capsys.readouterr().out
    assert "s1" in out
    assert "s2" not in out
    assert "1 session(s)" in out


def test_cmd_usage_malformed_lines(tmp_path, capsys):
    """Skips malformed JSON lines with a warning."""
    repo = tmp_path / "repo"
    ns = repo / ".nightshift"
    ns.mkdir(parents=True)
    content = 'not valid json\n{"session_id": "s1", "issue_id": "i1", "input_tokens": 5000, "output_tokens": 1000, "cost_usd": 0.10, "model": "m", "step": "coder"}\n'
    (ns / "usage.jsonl").write_text(content)

    with patch("host.cli.repo_root", return_value=repo):
        cmd_usage(_make_args(issue_id=None, since=None, until=None, daily=False, all_projects=False))
    captured = capsys.readouterr()
    assert "Warning: skipping malformed line" in captured.err
    assert "s1" in captured.out
    assert "1 session(s)" in captured.out


def test_cmd_usage_empty_file(tmp_path, capsys):
    """Empty usage.jsonl prints 'No usage entries found.'"""
    repo = tmp_path / "repo"
    ns = repo / ".nightshift"
    ns.mkdir(parents=True)
    (ns / "usage.jsonl").write_text("")

    with patch("host.cli.repo_root", return_value=repo):
        cmd_usage(_make_args(issue_id=None, since=None, until=None, daily=False, all_projects=False))
    out = capsys.readouterr().out
    assert "No usage entries found" in out


def test_cmd_usage_since_filter(tmp_path, capsys):
    """Entries before --since date are excluded from output."""
    repo = tmp_path / "repo"
    ns = repo / ".nightshift"
    ns.mkdir(parents=True)
    entries = [
        {"session_id": "old1", "issue_id": "i1", "input_tokens": 1000,
         "output_tokens": 500, "cost_usd": 0.05, "model": "m", "step": "coder",
         "completed_at": "2025-01-01T10:00:00"},
        {"session_id": "new1", "issue_id": "i2", "input_tokens": 2000,
         "output_tokens": 800, "cost_usd": 0.10, "model": "m", "step": "coder",
         "completed_at": "2025-06-15T10:00:00"},
    ]
    (ns / "usage.jsonl").write_text(
        "\n".join(json.dumps(e) for e in entries) + "\n")

    with patch("host.cli.repo_root", return_value=repo):
        cmd_usage(_make_args(issue_id=None, since="2025-03-01",
                             until=None, daily=False, all_projects=False))
    out = capsys.readouterr().out
    assert "old1" not in out
    assert "new1" in out
    assert "1 session(s)" in out


def test_cmd_usage_until_filter(tmp_path, capsys):
    """Entries after --until date are excluded from output."""
    repo = tmp_path / "repo"
    ns = repo / ".nightshift"
    ns.mkdir(parents=True)
    entries = [
        {"session_id": "old1", "issue_id": "i1", "input_tokens": 1000,
         "output_tokens": 500, "cost_usd": 0.05, "model": "m", "step": "coder",
         "completed_at": "2025-01-01T10:00:00"},
        {"session_id": "new1", "issue_id": "i2", "input_tokens": 2000,
         "output_tokens": 800, "cost_usd": 0.10, "model": "m", "step": "coder",
         "completed_at": "2025-06-15T10:00:00"},
    ]
    (ns / "usage.jsonl").write_text(
        "\n".join(json.dumps(e) for e in entries) + "\n")

    with patch("host.cli.repo_root", return_value=repo):
        cmd_usage(_make_args(issue_id=None, since=None,
                             until="2025-03-01", daily=False, all_projects=False))
    out = capsys.readouterr().out
    assert "old1" in out
    assert "new1" not in out
    assert "1 session(s)" in out


def test_cmd_usage_since_and_until(tmp_path, capsys):
    """Only entries within the date range are shown."""
    repo = tmp_path / "repo"
    ns = repo / ".nightshift"
    ns.mkdir(parents=True)
    entries = [
        {"session_id": "s1", "issue_id": "i1", "input_tokens": 1000,
         "output_tokens": 500, "cost_usd": 0.05, "model": "m", "step": "coder",
         "completed_at": "2025-01-01T10:00:00"},
        {"session_id": "s2", "issue_id": "i2", "input_tokens": 2000,
         "output_tokens": 800, "cost_usd": 0.10, "model": "m", "step": "coder",
         "completed_at": "2025-03-15T10:00:00"},
        {"session_id": "s3", "issue_id": "i3", "input_tokens": 3000,
         "output_tokens": 1200, "cost_usd": 0.20, "model": "m", "step": "coder",
         "completed_at": "2025-06-15T10:00:00"},
    ]
    (ns / "usage.jsonl").write_text(
        "\n".join(json.dumps(e) for e in entries) + "\n")

    with patch("host.cli.repo_root", return_value=repo):
        cmd_usage(_make_args(issue_id=None, since="2025-02-01",
                             until="2025-05-01", daily=False, all_projects=False))
    out = capsys.readouterr().out
    assert "s1" not in out
    assert "s2" in out
    assert "s3" not in out
    assert "1 session(s)" in out


def test_cmd_usage_daily_summary(tmp_path, capsys):
    """--daily groups entries by date with per-day subtotals."""
    repo = tmp_path / "repo"
    ns = repo / ".nightshift"
    ns.mkdir(parents=True)
    entries = [
        {"session_id": "s1", "issue_id": "i1", "input_tokens": 1000,
         "output_tokens": 500, "cost_usd": 0.05, "model": "m", "step": "coder",
         "completed_at": "2025-03-10T08:00:00"},
        {"session_id": "s2", "issue_id": "i2", "input_tokens": 2000,
         "output_tokens": 800, "cost_usd": 0.10, "model": "m", "step": "coder",
         "completed_at": "2025-03-10T14:00:00"},
        {"session_id": "s3", "issue_id": "i3", "input_tokens": 3000,
         "output_tokens": 1200, "cost_usd": 0.20, "model": "m", "step": "coder",
         "completed_at": "2025-03-11T09:00:00"},
    ]
    (ns / "usage.jsonl").write_text(
        "\n".join(json.dumps(e) for e in entries) + "\n")

    with patch("host.cli.repo_root", return_value=repo):
        cmd_usage(_make_args(issue_id=None, since=None, until=None,
                             daily=True, all_projects=False))
    out = capsys.readouterr().out
    assert "2025-03-10" in out
    assert "2025-03-11" in out
    assert "2 session(s)" in out  # day 1
    assert "1 session(s)" in out  # day 2 (also matches total but that's fine)
    assert "TOTAL" in out


def test_cmd_usage_all_projects(tmp_path, capsys):
    """--all-projects scans ~/src/*/.nightshift/usage.jsonl and aggregates."""
    src_dir = tmp_path / "src"
    # Project A
    pa = src_dir / "project-a" / ".nightshift"
    pa.mkdir(parents=True)
    (pa / "usage.jsonl").write_text(json.dumps(
        {"session_id": "a1", "issue_id": "i1", "input_tokens": 1000,
         "output_tokens": 500, "cost_usd": 0.05, "model": "m", "step": "coder",
         "completed_at": "2025-03-10T10:00:00"}) + "\n")
    # Project B
    pb = src_dir / "project-b" / ".nightshift"
    pb.mkdir(parents=True)
    (pb / "usage.jsonl").write_text(json.dumps(
        {"session_id": "b1", "issue_id": "i2", "input_tokens": 2000,
         "output_tokens": 800, "cost_usd": 0.10, "model": "m", "step": "coder",
         "completed_at": "2025-03-11T10:00:00"}) + "\n")

    with patch("host.cli._all_projects_src_dir", return_value=src_dir):
        cmd_usage(_make_args(issue_id=None, since=None, until=None,
                             daily=False, all_projects=True))
    out = capsys.readouterr().out
    assert "project-a" in out
    assert "project-b" in out
    assert "GRAND TOTAL" in out


def test_cmd_usage_all_projects_with_date_filter(tmp_path, capsys):
    """--all-projects combined with --since/--until filters correctly."""
    src_dir = tmp_path / "src"
    pa = src_dir / "project-a" / ".nightshift"
    pa.mkdir(parents=True)
    entries = [
        {"session_id": "a1", "issue_id": "i1", "input_tokens": 1000,
         "output_tokens": 500, "cost_usd": 0.05, "model": "m", "step": "coder",
         "completed_at": "2025-01-01T10:00:00"},
        {"session_id": "a2", "issue_id": "i2", "input_tokens": 2000,
         "output_tokens": 800, "cost_usd": 0.10, "model": "m", "step": "coder",
         "completed_at": "2025-06-01T10:00:00"},
    ]
    (pa / "usage.jsonl").write_text(
        "\n".join(json.dumps(e) for e in entries) + "\n")

    with patch("host.cli._all_projects_src_dir", return_value=src_dir):
        cmd_usage(_make_args(issue_id=None, since="2025-03-01", until=None,
                             daily=False, all_projects=True))
    out = capsys.readouterr().out
    assert "project-a" in out
    assert "a2" not in out  # individual entries not shown in all-projects mode
    assert "1 session(s)" in out


def test_cmd_usage_no_matching_entries(tmp_path, capsys):
    """Prints message when filters exclude everything."""
    repo = tmp_path / "repo"
    ns = repo / ".nightshift"
    ns.mkdir(parents=True)
    entries = [
        {"session_id": "s1", "issue_id": "i1", "input_tokens": 1000,
         "output_tokens": 500, "cost_usd": 0.05, "model": "m", "step": "coder",
         "completed_at": "2025-01-01T10:00:00"},
    ]
    (ns / "usage.jsonl").write_text(
        "\n".join(json.dumps(e) for e in entries) + "\n")

    with patch("host.cli.repo_root", return_value=repo):
        cmd_usage(_make_args(issue_id=None, since="2025-06-01", until=None,
                             daily=False, all_projects=False))
    out = capsys.readouterr().out
    assert "No matching usage entries." in out


# ── cmd_accept cost summary ────────────────────────────────────────────────


@pytest.fixture
def accept_env(tmp_path):
    """Set up common mocks for cmd_accept tests."""
    repo = tmp_path / "repo"
    repo.mkdir()
    sessions = repo / ".nightshift" / "sessions"
    sid = "abc123def456"
    session_dir = sessions / sid
    session_dir.mkdir(parents=True)

    mock_config = MagicMock()
    mock_config.workspace.base_branch = "main"
    mock_config.workspace.root = "worktrees"

    return {
        "repo": repo,
        "sessions": sessions,
        "sid": sid,
        "session_dir": session_dir,
        "config": mock_config,
    }


def _run_cmd_accept(env, args):
    """Run cmd_accept with all heavy dependencies mocked out."""
    sid = env["sid"]
    with (
        patch("host.cli.repo_root", return_value=env["repo"]),
        patch("host.cli.resolve_session", return_value=sid),
        patch("host.cli.load_workflow", return_value=env["config"]),
        patch("host.cli._resolve_workflow", return_value="WORKFLOW.md"),
        patch("host.cli.resolve_merge_ref", return_value=f"agent/{sid}"),
        patch("host.cli.check_branch_not_behind_base", return_value=None),
        patch("host.cli.merge_with_rebase_fallback"),
        patch("host.cli.verify_no_conflict_markers"),
        patch("host.cli.archive_session"),
        patch("host.cli.remove_worktree"),
        patch("host.cli._cleanup_review_artifacts"),
        patch("host.cli.get_tracker_with_fallback", return_value=MagicMock()),
        patch("host.cli.sessions_dir", return_value=env["sessions"]),
        patch("subprocess.run", return_value=MagicMock(returncode=0)),
    ):
        cmd_accept(args)


def test_accept_prints_cost_summary(accept_env, capsys):
    """cmd_accept output includes 'Cost:' line when usage data exists in state.json."""
    state_data = {
        "issue_id": "issue-1",
        "branch": "agent/abc123def456",
        "status": "waiting:review",
        "step": 3,
        "usage": {
            "input_tokens": 45000,
            "output_tokens": 12000,
            "cost_usd": 0.38,
            "model": "claude-sonnet-4-6",
        },
    }
    (accept_env["session_dir"] / "state.json").write_text(json.dumps(state_data))

    _run_cmd_accept(accept_env, _make_args(issue_id="issue-1", workflow=None))

    out = capsys.readouterr().out
    assert "Cost:" in out
    assert "45K input" in out
    assert "12K output" in out
    assert "$0.38" in out
    assert "claude-sonnet-4-6" in out
    assert "3 resumes" in out


def test_accept_no_cost_when_no_usage(accept_env, capsys):
    """cmd_accept output does not include 'Cost:' when no usage data in state.json."""
    state_data = {
        "issue_id": "issue-1",
        "branch": "agent/abc123def456",
        "status": "waiting:review",
        "step": 0,
        "usage": {
            "input_tokens": 0,
            "output_tokens": 0,
            "cost_usd": 0.0,
            "model": "",
        },
    }
    (accept_env["session_dir"] / "state.json").write_text(json.dumps(state_data))

    _run_cmd_accept(accept_env, _make_args(issue_id="issue-1", workflow=None))

    out = capsys.readouterr().out
    assert "Cost:" not in out


# ── Sibling review session cleanup ─────────────────────────────────────────


def test_accept_cleans_sibling_review(tmp_path, capsys):
    """cmd_accept cleans up sibling review session when it exists."""
    repo, run = _init_repo(tmp_path)
    sid = "coder1234567"

    # Create coder session
    coder_session = repo / ".nightshift" / "sessions" / sid
    coder_session.mkdir(parents=True)
    (coder_session / "state.json").write_text(json.dumps({
        "status": "waiting:review", "step": 2, "issue_id": sid,
    }))

    # Create sibling review session
    review_session = repo / ".nightshift" / "sessions" / f"review-{sid}"
    review_session.mkdir(parents=True)
    (review_session / "state.json").write_text(json.dumps({
        "status": "waiting:review", "step": 1,
    }))
    (review_session / "conversation.jsonl").write_text("")

    (repo / "WORKFLOW.md").write_text(
        "---\n"
        "agent:\n  kind: claude-code\n"
        "tracker:\n  kind: git-bug\n"
        "workspace:\n  kind: worktree\n  base_branch: main\n  root: .worktrees\n"
        "---\nPrompt\n"
    )

    mock_tracker = MagicMock()

    with (
        patch("host.cli.repo_root", return_value=repo),
        patch("host.cli.resolve_session", return_value=sid),
        patch("host.cli.resolve_merge_ref", return_value=f"agent/{sid}"),
        patch("host.cli.check_branch_not_behind_base", return_value=None),
        patch("host.cli.merge_with_rebase_fallback"),
        patch("host.cli.verify_no_conflict_markers"),
        patch("host.cli.remove_worktree"),
        patch("host.cli.get_tracker_with_fallback", return_value=mock_tracker),
        patch("subprocess.run", return_value=MagicMock(returncode=0)),
    ):
        cmd_accept(_make_args(issue_id=sid, workflow=None))

    # Review session should be cleaned up
    assert not review_session.exists()
    out = capsys.readouterr().out
    assert f"Cleaned up review session for {sid}" in out


def test_revise_cleans_sibling_review(tmp_path, capsys):
    """cmd_revise cleans up sibling review session when revising from waiting:review."""
    repo, run = _init_repo(tmp_path)
    sid = "revisetst123"

    # Create coder session in waiting:review status
    coder_session = repo / ".nightshift" / "sessions" / sid
    coder_session.mkdir(parents=True)
    (coder_session / "state.json").write_text(json.dumps({
        "status": "waiting:review", "step": 3, "issue_id": sid,
    }))

    # Create sibling review session
    review_session = repo / ".nightshift" / "sessions" / f"review-{sid}"
    review_session.mkdir(parents=True)
    (review_session / "state.json").write_text(json.dumps({
        "status": "waiting:review", "step": 1,
    }))
    (review_session / "conversation.jsonl").write_text("")

    (repo / "WORKFLOW.md").write_text(
        "---\n"
        "agent:\n  kind: claude-code\n"
        "tracker:\n  kind: git-bug\n"
        "workspace:\n  kind: worktree\n  base_branch: main\n  root: .worktrees\n"
        "---\nPrompt\n"
    )

    mock_comment = MagicMock()
    mock_comment.author = "reviewer"
    mock_comment.body = "Please fix this issue"
    mock_tracker = MagicMock()
    mock_tracker.get_comments.return_value = [mock_comment]

    with (
        patch("host.cli.repo_root", return_value=repo),
        patch("host.cli.resolve_session", return_value=sid),
        patch("host.cli.get_tracker_with_fallback", return_value=mock_tracker),
        patch("host.cli.remove_worktree"),
        patch("subprocess.run"),
    ):
        cmd_revise(_make_args(
            issue_id=sid,
            workflow=str(repo / "WORKFLOW.md"),
            message=None,
        ))

    # Review session should be cleaned up
    assert not review_session.exists()
    out = capsys.readouterr().out
    assert f"Cleaned up review session for {sid}" in out


def test_revise_no_review_is_noop(tmp_path, capsys):
    """cmd_revise is a no-op when there's no sibling review session."""
    repo, run = _init_repo(tmp_path)
    sid = "noreview1234"

    # Create coder session only - no sibling review
    coder_session = repo / ".nightshift" / "sessions" / sid
    coder_session.mkdir(parents=True)
    (coder_session / "state.json").write_text(json.dumps({
        "status": "waiting:review", "step": 2, "issue_id": sid,
    }))

    (repo / "WORKFLOW.md").write_text(
        "---\n"
        "agent:\n  kind: claude-code\n"
        "tracker:\n  kind: git-bug\n"
        "workspace:\n  kind: worktree\n  base_branch: main\n  root: .worktrees\n"
        "---\nPrompt\n"
    )

    mock_comment = MagicMock()
    mock_comment.author = "reviewer"
    mock_comment.body = "Needs work"
    mock_tracker = MagicMock()
    mock_tracker.get_comments.return_value = [mock_comment]

    with (
        patch("host.cli.repo_root", return_value=repo),
        patch("host.cli.resolve_session", return_value=sid),
        patch("host.cli.get_tracker_with_fallback", return_value=mock_tracker),
        patch("host.cli.remove_worktree"),
        patch("subprocess.run"),
    ):
        cmd_revise(_make_args(
            issue_id=sid,
            workflow=str(repo / "WORKFLOW.md"),
            message=None,
        ))

    # Should not print "Cleaned up review session" message
    out = capsys.readouterr().out
    assert "Cleaned up review session" not in out
    # Coder session should still exist
    assert coder_session.exists()


# ── SSM state validation tests ─────────────────────────────────────────────


def test_accept_validates_state(tmp_path, capsys):
    """cmd_accept uses SSM to validate transition to 'accepted' state."""
    repo, run = _init_repo(tmp_path)
    sid = "validaccept1"

    # Create session in waiting:review (valid for accept)
    session_dir = repo / ".nightshift" / "sessions" / sid
    session_dir.mkdir(parents=True)
    (session_dir / "state.json").write_text(json.dumps({
        "status": "waiting:review", "step": 2, "issue_id": sid,
    }))

    (repo / "WORKFLOW.md").write_text(
        "---\n"
        "agent:\n  kind: claude-code\n"
        "tracker:\n  kind: git-bug\n"
        "workspace:\n  kind: worktree\n  base_branch: main\n  root: .worktrees\n"
        "---\nPrompt\n"
    )

    mock_tracker = MagicMock()

    with (
        patch("host.cli.repo_root", return_value=repo),
        patch("host.cli.resolve_session", return_value=sid),
        patch("host.cli.resolve_merge_ref", return_value=f"agent/{sid}"),
        patch("host.cli.check_branch_not_behind_base", return_value=None),
        patch("host.cli.merge_with_rebase_fallback"),
        patch("host.cli.verify_no_conflict_markers"),
        patch("host.cli.remove_worktree"),
        patch("host.cli.get_tracker_with_fallback", return_value=mock_tracker),
        patch("subprocess.run", return_value=MagicMock(returncode=0)),
    ):
        cmd_accept(_make_args(issue_id=sid, workflow=None))

    # Should complete successfully
    out = capsys.readouterr().out
    assert "Accepted" in out


def test_accept_from_wrong_state_fails_with_message(tmp_path, capsys):
    """cmd_accept fails with clear message when called from invalid state."""
    repo, run = _init_repo(tmp_path)
    sid = "wrongstate12"

    # Create session in 'working' (cannot accept from working)
    session_dir = repo / ".nightshift" / "sessions" / sid
    session_dir.mkdir(parents=True)
    (session_dir / "state.json").write_text(json.dumps({
        "status": "working", "step": 2, "issue_id": sid,
    }))

    (repo / "WORKFLOW.md").write_text(
        "---\n"
        "agent:\n  kind: claude-code\n"
        "tracker:\n  kind: git-bug\n"
        "workspace:\n  kind: worktree\n  base_branch: main\n  root: .worktrees\n"
        "---\nPrompt\n"
    )

    with (
        patch("host.cli.repo_root", return_value=repo),
        patch("host.cli.resolve_session", return_value=sid),
        pytest.raises(SystemExit) as exc_info,
    ):
        cmd_accept(_make_args(issue_id=sid, workflow=None))

    assert exc_info.value.code == 1
    err = capsys.readouterr().err
    assert "working" in err
    assert "accepted" in err.lower()


def test_reject_validates_state(tmp_path, capsys):
    """cmd_reject validates transition to 'rejected' state via SSM."""
    repo, run = _init_repo(tmp_path)
    sid = "wrongreject1"

    # Create session in 'starting' (cannot reject from starting)
    session_dir = repo / ".nightshift" / "sessions" / sid
    session_dir.mkdir(parents=True)
    (session_dir / "state.json").write_text(json.dumps({
        "status": "starting", "step": 0, "issue_id": sid,
    }))

    (repo / "WORKFLOW.md").write_text(
        "---\n"
        "agent:\n  kind: claude-code\n"
        "tracker:\n  kind: git-bug\n"
        "workspace:\n  kind: worktree\n  base_branch: main\n  root: .worktrees\n"
        "---\nPrompt\n"
    )

    with (
        patch("host.cli.repo_root", return_value=repo),
        patch("host.cli.resolve_session", return_value=sid),
        pytest.raises(SystemExit) as exc_info,
    ):
        cmd_reject(_make_args(issue_id=sid, workflow=None))

    assert exc_info.value.code == 1
    err = capsys.readouterr().err
    assert "starting" in err
    assert "rejected" in err.lower()


def test_reject_suspended_session(tmp_path, capsys, monkeypatch):
    """cmd_reject should succeed from suspended:max-resumes (and other suspended states)."""
    repo, _ = _init_repo(tmp_path)
    sid = "suspendedreject"
    env = _clean_git_env()
    env["GIT_AUTHOR_NAME"] = "Test"
    env["GIT_AUTHOR_EMAIL"] = "test@test.com"
    env["GIT_COMMITTER_NAME"] = "Test"
    env["GIT_COMMITTER_EMAIL"] = "test@test.com"

    session_dir = repo / ".nightshift" / "sessions" / sid
    session_dir.mkdir(parents=True)
    (session_dir / "state.json").write_text(json.dumps({
        "status": "suspended:max-resumes", "step": 10, "issue_id": sid,
        "branch": f"agent/{sid}", "started_at": "2025-01-01T00:00:00Z",
    }))

    (repo / "WORKFLOW.md").write_text(
        "---\n"
        "agent:\n  kind: claude-code\n"
        "tracker:\n  kind: git-bug\n"
        "workspace:\n  kind: worktree\n  base_branch: main\n  root: .worktrees\n"
        "---\nPrompt\n"
    )

    worktree_dir = repo / ".worktrees" / f"agent-{sid}"
    worktree_dir.mkdir(parents=True)
    subprocess.run(
        ["git", "worktree", "add", str(worktree_dir), "-b", f"agent/{sid}"],
        cwd=str(repo), env=env, check=True,
    )

    with (
        patch("host.cli.repo_root", return_value=repo),
        patch("host.cli.resolve_session", return_value=sid),
        patch("host.cli.get_tracker_with_fallback") as mock_tracker,
    ):
        mock_tracker.return_value.set_status = lambda *a: None
        mock_tracker.return_value.add_comment = lambda *a: None
        cmd_reject(_make_args(issue_id=sid, workflow=None))

    out = capsys.readouterr().out
    assert "Rejected and cleaned up" in out


def test_resume_validates_state(tmp_path, capsys):
    """cmd_resume validates that session can transition to 'working'."""
    repo, run = _init_repo(tmp_path)
    sid = "badresume123"

    # Create session in 'accepted' (terminal state, cannot resume)
    session_dir = repo / ".nightshift" / "sessions" / sid
    session_dir.mkdir(parents=True)
    (session_dir / "state.json").write_text(json.dumps({
        "status": "accepted", "step": 5, "issue_id": sid,
    }))

    (repo / "WORKFLOW.md").write_text(
        "---\n"
        "agent:\n  kind: claude-code\n"
        "tracker:\n  kind: git-bug\n"
        "workspace:\n  kind: worktree\n  base_branch: main\n  root: .worktrees\n"
        "---\nPrompt\n"
    )

    with (
        patch("host.cli.repo_root", return_value=repo),
        patch("host.cli.resolve_session", return_value=sid),
        patch("host.cli._resolve_workflow", return_value=repo / "WORKFLOW.md"),
        pytest.raises(SystemExit) as exc_info,
    ):
        cmd_resume(_make_args(issue_id=sid, workflow=None))

    assert exc_info.value.code == 1
    err = capsys.readouterr().err
    assert "accepted" in err
    # Should mention it's a terminal state or cannot resume
    assert "terminal" in err.lower() or "cannot" in err.lower() or "resume" in err.lower()



# ── cmd_blocked ─────────────────────────────────────────────────────────────


def test_blocked_command_lists_blocked_issues(tmp_path, capsys):
    """cmd_blocked should list issues with blocked:<id> labels."""
    repo, run = _init_repo(tmp_path)

    (repo / "WORKFLOW.md").write_text(
        "---\n"
        "agent:\n  kind: claude-code\n"
        "tracker:\n  kind: git-bug\n"
        "workspace:\n  kind: worktree\n  base_branch: main\n  root: .worktrees\n"
        "---\nPrompt\n"
    )

    blocked_issue = TrackerIssue(
        id="blocked123456",
        identifier="blocked12345",
        title="Issue blocked by dependency",
        body="",
        status="open",
        labels=["nightshift", "blocked:dep123456"],
    )
    unblocked_issue = TrackerIssue(
        id="unblocked1234",
        identifier="unblocked123",
        title="Issue not blocked",
        body="",
        status="open",
        labels=["nightshift"],
    )

    mock_tracker = MagicMock()
    mock_tracker.list_issues.return_value = [blocked_issue, unblocked_issue]

    with (
        patch("host.cli.repo_root", return_value=repo),
        patch("host.cli.get_tracker_with_fallback", return_value=mock_tracker),
    ):
        cmd_blocked(_make_args(workflow=None))

    out = capsys.readouterr().out
    # Should show blocked issue
    assert "blocked12345" in out
    assert "dep123456" in out
    # Should NOT show unblocked issue
    assert "unblocked" not in out


def test_blocked_command_empty(tmp_path, capsys):
    """cmd_blocked shows message when no issues are blocked."""
    repo, run = _init_repo(tmp_path)

    (repo / "WORKFLOW.md").write_text(
        "---\n"
        "agent:\n  kind: claude-code\n"
        "tracker:\n  kind: git-bug\n"
        "workspace:\n  kind: worktree\n  base_branch: main\n  root: .worktrees\n"
        "---\nPrompt\n"
    )

    mock_tracker = MagicMock()
    mock_tracker.list_issues.return_value = []

    with (
        patch("host.cli.repo_root", return_value=repo),
        patch("host.cli.get_tracker_with_fallback", return_value=mock_tracker),
    ):
        cmd_blocked(_make_args(workflow=None))

    out = capsys.readouterr().out
    assert "No blocked issues" in out


# ── accept unblocks dependents ──────────────────────────────────────────────


def test_accept_unblocks_dependents(tmp_path, capsys):
    """cmd_accept should remove blocked:<accepted-id> labels from other issues."""
    repo, run = _init_repo(tmp_path)
    sid = "accepted1234"

    # Create session
    session_dir = repo / ".nightshift" / "sessions" / sid
    session_dir.mkdir(parents=True)
    (session_dir / "state.json").write_text(json.dumps({
        "status": "waiting:review", "step": 2, "issue_id": sid,
    }))

    (repo / "WORKFLOW.md").write_text(
        "---\n"
        "agent:\n  kind: claude-code\n"
        "tracker:\n  kind: git-bug\n"
        "workspace:\n  kind: worktree\n  base_branch: main\n  root: .worktrees\n"
        "---\nPrompt\n"
    )

    # Issue that was blocked by the one being accepted
    blocked_issue = TrackerIssue(
        id="dependent1234",
        identifier="dependent123",
        title="Dependent issue",
        body="",
        status="open",
        labels=["nightshift", "blocked:accepted1234"],
    )

    mock_tracker = MagicMock()
    mock_tracker.list_issues.return_value = [blocked_issue]

    with (
        patch("host.cli.repo_root", return_value=repo),
        patch("host.cli.resolve_session", return_value=sid),
        patch("host.cli.resolve_merge_ref", return_value=f"agent/{sid}"),
        patch("host.cli.check_branch_not_behind_base", return_value=None),
        patch("host.cli.merge_with_rebase_fallback"),
        patch("host.cli.verify_no_conflict_markers"),
        patch("host.cli.remove_worktree"),
        patch("host.cli.get_tracker_with_fallback", return_value=mock_tracker),
        patch("subprocess.run", return_value=MagicMock(returncode=0)),
    ):
        cmd_accept(_make_args(issue_id=sid, workflow=None))

    # Should have removed the blocked label
    mock_tracker.remove_label.assert_called_once_with(
        "dependent1234", "blocked:accepted1234"
    )
    out = capsys.readouterr().out
    assert "Unblocked dependent123" in out


def test_unblock_dependents_direct():
    """Test _unblock_dependents helper directly."""
    blocked_issue = TrackerIssue(
        id="dependent1234",
        identifier="dependent123",
        title="Dependent issue",
        body="",
        status="open",
        labels=["nightshift", "blocked:closed123456"],  # 12-char prefix
    )
    unrelated_issue = TrackerIssue(
        id="unrelated1234",
        identifier="unrelated123",
        title="Unrelated issue",
        body="",
        status="open",
        labels=["nightshift", "blocked:other1234567"],  # Different prefix
    )

    mock_tracker = MagicMock()
    mock_tracker.list_issues.return_value = [blocked_issue, unrelated_issue]

    _unblock_dependents(mock_tracker, "closed12345678")  # Full ID, truncated to 12

    # Should only remove the matching label
    mock_tracker.remove_label.assert_called_once_with(
        "dependent1234", "blocked:closed123456"
    )


def test_unblock_dependents_short_prefix():
    """Short blocked prefixes should still be removed when the issue closes."""
    blocked_issue = TrackerIssue(
        id="dependent1234",
        identifier="dependent123",
        title="Dependent issue",
        body="",
        status="open",
        labels=["nightshift", "blocked:abc1234"],
    )

    mock_tracker = MagicMock()
    mock_tracker.list_issues.return_value = [blocked_issue]

    _unblock_dependents(mock_tracker, "abc1234567890")

    mock_tracker.remove_label.assert_called_once_with(
        "dependent1234", "blocked:abc1234"
    )


def test_unblock_dependents_any_prefix_length():
    """All matching blocked prefixes should be removed, regardless of length."""
    blocked_issue = TrackerIssue(
        id="dependent1234",
        identifier="dependent123",
        title="Dependent issue",
        body="",
        status="open",
        labels=[
            "nightshift",
            "blocked:abc12",
            "blocked:abc1234",
            "blocked:abc123456789",
        ],
    )

    mock_tracker = MagicMock()
    mock_tracker.list_issues.return_value = [blocked_issue]

    _unblock_dependents(mock_tracker, "abc1234567890")

    assert mock_tracker.remove_label.call_args_list == [
        call("dependent1234", "blocked:abc12"),
        call("dependent1234", "blocked:abc1234"),
        call("dependent1234", "blocked:abc123456789"),
    ]
