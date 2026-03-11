"""Tests for adapters/notifiers/composite.py."""

from adapters.notifiers.composite import CompositeNotifier
from tests.conftest import MockNotifier


class TestCompositeNotifier:
    def test_notify_broadcasts_to_all(self):
        n1, n2 = MockNotifier(), MockNotifier()
        comp = CompositeNotifier([n1, n2])
        comp.notify("hello")
        assert "hello" in n1.notifications
        assert "hello" in n2.notifications

    def test_send_question_uses_primary(self):
        n1, n2 = MockNotifier(), MockNotifier()
        comp = CompositeNotifier([n1, n2])
        comp.send_question("issue-1", "What color?", "abc")
        assert len(n1.questions) == 1
        assert len(n2.questions) == 1

    def test_check_answer_uses_primary(self):
        n1, n2 = MockNotifier(), MockNotifier()
        n1.pending_answers["issue-1"] = "blue"
        comp = CompositeNotifier([n1, n2])
        assert comp.check_answer("issue-1") == "blue"

    def test_check_answer_no_primary(self):
        comp = CompositeNotifier([])
        assert comp.check_answer("issue-1") is None

    def test_clear_pending_uses_primary(self):
        n1 = MockNotifier()
        n1.pending_answers["issue-1"] = "answer"
        comp = CompositeNotifier([n1])
        comp.clear_pending("issue-1")
        assert "issue-1" not in n1.pending_answers

    def test_clear_pending_no_primary(self):
        comp = CompositeNotifier([])
        comp.clear_pending("issue-1")  # should not raise

    def test_start_stop(self):
        n1, n2 = MockNotifier(), MockNotifier()
        comp = CompositeNotifier([n1, n2])
        comp.start()
        comp.stop()

    def test_send_question_no_primary(self):
        comp = CompositeNotifier([])
        assert comp.send_question("x", "q?") is False
