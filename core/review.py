"""Shared review feedback logic — used by CLI and watcher."""

import re
from typing import Optional

BOT_PREFIXES = ("💭", "🤖", "❓", "📌", "⚠️", "✅", "⏸️", "🔄", "👤", "💬", "🛑", "🏁")

# Matches @nightshift <command> anywhere except inside backtick-quoted text
_NIGHTSHIFT_CMD_RE = re.compile(r"@nightshift\s+(revise|accept|reject)\b", re.IGNORECASE)
_FENCED_BLOCK_RE = re.compile(r"```[\s\S]*?```")
_INLINE_CODE_RE = re.compile(r"`[^`]+`")


def parse_nightshift_command(text: str) -> Optional[str]:
    """Extract @nightshift command from text, ignoring quoted sections.

    Returns the command string ('revise', 'accept', 'reject') or None.
    """
    # Strip code blocks and inline code before searching
    stripped = _FENCED_BLOCK_RE.sub("", text)
    stripped = _INLINE_CODE_RE.sub("", stripped)
    m = _NIGHTSHIFT_CMD_RE.search(stripped)
    return m.group(1).lower() if m else None


def strip_nightshift_command(text: str) -> str:
    """Remove @nightshift <command> from text, return the rest as feedback."""
    return _NIGHTSHIFT_CMD_RE.sub("", text).strip()


def collect_review_feedback(tracker, issue_id: str) -> list:
    """Get human comments posted after the last proof-of-work summary."""
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
