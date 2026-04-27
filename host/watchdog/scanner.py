"""Scan registered watchers and check their health."""

import logging
import os
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Iterator

import yaml

log = logging.getLogger("watchdog")

PROJECTS_D = Path.home() / ".nightshift" / "projects.d"
STALE_THRESHOLD_HOURS = 24


@dataclass
class WatcherStatus:
    """Status of a registered watcher instance."""

    project: str
    path: Path
    log_path: Path
    pid: int
    started: datetime
    alive: bool
    registration_file: Path

    @property
    def is_stale(self) -> bool:
        """Registration is stale if PID is dead and started >24h ago."""
        if self.alive:
            return False
        age = datetime.now(timezone.utc) - self.started
        return age > timedelta(hours=STALE_THRESHOLD_HOURS)


def check_pid_alive(pid: int) -> bool:
    """Check if a process with the given PID is alive."""
    try:
        os.kill(pid, 0)
        return True
    except (OSError, ProcessLookupError):
        return False


def parse_registration(reg_file: Path) -> WatcherStatus | None:
    """Parse a registration YAML file into WatcherStatus."""
    try:
        content = reg_file.read_text()
        data = yaml.safe_load(content)
        if not data:
            log.warning("Empty registration file: %s", reg_file)
            return None

        pid = int(data["pid"])
        started_str = data.get("started", "")
        if isinstance(started_str, datetime):
            started = started_str
        else:
            started = datetime.fromisoformat(started_str)
        if started.tzinfo is None:
            started = started.replace(tzinfo=timezone.utc)

        return WatcherStatus(
            project=reg_file.stem,
            path=Path(data["path"]),
            log_path=Path(data["log"]),
            pid=pid,
            started=started,
            alive=check_pid_alive(pid),
            registration_file=reg_file,
        )
    except (KeyError, ValueError, yaml.YAMLError) as e:
        log.warning("Failed to parse registration %s: %s", reg_file, e)
        return None


def scan_registrations(projects_d: Path | None = None) -> Iterator[WatcherStatus]:
    """Scan all registration files and yield WatcherStatus objects."""
    base = projects_d or PROJECTS_D
    if not base.exists():
        return

    for reg_file in sorted(base.glob("*.yaml")):
        status = parse_registration(reg_file)
        if status:
            yield status


def cleanup_stale(status: WatcherStatus) -> bool:
    """Remove stale registration file. Returns True if removed."""
    if not status.is_stale:
        return False
    try:
        status.registration_file.unlink()
        log.info("Removed stale registration: %s", status.registration_file)
        return True
    except OSError as e:
        log.warning("Failed to remove stale registration %s: %s", status.registration_file, e)
        return False
