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


def _build_prompt(config, issue, related, workspace, state_mgr, tracker,
                  issue_id, resume, step):
    """Build or load the agent prompt."""
    if resume and (p := state_mgr.read_resume_prompt()):
        tracker.add_comment(issue_id, f"🤖 Resuming from step {state_mgr.load_state().step}...")
        return p

    tracker.add_label(issue_id, "agent-in-progress")
    tracker.add_comment(issue_id, f"🤖 Starting on {issue.identifier}")
    state_mgr.update_status("working")

    if config.prompt_template:
        from core.prompts import render_template
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
    from core.prompts import build_initial_prompt
    return build_initial_prompt(issue.title, issue.body, related)


def _create_adapters(config):
    """Instantiate all adapters from config."""
    from adapters.trackers.static import StaticTracker
    from adapters.notifiers.composite import CompositeNotifier

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
    )

    try:
        runner.run(workspace=workspace)
    finally:
        notifier.stop()


if __name__ == "__main__":
    main()
