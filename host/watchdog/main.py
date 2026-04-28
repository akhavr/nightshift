"""Global watchdog entry point."""

from __future__ import annotations

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
from host.watchdog.config import load_config, WatchdogConfig
from host.watchdog import rules, llm, notify
from host.watchdog.scanner import discover_projects, read_log_tail

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
    watchers = list(discover_projects())
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


def run_once(config: WatchdogConfig, *, send_notifications: bool = True) -> int:
    """Run one watchdog pass using the new multi-project config."""
    issues = 0

    for status in discover_projects():
        try:
            log_lines = read_log_tail(status.log_path, config.watch.log_lines)
            anomalies = []
            anomalies.extend(rules.check_stale(status.log_path, config.watch.watcher_stale_s))
            anomalies.extend(rules.check_errors(log_lines, config.rules.error_threshold))
            anomalies.extend(rules.check_repeated(log_lines, config.rules.repeat_threshold))

            if not anomalies:
                continue

            issues += len(anomalies)
            snippet = "\n".join(log_lines[-config.watch.log_lines :])
            llm_summary = ""
            if config.llm.provider != "none":
                try:
                    llm_summary = llm.analyze(
                        snippet,
                        provider=config.llm.provider,
                        model=config.llm.model,
                        api_key=config.llm.api_key,
                        base_url=config.llm.base_url,
                    )
                except Exception as exc:
                    log.warning("LLM analysis failed for %s: %s", status.project, exc)
            if send_notifications:
                try:
                    notify.send_alert(status.project, anomalies, llm_summary, config)
                except Exception as exc:
                    log.warning("Failed to send alert for %s: %s", status.project, exc)
        except Exception as exc:
            log.warning("Watchdog processing failed for %s: %s", status.project, exc)

    return issues


def run_daemon(config: WatchdogConfig, shutdown_event: threading.Event) -> None:
    """Run the watchdog daemon loop."""
    log.info("Watchdog daemon started, polling every %ds", config.watch.interval_s)

    while not shutdown_event.is_set():
        try:
            run_once(config)
        except Exception as e:
            log.error("Check failed: %s", e)

        shutdown_event.wait(timeout=config.watch.interval_s)

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
    try:
        config = load_config(parsed.config)
    except ValueError as exc:
        log.error("Failed to load watchdog config: %s", exc)
        return 2

    if parsed.list:
        return list_watchers()

    if parsed.check:
        issues = run_once(config, send_notifications=not parsed.no_alerts)
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
