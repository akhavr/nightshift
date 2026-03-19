"""Shared constants for core modules."""

TITLE_TRUNCATE_LEN = 60  # truncation length for issue titles in notifications
MERGE_NEEDED_FILENAME = "merge-needed.txt"  # host writes on conflict, container consumes on resume
TRACKER_OUTBOX_FILENAME = "tracker-outbox.jsonl"  # container writes, host watcher processes
TRACKER_OUTBOX_PROCESSING = "tracker-outbox.processing"  # renamed during host processing (crash recovery)
