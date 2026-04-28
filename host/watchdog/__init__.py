"""Global watchdog for monitoring multiple watcher instances."""

from host.watchdog.scanner import (
    WatcherStatus,
    scan_registrations,
    discover_projects,
    check_pid_alive,
    read_log_tail,
)
from host.watchdog.config import WatchdogConfig, load_config
from host.watchdog.rules import Anomaly, check_stale, check_errors, check_repeated
from host.watchdog.llm import analyze
from host.watchdog.notify import send_alert
from host.watchdog.log_monitor import LogMonitor, ErrorMatch
from host.watchdog.alerter import Alerter
from host.watchdog.session_checker import StuckSession, find_stuck_sessions

__all__ = [
    "WatcherStatus",
    "scan_registrations",
    "discover_projects",
    "check_pid_alive",
    "read_log_tail",
    "WatchdogConfig",
    "load_config",
    "Anomaly",
    "check_stale",
    "check_errors",
    "check_repeated",
    "analyze",
    "send_alert",
    "LogMonitor",
    "ErrorMatch",
    "Alerter",
    "StuckSession",
    "find_stuck_sessions",
]
