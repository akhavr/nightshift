"""Template versioning and upgrade logic for WORKFLOW.md and REVIEW.md prompt sections."""

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

# Missing template_version is treated as version 0.0
DEFAULT_VERSION = 0


class TemplateVersion:
    """Represents a template version with major.minor semantics.

    Stored as 'major.minor' in YAML front matter (e.g., '1.0', '2.1').
    Plain integers are treated as 'N.0' for backward compatibility.
    Minor bumps are for additive changes; major bumps are for behavioral
    changes (replacements, consolidations).
    """

    def __init__(self, major: int = 0, minor: int = 0):
        self.major = major
        self.minor = minor

    @classmethod
    def parse(cls, value) -> "TemplateVersion":
        """Parse a version from YAML value (int, float, or string)."""
        if value is None:
            return cls(0, 0)
        s = str(value).strip()
        if "." in s:
            parts = s.split(".", 1)
            return cls(int(parts[0]), int(parts[1]))
        return cls(int(s), 0)

    def is_major_bump_from(self, old: "TemplateVersion") -> bool:
        """Return True if upgrading from old to self is a major bump."""
        return self.major > old.major

    def __str__(self) -> str:
        return f"{self.major}.{self.minor}"

    def __eq__(self, other) -> bool:
        if isinstance(other, TemplateVersion):
            return self.major == other.major and self.minor == other.minor
        return NotImplemented

    def __lt__(self, other) -> bool:
        if isinstance(other, TemplateVersion):
            return (self.major, self.minor) < (other.major, other.minor)
        return NotImplemented

    def __le__(self, other) -> bool:
        if isinstance(other, TemplateVersion):
            return (self.major, self.minor) <= (other.major, other.minor)
        return NotImplemented

    def __ge__(self, other) -> bool:
        if isinstance(other, TemplateVersion):
            return (self.major, self.minor) >= (other.major, other.minor)
        return NotImplemented

    def __gt__(self, other) -> bool:
        if isinstance(other, TemplateVersion):
            return (self.major, self.minor) > (other.major, other.minor)
        return NotImplemented

    def __repr__(self) -> str:
        return f"TemplateVersion({self.major}, {self.minor})"


def read_template_version(text: str) -> TemplateVersion:
    """Extract template_version from a template file's text.

    Returns TemplateVersion(0, 0) if missing.
    Supports both integer (e.g. 1) and dotted (e.g. 1.1) formats.
    """
    fm, _ = split_front_matter(text)
    if not fm:
        return TemplateVersion(0, 0)
    try:
        raw = yaml.safe_load(fm)
        if not isinstance(raw, dict):
            return TemplateVersion(0, 0)
        return TemplateVersion.parse(raw.get(VERSION_KEY, 0))
    except (yaml.YAMLError, ValueError, TypeError) as e:
        log.warning("Failed to parse template_version: %s", e)
        return TemplateVersion(0, 0)


def get_canonical_version() -> TemplateVersion:
    """Read the template_version from the shipped canonical template."""
    if not CANONICAL_TEMPLATE.exists():
        log.warning("Canonical template not found at %s", CANONICAL_TEMPLATE)
        return TemplateVersion(0, 0)
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


def _set_version_in_yaml(yaml_text: str, version) -> str:
    """Set or update template_version in the raw YAML string.

    Accepts int, str, or TemplateVersion. Preserves the rest of the YAML
    content as-is (no re-serialization).
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


def get_canonical_review_version() -> TemplateVersion:
    """Read the template_version from the shipped canonical review template."""
    if not CANONICAL_REVIEW_TEMPLATE.exists():
        log.warning("Canonical review template not found at %s", CANONICAL_REVIEW_TEMPLATE)
        return TemplateVersion(0, 0)
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
