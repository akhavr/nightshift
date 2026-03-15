"""HostWatcher -- coordinator for the watcher subsystem."""

import logging
import os
import subprocess
import threading
import time
from pathlib import Path

from host.constants import REVIEW_POLL_INTERVAL_S, MAIN_LOOP_SLEEP_S
from core.config import load_workflow, create_tracker
from host.watcher.telegram_relay import TelegramRelay
from host.watcher.qa_handler import QAHandler
from host.watcher.review_orchestrator import ReviewOrchestrator
from host.watcher.session_monitor import SessionMonitor

log = logging.getLogger("watcher")


class HostWatcher:
    """Coordinator: lifecycle orchestration for the watcher subsystem.

    Creates and delegates to focused helper classes:
    - telegram: TelegramRelay (Telegram communication)
    - qa: QAHandler (Q&A pause/unpause cycle)
    - reviews: ReviewOrchestrator (review orchestration)
    - monitor: SessionMonitor (orphan detection, closed issues, auto-start)
    """

    def __init__(self, sessions_dir: Path, repo_dir: Path, auto_start: bool = True):
        self.sessions_dir = sessions_dir
        self.repo_dir = repo_dir
        self.auto_start = auto_start
        self._auto_start_config = None  # Lazy-loaded from WORKFLOW.md
        self._tracker = None
        self._config = None
        self._recently_launched: dict[str, float] = {}

        tg_level = self._telegram_level_from_config()
        self.telegram = TelegramRelay(
            os.environ.get("TELEGRAM_BOT_TOKEN", ""),
            os.environ.get("TELEGRAM_CHAT_ID", ""),
            repo_dir.name,
            sessions_dir,
            level=tg_level,
        )
        self.qa = QAHandler(sessions_dir, self.telegram, self._get_tracker)
        self.reviews = ReviewOrchestrator(
            sessions_dir, repo_dir, self.telegram,
            self._get_tracker, self._recently_launched, self._launch_background,
        )
        self.monitor = SessionMonitor(
            sessions_dir, repo_dir, auto_start, self.telegram,
            self._get_tracker, self._get_auto_start_config,
            self._recently_launched, self._launch_background,
        )

    def _telegram_level_from_config(self) -> str:
        """Read notification level for the telegram notifier from WORKFLOW.md."""
        try:
            self._config = load_workflow(self.repo_dir / "WORKFLOW.md")
            for nc in self._config.notifications:
                if nc.kind == "telegram":
                    return nc.level
        except Exception as e:
            log.debug(f"Could not read notification level from WORKFLOW.md: {e}")
        return "all"

    def _get_tracker(self):
        """Lazy-init tracker from WORKFLOW.md."""
        if self._tracker is None:
            if self._config is None:
                self._config = load_workflow(self.repo_dir / "WORKFLOW.md")
            self._tracker = create_tracker(self._config, repo_dir=str(self.repo_dir))
        return self._tracker

    def _get_auto_start_config(self):
        """Lazy-load auto_start config from WORKFLOW.md."""
        if self._auto_start_config is None:
            if self._config is None:
                self._config = load_workflow(self.repo_dir / "WORKFLOW.md")
            self._auto_start_config = self._config.auto_start
        return self._auto_start_config

    def run(self, shutdown_event: threading.Event | None = None):
        """Main watcher loop -- delegates to helper classes.

        Args:
            shutdown_event: Optional event that, when set, causes the loop
                to exit cleanly. Used by signal handlers for graceful shutdown.
        """
        self._shutdown = shutdown_event or threading.Event()

        # Propagate shutdown event to tracker so interruptible subprocess
        # polls can be interrupted when stuck in git-bug calls
        tracker = self._get_tracker()
        if hasattr(tracker, '_shutdown'):
            tracker._shutdown = self._shutdown

        log.info(f"Watching {self.sessions_dir}")
        if self.telegram.enabled:
            log.info("Telegram polling enabled")
        else:
            log.info("Telegram not configured -- answers via CLI only")
        if self.auto_start:
            asc = self._get_auto_start_config()
            if asc.enabled:
                log.info(f"Auto-start enabled -- label={asc.label!r}, "
                         f"poll={asc.poll_interval_s}s, max_concurrent={asc.max_concurrent}")
            else:
                log.info("Auto-start: enabled via CLI but disabled in WORKFLOW.md config "
                         "(set auto_start.enabled: true)")
                self.auto_start = False
        else:
            log.info("Auto-start disabled")

        while not self._shutdown.is_set():
            tg_answers, tg_reviews = (
                self.telegram.poll_all(self.qa._paused) if self.telegram.enabled else ({}, {})
            )
            self.qa.scan_for_waiting()
            self.qa.check_for_answers(tg_answers)
            self._maybe_sync_tracker()
            self.reviews.check_reviews(tg_reviews)
            self.reviews.check_for_auto_review()
            self.reviews.check_reviewer_done()
            self.monitor.check_orphaned_sessions()
            self.monitor.check_closed_issues()
            if self.auto_start:
                self.monitor.check_new_issues()
            # Use event.wait() instead of time.sleep() so shutdown
            # can interrupt the sleep immediately
            self._shutdown.wait(timeout=MAIN_LOOP_SLEEP_S)

        # Terminate any in-flight tracker subprocesses (e.g. git-bug sync)
        tracker = self._get_tracker()
        if hasattr(tracker, 'terminate_current'):
            tracker.terminate_current()

        log.info("Watcher shutdown complete")

    def _maybe_sync_tracker(self):
        """Sync tracker at most once per review poll interval."""
        now = time.time()
        if now - self.reviews._last_poll < REVIEW_POLL_INTERVAL_S:
            return
        try:
            self._get_tracker().sync()
        except Exception as e:
            log.warning(f"Tracker sync failed: {e}")

    def _launch_background(self, cmd: list[str], sid: str):
        """Launch a subprocess in background, logging its output."""
        log_file = self.sessions_dir.parent / "watcher.log"
        try:
            f = open(log_file, "a")
            subprocess.Popen(cmd, cwd=str(self.repo_dir), stdout=f, stderr=f)
        except Exception as e:
            log.error(f"[{sid}] Failed to launch {cmd}: {e}")
