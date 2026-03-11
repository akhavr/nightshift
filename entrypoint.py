#!/usr/bin/env python3
"""Container entrypoint — reads WORKFLOW.md, instantiates adapters, runs session.

No hardcoded adapter imports. Adapter selection is driven by WORKFLOW.md.
"""

import logging
import os
import sys
from pathlib import Path

from core.config import (
    load_workflow, create_agent, create_tracker,
    create_workspace_mgr, create_notifiers,
)
from core.protocols import Workspace
from core.state import StateManager
from core.session import SessionRunner
from core.search import search_related_issues

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s")
log = logging.getLogger("entrypoint")


def _current_branch(repo_dir: str) -> str:
    import subprocess
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


def main():
    issue_id = os.environ["ISSUE_ID"]
    resume = os.environ.get("RESUME") == "--resume"
    step = os.environ.get("STEP", "")

    # Load config from WORKFLOW.md (mounted into container at /workspace)
    workflow_path = Path("/workspace/WORKFLOW.md")
    config = load_workflow(workflow_path)

    # Override max_turns from env if set (CLI takes precedence over WORKFLOW.md)
    max_turns = int(os.environ.get("MAX_TURNS", config.agent.max_turns))

    # Use static tracker inside container (issue data pre-dumped by host)
    from adapters.trackers.static import StaticTracker
    tracker = StaticTracker(session_dir="/session")
    agent = create_agent(config)

    # Inside the container, /workspace is already set up by the host
    # (worktree created and mounted). Build a Workspace directly.
    workspace_mgr = create_workspace_mgr(config, repo_root=Path("/workspace"))
    workspace = Workspace(
        path=Path("/workspace"),
        branch=_current_branch("/workspace"),
        is_new=False,
    )
    state_mgr = StateManager("/session")

    # Notifiers (may include Telegram, webhook, etc.)
    notifiers = create_notifiers(config, tracker=tracker)

    # Wrap in composite: broadcasts to all, Q&A through primary
    from adapters.notifiers.composite import CompositeNotifier
    notifier = CompositeNotifier(notifiers)
    notifier.start()

    # Load issue
    issue = tracker.get_issue(issue_id)
    if not issue:
        log.error(f"Issue {issue_id} not found")
        sys.exit(1)

    # Search related issues
    related = search_related_issues(issue, tracker.list_issues(), tracker)

    # Build or load prompt
    if resume and (p := state_mgr.read_resume_prompt()):
        tracker.add_comment(issue_id, f"🤖 Resuming from step {state_mgr.load_state().step}...")
        prompt = p
    else:
        tracker.add_label(issue_id, "agent-in-progress")
        tracker.add_comment(issue_id, f"🤖 Starting on {issue.identifier}")
        state_mgr.update_status("working")

        if config.prompt_template:
            from core.prompts import render_template
            # Extra template variables for review step
            extra_vars = {}
            if step == "review":
                extra_vars["diff"] = _read_diff()
                extra_vars["base_branch"] = os.environ.get("BASE_BRANCH", "master")
                extra_vars["agent_branch"] = workspace.branch
            prompt = render_template(
                config.prompt_template, issue=issue,
                related_context=related, attempt=None,
                **extra_vars,
            )
        else:
            from core.prompts import build_initial_prompt
            prompt = build_initial_prompt(issue.title, issue.body, related)

    runner = SessionRunner(
        agent=agent, tracker=tracker, notifier=notifier,
        workspace_mgr=workspace_mgr, state_mgr=state_mgr,
        issue=issue, prompt=prompt, max_turns=max_turns,
        terminal_statuses=tuple(config.terminal_statuses),
        merge_config=config.merge,
        hooks_config=config.hooks,
    )

    try:
        runner.run(workspace=workspace)
    finally:
        notifier.stop()


if __name__ == "__main__":
    main()
