#!/usr/bin/env python3
"""CLI — reads WORKFLOW.md for config, delegates to launch.py and watcher."""

import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from core.config import load_workflow, create_tracker
from core.review import collect_review_feedback, build_revise_prompt
from host.constants import SHORT_ID_LEN
from host.env import load_all_dotenv
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
        print(f"No sessions directory found", file=sys.stderr)
        sys.exit(1)
    matches = [d.name for d in sd.iterdir() if d.is_dir() and d.name.startswith(issue_id[:SHORT_ID_LEN])]
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        print(f"Ambiguous ID '{issue_id}' matches: {', '.join(matches)}", file=sys.stderr)
        sys.exit(1)
    # No match by session dir — return truncated (for start/new sessions)
    return issue_id[:SHORT_ID_LEN]


def cmd_start(a):
    cmd = [sys.executable, str(Path(__file__).parent / "launch.py"), a.issue_id]
    if a.max_turns:
        cmd += ["--max-turns", str(a.max_turns)]
    if a.workflow:
        cmd += ["--workflow", a.workflow]
    subprocess.run(cmd)


def cmd_resume(a):
    cmd = [sys.executable, str(Path(__file__).parent / "launch.py"),
           a.issue_id, "--resume"]
    if a.workflow:
        cmd += ["--workflow", a.workflow]
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
    log_file = repo_root() / ".nightshift" / "watcher.log"
    log_file.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        sys.executable, str(Path(__file__).parent / "watcher.py"),
        "--sessions-dir", str(sessions_dir()),
        "--log-file", str(log_file),
    ]
    if a.no_auto_start:
        cmd.append("--no-auto-start")
    print(f"Logging to {log_file}")
    subprocess.run(cmd)


def cmd_status(a):
    sd = sessions_dir()
    if not sd.exists():
        print("No sessions."); return
    print(f"{'SESSION':<14} {'STATUS':<26} {'STEP':>5} {'CPS':>4}")
    print("-" * 54)
    for f in sorted(sd.glob("*/state.json")):
        sid = f.parent.name
        try:
            s = json.loads(f.read_text())
            print(f"{sid:<14} {s.get('status','?'):<26} "
                  f"{s.get('step',0):>5} {len(s.get('checkpoints',[])):>4}")
        except Exception:
            print(f"{sid:<14} {'<error>':<26}")


def cmd_logs(a):
    log_file = sessions_dir() / resolve_session(a.issue_id) / "raw-output.log"
    if not log_file.exists():
        print("No log file.", file=sys.stderr); return
    subprocess.run(["tail", "-f", str(log_file)])


def cmd_history(a):
    cf = sessions_dir() / resolve_session(a.issue_id) / "conversation.jsonl"
    if not cf.exists():
        print("No history.", file=sys.stderr); return
    icons = {"thought":"💭","checkpoint":"📌","question":"❓","human_answer_sent":"👤",
             "tool_call":"🔧","tool_result":"📄","system":"⚙️","user":"📝"}
    for line in cf.read_text().strip().splitlines():
        try:
            e = json.loads(line)
            print(f"  {e['timestamp'][:19]}  {icons.get(e['role'],'•')} "
                  f"[{e['role']}] {e['content'][:120]}")
        except Exception:
            continue


DEFAULT_WORKFLOW_MD = """\
---
agent:
  kind: claude-code
  max_turns: 50
  stall_timeout_s: 300
  extra_args: []

tracker:
  kind: git-bug

workspace:
  kind: worktree
  base_branch: main
  root: .worktrees

notifications:
  - kind: telegram
    token: $TELEGRAM_BOT_TOKEN
    chat_id: $TELEGRAM_CHAT_ID

merge:
  require_review: true
  review_label: reviewed
  auto_merge_label: auto-merge

auto_start:
  enabled: false
  label: nightshift
  poll_interval_s: 30
  max_concurrent: 4

hooks:
  after_create: |
    echo "Workspace created"
  before_run: |
    echo "Starting agent run"
  after_run: |
    echo "Agent run finished"
  timeout_s: 60

terminal_statuses:
  - closed
---

You are working on the following issue:

**Title:** {{ issue.title }}
**Description:**
{{ issue.body }}

{% if attempt %}
This is continuation attempt {{ attempt }}. Review previous work and continue.
{% endif %}

**Related previous issues:**
{{ related_context }}

RULES:
1. Work on the current branch. The repo is already checked out.
2. For every significant thought: @@LOG@@ <your thought>
3. After meaningful work: @@CHECKPOINT@@ <description>
4. If you have a blocking question:
   a. Include all relevant context IN the question itself (code snippets,
      file paths, what you did, options you see) — the human reads ONLY
      the question text, they cannot see your other output.
   b. Output: @@QUESTION@@ <your self-contained question>
   c. Then output: @@WAITING@@
   d. The answer will appear as your next input.
5. When done: @@DONE@@
6. Commit frequently. Write tests where appropriate.

Begin by reading the codebase, then plan your approach.
"""

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
    # Try HEAD of origin
    result = subprocess.run(
        ["git", "symbolic-ref", "refs/remotes/origin/HEAD"],
        capture_output=True, text=True, cwd=str(repo),
    )
    if result.returncode == 0:
        return result.stdout.strip().split("/")[-1]
    # Fallback: current branch
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
    workflow_content = DEFAULT_WORKFLOW_MD.replace("base_branch: main", f"base_branch: {default_branch}")

    _scaffold_file(root / "WORKFLOW.md", workflow_content, a.force, f"base_branch: {default_branch}")
    _scaffold_file(root / "REVIEW.md", DEFAULT_REVIEW_MD, a.force)
    _scaffold_file(root / ".env.example", DEFAULT_ENV_EXAMPLE, a.force)

    aw_dir = root / ".nightshift" / "sessions"
    aw_dir.mkdir(parents=True, exist_ok=True)
    print(f"Created {aw_dir.parent}")

    _update_gitignore(root)

    print("\nNext steps:")
    print("  1. cp .env.example .env && edit .env with your credentials")
    print("  2. Review and customize WORKFLOW.md")
    print("  3. Run: nightshift start <issue-id>")


def _resolve_merge_ref(r: Path, branch: str, wt: Path) -> str:
    """Find the merge source: branch ref or worktree HEAD. Exits on failure."""
    result = subprocess.run(
        ["git", "rev-parse", "--verify", branch],
        capture_output=True, text=True, cwd=str(r),
    )
    if result.returncode == 0:
        return branch
    if wt.exists():
        wt_head = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            capture_output=True, text=True, cwd=str(wt),
        )
        if wt_head.returncode != 0:
            print(f"Branch {branch} not found and worktree HEAD unreadable.", file=sys.stderr)
            sys.exit(1)
        ref = wt_head.stdout.strip()
        print(f"Branch {branch} gone, using worktree HEAD {ref[:SHORT_ID_LEN]}")
        return ref
    print(f"Branch {branch} not found and no worktree at {wt}.", file=sys.stderr)
    sys.exit(1)


def _check_working_tree_clean(r: Path, base: str, config, issue_id: str):
    """Exit if repo has uncommitted changes."""
    status = subprocess.run(
        ["git", "status", "--porcelain"],
        capture_output=True, text=True, cwd=str(r),
    )
    dirty_files = [l for l in status.stdout.strip().splitlines()
                   if l and not l.startswith("??")]
    if dirty_files:
        file_list = "\n".join(dirty_files[:10])
        msg = (f"Cannot merge: working tree on `{base}` is not clean.\n"
               f"```\n{file_list}\n```\n"
               f"Commit or stash changes first.")
        print(f"Working tree not clean:\n{file_list}", file=sys.stderr)
        _report_accept_failure(config, r, issue_id, msg)
        sys.exit(1)


def _merge_with_rebase_fallback(r: Path, merge_ref: str, branch: str,
                                 base: str, issue_id: str, config):
    """Attempt merge; on conflict, rebase and retry. Exits on failure."""
    result = subprocess.run(
        ["git", "merge", "--no-ff", merge_ref,
         "-m", f"Merge {branch}: agent work on {issue_id}"],
        capture_output=True, text=True, cwd=str(r),
    )
    if result.returncode == 0:
        return

    merge_err = result.stderr.strip()
    if "local changes" in merge_err or "overwritten by merge" in merge_err:
        print(f"Merge failed — uncommitted changes on {base}:\n{merge_err}", file=sys.stderr)
        _report_accept_failure(config, r, issue_id,
                               f"Cannot merge: uncommitted changes on `{base}`. "
                               f"Commit or stash them first.")
        sys.exit(1)

    # Conflict — abort, rebase, retry
    subprocess.run(["git", "merge", "--abort"], capture_output=True, cwd=str(r))
    print(f"Merge conflict — rebasing {branch} onto {base}...")
    _rebase_and_retry_merge(r, branch, base, issue_id, config)


def _rebase_and_retry_merge(r: Path, branch: str, base: str,
                             issue_id: str, config):
    """Rebase branch onto base, then retry the merge. Exits on failure."""
    old_branch = subprocess.run(
        ["git", "branch", "--show-current"],
        capture_output=True, text=True, cwd=str(r),
    ).stdout.strip()
    subprocess.run(["git", "checkout", branch], capture_output=True, cwd=str(r))
    rebase = subprocess.run(
        ["git", "rebase", base],
        capture_output=True, text=True, cwd=str(r),
    )
    if rebase.returncode != 0:
        subprocess.run(["git", "rebase", "--abort"], capture_output=True, cwd=str(r))
        subprocess.run(["git", "checkout", old_branch], capture_output=True, cwd=str(r))
        details = rebase.stderr.strip()
        print(f"Rebase failed:\n{details}", file=sys.stderr)
        _report_accept_failure(
            config, r, issue_id,
            f"Merge conflicts with `{base}` that need manual resolution:\n"
            f"```\n{details}\n```\n"
            f"@nightshift revise")
        sys.exit(1)

    subprocess.run(["git", "checkout", old_branch], capture_output=True, cwd=str(r))
    print(f"Rebase successful, retrying merge...")

    result = subprocess.run(
        ["git", "merge", "--no-ff", branch,
         "-m", f"Merge {branch}: agent work on {issue_id}"],
        capture_output=True, text=True, cwd=str(r),
    )
    if result.returncode != 0:
        print(f"Merge still failed after rebase:\n{result.stderr}", file=sys.stderr)
        _report_accept_failure(config, r, issue_id,
                               f"Merge failed even after rebase:\n"
                               f"```\n{result.stderr.strip()}\n```")
        sys.exit(1)


def _verify_no_conflict_markers(r: Path, config, issue_id: str, sid: str):
    """Check for conflict markers post-merge. Resets and exits if found."""
    conflict_files = _check_conflict_markers(r)
    if not conflict_files:
        return
    file_list = "\n".join(conflict_files[:20])
    print(f"Conflict markers found after merge — aborting:\n{file_list}",
          file=sys.stderr)
    subprocess.run(["git", "reset", "--hard", "HEAD~1"],
                   capture_output=True, cwd=str(r))
    msg = (f"Merge aborted: conflict markers (`<<<<<<<`) found in "
           f"{len(conflict_files)} file(s) after rebase+merge:\n"
           f"```\n{file_list}\n```\n"
           f"Manual conflict resolution required.")
    _report_accept_failure(config, r, issue_id, msg)
    sd = sessions_dir() / sid
    if (sd / "state.json").exists():
        try:
            update_status(sd, "error:merge-conflict")
        except Exception as e:
            print(f"Failed to update session state: {e}", file=sys.stderr)
    sys.exit(1)


def cmd_accept(a):
    """Merge agent branch into base branch, then clean up."""
    r = repo_root()
    sid = resolve_session(a.issue_id)
    config = load_workflow(a.workflow or r / "WORKFLOW.md")
    branch = f"agent/{sid}"
    base = config.workspace.base_branch
    wt = r / config.workspace.root / f"agent-{sid}"

    merge_ref = _resolve_merge_ref(r, branch, wt)
    _check_working_tree_clean(r, base, config, a.issue_id)

    # Show what will be merged
    subprocess.run(["git", "log", "--oneline", f"{base}..{merge_ref}"], cwd=str(r))
    subprocess.run(["git", "diff", "--stat", f"{base}..{merge_ref}"], cwd=str(r))

    _merge_with_rebase_fallback(r, merge_ref, branch, base, a.issue_id, config)
    print(f"Merged into {base}")

    _verify_no_conflict_markers(r, config, a.issue_id, sid)

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


def _cleanup_review_artifacts(repo: Path, coder_sid: str, config):
    """Clean up reviewer worktree, branch, and session if they exist."""
    review_wt = repo / config.workspace.root / f"review-{coder_sid}"
    review_branch = f"review/{coder_sid}"
    review_session = repo / ".nightshift" / "sessions" / f"review-{coder_sid}"

    if review_wt.exists():
        remove_worktree(repo, review_wt, review_branch)
    else:
        # Still try to clean branch
        subprocess.run(["git", "branch", "-D", review_branch],
                       capture_output=True, cwd=str(repo))

    if review_session.exists():
        shutil.rmtree(review_session, ignore_errors=True)
        print(f"Cleaned up review session for {coder_sid}")


def _check_conflict_markers(repo: Path) -> list[str]:
    """Check files changed by the merge commit for conflict markers.

    Returns list of files containing markers, or empty list if clean.
    """
    # Get files changed in the merge commit vs its first parent (the base branch)
    diff_result = subprocess.run(
        ["git", "diff", "--name-only", "HEAD^1..HEAD"],
        capture_output=True, text=True, cwd=str(repo),
    )
    if diff_result.returncode != 0:
        print(f"Warning: git diff --name-only failed (rc={diff_result.returncode}), "
              f"skipping conflict marker check", file=sys.stderr)
        return []
    changed_files = [f for f in diff_result.stdout.strip().splitlines() if f]
    if not changed_files:
        return []

    conflict_files = []
    for fname in changed_files:
        fpath = repo / fname
        if not fpath.is_file():
            continue
        try:
            content = fpath.read_text(errors="replace")
        except Exception as e:
            print(f"Warning: cannot read {fname}: {e}", file=sys.stderr)
            continue
        if "\n<<<<<<<" in content or content.startswith("<<<<<<<"):
            conflict_files.append(fname)
    return conflict_files


def _report_accept_failure(config, repo: Path, issue_id: str, message: str):
    """Post accept failure to tracker as a comment."""
    try:
        tracker = create_tracker(config, repo_dir=str(repo))
        tracker.add_comment(issue_id, f"⚠️ Accept failed: {message}")
        tracker.sync()
    except Exception as e:
        print(f"Warning: failed to post failure to tracker: {e}", file=sys.stderr)




def cmd_reject(a):
    """Discard agent work: remove worktree, branch, and session."""
    r = repo_root()
    sid = resolve_session(a.issue_id)
    config = load_workflow(r / "WORKFLOW.md")
    branch = f"agent/{sid}"

    # Show what will be discarded
    result = subprocess.run(
        ["git", "log", "--oneline", f"{config.workspace.base_branch}..{branch}"],
        capture_output=True, text=True, cwd=str(r),
    )
    if result.stdout.strip():
        print(f"Discarding commits:\n{result.stdout.strip()}")

    wt = r / config.workspace.root / f"agent-{sid}"
    remove_worktree(r, wt, branch)

    # Clean up any review artifacts
    _cleanup_review_artifacts(r, sid, config)

    # Remove session
    ss = sessions_dir() / sid
    if ss.exists():
        shutil.rmtree(ss)

    # Close issue in tracker
    try:
        tracker = create_tracker(config, repo_dir=str(r))
        tracker.set_status(a.issue_id, "closed")
        tracker.add_comment(a.issue_id, "🛑 Rejected — agent work discarded.")
        tracker.sync()
    except Exception as e:
        print(f"Warning: failed to close issue in tracker: {e}", file=sys.stderr)

    print(f"Rejected and cleaned up {sid}")


def cmd_revise(a):
    """Resume agent with review feedback."""
    r = repo_root()
    sid = resolve_session(a.issue_id)
    sd = sessions_dir() / sid

    if not sd.exists() or not (sd / "state.json").exists():
        print(f"No session found for {sid}", file=sys.stderr)
        sys.exit(1)

    state = read_state(sd)
    if state.get("status") not in ("waiting:review", "waiting:human-review"):
        print(f"Session {sid} is not awaiting review (status: {state.get('status')})",
              file=sys.stderr)
        sys.exit(1)

    # Collect review comments from tracker
    config = load_workflow(a.workflow or r / "WORKFLOW.md")
    tracker = create_tracker(config, repo_dir=str(r))
    review_comments = collect_review_feedback(tracker, a.issue_id)

    # Combine with inline feedback
    inline = a.message if hasattr(a, "message") and a.message else None
    feedback = build_revise_prompt(review_comments, inline)

    if not feedback.strip() or (not review_comments and not inline):
        print("No review feedback found. Add comments to the issue or pass inline feedback.",
              file=sys.stderr)
        sys.exit(1)

    # Write resume prompt and update state
    (sd / "resume-prompt.md").write_text(feedback)
    update_status(sd, "working")

    print(f"Revising {sid} with {len(review_comments)} comment(s)" +
          (f" + inline feedback" if inline else ""))

    # Delegate to launch.py --resume
    cmd = [sys.executable, str(Path(__file__).parent / "launch.py"),
           a.issue_id, "--resume"]
    if a.workflow:
        cmd += ["--workflow", a.workflow]
    subprocess.run(cmd)


def cmd_cleanup(a):
    r = repo_root()
    sid = resolve_session(a.issue_id)
    config = load_workflow(r / "WORKFLOW.md")

    wt = r / config.workspace.root / f"agent-{sid}"
    remove_worktree(r, wt, f"agent/{sid}")

    ss = sessions_dir() / sid
    if ss.exists() and not a.keep_session:
        shutil.rmtree(ss)
    print(f"Cleaned up {sid}")


def _register_session_commands(s):
    """Register session lifecycle commands (start, resume, answer, accept, reject, revise, cleanup)."""
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

    sp = s.add_parser("revise", help="Resume agent with review feedback")
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
    sp.set_defaults(func=cmd_history)

    sp = s.add_parser("init", help="Scaffold WORKFLOW.md and .env.example")
    sp.add_argument("--force", action="store_true", help="Overwrite existing files")
    sp.set_defaults(func=cmd_init)

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
