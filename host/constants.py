"""Named constants for host-side modules.

Replaces magic numbers scattered across watcher.py, cli.py, and launch.py.
"""

# ── ID formatting ──────────────────────────────────────────
SHORT_ID_LEN = 12   # default truncation length for issue/commit IDs
from core.constants import REVIEW_SESSION_PREFIX  # canonical definition in core; re-exported for host callers  # noqa: F401,E501

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
PROVIDER_OUTAGE_RETRY_INTERVAL_S = 600  # Retry interval for provider outages (10 min)
MAX_PROVIDER_OUTAGE_RETRIES = 6         # Stop retrying provider outages after this many attempts (~1 hour)
COMMAND_BACKOFF_BASE_S = 60           # Base backoff for CLI command retries
COMMAND_BACKOFF_CAP_S = 1800          # Max backoff (30 min)
COMMAND_BACKOFF_CAP_CYCLES = 30       # Max backoff cycles (used for log messages)
DEFAULT_MAX_REVIEW_ROUNDS = 3         # Fallback max review rounds when config unavailable
BACKGROUND_LAUNCH_CHECK_S = 10        # How long to wait before checking background launches for early exit
GITBUG_CACHE_HEALTHCHECK_INTERVAL_S = 300  # How often to compare git-bug refs vs GraphQL cache

# ── Telegram ─────────────────────────────────────────────
TG_LONG_POLL_TIMEOUT_S = 1
TG_HTTP_TIMEOUT_S = 3
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

# ── Archive ───────────────────────────────────────────
ARCHIVE_DIR = "archive"  # Subdirectory of .nightshift/ for archived session data

# ── Usage tracking ────────────────────────────────────
USAGE_LOG_FILENAME = "usage.jsonl"  # Append-only usage log in .nightshift/

# ── Tracker lock monitoring ──────────────────────────
LOCK_TIMEOUT_S = 60  # Warn if git-bug lock held longer than this

# ── Tracker IPC (single-writer architecture) ──────────
TRACKER_SOCKET_FILENAME = "tracker.sock"  # Unix socket in .nightshift/
TRACKER_WRITER_QUEUE_SIZE = 100          # Bounded queue to prevent memory issues
TRACKER_SOCKET_MAX_WORKERS = 4           # ThreadPool for socket connections
SOCKET_SERVER_RESTART_BACKOFF_BASE_S = 5  # Base backoff for socket server restart
SOCKET_SERVER_RESTART_BACKOFF_CAP_S = 300  # Max backoff (5 min)
SOCKET_SERVER_MAX_RESTARTS = 10           # Stop restarting after this many failures
TRACKER_RELOAD_MAX_ATTEMPTS = 3           # Retry tracker creation on SIGHUP
TRACKER_RELOAD_BACKOFF_BASE_S = 0.5       # Base backoff between retries
TRACKER_TERMINATION_WAIT_S = 0.5          # Wait for old tracker to fully terminate

# ── Recently launched persistence ─────────────────────
RECENTLY_LAUNCHED_FILENAME = "recently_launched.json"  # Persisted launch timestamps

# ── Zombie container detection ────────────────────────
ZOMBIE_CHECK_INTERVAL_S = 60       # How often to check for zombie containers
ZOMBIE_TIMEOUT_MULTIPLIER = 2     # Alert if no events for stall_timeout_s * this multiplier
DEFAULT_STALL_TIMEOUT_S = 300     # Fallback stall timeout when config unavailable

# ── Session directory monitoring ───────────────────────
SESSION_SIZE_CHECK_INTERVAL_S = 300  # How often to check session directory sizes
SIZE_WARNING_THRESHOLD_MB = 100      # Warn when a session grows beyond this size
SIZE_CRITICAL_THRESHOLD_MB = 500     # Alert when a session grows beyond this size

# ── Disk space guardrail ──────────────────────────────────
MIN_FREE_GB = 1.0                 # Halt watcher if free disk space drops below this

# ── Dependency blocking ────────────────────────────────────
BLOCKED_LABEL_PREFIX = "blocked:"  # Label prefix for blocking dependencies

# ── Revise retry ────────────────────────────────────────────
REVISE_PENDING_FILENAME = "revise-pending.json"  # Marker for failed revise launches
