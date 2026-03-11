"""Named constants for host-side modules.

Replaces magic numbers scattered across watcher.py, cli.py, and launch.py.
"""

# ── ID formatting ──────────────────────────────────────────
SHORT_ID_LEN = 12   # default truncation length for issue/commit IDs

# ── Watcher polling intervals (seconds) ──────────────────
REVIEW_POLL_INTERVAL_S = 30
MAIN_LOOP_SLEEP_S = 2
PRE_PAUSE_DELAY_S = 1

# ── Timeouts and thresholds ──────────────────────────────
STILL_WAITING_LOG_INTERVAL_S = 300    # Log "still waiting" every 5 min
ORPHAN_GRACE_PERIOD_S = 120           # Grace period before treating as orphaned
COMMAND_BACKOFF_BASE_S = 60           # Base backoff for CLI command retries
COMMAND_BACKOFF_CAP_S = 1800          # Max backoff (30 min)
COMMAND_BACKOFF_CAP_CYCLES = 30       # Max backoff cycles (used for log messages)

# ── Telegram ─────────────────────────────────────────────
TG_LONG_POLL_TIMEOUT_S = 1
TG_HTTP_TIMEOUT_S = 5
TG_POST_TIMEOUT_S = 10
TG_MESSAGE_HARD_LIMIT = 4096
TG_MESSAGE_SOFT_LIMIT = 4000
TG_TRUNCATION_POINT = 3950
