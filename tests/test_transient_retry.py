"""Tests for transient error retry logic in HeadlessAgentBase.

REQ: REQ-031
"""

from adapters.agents.base import (
    HeadlessAgentBase,
    TRANSIENT_ERROR_PATTERNS,
    TRANSIENT_RETRY_DELAYS,
)


class TestIsTransientError:
    def test_is_transient_error_detects_500(self):
        """500 Internal Server Error is detected as transient."""
        assert HeadlessAgentBase._is_transient_error("Error code: 500 - Internal Server Error")
        assert HeadlessAgentBase._is_transient_error("HTTP 500")
        assert HeadlessAgentBase._is_transient_error("status 500")

    def test_is_transient_error_detects_429(self):
        """429 Too Many Requests is detected as transient."""
        assert HeadlessAgentBase._is_transient_error("Error code: 429 - Rate limit exceeded")
        assert HeadlessAgentBase._is_transient_error("HTTP 429")
        assert HeadlessAgentBase._is_transient_error("status 429")

    def test_is_transient_error_detects_502(self):
        """502 Bad Gateway is detected as transient."""
        assert HeadlessAgentBase._is_transient_error("Error code: 502 - Bad Gateway")

    def test_is_transient_error_detects_503(self):
        """503 Service Unavailable is detected as transient."""
        assert HeadlessAgentBase._is_transient_error("Error code: 503 - Service Unavailable")
        assert HeadlessAgentBase._is_transient_error("service unavailable")

    def test_is_transient_error_detects_504(self):
        """504 Gateway Timeout is detected as transient."""
        assert HeadlessAgentBase._is_transient_error("Error code: 504 - Gateway Timeout")

    def test_is_transient_error_detects_rate_limit(self):
        """Rate limit messages are detected as transient."""
        assert HeadlessAgentBase._is_transient_error("Rate limit exceeded")
        assert HeadlessAgentBase._is_transient_error("You have exceeded your rate limit")

    def test_is_transient_error_detects_overloaded(self):
        """Overloaded messages are detected as transient."""
        assert HeadlessAgentBase._is_transient_error("Server is overloaded")
        assert HeadlessAgentBase._is_transient_error("API is currently overloaded")

    def test_is_transient_error_case_insensitive(self):
        """Detection is case-insensitive."""
        assert HeadlessAgentBase._is_transient_error("RATE LIMIT")
        assert HeadlessAgentBase._is_transient_error("Service Unavailable")
        assert HeadlessAgentBase._is_transient_error("OVERLOADED")

    def test_is_transient_error_ignores_401(self):
        """401 Unauthorized is NOT a transient error."""
        assert not HeadlessAgentBase._is_transient_error("Error code: 401 - Unauthorized")

    def test_is_transient_error_ignores_auth_errors(self):
        """Authentication errors are NOT transient errors."""
        assert not HeadlessAgentBase._is_transient_error("Invalid API key")
        assert not HeadlessAgentBase._is_transient_error("authentication_error")

    def test_is_transient_error_ignores_normal_output(self):
        """Normal output is not detected as transient error."""
        assert not HeadlessAgentBase._is_transient_error("Successfully completed task")
        assert not HeadlessAgentBase._is_transient_error("Writing file...")


class TestTransientRetryCount:
    def test_retry_count_starts_at_zero(self):
        """Retry count is initialized to zero."""
        agent = HeadlessAgentBase("test")
        assert agent._transient_retry_count == 0

    def test_retry_count_resets_after_max(self):
        """Retry count resets to zero when max retries exceeded."""
        from unittest.mock import MagicMock
        from core.protocols import AgentEvent, AgentEventType

        agent = HeadlessAgentBase("test")
        # Set retry count to max
        agent._transient_retry_count = len(TRANSIENT_RETRY_DELAYS)

        # Create a transient error event
        ev = AgentEvent(
            type=AgentEventType.AUTH_FAILURE,
            content="Error code: 500 - Internal Server Error",
        )

        # _maybe_retry_transient should return False and reset counter
        handled = agent._maybe_retry_transient(ev)
        assert handled is False
        assert agent._transient_retry_count == 0

    def test_maybe_retry_transient_ignores_non_auth_failure(self):
        """Non-AUTH_FAILURE events are not retried."""
        from core.protocols import AgentEvent, AgentEventType

        agent = HeadlessAgentBase("test")
        ev = AgentEvent(
            type=AgentEventType.TEXT,
            content="Error code: 500",
        )

        handled = agent._maybe_retry_transient(ev)
        assert handled is False
        assert agent._transient_retry_count == 0

    def test_maybe_retry_transient_ignores_non_transient_auth_failure(self):
        """AUTH_FAILURE events that are not transient are not retried."""
        from core.protocols import AgentEvent, AgentEventType

        agent = HeadlessAgentBase("test")
        ev = AgentEvent(
            type=AgentEventType.AUTH_FAILURE,
            content="Invalid API key",
        )

        handled = agent._maybe_retry_transient(ev)
        assert handled is False
        assert agent._transient_retry_count == 0


class TestTransientRetryConstants:
    def test_retry_delays_are_defined(self):
        """TRANSIENT_RETRY_DELAYS has expected values."""
        assert TRANSIENT_RETRY_DELAYS == [30, 60, 120]

    def test_error_patterns_include_expected(self):
        """TRANSIENT_ERROR_PATTERNS includes expected patterns."""
        patterns = TRANSIENT_ERROR_PATTERNS
        assert "500" in patterns
        assert "502" in patterns
        assert "503" in patterns
        assert "504" in patterns
        assert "429" in patterns
        assert "rate limit" in patterns
        assert "overloaded" in patterns
        assert "service unavailable" in patterns


class TestStoreStartParams:
    def test_store_start_params_saves_values(self):
        """_store_start_params saves prompt, workspace, and max_turns."""
        from pathlib import Path

        agent = HeadlessAgentBase("test")
        agent._store_start_params("test prompt", Path("/workspace"), 100)

        assert agent._last_prompt == "test prompt"
        assert agent._last_workspace == Path("/workspace")
        assert agent._last_max_turns == 100

    def test_restart_raises_without_stored_params(self):
        """_restart raises RuntimeError if no params are stored."""
        import pytest

        agent = HeadlessAgentBase("test")
        with pytest.raises(RuntimeError, match="no stored start parameters"):
            agent._restart()
