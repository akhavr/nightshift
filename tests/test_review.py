"""Tests for core/review.py — command parsing, feedback collection, prompt building."""

from dataclasses import dataclass

from core.review import (
    parse_nightshift_command, strip_nightshift_command,
    collect_review_feedback, build_revise_prompt,
)


# --- Command parsing ---

def test_parse_revise():
    assert parse_nightshift_command("Fix the bug. @nightshift revise") == "revise"


def test_parse_accept():
    assert parse_nightshift_command("Looks good! @nightshift accept") == "accept"


def test_parse_reject():
    assert parse_nightshift_command("@nightshift reject") == "reject"


def test_parse_case_insensitive():
    assert parse_nightshift_command("@Nightshift Revise") == "revise"


def test_parse_command_in_middle():
    assert parse_nightshift_command("Please @nightshift revise this code") == "revise"


def test_parse_no_command():
    assert parse_nightshift_command("Just a normal comment") is None


def test_parse_ignores_inline_code():
    assert parse_nightshift_command("Use `@nightshift revise` to trigger") is None


def test_parse_ignores_fenced_code():
    text = "Example:\n```\n@nightshift revise\n```\nThat's how."
    assert parse_nightshift_command(text) is None


def test_parse_outside_code_block():
    text = "```\nsome code\n```\n@nightshift revise"
    assert parse_nightshift_command(text) == "revise"


def test_parse_with_comma():
    # @nightshift, revise — comma breaks the pattern, won't match
    assert parse_nightshift_command("@nightshift, revise") is None


# --- Strip command ---

def test_strip_command():
    assert strip_nightshift_command("Fix error handling. @nightshift revise") == "Fix error handling."


def test_strip_preserves_rest():
    assert strip_nightshift_command("@nightshift accept and done") == "and done"


# --- Feedback collection ---

@dataclass
class FakeComment:
    author: str
    body: str
    created_at: str = ""


class FakeTracker:
    def __init__(self, comments):
        self._comments = comments

    def sync(self):
        pass

    def get_comments(self, issue_id):
        return self._comments


def test_collect_after_proof_of_work():
    comments = [
        FakeComment("bot", "🏁 **Work complete — awaiting review**\nChanges..."),
        FakeComment("alice", "Fix the error handling"),
        FakeComment("bob", "Also add tests. @nightshift revise"),
    ]
    tracker = FakeTracker(comments)
    result = collect_review_feedback(tracker, "test-001")
    assert len(result) == 2
    assert result[0].author == "alice"
    assert result[1].author == "bob"


def test_collect_skips_bot_comments():
    comments = [
        FakeComment("bot", "🏁 **Work complete — awaiting review**"),
        FakeComment("bot", "✅ Merged"),
        FakeComment("alice", "Needs more tests"),
    ]
    tracker = FakeTracker(comments)
    result = collect_review_feedback(tracker, "test-001")
    assert len(result) == 1
    assert result[0].author == "alice"


def test_collect_no_proof_of_work():
    comments = [
        FakeComment("alice", "Some comment"),
        FakeComment("bot", "🤖 Bot message"),
    ]
    tracker = FakeTracker(comments)
    result = collect_review_feedback(tracker, "test-001")
    assert len(result) == 1
    assert result[0].author == "alice"


def test_collect_multiple_rounds():
    """Second proof-of-work: only collect comments after the latest one."""
    comments = [
        FakeComment("bot", "🏁 **Work complete — awaiting review**"),
        FakeComment("alice", "Fix X"),
        FakeComment("bot", "🏁 **Work complete — awaiting review**"),
        FakeComment("bob", "Fix Y"),
    ]
    tracker = FakeTracker(comments)
    result = collect_review_feedback(tracker, "test-001")
    assert len(result) == 1
    assert result[0].author == "bob"


# --- Prompt building ---

def test_build_prompt_with_comments():
    comments = [FakeComment("alice", "Fix the bug")]
    prompt = build_revise_prompt(comments)
    assert "alice" in prompt
    assert "Fix the bug" in prompt
    assert "@@DONE@@" in prompt


def test_build_prompt_with_inline():
    prompt = build_revise_prompt([], inline_feedback="Add tests")
    assert "Add tests" in prompt


def test_build_prompt_combined():
    comments = [FakeComment("alice", "Fix X")]
    prompt = build_revise_prompt(comments, inline_feedback="Also fix Y")
    assert "alice" in prompt
    assert "Fix X" in prompt
    assert "Also fix Y" in prompt


# --- Flexible verdict parsing ---

from core.review import parse_verdict


def test_parse_verdict_nightshift_command():
    """Primary pattern: @nightshift command."""
    assert parse_verdict("@nightshift approve") == "approve"
    assert parse_verdict("@nightshift revise") == "revise"
    assert parse_verdict("Issues found. @nightshift reject") == "reject"


def test_parse_verdict_bold_format():
    """Fallback: bold verdict (e.g., **APPROVE**)."""
    assert parse_verdict("**APPROVE**") == "approve"
    assert parse_verdict("**REJECT**") == "reject"
    assert parse_verdict("The code looks good. **APPROVE**") == "approve"
    assert parse_verdict("Issues found:\n- bug\n**REVISE**") == "revise"


def test_parse_verdict_heading_format():
    """Fallback: heading followed by verdict."""
    assert parse_verdict("### Verdict\nAPPROVE") == "approve"
    assert parse_verdict("## Verdict\nREJECT") == "reject"
    assert parse_verdict("## Verdict:\nREVISE") == "revise"
    assert parse_verdict("Verdict: APPROVE") == "approve"


def test_parse_verdict_standalone_line():
    """Fallback: verdict alone on its own line."""
    assert parse_verdict("APPROVE") == "approve"
    assert parse_verdict("some text\nREJECT\nmore text") == "reject"


def test_parse_verdict_prefers_nightshift_command():
    """@nightshift command takes precedence over other formats."""
    # Both patterns present - prefer @nightshift
    text = "**REJECT**\n\n@nightshift approve"
    assert parse_verdict(text) == "approve"


def test_parse_verdict_case_insensitive():
    """Verdict parsing is case-insensitive."""
    assert parse_verdict("**approve**") == "approve"
    assert parse_verdict("**Reject**") == "reject"


def test_parse_verdict_normalizes_reject_to_revise():
    """REJECT is mapped to revise (only approve/revise are valid verdicts)."""
    assert parse_verdict("**REJECT**") == "reject"
    assert parse_verdict("@nightshift reject") == "reject"


def test_parse_verdict_no_match():
    """No verdict found returns None."""
    assert parse_verdict("This is just commentary") is None
    assert parse_verdict("The code is bad") is None


def test_parse_verdict_ignores_code_blocks():
    """Verdict inside code blocks is ignored."""
    text = "```\n**APPROVE**\n```\nActual verdict: @nightshift revise"
    assert parse_verdict(text) == "revise"
