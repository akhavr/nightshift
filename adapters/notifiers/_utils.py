"""Shared utilities for notifier adapters."""

import os
import re


def redact_url(e: Exception) -> str:
    """Redact secrets from exception messages.

    - Telegram bot tokens: /bot<token>/ -> /bot<REDACTED>/
    - Query strings: ?param=value -> ?<PARAMS>
    """
    msg = str(e)
    msg = re.sub(r'/bot[^/]+/', '/bot<REDACTED>/', msg)  # Telegram tokens
    msg = re.sub(r'\?[^\s)]+', '?<PARAMS>', msg)  # Query strings
    return msg


def project_prefix(text: str) -> str:
    """Prefix message with [project] if PROJECT_NAME is set."""
    project = os.environ.get("PROJECT_NAME", "")
    if project:
        return f"[{project}] {text}"
    return text
