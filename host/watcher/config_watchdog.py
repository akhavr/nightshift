"""Config watchdog: monitors .git/config for unexpected modifications.

Detects when core.worktree=/workspace leaks into the main repo's config,
which breaks host-side git commands.
"""

import logging
import threading
import time
from pathlib import Path

log = logging.getLogger("watcher")

CONFIG_POLL_INTERVAL_S = 1.0  # Poll interval for config file mtime


class ConfigWatchdog:
    """Daemon thread that polls .git/config mtime and logs modifications."""

    def __init__(self, config_path: Path, shutdown_event: threading.Event | None = None):
        self._config_path = config_path
        self._shutdown = shutdown_event or threading.Event()
        self._thread: threading.Thread | None = None
        self._last_mtime: float | None = None

    def start(self):
        """Start the watchdog daemon thread."""
        if self._thread is not None and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._run, daemon=True, name="config-watchdog")
        self._thread.start()

    def stop(self):
        """Signal the watchdog to stop and wait for it."""
        self._shutdown.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)

    def join(self, timeout: float | None = None):
        """Wait for the watchdog thread to finish."""
        if self._thread is not None:
            self._thread.join(timeout=timeout)

    def _run(self):
        """Poll loop: check config file mtime and log on change."""
        # Record initial mtime
        if self._config_path.exists():
            try:
                self._last_mtime = self._config_path.stat().st_mtime
            except OSError as e:
                log.debug(f"Could not stat {self._config_path}: {e}")

        while not self._shutdown.is_set():
            self._check_config()
            self._shutdown.wait(timeout=CONFIG_POLL_INTERVAL_S)

    def _check_config(self):
        """Check if config file was modified and log if so."""
        if not self._config_path.exists():
            return

        try:
            current_mtime = self._config_path.stat().st_mtime
        except OSError as e:
            log.debug(f"Could not stat {self._config_path}: {e}")
            return

        if self._last_mtime is None:
            self._last_mtime = current_mtime
            return

        if current_mtime != self._last_mtime:
            self._last_mtime = current_mtime
            self._on_config_changed()

    def _on_config_changed(self):
        """Handle config file modification."""
        log.warning(f".git/config modified at {time.strftime('%H:%M:%S')}")

        # Check for the problematic core.worktree setting
        try:
            content = self._config_path.read_text()
            if "worktree = /workspace" in content:
                log.error(
                    "ALERT: core.worktree=/workspace detected in .git/config! "
                    "This will break host-side git commands. "
                    "Run: git config --unset core.worktree"
                )
        except OSError as e:
            log.warning(f"Could not read .git/config: {e}")
