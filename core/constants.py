"""Shared constants for core modules."""

TITLE_TRUNCATE_LEN = 60  # truncation length for issue titles in notifications
MERGE_NEEDED_FILENAME = "merge-needed.txt"  # host writes on conflict, container consumes on resume
TRACKER_OUTBOX_FILENAME = "tracker-outbox.jsonl"  # container writes, host watcher processes
TRACKER_OUTBOX_PROCESSING = "tracker-outbox.processing"  # renamed during host processing (crash recovery)

# ── git-bug lock retry ────────────────────────────────
LOCK_RETRY_ATTEMPTS = 6           # Max retries when git-bug repo is locked
LOCK_RETRY_BASE_DELAY_S = 1      # Base delay for exponential backoff (1, 2, 4, 8, 16, 32s)

# ── Tracker IPC ──────────────────────────────────────
TRACKER_IPC_TIMEOUT_S = 60       # Per-request timeout for socket clients
TRACKER_IPC_MAX_MESSAGE_BYTES = 1_048_576  # 1 MB buffer limit for recv_json_line
