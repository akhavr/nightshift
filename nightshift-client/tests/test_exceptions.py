"""Tests for nightshift_client exception hierarchy."""

import pytest

from nightshift_client.exceptions import (
    AuthError,
    NetworkError,
    NightshiftError,
    PushError,
    TrackerError,
)


class TestNightshiftErrorBase:
    """Test NightshiftError base class."""

    def test_nightshift_error_base(self):
        """NightshiftError inherits from Exception."""
        assert issubclass(NightshiftError, Exception)

    def test_nightshift_error_is_catchable(self):
        """NightshiftError can be raised and caught."""
        with pytest.raises(NightshiftError):
            raise NightshiftError("test error")

    def test_nightshift_error_preserves_message(self):
        """NightshiftError preserves the error message."""
        err = NightshiftError("test message")
        assert str(err) == "test message"


class TestPushError:
    """Test PushError for git push failures."""

    def test_push_error_inherits(self):
        """PushError inherits from NightshiftError."""
        assert issubclass(PushError, NightshiftError)

    def test_push_error_catchable_as_base(self):
        """PushError can be caught as NightshiftError."""
        with pytest.raises(NightshiftError):
            raise PushError("push failed")

    def test_push_error_preserves_message(self):
        """PushError preserves the underlying error message."""
        err = PushError("remote rejected: permission denied")
        assert str(err) == "remote rejected: permission denied"


class TestTrackerError:
    """Test TrackerError for git-bug CLI failures."""

    def test_tracker_error_inherits(self):
        """TrackerError inherits from NightshiftError."""
        assert issubclass(TrackerError, NightshiftError)

    def test_tracker_error_catchable_as_base(self):
        """TrackerError can be caught as NightshiftError."""
        with pytest.raises(NightshiftError):
            raise TrackerError("git-bug failed")

    def test_tracker_error_preserves_message(self):
        """TrackerError preserves the underlying error message."""
        err = TrackerError("git-bug: command not found")
        assert str(err) == "git-bug: command not found"


class TestAuthError:
    """Test AuthError for identity/auth issues."""

    def test_auth_error_inherits(self):
        """AuthError inherits from NightshiftError."""
        assert issubclass(AuthError, NightshiftError)

    def test_auth_error_catchable_as_base(self):
        """AuthError can be caught as NightshiftError."""
        with pytest.raises(NightshiftError):
            raise AuthError("authentication failed")

    def test_auth_error_preserves_message(self):
        """AuthError preserves the underlying error message."""
        err = AuthError("invalid credentials")
        assert str(err) == "invalid credentials"


class TestNetworkError:
    """Test NetworkError for remote connectivity issues."""

    def test_network_error_inherits(self):
        """NetworkError inherits from NightshiftError."""
        assert issubclass(NetworkError, NightshiftError)

    def test_network_error_catchable_as_base(self):
        """NetworkError can be caught as NightshiftError."""
        with pytest.raises(NightshiftError):
            raise NetworkError("connection refused")

    def test_network_error_preserves_message(self):
        """NetworkError preserves the underlying error message."""
        err = NetworkError("timeout connecting to remote")
        assert str(err) == "timeout connecting to remote"


class TestExceptionMessages:
    """Test that all exceptions preserve messages correctly."""

    def test_exception_messages(self):
        """All exception types preserve their messages."""
        exceptions = [
            (NightshiftError, "base error"),
            (PushError, "push error"),
            (TrackerError, "tracker error"),
            (AuthError, "auth error"),
            (NetworkError, "network error"),
        ]
        for exc_class, message in exceptions:
            err = exc_class(message)
            assert str(err) == message
            assert message in repr(err)

    def test_exception_args_preserved(self):
        """Exception args are preserved for all types."""
        for exc_class in [NightshiftError, PushError, TrackerError, AuthError, NetworkError]:
            err = exc_class("msg", "extra", 123)
            assert err.args == ("msg", "extra", 123)
