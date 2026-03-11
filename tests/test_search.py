"""Tests for core/search.py."""

from core.search import search_related_issues
from tests.conftest import MockTracker, make_test_issue


class TestSearchRelatedIssues:
    def test_no_keywords(self):
        target = make_test_issue(title="a b c", body="")
        assert search_related_issues(target, [], MockTracker()) == ""

    def test_finds_related_issue(self):
        target = make_test_issue(
            issue_id="t1", title="widget parser fails on large files",
            body="The widget parser crashes when parsing large input files",
        )
        related = make_test_issue(
            issue_id="r1", title="widget performance issue",
            body="widget parser is slow on large files and crashes",
            status="closed",
        )
        tracker = MockTracker({"t1": target, "r1": related})
        tracker.comments["r1"] = []
        tracker.add_comment("r1", "Fixed by caching parsed widgets")

        result = search_related_issues(target, [target, related], tracker, min_score=2)
        assert "widget" in result.lower() or "Fixed by caching" in result

    def test_skips_self(self):
        target = make_test_issue(issue_id="t1", title="widget parser fails", body="widget parser")
        tracker = MockTracker({"t1": target})
        result = search_related_issues(target, [target], tracker, min_score=1)
        assert result == ""

    def test_respects_max_chars(self):
        target = make_test_issue(
            issue_id="t1", title="widget parser fails badly",
            body="widget parser crashes widget parser widget parser",
        )
        issues = []
        tracker = MockTracker({"t1": target})
        for i in range(20):
            iss = make_test_issue(
                issue_id=f"r{i}", title=f"widget parser issue {i}",
                body="widget parser " * 20, status="closed",
            )
            issues.append(iss)
            tracker.issues[iss.id] = iss
            tracker.add_comment(iss.id, "Fixed widget parser " * 10)

        result = search_related_issues(target, [target] + issues, tracker, min_score=1, max_chars=500)
        assert len(result) <= 600  # some slack for the last entry

    def test_no_matches_below_min_score(self):
        target = make_test_issue(issue_id="t1", title="unique problem title", body="very specific")
        other = make_test_issue(issue_id="r1", title="unrelated issue", body="completely different")
        tracker = MockTracker({"t1": target, "r1": other})
        result = search_related_issues(target, [target, other], tracker, min_score=5)
        assert result == ""
