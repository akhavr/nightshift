#!/usr/bin/env python3
"""CLI — reads WORKFLOW.md for config, delegates to launch.py and watcher."""

import argparse
import json
import logging
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from core.config import load_workflow, create_tracker
from core.review import collect_review_feedback, build_revise_prompt
from host.constants import SHORT_ID_LEN, REVIEW_SESSION_PREFIX, LOG_PREVIEW_LEN, HISTORY_FOLLOW_POLL_S
from core.upgrade import (
    read_template_version, get_canonical_version,
    get_canonical_review_version,
    diff_prompt_sections, apply_upgrade,
    CANONICAL_TEMPLATE, CANONICAL_REVIEW_TEMPLATE,
    load_canonical_template, load_canonical_review_template,
)
from host.config_discovery import discover_workflow as _discover_workflow, write_local_config
from host.env import load_all_dotenv
from host.docker_utils import docker_stop
from host.merge import (
    resolve_merge_ref,
    merge_with_rebase_fallback, verify_no_conflict_markers,
    check_branch_not_behind_base,
)
from host.session_utils import (
    get_repo_root,
    read_state, write_state, update_status,
    force_remove_dir, remove_worktree,
)


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


def cmd_start(a):
    wf = _resolve_workflow(a)
    cmd = [sys.executable, str(Path(__file__).parent / "launch.py"), a.issue_id]
    if a.max_turns:
        cmd += ["--max-turns", str(a.max_turns)]
    cmd += ["--workflow", str(wf)]
    subprocess.run(cmd)


def cmd_resume(a):
    wf = _resolve_workflow(a)
    cmd = [sys.executable, str(Path(__file__).parent / "launch.py"),
           a.issue_id, "--resume"]
    cmd += ["--workflow", str(wf)]
    subprocess.run(cmd)


def cmd_answer(a):
    """Write answer.txt directly — works even if container is paused."""
    sid = resolve_session(a.issue_id)
    sd = sessions_dir() / sid
    if sd.exists():
        (sd / "answer.txt").write_text(a.message)
        print(f"Answer written for {sid}")
    else:
        print(f"No session found for {sid}", file=sys.stderr)


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
        except Exception:
            print(f"{sid:<14} {'<error>':<26}")


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

    print("\nNext steps:")
    print(f"  1. Review and customize {workflow_path.name} (notifications, auto_start, base_branch)")
    print("  2. Optionally: cp .env.example .env && edit (not needed if vars are already exported)")
    print("  3. Run: nightshift start <issue-id>")


def _upgrade_template(project_path: Path, canonical_path: Path,
                      label: str, apply: bool) -> bool:
    """Diff and optionally apply a canonical template upgrade to a project file.

    Returns True if the file was behind and needed an upgrade, False if up to date.
    Skips silently if the project file does not exist.
    """
    if not project_path.exists():
        return False

    if not canonical_path.exists():
        print(f"Canonical {label} template not found. Reinstall nightshift.",
              file=sys.stderr)
        return False

    project_text = project_path.read_text()
    canonical_text = canonical_path.read_text()

    project_version = read_template_version(project_text)
    canonical_version = read_template_version(canonical_text)

    if project_version >= canonical_version:
        print(f"{label} is up to date (template_version: {project_version}).")
        return False

    print(f"{label} template_version: {project_version} -> {canonical_version}")

    diff = diff_prompt_sections(project_text, canonical_text, label=label)
    if diff:
        print(f"\n{label} prompt section changes:\n")
        print(diff)
    else:
        print(f"\n{label} prompt sections are identical (only version bump needed).")

    if apply:
        updated = apply_upgrade(project_text, canonical_text)
        project_path.write_text(updated)
        print(f"\nApplied upgrade to {project_path} "
              f"(template_version: {canonical_version}).")
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

    workflow_changed = _upgrade_template(
        workflow_path, CANONICAL_TEMPLATE, "WORKFLOW.md", a.apply)

    # Also upgrade REVIEW.md if it exists next to the workflow file
    review_path = workflow_path.parent / "REVIEW.md"
    review_changed = _upgrade_template(
        review_path, CANONICAL_REVIEW_TEMPLATE, "REVIEW.md", a.apply)

    if not workflow_changed and not review_changed:
        return

    if not a.apply:
        print("\nRun `nightshift upgrade --apply` to apply these changes.")


def _report_accept_failure(config, repo: Path, issue_id: str, message: str):
    """Post accept failure to tracker as a comment."""
    try:
        tracker = create_tracker(config, repo_dir=str(repo))
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
        shutil.rmtree(review_session, ignore_errors=True)
        print(f"Cleaned up review session for {coder_sid}")


def cmd_accept(a):
    """Merge agent branch into base branch, then clean up."""
    r = repo_root()
    sid = resolve_session(a.issue_id)
    config = load_workflow(_resolve_workflow(a))
    branch = f"agent/{sid}"
    base = config.workspace.base_branch
    wt = r / config.workspace.root / f"agent-{sid}"

    merge_ref = resolve_merge_ref(r, branch, wt)

    # Verify agent branch is not behind base
    behind_msg = check_branch_not_behind_base(r, branch, base)
    if behind_msg:
        print(behind_msg, file=sys.stderr)
        _report_accept_failure(config, r, a.issue_id, behind_msg)
        sys.exit(1)

    # Show what will be merged
    subprocess.run(["git", "log", "--oneline", f"{base}..{merge_ref}"], cwd=str(r))
    subprocess.run(["git", "diff", "--stat", f"{base}..{merge_ref}"], cwd=str(r))

    merge_with_rebase_fallback(r, merge_ref, branch, base, a.issue_id, config,
                                _report_accept_failure)
    print(f"Merged into {base}")

    verify_no_conflict_markers(r, config, a.issue_id, sid,
                                sessions_dir(), _report_accept_failure)

    remove_worktree(r, wt, branch)
    _cleanup_review_artifacts(r, sid, config)

    try:
        tracker = create_tracker(config, repo_dir=str(r))
        tracker.set_status(a.issue_id, "closed")
        tracker.add_comment(a.issue_id, f"✅ Accepted and merged into `{base}`.")
        tracker.sync()
    except Exception as e:
        print(f"Warning: failed to close issue in tracker: {e}", file=sys.stderr)

    print(f"Accepted and cleaned up {sid}")


def cmd_reject(a):
    """Discard agent work: remove worktree, branch, and session."""
    r = repo_root()
    sid = resolve_session(a.issue_id)
    config = load_workflow(_resolve_workflow(a))
    branch = f"agent/{sid}"

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
        shutil.rmtree(ss)

    try:
        tracker = create_tracker(config, repo_dir=str(r))
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


def _collect_review_feedback(wf, repo, issue_id: str, inline) -> str:
    """Collect tracker comments and return a review revision prompt."""
    config = load_workflow(wf)
    tracker = create_tracker(config, repo_dir=str(repo))
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

    if status in WORKING_STATUSES:
        if not inline:
            print("A message is required when revising a working session.",
                  file=sys.stderr)
            sys.exit(1)
    elif status not in REVIEW_STATUSES:
        print(f"Session {sid} is not revisable "
              f"(status: {status})", file=sys.stderr)
        sys.exit(1)

    wf = _resolve_workflow(a)

    if status in WORKING_STATUSES:
        feedback = _stop_and_build_mid_flight(sid, sd, inline)
    else:
        feedback = _collect_review_feedback(wf, r, a.issue_id, inline)

    (sd / "resume-prompt.md").write_text(feedback)
    update_status(sd, "working")

    cmd = [sys.executable, str(Path(__file__).parent / "launch.py"),
           a.issue_id, "--resume"]
    cmd += ["--workflow", str(wf)]
    subprocess.run(cmd)


def cmd_issue(a):
    """Pass arguments directly to the tracker CLI with lock retry."""
    r = repo_root()
    wf = _resolve_workflow(a)
    config = load_workflow(wf)
    tracker = create_tracker(config, repo_dir=str(r))
    try:
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
        shutil.rmtree(ss)
    print(f"Cleaned up {sid}")


def _register_session_commands(s):
    """Register session lifecycle commands."""
    sp = s.add_parser("start")
    sp.add_argument("issue_id")
    sp.add_argument("--max-turns", type=int, default=None)
    sp.set_defaults(func=cmd_start)

    sp = s.add_parser("resume")
    sp.add_argument("issue_id")
    sp.set_defaults(func=cmd_resume)

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

    sp = s.add_parser("status")
    sp.set_defaults(func=cmd_status)

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
    sp.set_defaults(func=cmd_upgrade)

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
