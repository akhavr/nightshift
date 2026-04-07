"""Tests for core/prompts.py."""

import sys
from pathlib import Path
from unittest.mock import patch

from core.prompts import render_template, build_initial_prompt, build_resume_prompt
from core.state import StateManager, SessionState
from tests.conftest import make_test_issue


class TestRenderTemplate:
    def test_basic_template(self):
        issue = make_test_issue(title="Fix bug", body="It's broken")
        result = render_template("Title: {{ issue.title }}", issue)
        assert "Fix bug" in result

    def test_with_related_context(self):
        issue = make_test_issue()
        result = render_template("Related: {{ related_context }}", issue, related_context="prev fix")
        assert "prev fix" in result

    def test_with_attempt(self):
        issue = make_test_issue()
        result = render_template("Attempt {{ attempt }}", issue, attempt=3)
        assert "3" in result

    def test_with_extra_vars(self):
        issue = make_test_issue()
        result = render_template("Branch: {{ agent_branch }}", issue, agent_branch="agent/123")
        assert "agent/123" in result

    def test_conditional(self):
        issue = make_test_issue()
        template = "{% if related_context %}Has context{% endif %}"
        result = render_template(template, issue, related_context="some context")
        assert "Has context" in result

    def test_fallback_without_jinja2(self):
        """Test the simple fallback when jinja2 is not available."""
        issue = make_test_issue(title="Fix bug", body="It's broken")
        # Hide jinja2 from sys.modules so `import jinja2` raises ImportError
        real_jinja2 = sys.modules.pop("jinja2", None)
        real_jinja2_env = sys.modules.pop("jinja2.environment", None)
        try:
            with patch.dict(sys.modules, {"jinja2": None}):
                result = render_template(
                    "Title: {{ issue.title }}\nBody: {{ issue.body }}\n"
                    "ID: {{ issue.identifier }}\n"
                    "Related: {{ related_context }}\n"
                    "Attempt: {{ attempt }}\n"
                    "Extra: {{ custom_var }}\n"
                    "{% if something %}hidden{% endif %}",
                    issue, related_context="ctx", attempt=2, custom_var="val",
                )
        finally:
            if real_jinja2:
                sys.modules["jinja2"] = real_jinja2
            if real_jinja2_env:
                sys.modules["jinja2.environment"] = real_jinja2_env
        assert "Fix bug" in result
        assert "It's broken" in result
        assert "ctx" in result
        assert "2" in result
        assert "val" in result
        assert "{% if" not in result


class TestBuildInitialPrompt:
    def test_contains_issue_info(self):
        result = build_initial_prompt("Fix widget", "Widget is broken", "")
        assert "Fix widget" in result
        assert "Widget is broken" in result

    def test_markers_param_accepted_but_unused(self):
        """markers parameter is kept for backward compat but no longer used."""
        markers = {"log": "LOG", "checkpoint": "CP", "question": "Q", "waiting": "W", "done": "D"}
        result = build_initial_prompt("t", "b", "", markers=markers)
        # Markers should NOT appear in the output anymore
        assert "@@LOG@@" not in result
        assert "@@DONE@@" not in result


class TestBuildResumePrompt:
    def test_basic_resume(self, tmp_path):
        session_dir = tmp_path / "session"
        sm = StateManager(session_dir)
        sm._write(SessionState(issue_id="t1", branch="agent/t1", status="working"))
        sm.add_checkpoint("step one", 1, "abc1234")

        result = build_resume_prompt("Fix widget", "broken", "", sm)
        assert "Resuming work" in result
        assert "Fix widget" in result
        assert "step one" in result
        assert sm.resume_prompt_file.exists()

    def test_with_diff_fn(self, tmp_path):
        session_dir = tmp_path / "session"
        sm = StateManager(session_dir)
        sm._write(SessionState(issue_id="t1", branch="agent/t1", status="working"))

        result = build_resume_prompt("t", "b", "", sm, diff_fn=lambda: "1 file changed")
        assert "1 file changed" in result

    def test_with_checkpoint_summary(self, tmp_path):
        session_dir = tmp_path / "session"
        sm = StateManager(session_dir)
        sm._write(SessionState(issue_id="t1", branch="agent/t1", status="working"))

        result = build_resume_prompt("t", "b", "", sm, checkpoint_summary="Key decisions here")
        assert "Key decisions here" in result


class TestNoAtMarkers:
    """Phase 5: @@MARKER@@ instructions must not appear in rendered prompts."""

    AT_MARKERS = ["@@DONE@@", "@@CHECKPOINT@@", "@@QUESTION@@", "@@WAITING@@", "@@LOG@@"]

    def _render_workflow_prompt(self, agent_kind="claude-code"):
        issue = make_test_issue(title="Fix bug", body="It's broken")
        template = Path(__file__).parent.parent / "templates" / "WORKFLOW.md"
        content = template.read_text()
        parts = content.split("---", 2)
        prompt_body = parts[2] if len(parts) >= 3 else content
        return render_template(prompt_body, issue, agent_kind=agent_kind)

    def test_prompt_does_not_contain_at_markers(self):
        """Rendered WORKFLOW.md prompt for all agent kinds must NOT contain @@MARKER@@ instructions."""
        for agent_kind in ("claude-code", "openhands", "codex"):
            result = self._render_workflow_prompt(agent_kind=agent_kind)
            for marker in self.AT_MARKERS:
                assert marker not in result, (
                    f"Found {marker} in rendered prompt for agent_kind={agent_kind}"
                )

    def test_fallback_prompt_does_not_contain_at_markers(self):
        """build_initial_prompt() fallback must NOT contain @@MARKER@@ instructions."""
        result = build_initial_prompt("Fix widget", "Widget is broken", "")
        for marker in self.AT_MARKERS:
            assert marker not in result, f"Found {marker} in fallback prompt"


class TestSignalInstructions:
    def test_openhands_prompt_includes_signal_instructions(self):
        """When agent_kind is 'openhands', the rendered prompt contains signal file instructions."""
        issue = make_test_issue(title="Fix bug", body="It's broken")
        template = Path(__file__).parent.parent / "templates" / "WORKFLOW.md"
        # Read the prompt template (after the YAML front matter)
        content = template.read_text()
        # Extract prompt body after the closing ---
        parts = content.split("---", 2)
        prompt_body = parts[2] if len(parts) >= 3 else content

        result = render_template(prompt_body, issue, agent_kind="openhands")
        assert "/session/signal/done" in result
        assert "/session/signal/question.json" in result
        assert "/session/signal/checkpoint" in result
        assert "Signal Protocol" in result

    def test_non_openhands_prompt_no_signal_instructions(self):
        """When agent_kind is 'claude-code', the prompt does NOT contain signal file instructions."""
        issue = make_test_issue(title="Fix bug", body="It's broken")
        template = Path(__file__).parent.parent / "templates" / "WORKFLOW.md"
        content = template.read_text()
        parts = content.split("---", 2)
        prompt_body = parts[2] if len(parts) >= 3 else content

        result = render_template(prompt_body, issue, agent_kind="claude-code")
        assert "/session/signal/" not in result
