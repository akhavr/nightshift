"""Tests for entrypoint.py merge-needed.txt handling."""

from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

from core.constants import MERGE_NEEDED_FILENAME
from entrypoint import _read_merge_instructions


class TestReadMergeInstructions:

    def test_returns_none_when_no_file(self, tmp_path):
        """No merge-needed.txt means no merge instructions."""
        result = _read_merge_instructions(str(tmp_path))
        assert result is None

    def test_reads_and_deletes_merge_needed(self, tmp_path):
        """merge-needed.txt is read, instructions returned, and file deleted."""
        merge_file = tmp_path / MERGE_NEEDED_FILENAME
        merge_file.write_text(
            "merge_target: origin/master\n"
            "base_branch: master\n"
            "---\n"
            "CONFLICT (content): Merge conflict in file.txt\n"
        )

        result = _read_merge_instructions(str(tmp_path))

        assert result is not None
        assert "MERGE NEEDED" in result
        assert "master" in result
        assert "CONFLICT" in result
        assert "file.txt" in result
        # File should be consumed (deleted)
        assert not merge_file.exists()

    def test_instructions_contain_merge_steps(self, tmp_path):
        """Instructions should tell the agent how to merge."""
        merge_file = tmp_path / MERGE_NEEDED_FILENAME
        merge_file.write_text(
            "merge_target: origin/main\n"
            "base_branch: main\n"
            "---\n"
            "CONFLICT (content): Merge conflict in src/app.py\n"
        )

        result = _read_merge_instructions(str(tmp_path))

        assert "git fetch origin main" in result
        assert "git merge" in result
        assert "Resolve any merge conflicts" in result
        assert "test suite" in result

    def test_file_deleted_even_on_empty_content(self, tmp_path):
        """Even an empty merge-needed.txt should be consumed."""
        merge_file = tmp_path / MERGE_NEEDED_FILENAME
        merge_file.write_text("")

        result = _read_merge_instructions(str(tmp_path))

        assert result is not None  # still returns instructions (just minimal)
        assert not merge_file.exists()


class TestBuildPromptWithMerge:
    """Test that _build_prompt prepends merge instructions on resume."""

    @patch("entrypoint.render_template", return_value="template prompt")
    @patch("entrypoint.search_related_issues", return_value="")
    def test_resume_prompt_gets_merge_prepended(self, mock_search,
                                                  mock_render, tmp_path):
        """When resume-prompt.md and merge-needed.txt both exist,
        merge instructions are prepended."""
        from entrypoint import _build_prompt

        # Set up mock objects
        config = MagicMock()
        config.prompt_template = "template"
        issue = MagicMock()
        issue.identifier = "test-001"
        related = ""
        workspace = MagicMock()
        state_mgr = MagicMock()
        state_mgr.read_resume_prompt.return_value = "Continue working on the bug."
        mock_state = MagicMock()
        mock_state.step = 3
        state_mgr.load_state.return_value = mock_state
        tracker = MagicMock()

        # Write merge-needed.txt
        session_dir = tmp_path / "session"
        session_dir.mkdir()
        (session_dir / MERGE_NEEDED_FILENAME).write_text(
            "merge_target: origin/master\n"
            "base_branch: master\n"
            "---\n"
            "CONFLICT in file.txt\n"
        )

        with patch("entrypoint._read_merge_instructions") as mock_read:
            mock_read.return_value = "MERGE NEEDED: instructions here"

            result = _build_prompt(config, issue, related, workspace,
                                   state_mgr, tracker, "test-001",
                                   resume=True, step="")

        assert "MERGE NEEDED" in result
        assert "Continue working on the bug" in result

    @patch("entrypoint.render_template", return_value="rebuilt base prompt")
    def test_resume_no_resume_prompt_with_merge_rebuilds_prompt(
            self, mock_render, tmp_path):
        """When resume=True, no resume-prompt.md, but merge-needed.txt exists,
        merge instructions are prepended to a rebuilt base prompt."""
        from entrypoint import _build_prompt

        config = MagicMock()
        config.prompt_template = "template"
        issue = MagicMock()
        issue.identifier = "test-002"
        issue.title = "Fix bug"
        issue.body = "Details"
        state_mgr = MagicMock()
        state_mgr.read_resume_prompt.return_value = None  # no resume-prompt.md
        tracker = MagicMock()

        with patch("entrypoint._read_merge_instructions") as mock_read:
            mock_read.return_value = "MERGE NEEDED: merge master first"

            result = _build_prompt(config, issue, "", MagicMock(),
                                   state_mgr, tracker, "test-002",
                                   resume=True, step="")

        assert "MERGE NEEDED" in result
        assert "rebuilt base prompt" in result
        tracker.add_comment.assert_called_once()
        state_mgr.update_status.assert_called_once_with("working")

    @patch("entrypoint.build_initial_prompt", return_value="fallback prompt")
    def test_resume_no_resume_prompt_merge_uses_fallback(
            self, mock_fallback, tmp_path):
        """When resume=True, no resume-prompt.md, no template, but merge-needed.txt
        exists, uses fallback prompt builder."""
        from entrypoint import _build_prompt

        config = MagicMock()
        config.prompt_template = None  # no template
        issue = MagicMock()
        issue.identifier = "test-003"
        issue.title = "Fix bug"
        issue.body = "Details"
        state_mgr = MagicMock()
        state_mgr.read_resume_prompt.return_value = None
        tracker = MagicMock()

        with patch("entrypoint._read_merge_instructions") as mock_read:
            mock_read.return_value = "MERGE NEEDED: merge master first"

            result = _build_prompt(config, issue, "", MagicMock(),
                                   state_mgr, tracker, "test-003",
                                   resume=True, step="")

        assert "MERGE NEEDED" in result
        assert "fallback prompt" in result

    @patch("entrypoint.render_template", return_value="template prompt")
    def test_no_merge_file_resume_prompt_unchanged(self, mock_render, tmp_path):
        """When there's no merge-needed.txt, resume prompt is unchanged."""
        from entrypoint import _build_prompt

        config = MagicMock()
        config.prompt_template = "template"
        issue = MagicMock()
        issue.identifier = "test-001"
        state_mgr = MagicMock()
        state_mgr.read_resume_prompt.return_value = "Continue working."
        mock_state = MagicMock()
        mock_state.step = 1
        state_mgr.load_state.return_value = mock_state
        tracker = MagicMock()

        with patch("entrypoint._read_merge_instructions", return_value=None):
            result = _build_prompt(config, issue, "", MagicMock(),
                                   state_mgr, tracker, "test-001",
                                   resume=True, step="")

        assert result == "Continue working."
