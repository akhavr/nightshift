#!/usr/bin/env python3
"""CLI — reads WORKFLOW.md for config, delegates to launch.py and watcher."""

import argparse
import datetime
import json
import logging
import os
import shutil
import subprocess
import sys
import time
from collections import defaultdict
from dataclasses import asdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from core.config import load_workflow
from core.state_machine import SessionStateMachine, TERMINAL_STATES
from core.post_run import format_cost_line, format_token_count
from core.protocols import UsageData
from host.tracker_client import get_tracker_with_fallback
from core.review import collect_review_feedback, build_revise_prompt
from host.constants import (
    SHORT_ID_LEN, REVIEW_SESSION_PREFIX, LOG_PREVIEW_LEN,
    HISTORY_FOLLOW_POLL_S, OVERFLOW_FLAG_FILENAME, USAGE_LOG_FILENAME,
    BLOCKED_LABEL_PREFIX,
)
from core.upgrade import (
    read_template_version, get_canonical_version,
    get_canonical_review_version,
    diff_prompt_sections, apply_upgrade,
    CANONICAL_TEMPLATE, CANONICAL_REVIEW_TEMPLATE,
    load_canonical_template, load_canonical_review_template,
)
from core.upstream import (
    diff_reverse, detect_operation, validate_proposal,
    validate_line_count, build_proposal,
    count_prompt_lines, UpstreamProposal,
    PROMPT_SOFT_CAP_LINES, PROMPT_HARD_CAP_LINES,
)
from core.training_export import extract_training_data, export_jsonl
from host.config_discovery import discover_workflow as _discover_workflow, write_local_config
from host.env import load_all_dotenv
from host.docker_utils import docker_stop
from host.merge import (
    resolve_merge_ref,
    merge_with_rebase_fallback, verify_no_conflict_markers,
    check_branch_not_behind_base,
)
from host.git_utils import audit_worktree_symlinks
from host.rebase import sanitize_git_config
from host.session_utils import (
    archive_session,
    clear_completed_at,
    get_repo_root,
    read_state, write_state, update_status,
    force_remove_dir, remove_worktree,
)


def _validate_transition(sid: str, target_state: str) -> None:
    """Validate that SSM allows transition to target_state. Exits on invalid."""
    session_dir = repo_root() / ".nightshift" / "sessions" / sid
    if not session_dir.exists():
        return  # Let the command handle missing sessions
    try:
        state = read_state(session_dir)
    except Exception as e:
        logging.warning("Could not read state for session %s: %s", sid[:12], e)
        return  # Let the command handle corrupt state
    current = state.get("status", "starting")
    ssm = SessionStateMachine(initial_state=current)
    if not ssm.can_transition(target_state):
        if current in TERMINAL_STATES:
            print(f"Cannot {target_state} session '{sid[:12]}': already in terminal state '{current}'",
                  file=sys.stderr)
        else:
            print(f"Cannot transition from '{current}' to '{target_state}' for session '{sid[:12]}'",
                  file=sys.stderr)
        sys.exit(1)


def repo_root() -> Path:
    return get_repo_root()


def sessions_dir() -> Path:
    return repo_root() / ".nightshift" / "sessions"


def resolve_session(issue_id: str) -> str:
    """Resolve a prefix to the full session ID. Exits on ambiguity or no match."""
    sd = sessions_dir()
    if not sd.exists():
        print("No sessions directory found", file=sys.stderr)
        sys.exit(1)
    # Strip known prefix before truncating so the actual ID gets full SHORT_ID_LEN chars
    if issue_id.startswith(REVIEW_SESSION_PREFIX):
        bare_id = issue_id[len(REVIEW_SESSION_PREFIX):]
        match_prefix = REVIEW_SESSION_PREFIX + bare_id[:SHORT_ID_LEN]
    else:
        match_prefix = issue_id[:SHORT_ID_LEN]
    matches = [d.name for d in sd.iterdir() if d.is_dir() and d.name.startswith(match_prefix)]
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        print(f"Ambiguous ID '{issue_id}' matches: {', '.join(matches)}", file=sys.stderr)
        sys.exit(1)
    # No match by session dir — return truncated (for start/new sessions)
    return match_prefix


def _resolve_workflow(a) -> Path:
    """Resolve workflow path using discovery order."""
    return _discover_workflow(repo_root(), getattr(a, "workflow", None))


def _parse_bug_new_args(tracker_args: list[str]) -> tuple[str, str] | None:
    """Extract title/body from `bug new` args or return None for fallback."""
    if tracker_args[:2] != ["bug", "new"]:
        return None

    title = None
    body = None
    idx = 2
    while idx < len(tracker_args):
        arg = tracker_args[idx]
        if arg in ("-t", "--title") and idx + 1 < len(tracker_args):
            title = tracker_args[idx + 1]
            idx += 2
            continue
        if arg in ("-m", "--message") and idx + 1 < len(tracker_args):
            body = tracker_args[idx + 1]
            idx += 2
            continue
        if arg == "--non-interactive":
            idx += 1
            continue
        return None
    if title is None or body is None:
        return None
    return title, body


def _create_issue_via_tracker(tracker, title: str, body: str) -> str:
    """Use a first-class tracker method when available, else fall back to CLI."""
    create_issue = vars(tracker).get("create_issue")
    if not callable(create_issue):
        create_issue = getattr(type(tracker), "create_issue", None)
    if callable(create_issue):
        if getattr(type(tracker), "create_issue", None) is create_issue:
            return create_issue(tracker, title, body)
        return create_issue(title, body)
    return tracker.run_raw("bug", "new", "-t", title, "-m", body)


ISSUE_STATUS_WIDTH = 6
ISSUE_TITLE_MAX_LEN = 60


def _is_tty() -> bool:
    """Return whether stdout is an interactive terminal."""
    return sys.stdout.isatty()


def _format_issue_human(issue) -> str:
    """Render a tracker issue as a single human-readable line."""
    short_id = issue.id[:SHORT_ID_LEN]
    title = issue.title[:ISSUE_TITLE_MAX_LEN]
    return f"{short_id} {issue.status:<{ISSUE_STATUS_WIDTH}} {title}"


def _format_issue_list_human(issues: list) -> str:
    """Render tracker issue lists as human-readable lines."""
    return "\n".join(_format_issue_human(issue) for issue in issues)


def _format_issue_output(issue) -> str:
    """Render a tracker issue for CLI output."""
    if issue is None:
        return ""
    if _is_tty():
        return _format_issue_human(issue)
    return json.dumps(asdict(issue), indent=2)


def _format_issue_list_output(issues: list) -> str:
    """Render tracker issue lists for CLI output."""
    if _is_tty():
        return _format_issue_list_human(issues)
    return json.dumps([asdict(issue) for issue in issues], indent=2)


def _parse_bug_status_filter(tracker_args: list[str]) -> str | None:
    """Extract a status filter from `bug` and `bug ls` forms."""
    if not tracker_args or tracker_args[0] != "bug":
        return None

    args = tracker_args[1:]
    if args and args[0] == "ls":
        args = args[1:]

    if len(args) != 2:
        return None

    flag, status = args
    if flag not in ("-s", "--status"):
        return None
    return status


def _parse_bug_list_filters(tracker_args: list[str]) -> dict | None:
    """Extract filters from `bug` and `bug ls` forms.

    Returns a dict with keys: status (str|None), label (str|None), all (bool).
    Returns None if args don't match a list command pattern.
    """
    if not tracker_args or tracker_args[0] != "bug":
        return None

    args = tracker_args[1:]
    if args and args[0] == "ls":
        args = args[1:]

    filters = {"status": None, "label": None, "all": False}
    idx = 0
    while idx < len(args):
        arg = args[idx]
        if arg in ("-s", "--status") and idx + 1 < len(args):
            filters["status"] = args[idx + 1]
            idx += 2
            continue
        if arg in ("-l", "--label") and idx + 1 < len(args):
            filters["label"] = args[idx + 1]
            idx += 2
            continue
        if arg in ("-a", "--all"):
            filters["all"] = True
            idx += 1
            continue
        return None

    return filters


def _dispatch_bug_command(tracker, tracker_args: list[str]) -> str | None:
    """Route common bug commands to tracker methods when possible."""
    if tracker_args == ["bug"] or tracker_args == ["bug", "ls"]:
        return _format_issue_list_output(tracker.list_issues())

    filters = _parse_bug_list_filters(tracker_args)
    if filters is not None:
        issues = tracker.list_issues(status=filters["status"])
        if filters["label"]:
            issues = [i for i in issues if filters["label"] in (i.labels or [])]
        return _format_issue_list_output(issues)

    if len(tracker_args) == 3 and tracker_args[:2] == ["bug", "show"]:
        return _format_issue_output(tracker.get_issue(tracker_args[2]))

    if len(tracker_args) == 5 and tracker_args[:3] == ["bug", "label", "new"]:
        tracker.add_label(tracker_args[3], tracker_args[4])
        return ""

    if len(tracker_args) == 5 and tracker_args[:3] == ["bug", "label", "rm"]:
        tracker.remove_label(tracker_args[3], tracker_args[4])
        return ""

    if len(tracker_args) == 4 and tracker_args[:2] == ["bug", "status"]:
        status_map = {"open": "open", "close": "closed"}
        status = status_map.get(tracker_args[2])
        if status is None:
            return None
        tracker.set_status(tracker_args[3], status)
        return ""

    if len(tracker_args) >= 5 and tracker_args[:3] == ["bug", "comment", "new"]:
        issue_id = tracker_args[3]
        rest = tracker_args[4:]
        if rest and rest[0] in ("-m", "--message") and len(rest) >= 2:
            message = rest[1]
        else:
            message = " ".join(rest)
        tracker.add_comment(issue_id, message)
        return ""

    return None


def _build_resume_launch_cmd(issue_id: str, workflow_override: str | None = None) -> list[str]:
    """Build the launch.py resume command for coder and review sessions."""
    sid = resolve_session(issue_id)
    is_review = sid.startswith(REVIEW_SESSION_PREFIX)
    launch_issue_id = sid[len(REVIEW_SESSION_PREFIX):] if is_review else sid
    wf = (repo_root() / "REVIEW.md").resolve() if is_review else _discover_workflow(
        repo_root(), workflow_override
    )

    cmd = [sys.executable, str(Path(__file__).parent / "launch.py"), launch_issue_id, "--resume"]
    if is_review:
        cmd += ["--step", "review"]
    cmd += ["--workflow", str(wf)]
    return cmd


def cmd_start(a):
    wf = _resolve_workflow(a)
    cmd = [sys.executable, str(Path(__file__).parent / "launch.py"), a.issue_id]
    if a.max_turns:
        cmd += ["--max-turns", str(a.max_turns)]
    cmd += ["--workflow", str(wf)]
    subprocess.run(cmd)


def cmd_resume(a):
    sid = resolve_session(a.issue_id)
    _validate_transition(sid, "working")
    subprocess.run(_build_resume_launch_cmd(a.issue_id, getattr(a, "workflow", None)))


def cmd_answer(a):
    """Write answer.txt directly — works even if container is paused."""
    sid = resolve_session(a.issue_id)
    sd = sessions_dir() / sid
    if sd.exists():
        (sd / "answer.txt").write_text(a.message)
        print(f"Answer written for {sid}")
    else:
        print(f"No session found for {sid}", file=sys.stderr)


def _build_review_launch_cmd(sid: str) -> list[str]:
    """Build the launch.py command for a manual review session."""
    review_md = (repo_root() / "REVIEW.md").resolve()
    return [
        sys.executable,
        str(Path(__file__).parent / "launch.py"),
        sid,
        "--workflow",
        str(review_md),
        "--step",
        "review",
        "--coder-session",
        sid,
    ]


def cmd_review(a):
    """Manually launch a review session for a coder waiting on review."""
    sid = resolve_session(a.issue_id)
    sd = sessions_dir() / sid
    if not sd.exists() or not (sd / "state.json").exists():
        print(f"No session found for {sid}", file=sys.stderr)
        sys.exit(1)

    state = read_state(sd)
    status = state.get("status")
    if status != "waiting:review":
        print(f"Session {sid} is not waiting:review (status: {status})",
              file=sys.stderr)
        sys.exit(1)

    subprocess.run(_build_review_launch_cmd(sid))


def cmd_watcher(a):
    wf = _resolve_workflow(a)
    print(f"Using workflow: {wf}")
    log_file = repo_root() / ".nightshift" / "watcher.log"
    log_file.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        sys.executable, "-m", "host.watcher",
        "--sessions-dir", str(sessions_dir()),
        "--log-file", str(log_file),
        "--workflow", str(wf),
    ]
    if a.no_auto_start:
        cmd.append("--no-auto-start")
    print(f"Logging to {log_file}")
    # host.watcher uses absolute imports — ensure agent-worker root is on PYTHONPATH
    env = {**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parent.parent)}
    # Use os.execvpe to replace this process with the watcher, so signals
    # (SIGTERM/SIGINT) reach the watcher directly — no parent to orphan it.
    os.execvpe(cmd[0], cmd, env)


def cmd_watchdog(a):
    """Run the global watchdog to monitor multiple watcher instances."""
    from host.watchdog.main import main as watchdog_main
    args = []
    if a.list_watchers:
        args.append("--list")
    elif a.check:
        args.append("--check")
        if a.no_alerts:
            args.append("--no-alerts")
    if a.verbose:
        args.append("--verbose")
    if a.config:
        args.extend(["--config", str(a.config)])
    sys.exit(watchdog_main(args))


TITLE_MAX_LEN = 40


def _read_issue_title(session_dir: Path, state: dict | None = None) -> str:
    """Read issue title from state dict/state.json or fall back to issue.json."""
    if state is None:
        try:
            state = json.loads((session_dir / "state.json").read_text())
        except Exception as e:
            logging.debug("Failed to read state.json for title in %s: %s", session_dir, e)
            state = {}
    title = state.get("issue_title", "")
    if title:
        return title
    try:
        issue = json.loads((session_dir / "issue.json").read_text())
        return issue.get("title", "")
    except Exception as e:
        logging.debug("Failed to read issue.json for title in %s: %s", session_dir, e)
        return ""


def _truncate_title(title: str, max_len: int = TITLE_MAX_LEN) -> str:
    """Truncate title to max_len, adding ellipsis if needed."""
    if len(title) <= max_len:
        return title
    return title[:max_len - 1] + "\u2026"


def cmd_status(a):
    sd = sessions_dir()
    overflow_flag = _overflow_flag_path()
    overflow_active = overflow_flag.exists()
    if overflow_active:
        profile_name = _read_overflow_profile_name(overflow_flag)
        if profile_name:
            print(f"Overflow: ON (profile: {profile_name})")
        else:
            print("Overflow: ON")
    if not sd.exists():
        print("No sessions."); return
    print(f"{'SESSION':<14} {'STATUS':<26} {'STEP':>5} {'CPS':>4}  {'TITLE'}")
    for f in sorted(sd.glob("*/state.json")):
        sid = f.parent.name
        try:
            s = json.loads(f.read_text())
            title = _truncate_title(_read_issue_title(f.parent, state=s))
            print(f"{sid:<14} {s.get('status','?'):<26} "
                  f"{s.get('step',0):>5} {len(s.get('checkpoints',[])):>4}"
                  f"  {title}")
        except Exception as e:
            logging.error("Failed reading session status from %s: %s", f, e)
            print(f"{sid:<14} {'<error>':<26}")


def cmd_blocked(a):
    """List issues blocked by dependencies."""
    r = repo_root()
    config = load_workflow(_resolve_workflow(a))
    try:
        tracker = get_tracker_with_fallback(config, r)
        issues = tracker.list_issues(status="open")
    except Exception as e:
        print(f"Failed to fetch issues: {e}", file=sys.stderr)
        return

    # Collect issues with blocked labels
    blocked_issues = []
    for issue in issues:
        blocked_by = [l[len(BLOCKED_LABEL_PREFIX):]
                      for l in issue.labels if l.startswith(BLOCKED_LABEL_PREFIX)]
        if blocked_by:
            blocked_issues.append((issue, blocked_by))

    if not blocked_issues:
        print("No blocked issues.")
        return

    print(f"{'ISSUE':<14} {'BLOCKED BY':<14} TITLE")
    for issue, blockers in blocked_issues:
        title = issue.title[:50] if len(issue.title) > 50 else issue.title
        for i, blocker in enumerate(blockers):
            if i == 0:
                print(f"{issue.identifier:<14} {blocker:<14} {title}")
            else:
                print(f"{'':<14} {blocker:<14}")


def cmd_logs(a):
    log_file = sessions_dir() / resolve_session(a.issue_id) / "raw-output.log"
    if not log_file.exists():
        print("No log file.", file=sys.stderr); return
    subprocess.run(["tail", "-f", str(log_file)])


_HISTORY_ICONS = {
    "thought": "💭", "checkpoint": "📌", "question": "❓",
    "human_answer_sent": "👤", "tool_call": "🔧", "tool_result": "📄",
    "system": "⚙️", "user": "📝",
}


def _format_history_line(line):
    """Parse a single JSONL line and return a formatted string, or None on error."""
    try:
        e = json.loads(line)
        return (f"  {e['timestamp'][:19]}  {_HISTORY_ICONS.get(e['role'], '•')} "
                f"[{e['role']}] {e['content'][:LOG_PREVIEW_LEN * 2]}")
    except Exception as exc:
        logging.debug("Skipping malformed history line: %s", exc)
        return None


def cmd_history(a):
    cf = sessions_dir() / resolve_session(a.issue_id) / "conversation.jsonl"
    if not cf.exists():
        print("No history.", file=sys.stderr); return
    for line in cf.read_text().strip().splitlines():
        formatted = _format_history_line(line)
        if formatted:
            print(formatted)
    if not getattr(a, "follow", False):
        return
    try:
        with open(cf, "r") as fh:
            fh.seek(0, 2)  # seek to end
            while True:
                new_line = fh.readline()
                if new_line:
                    formatted = _format_history_line(new_line.strip())
                    if formatted:
                        print(formatted, flush=True)
                else:
                    time.sleep(HISTORY_FOLLOW_POLL_S)
    except KeyboardInterrupt:
        pass


DEFAULT_REVIEW_MD = """\
---
agent:
  kind: claude-code
  max_turns: 30

review:
  max_rounds: 3
---

You are a code reviewer. Review the following changes for the issue:

**Title:** {{ issue.title }}
**Description:**
{{ issue.body }}

**Diff to review:**
```
{{ diff }}
```

**Base branch:** {{ base_branch }}
**Agent branch:** {{ agent_branch }}

RULES:
1. Read the diff carefully. Check for correctness, security, and style.
2. Run tests if available.
3. For every observation: @@LOG@@ <your observation>
4. After reviewing: @@CHECKPOINT@@ <summary of findings>
5. If the code is acceptable: post @nightshift approve
6. If changes are needed: explain what needs fixing, then post @nightshift revise
7. When done: @@DONE@@

Begin by reading the diff and understanding the changes.
"""

DEFAULT_ENV_EXAMPLE = """\
# Nightshift environment variables
# Copy this file to .env and fill in your values.

# Telegram notifications (optional)
TELEGRAM_BOT_TOKEN=
TELEGRAM_CHAT_ID=

# Anthropic API key (if not using Claude Code OAuth)
# ANTHROPIC_API_KEY=

# GitHub token (for GitHub Issues tracker)
# GITHUB_TOKEN=
"""


def _detect_default_branch(repo: Path) -> str:
    """Detect the default branch (main, master, etc.)."""
    result = subprocess.run(
        ["git", "symbolic-ref", "refs/remotes/origin/HEAD"],
        capture_output=True, text=True, cwd=str(repo),
    )
    if result.returncode == 0:
        return result.stdout.strip().split("/")[-1]
    result = subprocess.run(
        ["git", "branch", "--show-current"],
        capture_output=True, text=True, cwd=str(repo),
    )
    if result.returncode == 0 and result.stdout.strip():
        return result.stdout.strip()
    return "main"


def _scaffold_file(path: Path, content: str, force: bool, label: str = ""):
    """Write a scaffold file, skipping if it exists and force is False."""
    if path.exists() and not force:
        print(f"{path.name} already exists at {path}. Use --force to overwrite.")
        return
    path.write_text(content)
    msg = f"Created {path}"
    if label:
        msg += f" ({label})"
    print(msg)


def _update_gitignore(root: Path):
    """Ensure .env, .worktrees/, .nightshift/ are in .gitignore."""
    gitignore = root / ".gitignore"
    lines = gitignore.read_text().splitlines() if gitignore.exists() else []
    entries_to_add = [e for e in [".env", ".worktrees/", ".nightshift/"] if e not in lines]
    if entries_to_add:
        with gitignore.open("a") as f:
            if lines and lines[-1] != "":
                f.write("\n")
            f.write("# Nightshift\n")
            for entry in entries_to_add:
                f.write(entry + "\n")
        print(f"Added {', '.join(entries_to_add)} to .gitignore")
    else:
        print(".gitignore already has nightshift entries")


def _install_pre_commit_hook(root: Path, force: bool = False):
    """Install pre-commit hook to reject conflict markers."""
    hook_src = Path(__file__).resolve().parent.parent / "hooks" / "pre-commit"
    if not hook_src.exists():
        print(f"Warning: pre-commit hook source not found at {hook_src}", file=sys.stderr)
        return

    hook_dst = root / ".git" / "hooks" / "pre-commit"
    if hook_dst.exists() and not force:
        print(f"Pre-commit hook already exists at {hook_dst}. Use --force to overwrite.")
        return

    hook_dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy(hook_src, hook_dst)
    hook_dst.chmod(0o755)
    print(f"Installed pre-commit hook (rejects conflict markers)")


def cmd_init(a):
    """Scaffold WORKFLOW.md and .env.example in the current repo."""
    try:
        root = repo_root()
    except subprocess.CalledProcessError:
        print("Not inside a git repository.", file=sys.stderr)
        sys.exit(1)

    default_branch = _detect_default_branch(root)
    workflow_content = load_canonical_template(default_branch)

    workflow_path = Path(a.workflow_path).expanduser().resolve() if a.workflow_path else root / "WORKFLOW.md"
    workflow_path.parent.mkdir(parents=True, exist_ok=True)

    # If WORKFLOW.md already exists and is behind, hint about upgrade
    if workflow_path.exists() and not a.force:
        existing_version = read_template_version(workflow_path.read_text())
        canonical_version = get_canonical_version()
        if existing_version < canonical_version:
            print(f"Hint: {workflow_path.name} template is behind "
                  f"(v{existing_version} < v{canonical_version}). "
                  f"Run `nightshift upgrade` to see prompt updates.")

    _scaffold_file(workflow_path, workflow_content, a.force, f"base_branch: {default_branch}")

    # If custom workflow path was specified, write .nightshift.yaml pointer
    if a.workflow_path:
        config_file = write_local_config(root, str(workflow_path))
        print(f"Created {config_file} (points to {workflow_path})")

    review_path = root / "REVIEW.md"
    if review_path.exists() and not a.force:
        existing_rv = read_template_version(review_path.read_text())
        canonical_rv = get_canonical_review_version()
        if existing_rv < canonical_rv:
            print(f"Hint: REVIEW.md template is behind "
                  f"(v{existing_rv} < v{canonical_rv}). "
                  f"Run `nightshift upgrade` to see prompt updates.")

    review_content = load_canonical_review_template() or DEFAULT_REVIEW_MD
    _scaffold_file(review_path, review_content, a.force)
    _scaffold_file(root / ".env.example", DEFAULT_ENV_EXAMPLE, a.force)

    aw_dir = root / ".nightshift" / "sessions"
    aw_dir.mkdir(parents=True, exist_ok=True)
    print(f"Created {aw_dir.parent}")

    _update_gitignore(root)
    _install_pre_commit_hook(root, a.force)

    print("\nNext steps:")
    print(f"  1. Review and customize {workflow_path.name} (notifications, auto_start, base_branch)")
    print("  2. Optionally: cp .env.example .env && edit (not needed if vars are already exported)")
    print("  3. Run: nightshift start <issue-id>")


def _load_template_pair(project_path: Path, canonical_path: Path,
                        label: str) -> tuple[str, str] | None:
    """Load project and canonical template texts.

    Returns (project_text, canonical_text) or None if either file is missing.
    Prints appropriate messages for missing files.
    """
    if not project_path.exists():
        return None

    if not canonical_path.exists():
        print(f"Canonical {label} template not found. Reinstall nightshift.",
              file=sys.stderr)
        return None

    return project_path.read_text(), canonical_path.read_text()


def _upgrade_template(project_path: Path, canonical_path: Path,
                      label: str, apply: bool,
                      force: bool = False) -> bool:
    """Diff and optionally apply a canonical template upgrade to a project file.

    Returns True if the file was behind and needed an upgrade, False if up to date.
    Skips silently if the project file does not exist.
    Major version bumps require force=True to apply.
    """
    pair = _load_template_pair(project_path, canonical_path, label)
    if pair is None:
        return False
    project_text, canonical_text = pair

    project_version = read_template_version(project_text)
    canonical_version = read_template_version(canonical_text)

    if project_version >= canonical_version:
        print(f"{label} is up to date (template_version: {project_version}).")
        return False

    is_major = canonical_version.is_major_bump_from(project_version)
    bump_label = "MAJOR" if is_major else "minor"
    print(f"{label} template_version: {project_version} -> {canonical_version} "
          f"({bump_label} version bump)")

    if is_major:
        print(f"\n  WARNING: This is a major version bump for {label}. "
              f"Behavioral changes (replacements/consolidations) were made. "
              f"Review the diff carefully.")

    diff = diff_prompt_sections(project_text, canonical_text, label=label)
    if diff:
        print(f"\n{label} prompt section changes:\n")
        print(diff)
    else:
        print(f"\n{label} prompt sections are identical (only version bump needed).")

    applied = False
    if apply:
        if is_major and not force:
            print(f"\n  Major version bump for {label} requires --force. "
                  f"Run `nightshift upgrade --apply --force` to apply.",
                  file=sys.stderr)
        else:
            updated = apply_upgrade(project_text, canonical_text)
            project_path.write_text(updated)
            print(f"\nApplied upgrade to {project_path} "
                  f"(template_version: {canonical_version}).")
            applied = True

    # Consolidation trigger: warn when template exceeds soft cap
    line_count = count_prompt_lines(canonical_text)
    if line_count > PROMPT_SOFT_CAP_LINES:
        print(f"\n  Template at {line_count}/{PROMPT_HARD_CAP_LINES} lines "
              f"— consider consolidation")

    # Return False when apply was requested but blocked (major without --force),
    # so the caller can show the --apply hint.
    if apply and not applied:
        return False

    return True


def cmd_upgrade(a):
    """Show or apply prompt section updates from the canonical templates."""
    try:
        repo_root()  # validate we're in a git repo
    except subprocess.CalledProcessError:
        print("Not inside a git repository.", file=sys.stderr)
        sys.exit(1)

    workflow_path = _resolve_workflow(a)
    if not workflow_path.exists():
        print(f"{workflow_path} not found. Run `nightshift init` first.",
              file=sys.stderr)
        sys.exit(1)

    if not CANONICAL_TEMPLATE.exists():
        print("Canonical template not found. Reinstall nightshift.",
              file=sys.stderr)
        sys.exit(1)

    force = getattr(a, "force", False)
    workflow_changed = _upgrade_template(
        workflow_path, CANONICAL_TEMPLATE, "WORKFLOW.md", a.apply, force)

    # Also upgrade REVIEW.md if it exists next to the workflow file
    review_path = workflow_path.parent / "REVIEW.md"
    review_changed = _upgrade_template(
        review_path, CANONICAL_REVIEW_TEMPLATE, "REVIEW.md", a.apply, force)

    if not workflow_changed and not review_changed:
        return

    if not a.apply:
        print("\nRun `nightshift upgrade --apply` to apply these changes.")


def _upstream_template(project_path: Path, canonical_path: Path,
                        label: str, project_name: str) -> UpstreamProposal | None:
    """Diff a project template against canonical and validate for upstreaming.

    Returns an UpstreamProposal if there are changes to propose, None otherwise.
    Prints validation issues and diff to stdout/stderr.
    """
    pair = _load_template_pair(project_path, canonical_path, label)
    if pair is None:
        return None
    project_text, canonical_text = pair

    diff_text = diff_reverse(project_text, canonical_text, label=label)
    if not diff_text:
        print(f"{label}: no prompt differences vs canonical.")
        return None

    operation = detect_operation(canonical_text, project_text)
    print(f"\n{label}: detected operation: {operation}")

    # Run validation
    issues = validate_proposal(project_text, operation)
    if issues:
        print(f"\n{label} validation issues:", file=sys.stderr)
        for issue in issues:
            print(f"  - {issue}", file=sys.stderr)
        print(f"\nFix validation issues before filing upstream.",
              file=sys.stderr)
        # Validation failed — don't build a proposal.
        return None

    # Show soft cap warning even if not a blocking issue
    line_result = validate_line_count(project_text, operation)
    if line_result and line_result[0] == "warning":
        print(f"  {line_result[1]}")

    # Show diff
    print(f"\n{label} proposed changes:\n")
    print(diff_text)

    proposal = build_proposal(project_text, label, project_name,
                               operation, diff_text)
    return proposal


def cmd_upstream(a):
    """Propose local prompt improvements back to the canonical templates."""
    try:
        repo_root()  # validate we're in a git repo
    except subprocess.CalledProcessError:
        print("Not inside a git repository.", file=sys.stderr)
        sys.exit(1)

    workflow_path = _resolve_workflow(a)
    if not workflow_path.exists():
        print(f"{workflow_path} not found. Run `nightshift init` first.",
              file=sys.stderr)
        sys.exit(1)

    project_name = a.project_name or Path.cwd().name

    proposals = []

    wf_proposal = _upstream_template(
        workflow_path, CANONICAL_TEMPLATE, "WORKFLOW.md",
        project_name)
    if wf_proposal:
        proposals.append(wf_proposal)

    review_path = workflow_path.parent / "REVIEW.md"
    rv_proposal = _upstream_template(
        review_path, CANONICAL_REVIEW_TEMPLATE, "REVIEW.md",
        project_name)
    if rv_proposal:
        proposals.append(rv_proposal)

    if not proposals:
        print("\nNo differences to propose upstream.")
        return

    if a.dry_run:
        print("\nDry run complete. Use `nightshift upstream` (without --dry-run) to file.")
        return

    # Confirm before filing
    answer = input("\nFile upstream proposal(s)? [y/N] ").strip().lower()
    if answer != "y":
        print("Aborted.")
        return

    # File issues upstream via the tracker CLI
    config = load_workflow(workflow_path)
    tracker = get_tracker_with_fallback(config, repo_root())

    filed = False
    for proposal in proposals:
        title = (f"[upstream] {proposal.operation}: "
                 f"{proposal.template_label} from {proposal.project_name}")
        body = proposal.format_issue_body()
        try:
            output = _create_issue_via_tracker(tracker, title, body)
            issue_id = output.strip() if output else "unknown"
            tracker.add_label(issue_id, "upstream")
            filed = True
            print(f"\nFiled upstream proposal: {title} (ID: {issue_id})")
        except Exception as e:
            print(f"Failed to file upstream proposal for "
                  f"{proposal.template_label}: {e}", file=sys.stderr)

    if filed:
        try:
            tracker.sync()
        except Exception as e:
            print(f"Warning: tracker sync failed: {e}", file=sys.stderr)


def _report_accept_failure(config, repo: Path, issue_id: str, message: str):
    """Post accept failure to tracker as a comment."""
    try:
        tracker = get_tracker_with_fallback(config, repo)
        tracker.add_comment(issue_id, f"⚠️ Accept failed: {message}")
        tracker.sync()
    except Exception as e:
        print(f"Warning: failed to post failure to tracker: {e}", file=sys.stderr)


def _cleanup_review_artifacts(repo: Path, coder_sid: str, config):
    """Clean up reviewer worktree, branch, and session if they exist."""
    review_wt = repo / config.workspace.root / f"{REVIEW_SESSION_PREFIX}{coder_sid}"
    review_branch = f"review/{coder_sid}"
    review_session = repo / ".nightshift" / "sessions" / f"{REVIEW_SESSION_PREFIX}{coder_sid}"

    if review_wt.exists():
        remove_worktree(repo, review_wt, review_branch)
    else:
        subprocess.run(["git", "branch", "-D", review_branch],
                       capture_output=True, cwd=str(repo))

    if review_session.exists():
        archive_session(review_session, repo)
        shutil.rmtree(review_session, ignore_errors=True)
        print(f"Cleaned up review session for {coder_sid}")


def _unblock_dependents(tracker, closed_issue_id: str) -> None:
    """Remove blocked:<id> labels from issues that depended on the closed issue."""

    try:
        issues = tracker.list_issues(status="open")
    except Exception as e:
        print(f"Warning: failed to scan for blocked issues: {e}", file=sys.stderr)
        return

    for issue in issues:
        for label in issue.labels:
            if not label.startswith(BLOCKED_LABEL_PREFIX):
                continue

            blocker_prefix = label[len(BLOCKED_LABEL_PREFIX):]
            if not closed_issue_id.startswith(blocker_prefix):
                continue

            try:
                tracker.remove_label(issue.id, label)
                print(f"Unblocked {issue.identifier} (dependency {blocker_prefix} closed)")
            except Exception as e:
                print(f"Warning: failed to remove {label} from "
                      f"{issue.identifier}: {e}", file=sys.stderr)


def cmd_accept(a):
    """Merge agent branch into base branch, then clean up."""
    r = repo_root()
    sid = resolve_session(a.issue_id)
    _validate_transition(sid, "accepted")
    config = load_workflow(_resolve_workflow(a))
    branch = f"agent/{sid}"
    base = config.workspace.base_branch
    wt = r / config.workspace.root / f"agent-{sid}"

    # Verify agent branch is not behind base
    behind_msg = check_branch_not_behind_base(r, branch, base)
    if behind_msg:
        print(behind_msg, file=sys.stderr)
        _report_accept_failure(config, r, a.issue_id, behind_msg)
        sys.exit(1)

    # Verify agent branch actually contains the current base HEAD
    merge_base = subprocess.run(
        ["git", "merge-base", "--is-ancestor", base, branch],
        cwd=str(r), capture_output=True,
    )
    if merge_base.returncode != 0:
        message = (
            f"Agent branch {branch} does not contain current {base} HEAD. "
            f"Run 'nightshift revise {a.issue_id} \"Merge latest {base}\"' first."
        )
        print(message, file=sys.stderr)
        _report_accept_failure(config, r, a.issue_id, message)
        sys.exit(1)

    escaping_symlinks = audit_worktree_symlinks(wt)
    if escaping_symlinks:
        details = "\n".join(
            f"- {symlink_path} -> {target_path}"
            for symlink_path, target_path in escaping_symlinks
        )
        message = (
            f"Refusing to accept because worktree symlinks resolve outside /workspace:\n"
            f"{details}"
        )
        print(message, file=sys.stderr)
        _report_accept_failure(config, r, a.issue_id, message)
        sys.exit(1)

    merge_ref = resolve_merge_ref(r, branch, wt)

    # Show what will be merged
    subprocess.run(["git", "log", "--oneline", f"{base}..{merge_ref}"], cwd=str(r))
    subprocess.run(["git", "diff", "--stat", f"{base}..{merge_ref}"], cwd=str(r))

    merge_with_rebase_fallback(r, merge_ref, branch, base, a.issue_id, config,
                                _report_accept_failure, worktree=wt)
    print(f"Merged into {base}")

    verify_no_conflict_markers(r, config, a.issue_id, sid,
                                sessions_dir(), _report_accept_failure)

    # Print cost summary before cleanup removes session files
    session_dir = sessions_dir() / sid
    try:
        state = read_state(session_dir)
        usage = state.get("usage", {})
        usage_data = UsageData(
            input_tokens=usage.get("input_tokens", 0),
            output_tokens=usage.get("output_tokens", 0),
            cost_usd=usage.get("cost_usd", 0.0),
            model=usage.get("model", ""),
        )
        resumes = state.get("step", 0)
        cost_line = format_cost_line(usage_data, resumes=resumes)
    except Exception as e:
        logging.debug("Could not read usage data: %s", e)
        cost_line = ""

    archive_session(session_dir, r)
    shutil.rmtree(session_dir, ignore_errors=True)
    remove_worktree(r, wt, branch)
    _cleanup_review_artifacts(r, sid, config)

    try:
        tracker = get_tracker_with_fallback(config, r)
        tracker.set_status(a.issue_id, "closed")
        tracker.add_comment(a.issue_id, f"✅ Accepted and merged into `{base}`.")
        _unblock_dependents(tracker, a.issue_id)
        tracker.sync()
    except Exception as e:
        print(f"Warning: failed to close issue in tracker: {e}", file=sys.stderr)

    print(f"Accepted and cleaned up {sid}")
    if cost_line:
        print(cost_line)


def cmd_reject(a):
    """Discard agent work: remove worktree, branch, and session."""
    r = repo_root()
    sid = resolve_session(a.issue_id)
    current_state = read_state(r / ".nightshift" / "sessions" / sid).get("status", "")
    if current_state == "starting":
        print(
            f"Cannot transition session '{sid[:12]}' to 'rejected': already in state '{current_state}'",
            file=sys.stderr,
        )
        sys.exit(1)
    _validate_transition(sid, "rejected")
    config = load_workflow(_resolve_workflow(a))
    branch = f"agent/{sid}"

    # Sanitize core.worktree if container set it to /workspace
    sanitize_git_config(r)

    result = subprocess.run(
        ["git", "log", "--oneline", f"{config.workspace.base_branch}..{branch}"],
        capture_output=True, text=True, cwd=str(r),
    )
    if result.stdout.strip():
        print(f"Discarding commits:\n{result.stdout.strip()}")

    wt = r / config.workspace.root / f"agent-{sid}"
    remove_worktree(r, wt, branch)
    _cleanup_review_artifacts(r, sid, config)

    ss = sessions_dir() / sid
    if ss.exists():
        archive_session(ss, r)
        shutil.rmtree(ss)

    try:
        tracker = get_tracker_with_fallback(config, r)
        tracker.set_status(a.issue_id, "closed")
        tracker.add_comment(a.issue_id, "🛑 Rejected — agent work discarded.")
        tracker.sync()
    except Exception as e:
        print(f"Warning: failed to close issue in tracker: {e}", file=sys.stderr)

    print(f"Rejected and cleaned up {sid}")


WORKING_STATUSES = ("working", "starting")
REVIEW_STATUSES = ("waiting:review", "waiting:human-review")


def _build_mid_flight_prompt(message: str) -> str:
    """Build a resume prompt for mid-flight course correction."""
    parts = [
        "## Mid-flight Course Correction\n",
        "The operator has stopped your session to provide new direction:\n",
        f"**Operator:** {message}\n",
        "\n## Instructions",
        "Follow the operator's guidance above.",
        "The codebase already has your previous work on this branch.",
        "Same marker rules apply (@@LOG@@, @@CHECKPOINT@@, @@DONE@@, etc.).",
        "When done: @@DONE@@",
    ]
    return "\n".join(parts)


def _stop_and_build_mid_flight(sid: str, sd, message: str) -> str:
    """Stop a running container and return a mid-flight course-correction prompt."""
    container = f"nightshift-{sid}"
    print(f"Stopping container {container}...")
    if not docker_stop(container):
        print(f"Warning: container {container} may not be running",
              file=sys.stderr)
    print(f"Revising working session {sid} with inline feedback")
    return _build_mid_flight_prompt(message)


def _collect_review_feedback(config, repo, issue_id: str, inline) -> str:
    """Collect tracker comments and return a review revision prompt."""
    tracker = get_tracker_with_fallback(config, repo)
    review_comments = collect_review_feedback(tracker, issue_id)
    feedback = build_revise_prompt(review_comments, inline)
    if not feedback.strip() or (not review_comments and not inline):
        print("No review feedback found. Add comments to the issue "
              "or pass inline feedback.", file=sys.stderr)
        sys.exit(1)
    print(f"Revising {issue_id} with {len(review_comments)} comment(s)" +
          (f" + inline feedback" if inline else ""))
    return feedback


def cmd_revise(a):
    """Resume agent with review feedback or mid-flight course correction."""
    r = repo_root()
    sid = resolve_session(a.issue_id)
    sd = sessions_dir() / sid

    if not sd.exists() or not (sd / "state.json").exists():
        print(f"No session found for {sid}", file=sys.stderr)
        sys.exit(1)

    state = read_state(sd)
    status = state.get("status")
    inline = a.message if hasattr(a, "message") and a.message else None
    is_suspended = isinstance(status, str) and status.startswith("suspended:")

    if status in WORKING_STATUSES:
        if not inline:
            print("A message is required when revising a working session.",
                  file=sys.stderr)
            sys.exit(1)
    elif status not in REVIEW_STATUSES and not is_suspended:
        print(f"Session {sid} is not revisable "
              f"(status: {status})", file=sys.stderr)
        sys.exit(1)

    wf = _resolve_workflow(a)
    config = load_workflow(wf)

    if status in WORKING_STATUSES:
        feedback = _stop_and_build_mid_flight(sid, sd, inline)
    else:
        feedback = _collect_review_feedback(config, r, a.issue_id, inline)
        # Clear completed_at when resuming from completion states (SSM-11)
        clear_completed_at(sd)
        # Clean up sibling review session if exists
        _cleanup_review_artifacts(r, sid, config)

    (sd / "resume-prompt.md").write_text(feedback)
    update_status(sd, "working")

    subprocess.run(_build_resume_launch_cmd(a.issue_id, getattr(a, "workflow", None)))


def cmd_issue(a):
    """Pass arguments directly to the tracker CLI with lock retry."""
    r = repo_root()
    wf = _resolve_workflow(a)
    config = load_workflow(wf)
    tracker = get_tracker_with_fallback(config, r)
    try:
        create_args = _parse_bug_new_args(a.tracker_args)
        if create_args is not None:
            output = _create_issue_via_tracker(tracker, *create_args)
        else:
            output = _dispatch_bug_command(tracker, a.tracker_args)
            if output is None:
                output = tracker.run_raw(*a.tracker_args)
    except NotImplementedError as e:
        print(f"Tracker '{config.tracker.kind}' does not support raw CLI passthrough: {e}",
              file=sys.stderr)
        sys.exit(1)
    if output:
        print(output)


def cmd_cleanup(a):
    r = repo_root()
    sid = resolve_session(a.issue_id)
    config = load_workflow(_resolve_workflow(a))

    wt = r / config.workspace.root / f"agent-{sid}"
    remove_worktree(r, wt, f"agent/{sid}")

    ss = sessions_dir() / sid
    if ss.exists() and not a.keep_session:
        archive_session(ss, r)
        shutil.rmtree(ss)
    print(f"Cleaned up {sid}")


def _overflow_flag_path() -> Path:
    """Return the path to the overflow flag file."""
    return repo_root() / ".nightshift" / OVERFLOW_FLAG_FILENAME


def _read_overflow_profile_name(flag: Path) -> str | None:
    """Read the selected overflow profile name from the overflow flag file."""
    if not flag.exists():
        return None
    try:
        profile_name = flag.read_text().strip()
    except OSError as e:
        logging.error("Failed reading overflow flag %s: %s", flag, e)
        return None
    return profile_name or None


def cmd_overflow(a):
    """Toggle overflow mode (alternate LLM provider) on or off."""
    flag = _overflow_flag_path()
    flag.parent.mkdir(parents=True, exist_ok=True)
    if a.state == "on":
        existing_profile = _read_overflow_profile_name(flag)
        if existing_profile:
            flag.write_text(f"{existing_profile}\n")
            print("Overflow ON -- new container launches will use the alternate "
                  f"provider profile '{existing_profile}'.")
        else:
            flag.touch()
            print("Overflow ON -- new container launches will use the alternate provider.")
    elif a.state == "off":
        if flag.exists():
            flag.unlink()
        print("Overflow OFF -- new container launches will use the primary provider.")
    elif a.state == "profile":
        workflow_path = _resolve_workflow(a)
        config = load_workflow(workflow_path)
        profile_name = a.profile_name
        if profile_name not in config.overflow.profiles:
            print(f"Unknown overflow profile '{profile_name}' in {workflow_path}",
                  file=sys.stderr)
            sys.exit(1)
        flag.write_text(f"{profile_name}\n")
        print("Overflow ON -- new container launches will use the alternate "
              f"provider profile '{profile_name}'.")


def cmd_export_training_data(a):
    """Export training data from completed session pairs (coder + review)."""
    sd = sessions_dir()
    if not sd.exists():
        print("No sessions directory found.", file=sys.stderr)
        sys.exit(1)

    verdict = a.verdict if hasattr(a, "verdict") else None
    examples = extract_training_data(sd, verdict_filter=verdict)

    if not examples:
        print("No training examples found. Requires completed sessions "
              "with matching review sessions containing a verdict.")
        return

    output = Path(a.output)
    count = export_jsonl(examples, output)

    # Print summary
    approvals = sum(1 for e in examples if e.review_verdict == "approve")
    revisions = sum(1 for e in examples if e.review_verdict == "revise")
    print(f"Exported {count} training example(s) to {output}")
    print(f"  Approved: {approvals}  |  Revisions: {revisions}")


def _parse_date(s):
    """Parse a YYYY-MM-DD string into a datetime.date."""
    return datetime.date.fromisoformat(s)


def _entry_date(entry):
    """Extract a datetime.date from an entry's completed_at or started_at."""
    for field in ("completed_at", "started_at"):
        val = entry.get(field)
        if not val:
            continue
        try:
            return datetime.date.fromisoformat(val[:10])
        except (ValueError, TypeError) as e:
            logging.debug("Cannot parse date from %s=%r: %s", field, val, e)
    return None


def _load_usage_entries(usage_file):
    """Read JSONL file and return list of dicts. Warns on malformed lines."""
    entries = []
    for line in usage_file.read_text().strip().splitlines():
        try:
            entries.append(json.loads(line))
        except json.JSONDecodeError as e:
            print(f"Warning: skipping malformed line: {e}", file=sys.stderr)
    return entries


def _filter_entries(entries, issue_id=None, since=None, until=None):
    """Filter entries by issue_id prefix and date range."""
    if issue_id:
        entries = [e for e in entries if issue_id in e.get("issue_id", "")]
    if since:
        since_d = _parse_date(since)
        entries = [e for e in entries if (_entry_date(e) or datetime.date.min) >= since_d]
    if until:
        until_d = _parse_date(until)
        entries = [e for e in entries if (_entry_date(e) or datetime.date.max) <= until_d]
    return entries


def _print_entries(entries):
    """Print the per-entry table and totals."""
    total_input = 0
    total_output = 0
    total_cost = 0.0
    for e in entries:
        inp = e.get("input_tokens", 0)
        out = e.get("output_tokens", 0)
        cost = e.get("cost_usd", 0.0)
        model = e.get("model", "?")
        sid = e.get("session_id", "?")
        step = e.get("step", "coder")
        total_input += inp
        total_output += out
        total_cost += cost
        in_k = format_token_count(inp)
        out_k = format_token_count(out)
        print(f"  {sid:<14} {step:<8} {in_k:>6} in / {out_k:>6} out  ${cost:.2f}  ({model})")
    print(f"\n  {'TOTAL':<14} {'':8} "
          f"{format_token_count(total_input):>6} in / {format_token_count(total_output):>6} out  "
          f"${total_cost:.2f}")
    print(f"  {len(entries)} session(s)")


def _print_daily_summary(entries):
    """Group entries by date, print per-day subtotals and grand total."""
    by_day = defaultdict(list)
    for e in entries:
        d = _entry_date(e)
        by_day[d or datetime.date.min].append(e)
    grand_in = grand_out = 0
    grand_cost = 0.0
    grand_count = 0
    for day in sorted(by_day):
        day_entries = by_day[day]
        day_in = sum(e.get("input_tokens", 0) for e in day_entries)
        day_out = sum(e.get("output_tokens", 0) for e in day_entries)
        day_cost = sum(e.get("cost_usd", 0.0) for e in day_entries)
        grand_in += day_in
        grand_out += day_out
        grand_cost += day_cost
        grand_count += len(day_entries)
        label = str(day) if day != datetime.date.min else "unknown"
        print(f"  {label}  {len(day_entries)} session(s)  "
              f"{format_token_count(day_in):>6} in / {format_token_count(day_out):>6} out  "
              f"${day_cost:.2f}")
    print(f"\n  {'TOTAL':<14} {grand_count} session(s)  "
          f"{format_token_count(grand_in):>6} in / {format_token_count(grand_out):>6} out  "
          f"${grand_cost:.2f}")


def _all_projects_src_dir():
    """Return the ~/src directory path for cross-project scanning."""
    return Path.home() / "src"


def _cmd_usage_all_projects(since, until, daily):
    """Scan ~/src/*/.nightshift/usage.jsonl, aggregate by project."""
    src_dir = _all_projects_src_dir()
    all_entries = []
    project_data = []
    for usage_file in sorted(src_dir.glob("*/.nightshift/" + USAGE_LOG_FILENAME)):
        project_name = usage_file.parent.parent.name
        entries = _load_usage_entries(usage_file)
        entries = _filter_entries(entries, since=since, until=until)
        if not entries:
            continue
        all_entries.extend(entries)
        total_in = sum(e.get("input_tokens", 0) for e in entries)
        total_out = sum(e.get("output_tokens", 0) for e in entries)
        total_cost = sum(e.get("cost_usd", 0.0) for e in entries)
        project_data.append((project_name, len(entries), total_in, total_out, total_cost))
    if not all_entries:
        print("No matching usage entries.")
        return
    if daily:
        _print_daily_summary(all_entries)
        return
    for name, count, inp, out, cost in project_data:
        print(f"  {name:<20} {count} session(s)  "
              f"{format_token_count(inp):>6} in / {format_token_count(out):>6} out  "
              f"${cost:.2f}")
    grand_in = sum(p[2] for p in project_data)
    grand_out = sum(p[3] for p in project_data)
    grand_cost = sum(p[4] for p in project_data)
    print(f"\n  {'GRAND TOTAL':<20} {len(all_entries)} session(s)  "
          f"{format_token_count(grand_in):>6} in / {format_token_count(grand_out):>6} out  "
          f"${grand_cost:.2f}")


def cmd_usage(a):
    """Show token usage and cost from completed sessions."""
    since = getattr(a, "since", None)
    until = getattr(a, "until", None)
    daily = getattr(a, "daily", False)
    all_projects = getattr(a, "all_projects", False)

    if all_projects:
        _cmd_usage_all_projects(since, until, daily)
        return

    usage_file = repo_root() / ".nightshift" / USAGE_LOG_FILENAME
    if not usage_file.exists():
        print("No usage data found. Sessions must complete before usage is recorded.")
        return

    entries = _load_usage_entries(usage_file)
    if not entries:
        print("No usage entries found.")
        return

    issue_id = getattr(a, "issue_id", None)
    entries = _filter_entries(entries, issue_id=issue_id, since=since, until=until)
    if not entries:
        print("No matching usage entries.")
        return

    if daily:
        _print_daily_summary(entries)
    else:
        _print_entries(entries)


def _register_session_commands(s):
    """Register session lifecycle commands."""
    sp = s.add_parser("start")
    sp.add_argument("issue_id")
    sp.add_argument("--max-turns", type=int, default=None)
    sp.set_defaults(func=cmd_start)

    sp = s.add_parser("resume")
    sp.add_argument("issue_id")
    sp.set_defaults(func=cmd_resume)

    sp = s.add_parser("review")
    sp.add_argument("issue_id")
    sp.set_defaults(func=cmd_review)

    sp = s.add_parser("answer")
    sp.add_argument("issue_id")
    sp.add_argument("message")
    sp.set_defaults(func=cmd_answer)

    sp = s.add_parser("accept", help="Merge agent work into base branch")
    sp.add_argument("issue_id")
    sp.set_defaults(func=cmd_accept)

    sp = s.add_parser("reject", help="Discard agent work and clean up")
    sp.add_argument("issue_id")
    sp.set_defaults(func=cmd_reject)

    sp = s.add_parser("revise", help="Resume agent with review feedback or mid-flight correction")
    sp.add_argument("issue_id")
    sp.add_argument("message", nargs="?", default=None, help="Inline review feedback")
    sp.set_defaults(func=cmd_revise)

    sp = s.add_parser("cleanup")
    sp.add_argument("issue_id")
    sp.add_argument("--keep-session", action="store_true")
    sp.set_defaults(func=cmd_cleanup)


def _build_parser() -> argparse.ArgumentParser:
    """Build the CLI argument parser with all subcommands."""
    p = argparse.ArgumentParser(prog="nightshift")
    p.add_argument("--workflow", default=None, help="Path to WORKFLOW.md")
    s = p.add_subparsers(dest="cmd", required=True)

    _register_session_commands(s)

    sp = s.add_parser("watcher")
    sp.add_argument("--no-auto-start", action="store_true",
                    help="Disable automatic starting of new issues")
    sp.set_defaults(func=cmd_watcher)

    sp = s.add_parser("watchdog", help="Monitor all nightshift watchers globally")
    sp.add_argument("--list", dest="list_watchers", action="store_true",
                    help="List registered watchers and their status")
    sp.add_argument("--check", action="store_true",
                    help="One-shot health check")
    sp.add_argument("--config", type=Path, default=None,
                    help="Path to watchdog.yaml config file")
    sp.add_argument("--no-alerts", action="store_true",
                    help="Suppress alerts (useful with --check)")
    sp.add_argument("-v", "--verbose", action="store_true",
                    help="Verbose output")
    sp.set_defaults(func=cmd_watchdog)

    sp = s.add_parser("status")
    sp.set_defaults(func=cmd_status)

    sp = s.add_parser("blocked", help="List issues blocked by dependencies")
    sp.set_defaults(func=cmd_blocked)

    sp = s.add_parser("logs")
    sp.add_argument("issue_id")
    sp.set_defaults(func=cmd_logs)

    sp = s.add_parser("history")
    sp.add_argument("issue_id")
    sp.add_argument("-f", "--follow", action="store_true",
                    help="Keep watching for new entries (like tail -f)")
    sp.set_defaults(func=cmd_history)

    sp = s.add_parser("issue", help="Pass commands to the tracker CLI with lock retry")
    sp.add_argument("tracker_args", nargs=argparse.REMAINDER,
                    help="Arguments passed directly to the tracker CLI")
    sp.set_defaults(func=cmd_issue)

    sp = s.add_parser("init", help="Scaffold WORKFLOW.md and .env.example")
    sp.add_argument("--force", action="store_true", help="Overwrite existing files")
    sp.add_argument("--workflow-path", default=None,
                    help="Custom location for workflow file (writes .nightshift.yaml pointer)")
    sp.set_defaults(func=cmd_init)

    sp = s.add_parser("upgrade", help="Upgrade WORKFLOW.md prompt to latest template")
    sp.add_argument("--apply", action="store_true",
                    help="Apply the upgrade (default: dry-run showing diff)")
    sp.add_argument("--force", action="store_true",
                    help="Force apply major version bumps (behavioral changes)")
    sp.set_defaults(func=cmd_upgrade)

    sp = s.add_parser("upstream",
                       help="Propose local prompt improvements to canonical templates")
    sp.add_argument("--dry-run", action="store_true",
                    help="Show diff and validation without filing (default: file issues)")
    sp.add_argument("--project-name", default=None,
                    help="Project name for provenance (default: current directory name)")
    sp.set_defaults(func=cmd_upstream)

    sp = s.add_parser("overflow", help="Switch new launches to alternate LLM provider")
    overflow_sub = sp.add_subparsers(dest="state", required=True)

    sp_on = overflow_sub.add_parser("on", help="Enable overflow mode")
    sp_on.set_defaults(func=cmd_overflow, state="on")

    sp_off = overflow_sub.add_parser("off", help="Disable overflow mode")
    sp_off.set_defaults(func=cmd_overflow, state="off")

    sp_profile = overflow_sub.add_parser(
        "profile", help="Enable overflow mode with a named profile"
    )
    sp_profile.add_argument("profile_name", help="Overflow profile name from WORKFLOW.md")
    sp_profile.set_defaults(func=cmd_overflow, state="profile")

    sp = s.add_parser("usage", help="Show token usage and cost from completed sessions")
    sp.add_argument("issue_id", nargs="?", default=None,
                    help="Filter by issue ID (prefix match)")
    sp.add_argument("--since", default=None,
                    help="Only show entries on or after this date (YYYY-MM-DD)")
    sp.add_argument("--until", default=None,
                    help="Only show entries on or before this date (YYYY-MM-DD)")
    sp.add_argument("--daily", action="store_true", default=False,
                    help="Group entries by date with per-day subtotals")
    sp.add_argument("--all-projects", action="store_true", default=False,
                    help="Scan ~/src/*/.nightshift/usage.jsonl and aggregate by project")
    sp.set_defaults(func=cmd_usage)

    sp = s.add_parser("export-training-data",
                       help="Export training data from session logs for finetuning")
    sp.add_argument("-o", "--output", default="training-data.jsonl",
                    help="Output JSONL file path (default: training-data.jsonl)")
    sp.add_argument("--verdict", choices=["approve", "revise"], default=None,
                    help="Filter by review verdict (default: all)")
    sp.set_defaults(func=cmd_export_training_data)

    return p


def main():
    # Load .env early so all commands see credentials
    try:
        load_all_dotenv(repo_root() / ".env")
    except subprocess.CalledProcessError:
        pass  # Not in a git repo (e.g. --help)

    p = _build_parser()
    a = p.parse_args()
    a.func(a)


if __name__ == "__main__":
    main()
