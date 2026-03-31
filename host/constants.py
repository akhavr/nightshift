"""Named constants for host-side modules.

Replaces magic numbers scattered across watcher.py, cli.py, and launch.py.
"""

# ── ID formatting ──────────────────────────────────────────
SHORT_ID_LEN = 12   # default truncation length for issue/commit IDs
REVIEW_SESSION_PREFIX = "review-"  # prefix for review session IDs

# ── Watcher polling intervals (seconds) ──────────────────
REVIEW_POLL_INTERVAL_S = 30
MAIN_LOOP_SLEEP_S = 2
PRE_PAUSE_DELAY_S = 1
ISSUE_REDUMP_INTERVAL_S = 30  # Min interval between issue.json re-dumps per session

# ── Timeouts and thresholds ──────────────────────────────
STILL_WAITING_LOG_INTERVAL_S = 300    # Log "still waiting" every 5 min
ORPHAN_GRACE_PERIOD_S = 120           # Grace period before treating as orphaned
LAUNCH_GRACE_PERIOD_S = 60            # Grace period for recently launched sessions without state.json
MAX_ORPHAN_RESUMES = 3                # Stop auto-resuming after this many orphan cycles
AUTH_RETRY_INTERVAL_S = 300           # Slow retry interval for auth failures (5 min)
MAX_AUTH_RETRIES = 6                  # Stop retrying auth failures after this many attempts (~30 min)
COMMAND_BACKOFF_BASE_S = 60           # Base backoff for CLI command retries
COMMAND_BACKOFF_CAP_S = 1800          # Max backoff (30 min)
COMMAND_BACKOFF_CAP_CYCLES = 30       # Max backoff cycles (used for log messages)
DEFAULT_MAX_REVIEW_ROUNDS = 3         # Fallback max review rounds when config unavailable
BACKGROUND_LAUNCH_CHECK_S = 10        # How long to wait before checking background launches for early exit

# ── Telegram ─────────────────────────────────────────────
TG_LONG_POLL_TIMEOUT_S = 1
TG_HTTP_TIMEOUT_S = 5
TG_POST_TIMEOUT_S = 10
TG_MESSAGE_HARD_LIMIT = 4096
TG_MESSAGE_SOFT_LIMIT = 4000
TG_TRUNCATION_POINT = 3950

# ── Adapters ───────────────────────────────────────────
HTTP_REQUEST_TIMEOUT_S = 10       # Default timeout for outgoing HTTP calls
PROCESS_TERMINATE_TIMEOUT_S = 10  # Timeout for process termination before kill
LOG_PREVIEW_LEN = 60             # Truncation length for log message previews
HISTORY_FOLLOW_POLL_S = 0.5      # Poll interval for `history --follow` mode
CONFLICT_FILE_PREVIEW_LEN = 20   # Max conflict files to show

# ── Overflow (alternate LLM provider) ─────────────────
OVERFLOW_FLAG_FILENAME = "overflow"  # Flag file in .nightshift/

# ── Tracker IPC (single-writer architecture) ──────────
TRACKER_SOCKET_FILENAME = "tracker.sock"  # Unix socket in .nightshift/
TRACKER_WRITER_QUEUE_SIZE = 100          # Bounded queue to prevent memory issues
TRACKER_SOCKET_MAX_WORKERS = 4           # ThreadPool for socket connections
