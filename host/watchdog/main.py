"""Global watchdog entry point."""

import argparse
import logging
import signal
import sys
import threading
from pathlib import Path

from host.watchdog.scanner import scan_registrations, cleanup_stale, WatcherStatus, PROJECTS_D
from host.watchdog.log_monitor import LogMonitor
from host.watchdog.alerter import Alerter, AlertConfig
from host.watchdog.session_checker import find_stuck_sessions, STUCK_THRESHOLD_MINUTES

log = logging.getLogger("watchdog")

REPEATED_ERROR_THRESHOLD = 3


def setup_logging(verbose: bool = False) -> None:
    """Configure logging for watchdog."""
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )


def format_status_line(status: WatcherStatus) -> str:
    """Format a single watcher status for display."""
    state = "✓" if status.alive else "✗"
    started = status.started.strftime("%Y-%m-%d %H:%M")
    return f"{state} {status.project:20} PID={status.pid:6}  started={started}  {status.path}"


def list_watchers() -> int:
    """List all registered watchers and their status."""
    watchers = list(scan_registrations())
    if not watchers:
        print(f"No watchers registered in {PROJECTS_D}")
        return 0

    print(f"Registered watchers ({len(watchers)}):\n")
    for status in watchers:
        print(format_status_line(status))

    alive = sum(1 for w in watchers if w.alive)
    dead = len(watchers) - alive
    print(f"\nAlive: {alive}, Dead: {dead}")
    return 0 if dead == 0 else 1


def check_once(alerter: Alerter, log_monitor: LogMonitor, do_alerts: bool = True) -> int:
    """Run a single health check pass. Returns count of issues found."""
    issues = 0

    for status in scan_registrations():
        if not status.alive:
            issues += 1
            msg = f"Watcher crashed: *{status.project}* (PID {status.pid})"
            log.warning(msg)
            if do_alerts:
                alerter.send(f"crash:{status.project}", msg)
            if status.is_stale:
                cleanup_stale(status)
            continue

        for error in log_monitor.tail(status.log_path, status.project):
            count = log_monitor.count_error(status.project, error.error_type)
            if error.error_type == "missing_bug" and count < REPEATED_ERROR_THRESHOLD:
                continue

            issues += 1
            msg = f"Error in *{status.project}*: `{error.error_type}` — {error.line[:100]}"
            log.warning(msg)
            if do_alerts:
                alerter.send(f"error:{status.project}:{error.error_type}", msg)

    for stuck in find_stuck_sessions(PROJECTS_D, STUCK_THRESHOLD_MINUTES):
        issues += 1
        msg = (
            f"Session stuck: *{stuck.project}* `{stuck.session_id}` "
            f"in `{stuck.status}` for {stuck.minutes_stuck} min"
        )
        log.warning(msg)
        if do_alerts:
            alerter.send(f"stuck:{stuck.project}:{stuck.session_id}", msg)

    return issues


def run_daemon(config: AlertConfig, shutdown_event: threading.Event) -> None:
    """Run the watchdog daemon loop."""
    alerter = Alerter(config=config)
    log_monitor = LogMonitor()

    log.info("Watchdog daemon started, polling every %ds", config.poll_interval_s)

    while not shutdown_event.is_set():
        try:
            check_once(alerter, log_monitor)
        except Exception as e:
            log.error("Check failed: %s", e)

        shutdown_event.wait(timeout=config.poll_interval_s)

    log.info("Watchdog daemon stopped")


def main(args: list[str] | None = None) -> int:
    """Main entry point."""
    parser = argparse.ArgumentParser(description="Monitor nightshift watchers")
    parser.add_argument("--list", action="store_true", help="List registered watchers")
    parser.add_argument("--check", action="store_true", help="One-shot health check")
    parser.add_argument("--config", type=Path, help="Path to watchdog.yaml")
    parser.add_argument("--verbose", "-v", action="store_true", help="Verbose output")
    parser.add_argument("--no-alerts", action="store_true", help="Suppress alerts (for --check)")
    parsed = parser.parse_args(args)

    setup_logging(parsed.verbose)
    config = AlertConfig.load(parsed.config)

    if parsed.list:
        return list_watchers()

    if parsed.check:
        alerter = Alerter(config=config)
        log_monitor = LogMonitor()
        issues = check_once(alerter, log_monitor, do_alerts=not parsed.no_alerts)
        if issues:
            print(f"Found {issues} issue(s)")
            return 1
        print("All watchers healthy")
        return 0

    shutdown_event = threading.Event()

    def handle_signal(signum, frame):
        log.info("Received signal %d, shutting down", signum)
        shutdown_event.set()

    signal.signal(signal.SIGTERM, handle_signal)
    signal.signal(signal.SIGINT, handle_signal)

    run_daemon(config, shutdown_event)
    return 0


if __name__ == "__main__":
    sys.exit(main())
