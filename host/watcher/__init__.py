"""Host watcher package -- pauses idle containers, collects answers, monitors reviews.

Handles three concerns:
1. Q&A: pauses containers on waiting.json, collects answers via Telegram/CLI
2. Review: monitors waiting:review sessions for @nightshift commands in
   tracker comments or Telegram replies, triggers revise/accept/reject
3. Telegram relay: posts Telegram replies as tracker comments for audit trail

    python -m host.watcher --sessions-dir .nightshift/sessions
"""

import logging
import shutil
import subprocess
import time

# -- Logging setup (module-level, same as original watcher.py) ----------------
logging.basicConfig(level=logging.INFO, format="%(asctime)s [watcher] %(message)s")
log = logging.getLogger("watcher")

# -- Re-export requests and HAS_REQUESTS at package level for patch compat ----
try:
    import requests
    HAS_REQUESTS = True
except ImportError:
    HAS_REQUESTS = False

# -- Module-level constant used by SessionMonitor ----------------------------
from host.watcher.session_monitor import _ACTIVE_STATUSES  # noqa: E402

# -- Re-export docker utilities so test patches on host.watcher.X resolve -----
from host.docker_utils import (  # noqa: E402
    docker_pause, docker_unpause, docker_stop, docker_container_status,
)

# -- Re-export session utilities patched by tests -----------------------------
from host.session_utils import remove_worktree  # noqa: E402

# -- Re-export classes from submodules ----------------------------------------
from host.watcher.telegram_relay import TelegramRelay  # noqa: E402
from host.watcher.qa_handler import QAHandler  # noqa: E402
from host.watcher.verdict_handler import VerdictHandler  # noqa: E402
from host.watcher.command_executor import CommandExecutor  # noqa: E402
from host.watcher.review_orchestrator import ReviewOrchestrator  # noqa: E402
from host.watcher.session_monitor import SessionMonitor  # noqa: E402
from host.watcher.host_watcher import HostWatcher  # noqa: E402
from host.watcher.main import main  # noqa: E402

__all__ = [
    "TelegramRelay",
    "QAHandler",
    "VerdictHandler",
    "CommandExecutor",
    "ReviewOrchestrator",
    "SessionMonitor",
    "HostWatcher",
    "main",
    "HAS_REQUESTS",
    "_ACTIVE_STATUSES",
    # Re-exported for test patch compatibility
    "docker_pause",
    "docker_unpause",
    "docker_stop",
    "docker_container_status",
    "remove_worktree",
    "requests",
    "subprocess",
    "shutil",
    "time",
    "log",
]
