#!/usr/bin/env python3
"""Container entrypoint — reads WORKFLOW.md, instantiates adapters, runs session.

No hardcoded adapter imports. Adapter selection is driven by WORKFLOW.md.
"""

import json
import logging
import os
import subprocess
import sys
from pathlib import Path

from adapters.notifiers.composite import CompositeNotifier
from adapters.trackers.static import StaticTracker
from core.config import (
    load_workflow, create_agent, create_tracker,
    create_workspace_mgr, create_notifiers,
)
from core.constants import MERGE_NEEDED_FILENAME
from core.prompts import render_template, build_initial_prompt
from core.protocols import Workspace
from core.state import StateManager
from core.session import SessionRunner
from core.search import search_related_issues

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s")
log = logging.getLogger("entrypoint")


def _current_branch(repo_dir: str) -> str:
    try:
        return subprocess.check_output(
            ["git", "branch", "--show-current"],
            cwd=repo_dir, stderr=subprocess.DEVNULL,
        ).decode().strip()
    except (subprocess.CalledProcessError, FileNotFoundError):
        return "unknown"


def _read_diff() -> str:
    """Read pre-generated diff from session dir (created by host-side _prepare_review_session)."""
    diff_path = Path("/session/diff.patch")
    if diff_path.exists():
        return diff_path.read_text().strip()
    return "N/A"


def _read_merge_instructions(session_dir: str) -> str | None:
    """Read and consume merge-needed.txt if it exists.

    Returns merge instructions string, or None if no merge is needed.
    The file is deleted after reading so it is only consumed once.
    """
    merge_path = Path(session_dir) / MERGE_NEEDED_FILENAME
    if not merge_path.exists():
        return None

    content = merge_path.read_text().strip()
    merge_path.unlink()
    log.info("Consumed %s — agent will be instructed to merge", MERGE_NEEDED_FILENAME)

    # Parse the merge target from the file
    lines = content.split("\n")
    base_branch = ""
    for line in lines:
        if line.startswith("base_branch:"):
            base_branch = line.split(":", 1)[1].strip()
            break

    conflict_details = ""
    sep_idx = content.find("---\n")
    if sep_idx != -1:
        conflict_details = content[sep_idx + 4:].strip()

    return (
        f"MERGE NEEDED: The base branch ({base_branch}) has advanced while you were "
        f"working. Before continuing your task, you must merge the latest changes.\n\n"
        f"Conflict details from the host-side merge attempt:\n"
        f"{conflict_details}\n\n"
        f"Please:\n"
        f"1. Run `git fetch origin {base_branch}` to get latest changes\n"
        f"2. Run `git merge origin/{base_branch}` (or `git merge {base_branch}`)\n"
        f"3. Resolve any merge conflicts\n"
        f"4. Commit the merge\n"
        f"5. Run the test suite to confirm no regressions\n"
        f"6. Then continue with your original task\n"
    )


def _build_prompt(config, issue, related, workspace, state_mgr, tracker,
                  issue_id, resume, step):
    """Build or load the agent prompt."""
    if resume and (p := state_mgr.read_resume_prompt()):
        tracker.add_comment(issue_id, f"🤖 Resuming from step {state_mgr.load_state().step}...")
        # Prepend merge instructions if merge-needed.txt exists
        merge_instructions = _read_merge_instructions("/session")
        if merge_instructions:
            p = merge_instructions + "\n---\n\n" + p
        return p

    # On resume without a resume-prompt, still check for merge instructions
    if resume:
        merge_instructions = _read_merge_instructions("/session")
        if merge_instructions:
            tracker.add_comment(issue_id, f"🤖 Resuming with merge instructions...")
            state_mgr.update_status("working")
            base_prompt = ""
            if config.prompt_template:
                base_prompt = render_template(
                    config.prompt_template, issue=issue,
                    related_context=related, attempt=None,
                )
            else:
                base_prompt = build_initial_prompt(issue.title, issue.body, related)
            return merge_instructions + "\n---\n\n" + base_prompt

    tracker.add_label(issue_id, "agent-in-progress")
    tracker.add_comment(issue_id, f"🤖 Starting on {issue.identifier}")
    state_mgr.update_status("working")

    if config.prompt_template:
        extra_vars = {}
        if step == "review":
            extra_vars["diff"] = _read_diff()
            extra_vars["base_branch"] = os.environ.get("BASE_BRANCH", "master")
            extra_vars["agent_branch"] = workspace.branch
        return render_template(
            config.prompt_template, issue=issue,
            related_context=related, attempt=None,
            **extra_vars,
        )
    return build_initial_prompt(issue.title, issue.body, related)


def _create_adapters(config):
    """Instantiate all adapters from config."""
    tracker = StaticTracker(session_dir="/session")
    agent = create_agent(config)
    workspace_mgr = create_workspace_mgr(config, repo_root=Path("/workspace"))
    workspace = Workspace(
        path=Path("/workspace"),
        branch=_current_branch("/workspace"),
        is_new=False,
    )
    state_mgr = StateManager("/session")
    notifiers = create_notifiers(config, tracker=tracker)
    notifier = CompositeNotifier(notifiers)
    return tracker, agent, workspace_mgr, workspace, state_mgr, notifier


def main():
    issue_id = os.environ["ISSUE_ID"]
    resume = os.environ.get("RESUME") == "--resume"
    step = os.environ.get("STEP", "")

    config = load_workflow(Path("/workspace/WORKFLOW.md"))

    # Extend agent extra_args with overflow args injected by host
    overflow_args_raw = os.environ.get("OVERFLOW_EXTRA_ARGS", "")
    if overflow_args_raw:
        try:
            overflow_args = json.loads(overflow_args_raw)
            config.agent.extra_args = list(overflow_args) + config.agent.extra_args
        except json.JSONDecodeError as e:
            log.error(f"Failed to parse OVERFLOW_EXTRA_ARGS: {e}")

    max_turns = int(os.environ.get("MAX_TURNS", config.agent.max_turns))

    tracker, agent, workspace_mgr, workspace, state_mgr, notifier = _create_adapters(config)
    notifier.start()

    issue = tracker.get_issue(issue_id)
    if not issue:
        log.error(f"Issue {issue_id} not found")
        sys.exit(1)

    related = search_related_issues(issue, tracker.list_issues(), tracker)
    prompt = _build_prompt(config, issue, related, workspace, state_mgr,
                           tracker, issue_id, resume, step)

    runner = SessionRunner(
        agent=agent, tracker=tracker, notifier=notifier,
        workspace_mgr=workspace_mgr, state_mgr=state_mgr,
        issue=issue, prompt=prompt, max_turns=max_turns,
        terminal_statuses=tuple(config.terminal_statuses),
        merge_config=config.merge,
        hooks_config=config.hooks,
        workspace_config=config.workspace,
        is_review=(step == "review"),
    )

    try:
        runner.run(workspace=workspace)
    finally:
        notifier.stop()


if __name__ == "__main__":
    main()
