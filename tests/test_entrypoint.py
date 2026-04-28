"""Tests for entrypoint.py prompt assembly."""

from unittest.mock import MagicMock, patch

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
