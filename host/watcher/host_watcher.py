"""HostWatcher -- coordinator for the watcher subsystem."""

import logging
import os
import subprocess
import threading
import time
from pathlib import Path

from host.constants import (
    REVIEW_POLL_INTERVAL_S, MAIN_LOOP_SLEEP_S, TRACKER_SOCKET_FILENAME,
    BACKGROUND_LAUNCH_CHECK_S, REVIEW_SESSION_PREFIX,
)
from host.session_utils import read_state, update_status
from core.config import load_workflow, create_tracker, WorkflowConfig
from host.watcher.tracker_writer import TrackerWriter, TrackerSocketServer, QueueTrackerProxy
from host.watcher.telegram_relay import TelegramRelay
from host.watcher.qa_handler import QAHandler
from host.watcher.review_orchestrator import ReviewOrchestrator
from host.watcher.session_monitor import SessionMonitor
from host.watcher.issue_sync import sync_sessions
from adapters.trackers.git_bug import repair_lamport_clocks

log = logging.getLogger("watcher")


def _diff_config(old: WorkflowConfig, new: WorkflowConfig) -> list[str]:
    """Compare two WorkflowConfigs and return a list of human-readable changes."""
    changes: list[str] = []
    if old.notifications != new.notifications:
        changes.append("notifications")
    if old.auto_start != new.auto_start:
        changes.append("auto_start")
    if old.merge != new.merge:
        changes.append("merge")
    if old.review != new.review:
        changes.append("review")
    if old.tracker != new.tracker:
        changes.append("tracker")
    if old.hooks != new.hooks:
        changes.append("hooks")
    if old.workspace != new.workspace:
        changes.append("workspace")
    if old.agent != new.agent:
        changes.append("agent")
    if old.overflow != new.overflow:
        changes.append("overflow")
    return changes


class HostWatcher:
    """Coordinator: lifecycle orchestration for the watcher subsystem.

    Creates and delegates to focused helper classes:
    - telegram: TelegramRelay (Telegram communication)
    - qa: QAHandler (Q&A pause/unpause cycle)
    - reviews: ReviewOrchestrator (review orchestration)
    - monitor: SessionMonitor (orphan detection, closed issues, auto-start)
    """

    def __init__(self, sessions_dir: Path, repo_dir: Path, auto_start: bool = True,
                 workflow_path: Path | None = None):
        self.sessions_dir = sessions_dir
        self.repo_dir = repo_dir
        self.auto_start = auto_start
        self.workflow_path = workflow_path or (repo_dir / "WORKFLOW.md")
        self._auto_start_config = None  # Lazy-loaded from workflow
        self._tracker = None
        self._config = None
        self._recently_launched: dict[str, float] = {}
        self._background_procs: dict[str, tuple] = {}
        self._writer: TrackerWriter | None = None
        self._socket_server: TrackerSocketServer | None = None
        self._proxy: QueueTrackerProxy | None = None

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
            workflow_path=self.workflow_path,
        )
        self.monitor = SessionMonitor(
            sessions_dir, repo_dir, auto_start, self.telegram,
            self._get_tracker, self._get_auto_start_config,
            self._recently_launched, self._launch_background,
            workflow_path=self.workflow_path,
            review_orchestrator=self.reviews,
        )

    @staticmethod
    def _telegram_level(config: WorkflowConfig | None) -> str:
        """Extract telegram notification level from a WorkflowConfig, defaulting to 'all'."""
        if config is None:
            return "all"
        for nc in config.notifications:
            if nc.kind == "telegram":
                return nc.level
        return "all"

    def _telegram_level_from_config(self) -> str:
        """Read notification level for the telegram notifier from workflow config."""
        try:
            self._config = load_workflow(self.workflow_path)
            return self._telegram_level(self._config)
        except Exception as e:
            log.debug(f"Could not read notification level from WORKFLOW.md: {e}")
        return "all"

    def _get_tracker(self):
        """Return the tracker proxy (if writer is active) or direct tracker.

        When the writer thread is running, all tracker calls are routed
        through the queue proxy for serialized, contention-free access.
        Falls back to the direct tracker during init (before writer starts).
        """
        if self._proxy is not None:
            return self._proxy
        if self._tracker is None:
            if self._config is None:
                self._config = load_workflow(self.workflow_path)
            self._tracker = create_tracker(self._config, repo_dir=str(self.repo_dir))
        return self._tracker

    def _get_auto_start_config(self):
        """Lazy-load auto_start config from workflow config."""
        if self._auto_start_config is None:
            if self._config is None:
                self._config = load_workflow(self.workflow_path)
            self._auto_start_config = self._config.auto_start
        return self._auto_start_config

    def reload_config(self):
        """Re-read workflow file and update in-memory config and adapters.

        Called when SIGHUP is received. Logs what changed.
        """
        log.info(f"Reloading config from {self.workflow_path}")
        old_config = self._config
        try:
            new_config = load_workflow(self.workflow_path)
        except Exception as e:
            log.error(f"Config reload failed, keeping current config: {e}")
            return

        changes = _diff_config(old_config, new_config) if old_config else ["initial load"]
        self._config = new_config

        # Update notification level on TelegramRelay
        self.telegram.set_level(self._telegram_level(new_config))

        # Update auto_start config
        self._auto_start_config = new_config.auto_start
        if self.auto_start and not new_config.auto_start.enabled:
            self.auto_start = False
            log.info("Auto-start disabled by config reload")
        elif not self.auto_start and new_config.auto_start.enabled:
            self.auto_start = True
            log.info("Auto-start enabled by config reload")

        # Recreate tracker (kind or extra settings may have changed)
        old_tracker = self._tracker
        self._tracker = None  # force re-creation on next _get_tracker()
        # Temporarily disable proxy so _get_tracker creates a direct tracker
        saved_proxy = self._proxy
        self._proxy = None
        new_tracker = self._get_tracker()
        self._proxy = saved_proxy
        # Propagate shutdown event to new tracker
        if hasattr(new_tracker, '_shutdown') and hasattr(self, '_shutdown'):
            new_tracker._shutdown = self._shutdown
        # Swap the writer's underlying tracker instance
        if self._writer:
            self._writer.tracker = new_tracker
        # Terminate old tracker's in-flight subprocess if present
        if old_tracker and hasattr(old_tracker, 'terminate_current'):
            old_tracker.terminate_current()

        if changes:
            log.info(f"Reloaded config -- changes: {', '.join(changes)}")
        else:
            log.info("Reloaded config -- no changes detected")

    def run(self, shutdown_event: threading.Event | None = None,
            reload_event: threading.Event | None = None):
        """Main watcher loop -- delegates to helper classes.

        Args:
            shutdown_event: Optional event that, when set, causes the loop
                to exit cleanly. Used by signal handlers for graceful shutdown.
            reload_event: Optional event that, when set, triggers a config
                reload from the workflow file. Used by SIGHUP handler.
        """
        self._shutdown = shutdown_event or threading.Event()
        self._reload = reload_event or threading.Event()

        # Propagate shutdown event to QAHandler so pre-pause sleep
        # can be interrupted immediately on Ctrl-C
        self.qa._shutdown = self._shutdown

        # Propagate shutdown event to TelegramRelay so long-poll HTTP
        # requests can be interrupted on shutdown
        self.telegram._shutdown = self._shutdown

        # Repair corrupt lamport clocks before any git-bug operations
        repair_lamport_clocks(self.repo_dir)

        # Propagate shutdown event to tracker so interruptible subprocess
        # polls can be interrupted when stuck in git-bug calls
        tracker = self._get_tracker()
        if hasattr(tracker, '_shutdown'):
            tracker._shutdown = self._shutdown

        # Start the single-writer infrastructure
        self._writer = TrackerWriter(tracker, self._shutdown)
        self._writer.start()
        self._proxy = QueueTrackerProxy(self._writer)

        socket_path = self.sessions_dir.parent / TRACKER_SOCKET_FILENAME
        self._socket_server = TrackerSocketServer(socket_path, self._writer,
                                                   self._shutdown)
        self._socket_server.start()

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
            if self._reload.is_set():
                self._reload.clear()
                self.reload_config()
            tg_answers, tg_reviews = (
                self.telegram.poll_all(self.qa._paused) if self.telegram.enabled else ({}, {})
            )
            self.qa.scan_for_waiting()
            self.qa.check_for_answers(tg_answers)
            self._maybe_sync_tracker()
            self.reviews.check_reviews(tg_reviews)
            self.reviews.check_for_auto_review()
            self.reviews.check_reviewer_done()
            self._sync_issue_data()
            self.check_background_launches()
            self.monitor.check_orphaned_sessions()
            self.monitor.check_auth_failures()
            self.monitor.check_closed_issues()
            if self.auto_start:
                self.monitor.check_new_issues()
            # Use event.wait() instead of time.sleep() so shutdown
            # can interrupt the sleep immediately
            self._shutdown.wait(timeout=MAIN_LOOP_SLEEP_S)

        # Stop socket server and writer thread (drains pending operations)
        if self._socket_server:
            self._socket_server.stop()
        if self._writer:
            self._writer.stop()
        self._proxy = None

        # Terminate any in-flight tracker subprocesses (e.g. git-bug sync)
        if self._tracker and hasattr(self._tracker, 'terminate_current'):
            self._tracker.terminate_current()

        log.info("Watcher shutdown complete")

    def _sync_issue_data(self):
        """Process outbox writes and re-dump issue.json for active sessions."""
        try:
            tracker = self._get_tracker()
            sync_sessions(self.sessions_dir, tracker)
        except Exception as e:
            log.warning(f"Issue sync failed: {e}")

    def _maybe_sync_tracker(self):
        """Sync tracker at most once per review poll interval."""
        now = time.time()
        if now - self.reviews._last_poll < REVIEW_POLL_INTERVAL_S:
            return
        try:
            self._get_tracker().sync()
        except Exception as e:
            log.warning(f"Tracker sync failed: {e}")

    def _launch_background(self, cmd: list[str], sid: str) -> bool:
        """Launch a subprocess in background, logging its output.

        Stores the Popen handle so check_background_launches() can detect
        early failures and revert session status.

        Returns:
            True if the subprocess was successfully started, False on failure.
        """
        log_file = self.sessions_dir.parent / "watcher.log"
        f = None
        try:
            f = open(log_file, "a")
            proc = subprocess.Popen(cmd, cwd=str(self.repo_dir), stdout=f, stderr=f)
            self._background_procs[sid] = (proc, f, time.time())
            return True
        except Exception as e:
            log.error(f"[{sid}] Failed to launch {cmd}: {e}")
            if f is not None:
                f.close()
            return False

    def check_background_launches(self):
        """Poll recently launched background processes for early exit.

        If a process exits with non-zero within the grace period, log the
        error and revert associated session status so recovery paths can
        pick it up.
        """
        done_sids = []
        for sid, (proc, log_fh, launch_time) in list(self._background_procs.items()):
            rc = proc.poll()
            if rc is None:
                # Still running -- close log handle (subprocess inherited the fd)
                # and stop tracking after grace period
                elapsed = time.time() - launch_time
                if elapsed > BACKGROUND_LAUNCH_CHECK_S:
                    log_fh.close()
                    done_sids.append(sid)
                continue
            # Process exited
            log_fh.close()
            done_sids.append(sid)
            if rc != 0:
                log.error(f"[{sid}] Background launch failed (exit code {rc})")
                self._revert_failed_launch(sid)

        for sid in done_sids:
            self._background_procs.pop(sid, None)

    def _revert_failed_launch(self, sid: str):
        """Revert session status after a background launch failure.

        For review sessions: revert the coder session from 'reviewing'
        back to 'waiting:review' so auto-review can retry.
        """
        if not sid.startswith(REVIEW_SESSION_PREFIX):
            return

        coder_sid = sid[len(REVIEW_SESSION_PREFIX):]
        coder_dir = self.sessions_dir / coder_sid
        if not coder_dir.exists() or not (coder_dir / "state.json").exists():
            return

        try:
            state = read_state(coder_dir)
            if state.get("status") == "reviewing":
                update_status(coder_dir, "waiting:review")
                log.info(f"[{coder_sid}] Reverted from 'reviewing' to "
                         f"'waiting:review' after review launch failure")
        except Exception as e:
            log.error(f"[{coder_sid}] Failed to revert status after launch failure: {e}")
