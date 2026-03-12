"""Main entry point for the host watcher."""

import argparse
import logging
import subprocess
from pathlib import Path

from host.env import load_all_dotenv
from host.session_utils import get_repo_root
from host.watcher.host_watcher import HostWatcher


def main():
    p = argparse.ArgumentParser(description="Host watcher -- pause/unpause, review monitor")
    p.add_argument("--sessions-dir", required=True, help=".nightshift/sessions path")
    p.add_argument("--no-auto-start", action="store_true",
                   help="Disable automatic starting of new issues")
    p.add_argument("--log-file", default=None,
                   help="Log to file instead of stderr")
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

    # Load .env from repo root (does not override existing env vars)
    try:
        repo = get_repo_root()
        load_all_dotenv(repo / ".env")
    except subprocess.CalledProcessError:
        repo = Path.cwd()

    HostWatcher(Path(a.sessions_dir), repo, auto_start=not a.no_auto_start).run()
