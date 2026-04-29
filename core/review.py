"""Shared review feedback logic — used by CLI and watcher."""

import re
from typing import Optional

import logging

log = logging.getLogger(__name__)

BOT_PREFIXES = ("💭", "🤖", "❓", "📌", "⚠️", "✅", "⏸️", "🔄", "👤", "💬", "🛑", "🏁")

# Matches @nightshift <command> anywhere except inside backtick-quoted text
_NIGHTSHIFT_CMD_RE = re.compile(r"@nightshift\s+(revise|accept|reject|approve)\b", re.IGNORECASE)
_FENCED_BLOCK_RE = re.compile(r"```[\s\S]*?```")
_INLINE_CODE_RE = re.compile(r"`[^`]+`")

# Flexible verdict patterns (checked in order of priority)
_VERDICT_PATTERNS = [
    # 1. @nightshift command (most reliable)
    re.compile(r"@nightshift\s+(approve|revise|reject)\b", re.IGNORECASE),
    # 2. Bold format: **APPROVE**, **REVISE**, **REJECT**
    re.compile(r"\*\*(approve|revise|reject)\*\*", re.IGNORECASE),
    # 3. Heading format: Verdict: APPROVE or ### Verdict\nAPPROVE
    re.compile(r"(?:^|\n)#+\s*Verdict[:\s]*\n?(approve|revise|reject)\b", re.IGNORECASE | re.MULTILINE),
    re.compile(r"Verdict[:\s]+(approve|revise|reject)\b", re.IGNORECASE),
    # 4. Standalone line: just the verdict word on its own line
    re.compile(r"(?:^|\n)(approve|revise|reject)(?:\n|$)", re.IGNORECASE),
]


def parse_nightshift_command(text: str) -> Optional[str]:
    """Extract @nightshift command from text, ignoring quoted sections.

    Returns the command string ('revise', 'accept', 'reject') or None.
    """
    # Strip code blocks and inline code before searching
    stripped = _FENCED_BLOCK_RE.sub("", text)
    stripped = _INLINE_CODE_RE.sub("", stripped)
    m = _NIGHTSHIFT_CMD_RE.search(stripped)
    return m.group(1).lower() if m else None


def parse_verdict(text: str) -> Optional[str]:
    """Extract verdict from text using flexible pattern matching.

    Tries patterns in order of reliability:
    1. @nightshift approve/revise/reject (most reliable)
    2. **APPROVE**/**REVISE**/**REJECT** (bold format)
    3. Verdict: APPROVE or ### Verdict\\nAPPROVE (heading format)
    4. Standalone APPROVE/REVISE/REJECT on its own line

    Returns 'approve', 'revise', or 'reject', or None if no verdict found.
    Logs a warning when fallback patterns are used (indicates prompt needs work).
    """
    # Strip code blocks and inline code before searching
    stripped = _FENCED_BLOCK_RE.sub("", text)
    stripped = _INLINE_CODE_RE.sub("", stripped)

    for i, pattern in enumerate(_VERDICT_PATTERNS):
        m = pattern.search(stripped)
        if m:
            verdict = m.group(1).lower()
            if i > 0:
                log.warning(
                    f"Verdict '{verdict}' extracted via fallback pattern (not @nightshift). "
                    "Consider strengthening reviewer instructions."
                )
            return verdict

    return None


def strip_nightshift_command(text: str) -> str:
    """Remove @nightshift <command> from text, return the rest as feedback."""
    return _NIGHTSHIFT_CMD_RE.sub("", text).strip()


def collect_review_feedback(tracker, issue_id: str, sync: bool = True) -> list:
    """Get human comments posted after the last proof-of-work summary."""
    if sync:
        tracker.sync()
    comments = tracker.get_comments(issue_id)

    # Find the last proof-of-work comment
    pow_idx = -1
    for i, c in enumerate(comments):
        if "Work complete" in c.body and "awaiting review" in c.body:
            pow_idx = i

    if pow_idx == -1:
        candidates = comments
    else:
        candidates = comments[pow_idx + 1:]

    return [c for c in candidates
            if not any(c.body.startswith(p) for p in BOT_PREFIXES)]


def build_revise_prompt(review_comments, inline_feedback=None) -> str:
    """Build a resume prompt from review feedback."""
    parts = ["## Review Feedback\n",
             "Your previous work has been reviewed. Please address the following feedback:\n"]

    if review_comments:
        for c in review_comments:
            parts.append(f"**{c.author}:** {c.body}\n")

    if inline_feedback:
        parts.append(f"**Reviewer:** {inline_feedback}\n")

    parts.append("\n## Instructions")
    parts.append("Address ALL the review feedback above.")
    parts.append("The codebase already has your previous work on this branch.")
    parts.append("Same marker rules apply (@@LOG@@, @@CHECKPOINT@@, @@DONE@@, etc.).")
    parts.append("When all feedback is addressed: @@DONE@@")

    return "\n".join(parts)
