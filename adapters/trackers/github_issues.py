"""GitHub Issues adapter (sketch). Would use requests or PyGithub."""

from typing import Optional
from core.protocols import IssueTracker, TrackerIssue, TrackerComment


class GitHubIssuesTracker:
    def __init__(self, repo: str, token: str):
        # repo = "owner/repo", token = GitHub PAT
        self.repo = repo
        self.token = token

    def get_issue(self, issue_id: str) -> Optional[TrackerIssue]:
        # GET /repos/{owner}/{repo}/issues/{number}
        raise NotImplementedError

    def add_comment(self, issue_id: str, body: str) -> None:
        # POST /repos/{owner}/{repo}/issues/{number}/comments
        raise NotImplementedError

    def run_raw(self, *args: str) -> str:
        raise NotImplementedError("GitHubIssuesTracker does not support raw CLI passthrough")

    # etc.
