"""Tests for entrypoint.py prompt assembly and docker-entrypoint.sh."""

import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from core.config.models import OverflowConfig


def test_prompt_snippet_appended_when_overflow_active():
    from entrypoint import _build_prompt

    config = MagicMock()
    config.prompt_template = None
    issue = MagicMock()
    issue.title = "Fix bug"
    issue.body = "Bug details"
    related = ""
    workspace = MagicMock()
    state_mgr = MagicMock()
    state_mgr.read_resume_prompt.return_value = None
    tracker = MagicMock()
    overflow_config = OverflowConfig(prompt_snippet="## Signal Protocol\nRun `touch /session/signal/done` when finished.")

    with patch("entrypoint._read_merge_instructions", return_value=None):
        prompt = _build_prompt(
            config, issue, related, workspace, state_mgr, tracker,
            "issue-1", resume=False, step="", overflow_config=overflow_config
        )

    assert prompt.startswith("You are working on the following issue:")
    assert "## Signal Protocol" in prompt
    assert prompt.rstrip().endswith("when finished.")


class TestDockerEntrypointGuardrails:
    """Tests for docker-entrypoint.sh guardrails."""

    def test_entrypoint_fails_on_empty_repo_git(self, tmp_path):
        """Entrypoint exits non-zero when /repo-git is empty."""
        # Create a minimal test script that checks the guardrail logic
        test_script = tmp_path / "test_guardrail.sh"
        test_script.write_text("""#!/bin/sh
# Simulate the guardrail check from docker-entrypoint.sh
REPO_GIT="$1"

if [ -d "$REPO_GIT" ] && [ -z "$(ls -A "$REPO_GIT" 2>/dev/null)" ]; then
    echo "ERROR: /repo-git mount is empty - git overlay setup failed" >&2
    exit 1
fi
exit 0
""")
        test_script.chmod(0o755)

        # Create an empty /repo-git directory
        repo_git = tmp_path / "repo-git"
        repo_git.mkdir()

        result = subprocess.run(
            [str(test_script), str(repo_git)],
            capture_output=True,
            text=True,
        )

        assert result.returncode == 1
        assert "empty" in result.stderr

    def test_entrypoint_passes_with_nonempty_repo_git(self, tmp_path):
        """Entrypoint passes when /repo-git has content."""
        test_script = tmp_path / "test_guardrail.sh"
        test_script.write_text("""#!/bin/sh
REPO_GIT="$1"

if [ -d "$REPO_GIT" ] && [ -z "$(ls -A "$REPO_GIT" 2>/dev/null)" ]; then
    echo "ERROR: /repo-git mount is empty - git overlay setup failed" >&2
    exit 1
fi
exit 0
""")
        test_script.chmod(0o755)

        # Create a non-empty /repo-git directory
        repo_git = tmp_path / "repo-git"
        repo_git.mkdir()
        (repo_git / "HEAD").write_text("ref: refs/heads/master\n")

        result = subprocess.run(
            [str(test_script), str(repo_git)],
            capture_output=True,
            text=True,
        )

        assert result.returncode == 0

    def test_entrypoint_passes_when_repo_git_missing(self, tmp_path):
        """Entrypoint passes when /repo-git doesn't exist (not mounted)."""
        test_script = tmp_path / "test_guardrail.sh"
        test_script.write_text("""#!/bin/sh
REPO_GIT="$1"

if [ -d "$REPO_GIT" ] && [ -z "$(ls -A "$REPO_GIT" 2>/dev/null)" ]; then
    echo "ERROR: /repo-git mount is empty - git overlay setup failed" >&2
    exit 1
fi
exit 0
""")
        test_script.chmod(0o755)

        # Directory doesn't exist
        nonexistent = tmp_path / "nonexistent"

        result = subprocess.run(
            [str(test_script), str(nonexistent)],
            capture_output=True,
            text=True,
        )

        assert result.returncode == 0
