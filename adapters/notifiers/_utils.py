"""Shared utilities for notifier adapters."""

import os


def project_prefix(text: str) -> str:
    """Prefix message with [project] if PROJECT_NAME is set."""
    project = os.environ.get("PROJECT_NAME", "")
    if project:
        return f"[{project}] {text}"
    return text
