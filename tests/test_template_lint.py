"""Template lint suite for canonical templates (REQ-027).

These tests gate template changes by enforcing:
- No project-specific references (via configurable blocklist)
- No absolute paths or specific module references
- Prompt size under hard cap (150 lines), warning at soft cap (100 lines)
- All Jinja2 variables in the known set
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.upstream import (
    count_prompt_lines,
    validate_jinja2_vars,
    validate_no_blocklist_terms,
    load_blocklist,
    PROMPT_HARD_CAP_LINES,
    PROMPT_SOFT_CAP_LINES,
    KNOWN_JINJA2_VARS,
)
from core.upgrade import (
    CANONICAL_TEMPLATE,
    CANONICAL_REVIEW_TEMPLATE,
    get_prompt_section,
)

# Absolute path patterns that should never appear in templates
_ABS_PATH_PATTERNS = ["/home/", "/usr/", "/opt/", "/var/", "/etc/", "C:\\"]


def _load_template(path: Path) -> str:
    """Load a canonical template, skip if not found."""
    if not path.exists():
        pytest.skip(f"Template not found: {path}")
    return path.read_text()


class TestWorkflowTemplateLint:
    """Lint checks for the canonical WORKFLOW.md template."""

    def test_no_unknown_jinja2_vars(self):
        text = _load_template(CANONICAL_TEMPLATE)
        unknown = validate_jinja2_vars(text)
        assert unknown == [], (
            f"Unknown Jinja2 variables in WORKFLOW.md: {unknown}. "
            f"Known set: {sorted(KNOWN_JINJA2_VARS)}")

    def test_prompt_under_hard_cap(self):
        text = _load_template(CANONICAL_TEMPLATE)
        lines = count_prompt_lines(text)
        assert lines <= PROMPT_HARD_CAP_LINES, (
            f"WORKFLOW.md prompt has {lines} lines, exceeds hard cap "
            f"of {PROMPT_HARD_CAP_LINES}")

    def test_prompt_under_soft_cap(self):
        text = _load_template(CANONICAL_TEMPLATE)
        lines = count_prompt_lines(text)
        if lines > PROMPT_SOFT_CAP_LINES:
            pytest.skip(
                f"WORKFLOW.md at {lines}/{PROMPT_HARD_CAP_LINES} lines "
                f"(soft cap: {PROMPT_SOFT_CAP_LINES}) - consider consolidation")

    def test_no_absolute_paths(self):
        text = _load_template(CANONICAL_TEMPLATE)
        prompt = get_prompt_section(text)
        for pattern in _ABS_PATH_PATTERNS:
            assert pattern not in prompt, (
                f"Absolute path pattern '{pattern}' found in WORKFLOW.md prompt")

    def test_no_blocklist_terms(self):
        text = _load_template(CANONICAL_TEMPLATE)
        blocklist = load_blocklist()
        found = validate_no_blocklist_terms(text, blocklist)
        assert found == [], (
            f"Blocklist terms found in WORKFLOW.md: {found}")

    def test_workflow_has_feedback_logging_section(self):
        """REQ-035: WORKFLOW.md must have a Feedback Logging section."""
        text = _load_template(CANONICAL_TEMPLATE)
        prompt = get_prompt_section(text)
        assert "## Feedback Logging" in prompt, (
            "WORKFLOW.md must have a '## Feedback Logging' section for REQ-035")
        assert "reviewer-issues.yaml" in prompt, (
            "WORKFLOW.md Feedback Logging must reference reviewer-issues.yaml")


class TestReviewTemplateLint:
    """Lint checks for the canonical REVIEW.md template."""

    def test_no_unknown_jinja2_vars(self):
        text = _load_template(CANONICAL_REVIEW_TEMPLATE)
        unknown = validate_jinja2_vars(text)
        assert unknown == [], (
            f"Unknown Jinja2 variables in REVIEW.md: {unknown}. "
            f"Known set: {sorted(KNOWN_JINJA2_VARS)}")

    def test_prompt_under_hard_cap(self):
        text = _load_template(CANONICAL_REVIEW_TEMPLATE)
        lines = count_prompt_lines(text)
        assert lines <= PROMPT_HARD_CAP_LINES, (
            f"REVIEW.md prompt has {lines} lines, exceeds hard cap "
            f"of {PROMPT_HARD_CAP_LINES}")

    def test_prompt_under_soft_cap(self):
        text = _load_template(CANONICAL_REVIEW_TEMPLATE)
        lines = count_prompt_lines(text)
        if lines > PROMPT_SOFT_CAP_LINES:
            pytest.skip(
                f"REVIEW.md at {lines}/{PROMPT_HARD_CAP_LINES} lines "
                f"(soft cap: {PROMPT_SOFT_CAP_LINES}) - consider consolidation")

    def test_no_absolute_paths(self):
        text = _load_template(CANONICAL_REVIEW_TEMPLATE)
        prompt = get_prompt_section(text)
        for pattern in _ABS_PATH_PATTERNS:
            assert pattern not in prompt, (
                f"Absolute path pattern '{pattern}' found in REVIEW.md prompt")

    def test_no_blocklist_terms(self):
        text = _load_template(CANONICAL_REVIEW_TEMPLATE)
        blocklist = load_blocklist()
        found = validate_no_blocklist_terms(text, blocklist)
        assert found == [], (
            f"Blocklist terms found in REVIEW.md: {found}")

    def test_review_has_feedback_logging_section(self):
        """REQ-035: REVIEW.md must have a Feedback Logging section."""
        text = _load_template(CANONICAL_REVIEW_TEMPLATE)
        prompt = get_prompt_section(text)
        assert "## Feedback Logging" in prompt, (
            "REVIEW.md must have a '## Feedback Logging' section for REQ-035")
        assert "coder-issues.yaml" in prompt, (
            "REVIEW.md Feedback Logging must reference coder-issues.yaml")
