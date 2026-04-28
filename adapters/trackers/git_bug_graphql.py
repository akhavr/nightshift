"""git-bug GraphQL API tracker adapter."""

from __future__ import annotations

import atexit
import json
import logging
import socket
import subprocess
import time
import shutil
from pathlib import Path
from typing import Any

import requests

from core.protocols import SHORT_ID_LEN, TrackerComment, TrackerIssue

log = logging.getLogger(__name__)

_BIND_HOST = "127.0.0.1"
_GRAPHQL_PATH = "/graphql"
_READY_TIMEOUT_S = 30
_READY_POLL_INTERVAL_S = 0.1
_REQUEST_TIMEOUT_S = 30
_SHUTDOWN_TIMEOUT_S = 5
_QUERY_PAGE_SIZE = 100
_RAW_CMD_TIMEOUT_S = 30
_LOCK_CONFLICT_TEXT = "already locked by the process pid"
_GITBUG_CACHE_BUGS = Path(".git") / "git-bug" / "cache" / "bugs"

# Commands with NO GraphQL equivalent - cannot run while webui is active
_CLI_ONLY_COMMANDS = {"pull", "push", "user", "bridge", "webui", "termui"}

_BUG_FIELDS = """
id
title
status
createdAt
lastEdit
labels { name }
comments(first: 100) {
  nodes {
    author { displayName name login }
    message
  }
}
"""


class GitBugGraphQLError(RuntimeError):
    def __init__(self, errors: Any, data: dict[str, Any] | None = None):
        super().__init__(f"git-bug GraphQL error: {errors}")
        self.errors = errors
        self.data = data or {}


class GitBugGraphQLTracker:
    """IssueTracker implementation backed by `git-bug webui` GraphQL."""

    def __init__(self, repo_dir: str | Path = "/workspace", sync: bool = False):
        self.cwd = str(repo_dir)
        self._sync_enabled = sync
        self.port = _find_free_port()
        self.graphql_url = f"http://{_BIND_HOST}:{self.port}{_GRAPHQL_PATH}"
        self.request_timeout_s = _REQUEST_TIMEOUT_S
        self._proc = self._start_webui()
        atexit.register(self.terminate)
        try:
            self._wait_until_ready()
        except Exception as e:
            log.warning("Failed to start git-bug GraphQL tracker for %s: %s", self.cwd, e)
            self.terminate()
            raise

    def _start_webui(self) -> subprocess.Popen:
        cmd = [
            "git-bug", "webui", "--no-open", "--host", _BIND_HOST,
            "--port", str(self.port),
        ]
        return subprocess.Popen(
            cmd,
            cwd=self.cwd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            text=True,
        )

    def _wait_until_ready(self) -> None:
        deadline = time.monotonic() + _READY_TIMEOUT_S
        last_error: Exception | None = None
        while time.monotonic() < deadline:
            if self._proc.poll() is not None:
                raise RuntimeError("git-bug webui exited before GraphQL became ready")
            try:
                response = requests.head(
                    self.graphql_url,
                    timeout=self.request_timeout_s,
                )
                if response.status_code < 500:
                    return
            except Exception as e:
                log.debug("git-bug GraphQL readiness check failed: %s", e)
                last_error = e
            time.sleep(_READY_POLL_INTERVAL_S)
        raise TimeoutError(f"git-bug GraphQL did not become ready: {last_error}")

    def _query(self, query: str, variables: dict[str, Any] | None = None) -> dict[str, Any]:
        response = requests.post(
            self.graphql_url,
            json={"query": query, "variables": variables or {}},
            timeout=self.request_timeout_s,
        )
        response.raise_for_status()
        payload = response.json()
        if payload.get("errors"):
            raise GitBugGraphQLError(payload["errors"], payload.get("data") or {})
        return payload.get("data", {})

    def get_issue(self, issue_id: str) -> TrackerIssue | None:
        data = self._query(_GET_ISSUE_QUERY, {"id": self._short(issue_id)})
        bug = data.get("repository", {}).get("bug")
        return _to_issue(bug) if bug else None

    def list_issues(self, status: str | list[str] | None = None) -> list[TrackerIssue]:
        statuses = {status} if isinstance(status, str) else set(status or [])
        try:
            issues = [_to_issue(bug) for bug in self._bug_nodes()]
        except Exception as exc:
            if not self._recover_stale_cache(exc):
                raise
            issues = [_to_issue(bug) for bug in self._bug_nodes()]
        if not statuses:
            return issues
        return [issue for issue in issues if issue.status in statuses]

    def _recover_stale_cache(self, exc: Exception) -> bool:
        if not self._is_stale_cache_error(exc):
            return False

        context = self._stale_cache_context(exc)
        if context:
            log.warning("git-bug cache stale while listing issues (%s); rebuilding cache", context)
        else:
            log.warning("git-bug cache stale while listing issues; rebuilding cache")

        try:
            self.rebuild_cache()
        except Exception as e:
            log.error("Failed to rebuild git-bug cache: %s", e)
            return False
        return True

    def rebuild_cache(self) -> None:
        """Clear the persisted cache and restart the GraphQL webui."""
        self.clear_cache()
        self.restart_webui()

    @staticmethod
    def _is_stale_cache_error(exc: Exception) -> bool:
        return "bug doesn't exist" in str(exc)

    @staticmethod
    def _stale_cache_context(exc: Exception) -> str | None:
        details: list[str] = []

        bug_id = GitBugGraphQLTracker._stale_cache_bug_id(exc)
        if bug_id:
            details.append(f"bug={bug_id}")

        text = str(exc)
        if "path:" in text:
            details.append(f"path={text.split('path:', 1)[1].strip()}")

        return ", ".join(details) or None

    @staticmethod
    def _stale_cache_bug_id(exc: Exception) -> str | None:
        data = getattr(exc, "data", None)
        if not isinstance(data, dict):
            return None

        nodes = (
            data.get("repository", {})
            .get("allBugs", {})
            .get("nodes", [])
        )
        if not isinstance(nodes, list):
            return None

        for node in nodes:
            if isinstance(node, dict):
                bug_id = node.get("id")
                if bug_id:
                    return str(bug_id)
        return None

    def _bug_nodes(self) -> list[dict[str, Any]]:
        after = None
        bugs: list[dict[str, Any]] = []
        while True:
            data = self._query(
                _LIST_ISSUES_QUERY,
                {"first": _QUERY_PAGE_SIZE, "after": after},
            )
            conn = data.get("repository", {}).get("allBugs", {})
            bugs.extend(conn.get("nodes") or [])
            page = conn.get("pageInfo") or {}
            if not page.get("hasNextPage"):
                return bugs
            after = page.get("endCursor")

    def get_comments(self, issue_id: str) -> list[TrackerComment]:
        data = self._query(_GET_COMMENTS_QUERY, {"id": self._short(issue_id)})
        bug = data.get("repository", {}).get("bug") or {}
        return [_to_comment(comment) for comment in _connection_nodes(bug.get("comments"))]

    def add_comment(self, issue_id: str, body: str) -> None:
        self._query(_ADD_COMMENT_MUTATION, {"id": self._short(issue_id), "body": body})

    def create_issue(self, title: str, body: str) -> str:
        data = self._query(_CREATE_ISSUE_MUTATION, {"title": title, "message": body})
        return data["bugCreate"]["bug"]["id"]

    def add_label(self, issue_id: str, label: str) -> None:
        self._change_labels(issue_id, added=[label], removed=[])

    def remove_label(self, issue_id: str, label: str) -> None:
        self._change_labels(issue_id, added=[], removed=[label])

    def _change_labels(self, issue_id: str, added: list[str], removed: list[str]) -> None:
        self._query(
            _CHANGE_LABELS_MUTATION,
            {"id": self._short(issue_id), "added": added, "removed": removed},
        )

    def set_status(self, issue_id: str, status: str) -> None:
        mutation = _STATUS_CLOSE_MUTATION if status == "closed" else _STATUS_OPEN_MUTATION
        self._query(mutation, {"id": self._short(issue_id)})

    def _show_issue(self, issue_id: str, fmt: str | None) -> str:
        """Fetch and format an issue for 'bug show' output."""
        data = self._query(_GET_ISSUE_QUERY, {"id": self._short(issue_id)})
        bug = data.get("repository", {}).get("bug")
        if not bug:
            return f"bug {issue_id} not found"

        if fmt == "json":
            return self._format_issue_json(bug)
        return self._format_issue_default(bug)

    def _format_issue_json(self, bug: dict[str, Any]) -> str:
        """Format issue as JSON for 'bug show -f json'."""
        comments = _connection_nodes(bug.get("comments"))
        return json.dumps({
            "id": bug.get("id", ""),
            "title": bug.get("title", ""),
            "status": str(bug.get("status", "")).lower(),
            "createdAt": bug.get("createdAt"),
            "lastEdit": bug.get("lastEdit"),
            "labels": _label_names(bug.get("labels")),
            "comments": [
                {
                    "author": _author_name(c.get("author")),
                    "message": c.get("message", ""),
                }
                for c in comments
            ],
        })

    def _format_issue_default(self, bug: dict[str, Any]) -> str:
        """Format issue as human-readable text for 'bug show'."""
        issue_id = bug.get("id", "")
        title = bug.get("title", "Unknown")
        status = str(bug.get("status", "unknown")).lower()
        labels = _label_names(bug.get("labels"))
        comments = _connection_nodes(bug.get("comments"))

        lines = [
            f"bug {issue_id[:SHORT_ID_LEN]}",
            f"Title: {title}",
            f"Status: {status}",
        ]
        if labels:
            lines.append(f"Labels: {', '.join(labels)}")
        lines.append("")

        for i, c in enumerate(comments):
            author = _author_name(c.get("author"))
            message = c.get("message", "")
            lines.append(f"#{i} {author}")
            lines.append(message)
            lines.append("")

        return "\n".join(lines).strip()

    def sync(self) -> None:
        """No-op: git-bug webui does not expose pull/push operations."""
        return None

    def _is_webui_alive(self) -> bool:
        """Check if the webui subprocess is still running."""
        return self._proc.poll() is None

    def run_raw(self, *args: str) -> str:
        if self._is_webui_alive():
            # Check for known CLI-only commands (no GraphQL equivalent)
            if len(args) >= 1 and args[0] in _CLI_ONLY_COMMANDS:
                raise RuntimeError(
                    f"'{args[0]}' has no GraphQL equivalent and cannot run while webui is active. "
                    "Stop the webui or use 'tracker.kind: git-bug' (CLI-only tracker)."
                )

            result = self._run_raw_via_graphql(args)
            if result is not None:
                return result

            # Unrecognized command - might be mappable but isn't yet
            raise RuntimeError(
                f"Command '{' '.join(args)}' not recognized by GraphQL router. "
                "Either add parsing support in _parse_raw_command(), or run without webui."
            )
        return self._run_raw_via_cli(args)

    def _run_raw_via_graphql(self, args: tuple[str, ...]) -> str | None:
        """Try to route the command through GraphQL mutations.

        Returns the result string on success, or None if the command cannot be
        mapped to a GraphQL mutation (caller should fall back to CLI).
        """
        parsed = self._parse_raw_command(args)
        if parsed is None:
            return None

        method, method_args = parsed
        try:
            if method == "add_comment":
                self.add_comment(*method_args)
                return ""
            elif method == "create_issue":
                issue_id = self.create_issue(*method_args)
                return issue_id
            elif method == "add_label":
                self.add_label(*method_args)
                return ""
            elif method == "remove_label":
                self.remove_label(*method_args)
                return ""
            elif method == "set_status":
                self.set_status(*method_args)
                return ""
            elif method == "show_issue":
                return self._show_issue(*method_args)
        except Exception as e:
            log.warning("GraphQL mutation failed for %s: %s", args, e)
            return ""
        return None

    def _parse_raw_command(self, args: tuple[str, ...]) -> tuple[str, tuple[str, ...]] | None:
        """Parse raw CLI args into a method name and arguments.

        Returns (method_name, method_args) or None if the command cannot be mapped.
        """
        if len(args) < 2:
            return None

        if args[0] != "bug":
            return None

        cmd = args[1]
        rest = args[2:]

        if cmd == "comment" and len(rest) >= 4:
            if rest[0] == "new":
                issue_id = rest[1]
                message = self._extract_arg(rest[2:], ("-m", "--message"))
                if message is not None:
                    return ("add_comment", (issue_id, message))

        elif cmd == "add":
            title = self._extract_arg(rest, ("-t", "--title"))
            message = self._extract_arg(rest, ("-m", "--message"))
            if title is not None and message is not None:
                return ("create_issue", (title, message))

        elif cmd == "label" and len(rest) >= 3:
            if rest[0] in ("new", "add"):
                return ("add_label", (rest[1], rest[2]))
            elif rest[0] in ("rm", "remove"):
                return ("remove_label", (rest[1], rest[2]))

        elif cmd == "status" and len(rest) >= 2:
            if rest[0] == "close":
                return ("set_status", (rest[1], "closed"))
            elif rest[0] == "open":
                return ("set_status", (rest[1], "open"))

        elif cmd == "show" and len(rest) >= 1:
            issue_id = rest[0]
            fmt = self._extract_arg(rest[1:], ("-f", "--format"))
            return ("show_issue", (issue_id, fmt))

        return None

    @staticmethod
    def _extract_arg(args: tuple[str, ...], flags: tuple[str, ...]) -> str | None:
        """Extract argument value for given flags from args."""
        for i, arg in enumerate(args):
            if arg in flags and i + 1 < len(args):
                return args[i + 1]
        return None

    def _run_raw_via_cli(self, args: tuple[str, ...]) -> str:
        """Fall back to running the command via CLI subprocess."""
        try:
            result = subprocess.run(
                ["git-bug", *args],
                cwd=self.cwd,
                capture_output=True,
                text=True,
                timeout=_RAW_CMD_TIMEOUT_S,
            )
        except (OSError, subprocess.TimeoutExpired) as e:
            log.warning("git-bug raw command failed for %s: %s", args, e)
            return ""
        if result.returncode != 0:
            stderr = result.stderr.strip()
            if _LOCK_CONFLICT_TEXT in stderr:
                log.warning(
                    "git-bug raw command hit repository lock for %s while webui is running: %s",
                    args,
                    stderr,
                )
            else:
                log.warning("git-bug raw command failed for %s: %s", args, stderr)
        return result.stdout.strip()

    def clear_cache(self) -> None:
        """Remove the persisted git-bug bug cache so it can be rebuilt."""
        cache_path = Path(self.cwd) / _GITBUG_CACHE_BUGS
        if not cache_path.exists():
            return
        if cache_path.is_dir():
            shutil.rmtree(cache_path)
        else:
            cache_path.unlink()
        log.info("Cleared git-bug cache at %s", cache_path)

    def restart_webui(self) -> None:
        """Restart the git-bug webui process in place."""
        self.terminate()
        self._proc = self._start_webui()
        self._wait_until_ready()

    def terminate(self) -> None:
        proc = getattr(self, "_proc", None)
        if proc is None or proc.poll() is not None:
            return
        proc.terminate()
        try:
            proc.wait(timeout=_SHUTDOWN_TIMEOUT_S)
        except subprocess.TimeoutExpired as e:
            log.warning("git-bug webui did not terminate gracefully: %s", e)
            proc.kill()
            proc.wait()

    def shutdown(self) -> None:
        self.terminate()

    def terminate_current(self) -> None:
        self.terminate()

    @staticmethod
    def _short(issue_id: str) -> str:
        return issue_id[:SHORT_ID_LEN]


def _find_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind((_BIND_HOST, 0))
        return int(sock.getsockname()[1])


def _connection_nodes(connection: dict[str, Any] | None) -> list[dict[str, Any]]:
    if not connection:
        return []
    return connection.get("nodes") or []


def _label_names(labels: dict[str, Any] | list[Any] | None) -> list[str]:
    if isinstance(labels, dict):
        return [item.get("name", "") for item in _connection_nodes(labels)]
    if isinstance(labels, list):
        return [item.get("name", "") if isinstance(item, dict) else str(item) for item in labels]
    return []


def _author_name(author: dict[str, Any] | None) -> str:
    if not author:
        return "unknown"
    return author.get("displayName") or author.get("name") or author.get("login") or "unknown"


def _to_comment(comment: dict[str, Any]) -> TrackerComment:
    return TrackerComment(
        author=_author_name(comment.get("author")),
        body=comment.get("message", ""),
        created_at=comment.get("createdAt") or comment.get("created_at"),
    )


def _to_issue(bug: dict[str, Any]) -> TrackerIssue:
    comments = _connection_nodes(bug.get("comments"))
    issue_id = bug.get("id", "")
    return TrackerIssue(
        id=issue_id,
        identifier=issue_id[:SHORT_ID_LEN],
        title=bug.get("title", "Unknown"),
        body=comments[0].get("message", "") if comments else "",
        status=str(bug.get("status", "unknown")).lower(),
        labels=[name.lower() for name in _label_names(bug.get("labels")) if name],
        created_at=bug.get("createdAt") or bug.get("created_at"),
        updated_at=bug.get("lastEdit") or bug.get("updatedAt") or bug.get("updated_at"),
    )


_GET_ISSUE_QUERY = f"""
query GetIssue($id: String!) {{
  repository {{
    bug(prefix: $id) {{
      {_BUG_FIELDS}
    }}
  }}
}}
"""

_GET_COMMENTS_QUERY = f"""
query GetComments($id: String!) {{
  repository {{
    bug(prefix: $id) {{
      comments(first: 100) {{
        nodes {{
          author {{ displayName name login }}
          message
        }}
      }}
    }}
  }}
}}
"""

_LIST_ISSUES_QUERY = f"""
query ListIssues($first: Int!, $after: String) {{
  repository {{
    allBugs(first: $first, after: $after) {{
      nodes {{
        {_BUG_FIELDS}
      }}
      pageInfo {{ hasNextPage endCursor }}
    }}
  }}
}}
"""

_ADD_COMMENT_MUTATION = """
mutation AddComment($id: String!, $body: String!) {
  bugAddComment(input: {prefix: $id, message: $body}) {
    bug { id }
  }
}
"""

_CREATE_ISSUE_MUTATION = """
mutation CreateBug($title: String!, $message: String!) {
  bugCreate(input: {title: $title, message: $message}) {
    bug { id }
  }
}
"""

_CHANGE_LABELS_MUTATION = """
mutation ChangeLabels($id: String!, $added: [String!], $removed: [String!]) {
  bugChangeLabels(input: {prefix: $id, added: $added, Removed: $removed}) {
    bug { id }
  }
}
"""

_STATUS_OPEN_MUTATION = """
mutation OpenBug($id: String!) {
  bugStatusOpen(input: {prefix: $id}) {
    bug { id }
  }
}
"""

_STATUS_CLOSE_MUTATION = """
mutation CloseBug($id: String!) {
  bugStatusClose(input: {prefix: $id}) {
    bug { id }
  }
}
"""
