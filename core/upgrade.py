"""Template versioning and upgrade logic for WORKFLOW.md prompt sections."""

import difflib
import logging
from pathlib import Path

import yaml

from core.config.loader import split_front_matter

log = logging.getLogger(__name__)

TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"
CANONICAL_TEMPLATE = TEMPLATES_DIR / "WORKFLOW.md"
CANONICAL_REVIEW_TEMPLATE = TEMPLATES_DIR / "REVIEW.md"

# Field name in YAML front matter
VERSION_KEY = "template_version"

# Missing template_version is treated as version 0
DEFAULT_VERSION = 0


def read_template_version(text: str) -> int:
    """Extract template_version from WORKFLOW.md text. Returns 0 if missing."""
    fm, _ = split_front_matter(text)
    if not fm:
        return DEFAULT_VERSION
    try:
        raw = yaml.safe_load(fm)
        if not isinstance(raw, dict):
            return DEFAULT_VERSION
        return int(raw.get(VERSION_KEY, DEFAULT_VERSION))
    except (yaml.YAMLError, ValueError, TypeError) as e:
        log.warning("Failed to parse template_version: %s", e)
        return DEFAULT_VERSION


def get_canonical_version() -> int:
    """Read the template_version from the shipped canonical template."""
    if not CANONICAL_TEMPLATE.exists():
        log.warning("Canonical template not found at %s", CANONICAL_TEMPLATE)
        return DEFAULT_VERSION
    return read_template_version(CANONICAL_TEMPLATE.read_text())


def get_prompt_section(text: str) -> str:
    """Extract the prompt section (everything after the closing --- fence)."""
    _, prompt = split_front_matter(text)
    return prompt


def get_yaml_section(text: str) -> str:
    """Extract the raw YAML front matter string (without --- fences)."""
    fm, _ = split_front_matter(text)
    return fm


def diff_prompt_sections(project_text: str, canonical_text: str,
                         label: str = "WORKFLOW.md") -> str:
    """Generate a unified diff of the prompt section only.

    Returns an empty string if the prompt sections are identical.
    """
    project_prompt = get_prompt_section(project_text).splitlines(keepends=True)
    canonical_prompt = get_prompt_section(canonical_text).splitlines(keepends=True)

    diff_lines = list(difflib.unified_diff(
        project_prompt,
        canonical_prompt,
        fromfile=f"current {label} (prompt section)",
        tofile="canonical template (prompt section)",
    ))

    return "".join(diff_lines)


def _set_version_in_yaml(yaml_text: str, version: int) -> str:
    """Set or update template_version in the raw YAML string.

    Preserves the rest of the YAML content as-is (no re-serialization).
    """
    lines = yaml_text.splitlines(keepends=True)
    version_line = f"{VERSION_KEY}: {version}\n"

    for i, line in enumerate(lines):
        stripped = line.lstrip()
        if stripped.startswith(f"{VERSION_KEY}:"):
            lines[i] = version_line
            return "".join(lines)

    # Not found — insert at the beginning (after any leading whitespace)
    insert_idx = 0
    for i, line in enumerate(lines):
        if line.strip():
            insert_idx = i
            break
    lines.insert(insert_idx, version_line)
    return "".join(lines)


def load_canonical_template(base_branch: str = "main") -> str:
    """Load the canonical template with base_branch substituted.

    Used by ``cmd_init`` so there is a single source of truth for the
    default WORKFLOW.md content.
    """
    if not CANONICAL_TEMPLATE.exists():
        log.warning("Canonical template not found at %s", CANONICAL_TEMPLATE)
        return ""
    text = CANONICAL_TEMPLATE.read_text()
    return text.replace("base_branch: main", f"base_branch: {base_branch}")


def get_canonical_review_version() -> int:
    """Read the template_version from the shipped canonical review template."""
    if not CANONICAL_REVIEW_TEMPLATE.exists():
        log.warning("Canonical review template not found at %s", CANONICAL_REVIEW_TEMPLATE)
        return DEFAULT_VERSION
    return read_template_version(CANONICAL_REVIEW_TEMPLATE.read_text())


def load_canonical_review_template() -> str:
    """Load the canonical review template.

    Used by ``cmd_init`` so there is a single source of truth for the
    default REVIEW.md content.
    """
    if not CANONICAL_REVIEW_TEMPLATE.exists():
        log.warning("Canonical review template not found at %s", CANONICAL_REVIEW_TEMPLATE)
        return ""
    return CANONICAL_REVIEW_TEMPLATE.read_text()


def apply_upgrade(project_text: str, canonical_text: str) -> str:
    """Apply the canonical prompt section to a project template file.

    Preserves the project's YAML config (except bumps template_version).
    Returns the full updated file content.
    """
    canonical_version = read_template_version(canonical_text)
    project_yaml = get_yaml_section(project_text)
    canonical_prompt = get_prompt_section(canonical_text)

    updated_yaml = _set_version_in_yaml(project_yaml, canonical_version)

    return f"---{updated_yaml}---{canonical_prompt}"
