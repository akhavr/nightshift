"""Issue data dumper — writes issue.json and issues.json for the container.

The container's StaticTracker reads these files instead of hitting the
real tracker over the network.
"""

import json
import sys
from dataclasses import asdict
from pathlib import Path

from core.config import create_tracker


def dump_issue_data(config, repo: Path, session_dir: Path,
                    issue_id: str, is_review: bool, is_resume: bool):
    """Dump issue data to session dir for the static tracker inside the container."""
    issue_json = session_dir / "issue.json"
    issues_json = session_dir / "issues.json"

    if is_review and issue_json.exists():
        return  # Already copied from coder session

    tracker = create_tracker(config, repo_dir=str(repo))
    issue = tracker.get_issue(issue_id)

    if not issue and is_resume and issue_json.exists():
        print(f"Tracker unavailable, reusing cached issue data for resume")
    elif not issue:
        print(f"Issue {issue_id} not found", file=sys.stderr)
        sys.exit(1)
    else:
        issue_json.write_text(json.dumps(asdict(issue), indent=2))
        all_issues = tracker.list_issues()
        issues_json.write_text(
            json.dumps([asdict(i) for i in all_issues], indent=2)
        )
        print(f"Dumped issue + {len(all_issues)} issues to {session_dir}")
