"""Label-to-state mapping for nightshift client.

Maps git-bug labels to client state strings per docs/nightshift-client.md.
"""

# Mapping from git-bug label to client state string.
# Order defines priority: earlier entries take precedence when multiple labels present.
STATE_LABEL_MAP: dict[str, str] = {
    "needs-human-input": "question",
    "status:suspended-auth": "suspended_auth",
    "status:suspended-max-resumes": "suspended_max_resumes",
    "status:suspended": "suspended",
    "status:cancelled": "cancelled",
    "status:waiting-human-review": "waiting_human_review",
    "status:waiting-review": "waiting_review",
    "status:pending-review": "pending_review",
    "status:reviewing": "reviewing",
    "status:accepted": "accepted",
    "status:starting": "starting",
    "status:working": "working",
}


def labels_to_state(labels: list[str]) -> str:
    """Convert git-bug labels to client state string.

    Args:
        labels: List of git-bug labels on the issue.

    Returns:
        Client state string. Returns "pending" if only "nightshift" label
        is present.

    Raises:
        ValueError: If the "nightshift" label is not present.
    """
    label_set = set(labels)

    # Must have nightshift label to be a nightshift issue
    if "nightshift" not in label_set:
        raise ValueError("Not a nightshift issue: missing 'nightshift' label")

    # Check labels in priority order (dict preserves insertion order)
    for label in STATE_LABEL_MAP:
        if label in label_set:
            return STATE_LABEL_MAP[label]

    # nightshift label present but no status label = pending
    return "pending"
