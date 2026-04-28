"""HostWatcher -- coordinator for the watcher subsystem."""

import json
import logging
import os
import subprocess
import threading
import time
from pathlib import Path

from host.constants import (
    REVIEW_POLL_INTERVAL_S, MAIN_LOOP_SLEEP_S, TRACKER_SOCKET_FILENAME,
    BACKGROUND_LAUNCH_CHECK_S, REVIEW_SESSION_PREFIX, RECENTLY_LAUNCHED_FILENAME,
    ORPHAN_GRACE_PERIOD_S, LOCK_TIMEOUT_S, MIN_FREE_GB,
    SOCKET_SERVER_RESTART_BACKOFF_BASE_S, SOCKET_SERVER_RESTART_BACKOFF_CAP_S,
    SOCKET_SERVER_MAX_RESTARTS,
    TRACKER_RELOAD_MAX_ATTEMPTS, TRACKER_RELOAD_BACKOFF_BASE_S,
    TRACKER_TERMINATION_WAIT_S, GITBUG_CACHE_HEALTHCHECK_INTERVAL_S,
)
from host.session_utils import read_state, update_status, get_active_session_ids
from core.config import load_workflow, create_tracker, WorkflowConfig
from host.watcher.tracker_writer import TrackerWriter, TrackerSocketServer, QueueTrackerProxy
from host.watcher.telegram_relay import TelegramRelay
from host.watcher.qa_handler import QAHandler
from host.watcher.review_orchestrator import ReviewOrchestrator
from host.watcher.session_monitor import SessionMonitor
from host.watcher.issue_sync import sync_sessions
from host.watcher.config_watchdog import ConfigWatchdog
from adapters.trackers.git_bug import repair_lamport_clocks

log = logging.getLogger("watcher")


class RecentlyLaunchedDict(dict):
    """Dict subclass that persists to disk on mutation.

    Used for _recently_launched to survive watcher restarts.
    """

    def __init__(self, persist_path: Path):
        super().__init__()
        self._persist_path = persist_path
        self._load_and_prune()

    def _load_and_prune(self):
        """Load from disk and prune entries older than grace period."""
        if not self._persist_path.exists():
            return
        try:
            data = json.loads(self._persist_path.read_text())
            if not isinstance(data, dict):
                log.warning(f"Invalid recently_launched.json format, starting fresh")
                return
        except (json.JSONDecodeError, OSError) as e:
            log.warning(f"Failed to load recently_launched.json: {e}")
            return

        now = time.time()
        pruned = False
        for sid, ts in list(data.items()):
            if not isinstance(ts, (int, float)):
                pruned = True
                continue
            if now - ts > ORPHAN_GRACE_PERIOD_S:
                pruned = True
            else:
                super().__setitem__(sid, ts)

        if pruned:
            self._persist()

    def _persist(self):
        """Write current state to disk."""
        try:
            self._persist_path.write_text(json.dumps(dict(self)))
        except OSError as e:
            log.warning(f"Failed to persist recently_launched.json: {e}")

    def __setitem__(self, key, value):
        super().__setitem__(key, value)
        self._persist()

    def __delitem__(self, key):
        super().__delitem__(key)
        self._persist()

    def pop(self, key, *args):
        result = super().pop(key, *args)
        self._persist()
        return result


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


def detect_orphan_refs(repo: Path) -> list[str]:
    """Return refs under agent/review branches that point to missing commits."""
    result = subprocess.run(
        [
            "git",
            "for-each-ref",
            "--format=%(refname) %(objectname)",
            "refs/heads/agent/",
            "refs/heads/review/",
        ],
        cwd=str(repo),
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        log.warning(
            "Failed to enumerate agent/review refs for orphan detection: %s",
            result.stderr.strip() or result.stdout.strip(),
        )

    orphans: list[str] = []
    for line in result.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        parts = line.split()
        if len(parts) != 2:
            log.warning("Skipping malformed ref line during orphan detection: %r", line)
            continue
        ref, sha = parts
        verify = subprocess.run(
            ["git", "cat-file", "-t", sha],
            cwd=str(repo),
            capture_output=True,
            text=True,
        )
        if verify.returncode != 0 or verify.stdout.strip() != "commit":
            orphans.append(ref)
    return orphans


def cleanup_orphan_refs(repo: Path, refs: list[str]) -> None:
    """Delete orphaned refs from the repository."""
    for ref in refs:
        result = subprocess.run(
            ["git", "update-ref", "-d", ref],
            cwd=str(repo),
            capture_output=True,
            text=True,
        )
        if result.returncode == 0:
            log.warning("Deleted orphan ref: %s", ref)
        else:
            log.warning(
                "Failed to delete orphan ref %s: %s",
                ref,
                result.stderr.strip() or result.stdout.strip(),
            )


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
        persist_path = sessions_dir.parent / RECENTLY_LAUNCHED_FILENAME
        self._recently_launched: dict[str, float] = RecentlyLaunchedDict(persist_path)
        self._background_procs: dict[str, tuple] = {}
        self._writer: TrackerWriter | None = None
        self._socket_server: TrackerSocketServer | None = None
        self._proxy: QueueTrackerProxy | None = None
        self._socket_restart_count = 0
        self._socket_last_restart: float = 0.0
        self._last_gitbug_cache_health_check: float = 0.0
        self._startup_orphan_refs_checked = False

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

        # Start config watchdog (monitors .git/config for pollution)
        self._config_watchdog = ConfigWatchdog(repo_dir / ".git" / "config")
        self._config_watchdog.start()

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

    def _gitbug_tracker(self):
        """Return the direct git-bug tracker instance, if one is active."""
        if self._writer is not None:
            return self._writer.tracker
        return self._tracker

    def _check_orphan_refs(self):
        """Detect and clean refs that point to missing commits."""
        try:
            orphan_refs = detect_orphan_refs(self.repo_dir)
        except Exception as e:
            log.warning("Orphan ref check failed: %s", e)
            return
        if orphan_refs:
            log.warning("Orphan agent/review refs detected: %s", ", ".join(orphan_refs))
            cleanup_orphan_refs(self.repo_dir, orphan_refs)

    def _cleanup_orphan_refs_once(self):
        """Run the orphan-ref sweep only once during startup."""
        if self._startup_orphan_refs_checked:
            return
        self._startup_orphan_refs_checked = True
        self._check_orphan_refs()

    def _clear_gitbug_cache(self) -> bool:
        """Clear the persisted git-bug cache if the tracker supports it."""
        tracker = self._gitbug_tracker()
        if tracker is None or not hasattr(tracker, "clear_cache"):
            return False
        tracker.clear_cache()
        return True

    def _count_gitbug_refs(self) -> int | None:
        """Count refs/bugs entries in the repo for git-bug health checks."""
        result = subprocess.run(
            ["git", "show-ref", "refs/bugs"],
            cwd=str(self.repo_dir),
            capture_output=True,
            text=True,
        )
        if result.returncode not in (0, 1):
            log.warning("git-bug cache health check failed to count refs: %s",
                        result.stderr.strip() or result.stdout.strip())
            return None
        return len([line for line in result.stdout.splitlines() if line.strip()])

    def _check_gitbug_cache_health(self):
        """Compare git-bug refs to cached GraphQL issues and rebuild on mismatch."""
        now = time.time()
        if now - self._last_gitbug_cache_health_check < GITBUG_CACHE_HEALTHCHECK_INTERVAL_S:
            return
        self._last_gitbug_cache_health_check = now

        tracker = self._gitbug_tracker()
        if tracker is None or not hasattr(tracker, "list_issues"):
            return

        refs_count = self._count_gitbug_refs()
        if refs_count is None:
            return

        try:
            cache_count = len(tracker.list_issues())
        except Exception as e:
            log.warning("git-bug cache health check failed: %s", e)
            return

        log.info("git-bug cache health: refs=%d cache=%d", refs_count, cache_count)
        if refs_count == cache_count:
            return

        log.warning("git-bug cache mismatch detected (refs=%d, cache=%d); rebuilding cache",
                    refs_count, cache_count)
        try:
            if hasattr(tracker, "rebuild_cache"):
                tracker.rebuild_cache()
            else:
                self._clear_gitbug_cache()
                if hasattr(tracker, "restart_webui"):
                    tracker.restart_webui()
        except Exception as e:
            log.error("Failed to rebuild git-bug cache after mismatch: %s", e)

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

        # Terminate old tracker BEFORE creating new one to release locks
        if old_tracker and hasattr(old_tracker, 'terminate_current'):
            old_tracker.terminate_current()
            # Wait for webui/subprocess to fully exit
            time.sleep(TRACKER_TERMINATION_WAIT_S)

        self._tracker = None  # force re-creation on next _get_tracker()
        # Temporarily disable proxy so _get_tracker creates a direct tracker
        saved_proxy = self._proxy
        self._proxy = None

        # Retry tracker creation with exponential backoff
        new_tracker = None
        last_error = None
        for attempt in range(TRACKER_RELOAD_MAX_ATTEMPTS):
            try:
                new_tracker = self._get_tracker()
                break
            except Exception as e:
                last_error = e
                if attempt < TRACKER_RELOAD_MAX_ATTEMPTS - 1:
                    backoff = TRACKER_RELOAD_BACKOFF_BASE_S * (2 ** attempt)
                    log.warning(f"Tracker creation attempt {attempt + 1} failed: {e}, "
                                f"retrying in {backoff}s")
                    time.sleep(backoff)

        if new_tracker is not None:
            self._proxy = saved_proxy
            # Propagate shutdown event to new tracker
            if hasattr(new_tracker, '_shutdown') and hasattr(self, '_shutdown'):
                new_tracker._shutdown = self._shutdown
            # Swap the writer's underlying tracker instance
            if self._writer:
                self._writer.tracker = new_tracker
        else:
            log.error(f"Tracker creation failed after {TRACKER_RELOAD_MAX_ATTEMPTS} "
                      f"attempts: {last_error}")
            self._tracker = old_tracker
            self._proxy = saved_proxy

        if changes:
            log.info(f"Reloaded config -- changes: {', '.join(changes)}")
        else:
            log.info("Reloaded config -- no changes detected")

    def run(self, shutdown_event: threading.Event | None = None,
            reload_event: threading.Event | None = None,
            cache_clear_event: threading.Event | None = None):
        """Main watcher loop -- delegates to helper classes.

        Args:
            shutdown_event: Optional event that, when set, causes the loop
                to exit cleanly. Used by signal handlers for graceful shutdown.
            reload_event: Optional event that, when set, triggers a config
                reload from the workflow file. Used by SIGHUP handler.
            cache_clear_event: Optional event that, when set, clears the
                persisted git-bug cache before reloading tracker config.
        """
        self._shutdown = shutdown_event or threading.Event()
        self._reload = reload_event or threading.Event()
        self._cache_clear = cache_clear_event or threading.Event()

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

        # Clean up stale review sessions left over from previous watcher crash
        # (race condition: review container exits but watcher restarts before cleanup)
        self.monitor.cleanup_stale_review_sessions()

        # Remove stale blocked:<id> labels where the blocking issue is already closed
        self.monitor.cleanup_stale_blocked_labels()

        # Clean up orphaned agent/review refs before entering the main loop
        self._cleanup_orphan_refs_once()

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
            if self._cache_clear.is_set():
                self._cache_clear.clear()
                if self._clear_gitbug_cache():
                    log.info("Cleared git-bug cache before config reload")
            if self._reload.is_set():
                self._reload.clear()
                self.reload_config()
            self._check_gitbug_cache_health()
            self._check_orphan_refs()
            if not self._check_worktree_integrity():
                break
            if not self._check_disk_space():
                break
            self._check_socket_server_health()
            self._check_tracker_lock()
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
            self.monitor.check_provider_outages()
            self.monitor.check_zombie_containers()
            self.monitor.check_session_sizes()
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

        # Stop config watchdog
        if self._config_watchdog:
            self._config_watchdog.stop()

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
        if self._config is None:
            self._config = load_workflow(self.workflow_path)
        if not self._config.tracker.sync:
            return
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

        Returns True on success, False on failure.
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

    def _check_socket_server_health(self):
        """Check if socket server is alive and restart if dead.

        Uses exponential backoff if it keeps dying. After MAX_RESTARTS,
        logs an error but does not attempt further restarts until the
        backoff period has elapsed.
        """
        if self._socket_server is None:
            return

        if self._socket_server.is_alive():
            return

        now = time.time()

        # Check backoff: wait longer between restart attempts
        if self._socket_restart_count > 0:
            backoff = min(
                SOCKET_SERVER_RESTART_BACKOFF_BASE_S * (2 ** (self._socket_restart_count - 1)),
                SOCKET_SERVER_RESTART_BACKOFF_CAP_S
            )
            if now - self._socket_last_restart < backoff:
                return

        # Check if we've hit the max restart limit
        if self._socket_restart_count >= SOCKET_SERVER_MAX_RESTARTS:
            # Reset counter after cap period to allow eventual retry
            if now - self._socket_last_restart >= SOCKET_SERVER_RESTART_BACKOFF_CAP_S:
                log.warning("Socket server restart limit reached, resetting counter after backoff")
                self._socket_restart_count = 0
            else:
                return

        log.error("Tracker socket server thread died, restarting "
                  f"(attempt {self._socket_restart_count + 1})")
        try:
            self._socket_server.restart()
            self._socket_restart_count += 1
            self._socket_last_restart = now
        except Exception as e:
            log.error(f"Failed to restart socket server: {e}")
            self._socket_restart_count += 1
            self._socket_last_restart = now

    def _check_worktree_integrity(self) -> bool:
        """Check if .git/worktrees/ exists when active sessions exist.

        If worktrees directory is missing while sessions are active, logs a
        CRITICAL error and triggers shutdown to prevent cascading failures.

        Returns True if integrity check passes, False if watcher should halt.
        """
        worktrees_dir = self.repo_dir / ".git" / "worktrees"

        active_sids = get_active_session_ids(self.repo_dir)
        if not active_sids:
            return True

        if worktrees_dir.exists():
            return True

        log.critical(
            "FATAL: .git/worktrees/ deleted while sessions active: %s. "
            "Run git worktree repair. Halting watcher.",
            active_sids
        )
        self._shutdown.set()
        return False

    def _check_disk_space(self) -> bool:
        """Check if disk has enough free space.

        If free space drops below MIN_FREE_GB, logs a CRITICAL error and
        triggers shutdown to prevent silent failures from disk exhaustion.

        Returns True if disk space is sufficient, False if watcher should halt.
        """
        stat = os.statvfs(self.repo_dir)
        free_gb = (stat.f_bavail * stat.f_frsize) / (1024**3)
        if free_gb < MIN_FREE_GB:
            log.critical("Disk space low: %.2fGB free. Halting.", free_gb)
            self._shutdown.set()
            return False
        return True

    def _check_tracker_lock(self):
        """Check if git-bug lock file is held too long and warn if so.

        Git-bug lock can get stuck if a process crashes holding it.
        This monitors the lock file age and logs a warning if it's been
        held longer than LOCK_TIMEOUT_S. Does not halt - just warns.

        If the lock file contains a PID, logs the process command line
        and parent process info to help identify what's holding the lock.
        """
        lock_file = self.repo_dir / ".git" / "git-bug" / "lock"
        if lock_file.exists():
            age = time.time() - lock_file.stat().st_mtime
            if age > LOCK_TIMEOUT_S:
                try:
                    pid = int(lock_file.read_text().strip())
                    # Get process command line
                    ps_result = subprocess.run(
                        ["ps", "-p", str(pid), "-o", "args=,ppid="],
                        capture_output=True, text=True
                    )
                    ps_output = ps_result.stdout.strip()
                    if ps_output:
                        # Parse cmdline and ppid from output
                        # Format is: "cmdline ppid" where ppid is the last field
                        parts = ps_output.rsplit(None, 1)
                        if len(parts) == 2:
                            cmdline, ppid_str = parts
                            try:
                                if int(ppid_str) == os.getpid():
                                    return
                            except ValueError:
                                pass
                            # Get parent process command line
                            parent_result = subprocess.run(
                                ["ps", "-p", ppid_str, "-o", "args="],
                                capture_output=True, text=True
                            )
                            parent_cmdline = parent_result.stdout.strip() or "unknown"
                            log.warning(
                                "git-bug lock held for %.0fs by pid %d (%s), "
                                "parent pid %s (%s)",
                                age, pid, cmdline, ppid_str, parent_cmdline
                            )
                        else:
                            log.warning(
                                "git-bug lock held for %.0fs by pid %d (%s)",
                                age, pid, ps_output or "unknown"
                            )
                    else:
                        # Process not found (dead)
                        log.warning(
                            "git-bug lock held for %.0fs by pid %d (process not found)",
                            age, pid
                        )
                except (ValueError, OSError) as e:
                    log.warning("git-bug lock held for %.0fs, may be stuck (%s)", age, e)

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
            else:
                self._handle_successful_completion(sid)

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

    def _handle_successful_completion(self, sid: str):
        """Handle post-completion actions for successful background launches.

        For review sessions: extract verdict from conversation, process it
        (approve -> waiting:human-review, revise -> resume coder), and clean up.

        For coder sessions: no action needed (container sets its own status).
        """
        if not sid.startswith(REVIEW_SESSION_PREFIX):
            return

        review_dir = self.sessions_dir / sid
        if not review_dir.exists() or not (review_dir / "state.json").exists():
            return

        coder_sid = sid[len(REVIEW_SESSION_PREFIX):]
        coder_dir = self.sessions_dir / coder_sid

        try:
            state = read_state(review_dir)
            issue_id = state.get("issue_id", "")

            # Extract verdict from conversation log
            conv_log = review_dir / "conversation.jsonl"
            verdict = self.reviews.verdicts.extract_reviewer_verdict(conv_log, issue_id)

            if verdict and coder_dir.exists():
                log.info(f"[{sid}] Processing verdict: {verdict}")
                if verdict == "approve":
                    self.reviews.verdicts.handle_reviewer_approve(
                        coder_sid, coder_dir, issue_id)
                elif verdict == "revise":
                    self.reviews._posted_done.discard(coder_sid)
                    self.reviews.verdicts.handle_reviewer_revise(
                        coder_sid, coder_dir, issue_id, review_dir)

            # Clean up review session
            self.reviews.cleanup_review_session(sid, review_dir)
        except Exception as e:
            log.error(f"[{sid}] Failed to handle successful completion: {e}")
