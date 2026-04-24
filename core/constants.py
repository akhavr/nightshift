"""Shared constants for core modules."""

REVIEW_SESSION_PREFIX = "review-"  # prefix for review session IDs
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

# ── Litellm proxy (overflow) ────────────────────────
LITELLM_PROXY_PORT = 4000               # Port litellm proxy listens on inside container
LITELLM_CONFIG_CONTAINER_PATH = "/session/litellm-config.yaml"  # Where config is mounted
LITELLM_HEALTH_TIMEOUT_S = 30           # Max wait for proxy health check
LITELLM_HEALTH_POLL_INTERVAL_S = 0.5    # Poll interval for health check
TOKEN_PRICING_UNIT = 1_000_000          # Provider pricing is configured per 1M tokens

# ── MCP signal server ──────────────────────────────────
MCP_SIGNAL_SERVER = "nightshift-signals"  # MCP server name for signal tools
MCP_CONFIG_CONTAINER_PATH = "/opt/nightshift/mcp-config.json"  # MCP config mounted in container
MCP_SIGNAL_SERVER_PREFIX = "mcp__nightshift-signals__"         # Prefix for signal tool names

# ── File-based signals (fallback for non-MCP agents) ──
SIGNAL_DIR_NAME = "signal"  # subdirectory of /session/ for file-based signals

# ── Large prompt handling ─────────────────────────────────
PROMPT_FILE_THRESHOLD = 100_000  # bytes; prompts larger than this use file-based passing
PROMPT_FILE_NAME = "prompt.txt"  # written to session dir when prompt exceeds threshold
