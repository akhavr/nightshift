"""Label-to-state mapping for nightshift client.

Maps git-bug labels to client state strings per docs/nightshift-client.md.
"""

# Mapping from git-bug label to client state string.
# Order matters for priority: earlier entries take precedence.
STATE_LABEL_MAP: dict[str, str] = {
    "needs-human-input": "question",
    "status:suspended-auth": "suspended_auth",
    "status:suspended-max-resumes": "suspended_max_resumes",
    "status:suspended": "suspended",
    "status:cancelled": "cancelled",
    "status:waiting-review": "waiting_review",
    "status:waiting-human-review": "waiting_human_review",
    "status:pending-review": "pending_review",
    "status:reviewing": "reviewing",
    "status:accepted": "accepted",
    "status:starting": "starting",
    "status:working": "working",
}

# Priority order for state resolution.
# Higher priority labels are checked first.
_PRIORITY_ORDER: list[str] = [
    "needs-human-input",  # Question state takes highest priority
    "status:suspended-auth",
    "status:suspended-max-resumes",
    "status:suspended",
    "status:cancelled",
    "status:waiting-human-review",
    "status:waiting-review",
    "status:pending-review",
    "status:reviewing",
    "status:accepted",
    "status:starting",
    "status:working",
]


def labels_to_state(labels: list[str]) -> str:
    """Convert git-bug labels to client state string.

    Args:
        labels: List of git-bug labels on the issue.

    Returns:
        Client state string. Returns "pending" if only "nightshift" label
        is present, "unknown" if nightshift label is missing.
    """
    label_set = set(labels)

    # Must have nightshift label to be a nightshift issue
    if "nightshift" not in label_set:
        return "unknown"

    # Check labels in priority order
    for label in _PRIORITY_ORDER:
        if label in label_set:
            return STATE_LABEL_MAP[label]

    # nightshift label present but no status label = pending
    return "pending"
