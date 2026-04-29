"""Exception hierarchy for nightshift-client.

All exceptions inherit from NightshiftError, allowing callers to catch
the base class for any nightshift-related failure, or catch specific
subclasses for targeted error handling.
"""


class NightshiftError(Exception):
    """Base exception for all nightshift-client errors."""

    pass


class PushError(NightshiftError):
    """Git push operation failed.

    Raised when pushing commits to the remote repository fails,
    e.g., due to permission issues or rejected refs.
    """

    pass


class TrackerError(NightshiftError):
    """Git-bug CLI operation failed.

    Raised when interacting with the git-bug issue tracker fails,
    e.g., CLI not found, malformed output, or lock contention.
    """

    pass


class AuthError(NightshiftError):
    """Authentication or identity error.

    Raised when authentication fails, credentials are invalid,
    or required identity information is missing.
    """

    pass


class NetworkError(NightshiftError):
    """Remote connectivity error.

    Raised when network operations fail due to connectivity issues,
    timeouts, or unreachable hosts.
    """

    pass
