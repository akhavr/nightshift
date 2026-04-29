"""Tests for state mapping module."""

import pytest

from nightshift_client._state import labels_to_state, STATE_LABEL_MAP


class TestLabelsToState:
    """Tests for labels_to_state function."""

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
        # needs-human-input takes priority over status:working
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

    def test_starting_state(self):
        """status:starting maps to 'starting'."""
        assert labels_to_state(["nightshift", "status:starting"]) == "starting"

    def test_waiting_review_state(self):
        """status:waiting-review maps to 'waiting_review'."""
        assert labels_to_state(["nightshift", "status:waiting-review"]) == "waiting_review"

    def test_waiting_human_review_state(self):
        """status:waiting-human-review maps to 'waiting_human_review'."""
        assert labels_to_state(["nightshift", "status:waiting-human-review"]) == "waiting_human_review"

    def test_reviewing_state(self):
        """status:reviewing maps to 'reviewing'."""
        assert labels_to_state(["nightshift", "status:reviewing"]) == "reviewing"

    def test_pending_review_state(self):
        """status:pending-review maps to 'pending_review'."""
        assert labels_to_state(["nightshift", "status:pending-review"]) == "pending_review"

    def test_accepted_state(self):
        """status:accepted maps to 'accepted'."""
        assert labels_to_state(["nightshift", "status:accepted"]) == "accepted"

    def test_suspended_auth_state(self):
        """status:suspended-auth maps to 'suspended_auth'."""
        assert labels_to_state(["nightshift", "status:suspended-auth"]) == "suspended_auth"

    def test_suspended_max_resumes_state(self):
        """status:suspended-max-resumes maps to 'suspended_max_resumes'."""
        assert labels_to_state(["nightshift", "status:suspended-max-resumes"]) == "suspended_max_resumes"

    def test_suspended_generic_state(self):
        """status:suspended maps to 'suspended'."""
        assert labels_to_state(["nightshift", "status:suspended"]) == "suspended"

    def test_cancelled_state(self):
        """status:cancelled maps to 'cancelled'."""
        assert labels_to_state(["nightshift", "status:cancelled"]) == "cancelled"

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
        expected_labels = [
            "status:working",
            "status:starting",
            "needs-human-input",
            "status:waiting-review",
            "status:waiting-human-review",
            "status:reviewing",
            "status:pending-review",
            "status:accepted",
            "status:suspended-auth",
            "status:suspended-max-resumes",
            "status:suspended",
            "status:cancelled",
        ]
        for label in expected_labels:
            assert label in STATE_LABEL_MAP, f"Missing mapping for {label}"
