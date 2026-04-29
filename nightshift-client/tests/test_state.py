"""Tests for state mapping module."""

import pytest

from nightshift_client._state import labels_to_state, STATE_LABEL_MAP


# All single-label mappings for parametrized testing
SINGLE_LABEL_CASES = [
    ("status:working", "working"),
    ("status:starting", "starting"),
    ("needs-human-input", "question"),
    ("status:waiting-review", "waiting_review"),
    ("status:waiting-human-review", "waiting_human_review"),
    ("status:reviewing", "reviewing"),
    ("status:pending-review", "pending_review"),
    ("status:accepted", "accepted"),
    ("status:suspended-auth", "suspended_auth"),
    ("status:suspended-max-resumes", "suspended_max_resumes"),
    ("status:suspended", "suspended"),
    ("status:cancelled", "cancelled"),
]


class TestLabelsToState:
    """Tests for labels_to_state function."""

    @pytest.mark.parametrize("label,expected_state", SINGLE_LABEL_CASES)
    def test_label_to_state(self, label: str, expected_state: str):
        """Each status label maps to the correct state."""
        assert labels_to_state(["nightshift", label]) == expected_state

    def test_label_to_state_working(self):
        """status:working maps to 'working'."""
        assert labels_to_state(["nightshift", "status:working"]) == "working"

    def test_label_to_state_question(self):
        """needs-human-input maps to 'question'."""
        assert labels_to_state(["nightshift", "needs-human-input"]) == "question"

    def test_label_to_state_pending(self):
        """nightshift alone (no status) maps to 'pending'."""
        assert labels_to_state(["nightshift"]) == "pending"

    def test_multiple_labels_priority(self):
        """Most specific status label wins when multiple present."""
        labels = ["nightshift", "status:working", "needs-human-input"]
        assert labels_to_state(labels) == "question"

    def test_suspended_auth_over_suspended(self):
        """status:suspended-auth takes priority over status:suspended."""
        labels = ["nightshift", "status:suspended", "status:suspended-auth"]
        assert labels_to_state(labels) == "suspended_auth"

    def test_suspended_max_resumes_over_suspended(self):
        """status:suspended-max-resumes takes priority over status:suspended."""
        labels = ["nightshift", "status:suspended", "status:suspended-max-resumes"]
        assert labels_to_state(labels) == "suspended_max_resumes"

    def test_waiting_human_review_over_waiting_review(self):
        """status:waiting-human-review takes priority over status:waiting-review."""
        labels = ["nightshift", "status:waiting-review", "status:waiting-human-review"]
        assert labels_to_state(labels) == "waiting_human_review"

    def test_empty_labels_raises(self):
        """Empty labels list raises ValueError."""
        with pytest.raises(ValueError, match="missing 'nightshift' label"):
            labels_to_state([])

    def test_no_nightshift_label_raises(self):
        """Labels without nightshift raises ValueError."""
        with pytest.raises(ValueError, match="missing 'nightshift' label"):
            labels_to_state(["some-other-label"])

    def test_state_label_map_contains_all_mappings(self):
        """STATE_LABEL_MAP contains all documented mappings."""
        expected_labels = [label for label, _ in SINGLE_LABEL_CASES]
        for label in expected_labels:
            assert label in STATE_LABEL_MAP, f"Missing mapping for {label}"
