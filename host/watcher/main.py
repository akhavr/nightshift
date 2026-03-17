"""Main entry point for the host watcher."""

import argparse
import logging
import signal
import subprocess
import threading
from pathlib import Path

from host.env import load_all_dotenv
from host.session_utils import get_repo_root
from host.watcher.host_watcher import HostWatcher

log = logging.getLogger("watcher")

# Module-level shutdown event so signal handlers can set it
shutdown_event = threading.Event()


def _handle_shutdown(signum, frame):
    """Signal handler for SIGTERM/SIGINT — sets the shutdown event."""
    sig_name = signal.Signals(signum).name
    log.info(f"Received {sig_name}, shutting down...")
    shutdown_event.set()


def main():
    p = argparse.ArgumentParser(description="Host watcher -- pause/unpause, review monitor")
    p.add_argument("--sessions-dir", required=True, help=".nightshift/sessions path")
    p.add_argument("--no-auto-start", action="store_true",
                   help="Disable automatic starting of new issues")
    p.add_argument("--log-file", default=None,
                   help="Log to file instead of stderr")
    p.add_argument("--workflow", default=None,
                   help="Path to workflow file (resolved by CLI)")
    a = p.parse_args()

    # Reconfigure logging to file if requested
    if a.log_file:
        for handler in logging.root.handlers[:]:
            logging.root.removeHandler(handler)
        logging.basicConfig(
            level=logging.INFO,
            format="%(asctime)s [watcher] %(message)s",
            filename=a.log_file,
        )

    # Install signal handlers before starting the main loop
    signal.signal(signal.SIGTERM, _handle_shutdown)
    signal.signal(signal.SIGINT, _handle_shutdown)

    # Load .env from repo root (does not override existing env vars)
    try:
        repo = get_repo_root()
        load_all_dotenv(repo / ".env")
    except subprocess.CalledProcessError:
        repo = Path.cwd()

    workflow_path = Path(a.workflow) if a.workflow else None
    watcher = HostWatcher(Path(a.sessions_dir), repo, auto_start=not a.no_auto_start,
                          workflow_path=workflow_path)
    watcher.run(shutdown_event=shutdown_event)
