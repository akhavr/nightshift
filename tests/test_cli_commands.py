"""Tests for CLI commands: status, answer, history, init, revise, cleanup."""

import json
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch, MagicMock, call

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from host.cli import (
    cmd_status,
    cmd_answer,
    cmd_history,
    cmd_init,
    cmd_revise,
    cmd_cleanup,
    _read_issue_title,
    _truncate_title,
    _format_history_line,
)


# ── helpers ──────────────────────────────────────────────────────────────────


def _make_args(**kwargs):
    """Create a simple namespace-like args object."""
    args = MagicMock()
    for k, v in kwargs.items():
        setattr(args, k, v)
    return args


def _init_repo(tmp_path):
    """Create a git repo with an initial commit on main."""
    repo = tmp_path / "repo"
    repo.mkdir()
    run = lambda *args: subprocess.run(
        args, cwd=str(repo), capture_output=True, text=True
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
