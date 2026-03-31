"""Upstream proposal logic: reverse diff, validation, and operation type detection.

Used by `nightshift upstream` to propose local prompt improvements back to
the canonical templates in the agent-worker repo (REQ-027).
"""

import logging
import re
from pathlib import Path

from core.upgrade import (
    TEMPLATES_DIR,
    get_prompt_section,
    diff_prompt_sections,
)

log = logging.getLogger(__name__)

# Guardrail limits (in lines of the prompt section)
PROMPT_HARD_CAP_LINES = 150
PROMPT_SOFT_CAP_LINES = 100

BLOCKLIST_PATH = TEMPLATES_DIR / "blocklist.txt"

# Known Jinja2 variables available in templates
KNOWN_JINJA2_VARS = frozenset({
    "issue.title",
    "issue.body",
    "issue.identifier",
    "related_context",
    "attempt",
    "diff",
    "base_branch",
    "agent_branch",
})

# Regex to find Jinja2 variable references: {{ var }} and {% if var %}
_JINJA2_VAR_RE = re.compile(r"\{\{[\s]*([a-zA-Z_.]+)[\s]*\}\}")
_JINJA2_IF_RE = re.compile(r"\{%[\s]*if[\s]+([a-zA-Z_.]+)")


class UpstreamProposal:
    """Represents a validated upstream proposal ready to be filed."""

    def __init__(self, template_label: str, operation: str,
                 diff_text: str, project_name: str,
                 new_line_count: int):
        self.template_label = template_label
        self.operation = operation
        self.diff_text = diff_text
        self.project_name = project_name
        self.new_line_count = new_line_count

    def format_issue_body(self) -> str:
        """Format the proposal as an issue body for the upstream tracker."""
        return (
            f"## Upstream Template Proposal\n\n"
            f"**Template:** {self.template_label}\n"
            f"**Operation:** {self.operation}\n"
            f"**Source project:** {self.project_name}\n"
            f"**New prompt line count:** {self.new_line_count}\n\n"
            f"### Diff\n\n"
            f"```diff\n{self.diff_text}\n```\n"
        )


def load_blocklist() -> list[str]:
    """Load project-specific term blocklist from templates/blocklist.txt.

    Each non-empty, non-comment line is a case-insensitive term to reject.
    Returns an empty list if the file does not exist.
    """
    if not BLOCKLIST_PATH.exists():
        return []
    terms = []
    for line in BLOCKLIST_PATH.read_text().splitlines():
        stripped = line.strip()
        if stripped and not stripped.startswith("#"):
            terms.append(stripped)
    return terms


def diff_reverse(project_text: str, canonical_text: str,
                 label: str = "WORKFLOW.md") -> str:
    """Generate a unified diff showing local changes vs canonical (reversed).

    The diff shows what the project has added/changed relative to canonical,
    with canonical as the "from" and project as the "to".
    """
    return diff_prompt_sections(canonical_text, project_text, label=label)


def detect_operation(canonical_text: str, project_text: str) -> str:
    """Detect the operation type based on the diff between canonical and project.

    Returns one of: 'add', 'replace', 'consolidate', 'none'.
    """
    canonical_lines = get_prompt_section(canonical_text).strip().splitlines()
    project_lines = get_prompt_section(project_text).strip().splitlines()

    canonical_set = set(canonical_lines)
    project_set = set(project_lines)

    added = project_set - canonical_set
    removed = canonical_set - project_set

    if not added and not removed:
        return "none"

    if not removed and added:
        return "add"

    if removed and added and len(project_lines) < len(canonical_lines):
        return "consolidate"

    if removed and added:
        return "replace"

    # Lines only removed (unlikely for upstream proposals)
    return "consolidate"


def extract_jinja2_vars(text: str) -> set[str]:
    """Extract all Jinja2 variable references from a template's prompt section."""
    prompt = get_prompt_section(text)
    vars_found = set()
    for match in _JINJA2_VAR_RE.finditer(prompt):
        vars_found.add(match.group(1))
    for match in _JINJA2_IF_RE.finditer(prompt):
        vars_found.add(match.group(1))
    return vars_found


def count_prompt_lines(text: str) -> int:
    """Count the number of lines in the prompt section."""
    prompt = get_prompt_section(text).strip()
    if not prompt:
        return 0
    return len(prompt.splitlines())


def validate_no_blocklist_terms(text: str, blocklist: list[str]) -> list[str]:
    """Check prompt section for project-specific terms from the blocklist.

    Returns a list of found terms (empty means clean).
    """
    prompt = get_prompt_section(text).lower()
    found = []
    for term in blocklist:
        if term.lower() in prompt:
            found.append(term)
    return found


def validate_jinja2_vars(text: str) -> list[str]:
    """Check that all Jinja2 variables are from the known set.

    Returns a list of unknown variables (empty means clean).
    """
    found = extract_jinja2_vars(text)
    unknown = sorted(found - KNOWN_JINJA2_VARS)
    return unknown


def validate_line_count(text: str, operation: str) -> tuple[str, str] | None:
    """Validate prompt line count against limits.

    Returns a (level, message) tuple where level is 'error' or 'warning',
    or None if OK. 'error' means the proposal should be rejected;
    'warning' is informational only.
    """
    lines = count_prompt_lines(text)

    if operation == "add" and lines > PROMPT_HARD_CAP_LINES:
        return ("error",
                f"Add operation would result in {lines} lines, "
                f"exceeding the hard cap of {PROMPT_HARD_CAP_LINES}. "
                f"Consider a consolidation instead.")

    if lines > PROMPT_SOFT_CAP_LINES:
        return ("warning",
                f"Template at {lines}/{PROMPT_HARD_CAP_LINES} lines "
                f"(soft cap: {PROMPT_SOFT_CAP_LINES}). "
                f"Consider consolidation.")

    return None


def validate_proposal(project_text: str, operation: str) -> list[str]:
    """Run all client-side validation filters on a proposal.

    Returns a list of issues found (empty means the proposal is clean).
    """
    issues = []

    # Check blocklist
    blocklist = load_blocklist()
    found_terms = validate_no_blocklist_terms(project_text, blocklist)
    if found_terms:
        issues.append(
            f"Project-specific terms found: {', '.join(found_terms)}. "
            f"Remove project-specific references before upstreaming.")

    # Check Jinja2 vars
    unknown_vars = validate_jinja2_vars(project_text)
    if unknown_vars:
        issues.append(
            f"Unknown Jinja2 variables: {', '.join(unknown_vars)}. "
            f"Only these are allowed: {', '.join(sorted(KNOWN_JINJA2_VARS))}")

    # Check line count
    line_result = validate_line_count(project_text, operation)
    if line_result and line_result[0] == "error":
        issues.append(line_result[1])

    return issues


def build_proposal(project_text: str, label: str, project_name: str,
                    operation: str, diff_text: str) -> UpstreamProposal:
    """Build an UpstreamProposal from pre-computed operation and diff.

    Callers must pass operation (from detect_operation) and diff_text
    (from diff_reverse) to avoid redundant computation.
    Does not perform validation — call validate_proposal() first.
    """
    line_count = count_prompt_lines(project_text)

    return UpstreamProposal(
        template_label=label,
        operation=operation,
        diff_text=diff_text,
        project_name=project_name,
        new_line_count=line_count,
    )
