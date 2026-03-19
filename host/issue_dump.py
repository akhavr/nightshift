"""Issue data dumper — writes issue.json and issues.json for the container.

The container's StaticTracker reads these files instead of hitting the
real tracker over the network.

Also supports re-dumping for live sync: the host watcher calls
redump_issue() periodically so the container sees new comments.
"""

import json
import logging
import sys
from dataclasses import asdict
from pathlib import Path

from core.config import create_tracker
from core.protocols import TrackerComment

log = logging.getLogger(__name__)


def _issue_dict_with_comments(tracker, issue_id: str) -> dict | None:
    """Build an issue dict including comments for the container."""
    issue = tracker.get_issue(issue_id)
    if not issue:
        return None
    d = asdict(issue)
    try:
        comments = tracker.get_comments(issue_id)
        d["comments"] = [asdict(c) for c in comments]
    except Exception as e:
        log.warning(f"Failed to fetch comments for {issue_id}: {e}")
        d["comments"] = []
    return d


def dump_issue_data(config, repo: Path, session_dir: Path,
                    issue_id: str, is_review: bool, is_resume: bool):
    """Dump issue data to session dir for the static tracker inside the container."""
    issue_json = session_dir / "issue.json"
    issues_json = session_dir / "issues.json"

    if is_review and issue_json.exists():
        return  # Already copied from coder session

    tracker = create_tracker(config, repo_dir=str(repo))
    issue_dict = _issue_dict_with_comments(tracker, issue_id)

    if not issue_dict and is_resume and issue_json.exists():
        print(f"Tracker unavailable, reusing cached issue data for resume")
    elif not issue_dict:
        print(f"Issue {issue_id} not found", file=sys.stderr)
        sys.exit(1)
    else:
        issue_json.write_text(json.dumps(issue_dict, indent=2))
        all_issues = tracker.list_issues()
        issues_json.write_text(
            json.dumps([asdict(i) for i in all_issues], indent=2)
        )
        print(f"Dumped issue + {len(all_issues)} issues to {session_dir}")


def redump_issue(tracker, issue_id: str, session_dir: Path) -> bool:
    """Re-dump issue.json with fresh data including comments.

    Called by the host watcher to keep the container's StaticTracker
    in sync with the real tracker.

    Returns True if issue.json was updated, False on error or no issue.
    """
    issue_dict = _issue_dict_with_comments(tracker, issue_id)
    if not issue_dict:
        log.warning(f"redump_issue: issue {issue_id} not found")
        return False

    issue_json = session_dir / "issue.json"
    tmp = issue_json.with_suffix(".tmp")
    try:
        tmp.write_text(json.dumps(issue_dict, indent=2))
        tmp.rename(issue_json)
    except OSError as e:
        log.warning(f"redump_issue: failed to write {issue_json}: {e}")
        return False
    return True
