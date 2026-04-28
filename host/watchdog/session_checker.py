"""Check for stuck sessions across projects."""

import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

import yaml

from host.docker_utils import docker_container_status

log = logging.getLogger("watchdog")

STUCK_THRESHOLD_MINUTES = 30
ACTIVE_STATUSES = {"working", "starting", "reviewing"}


@dataclass
class StuckSession:
    """A session that appears to be stuck."""

    project: str
    session_id: str
    status: str
    last_update: datetime
    minutes_stuck: int
    session_dir: Path


@dataclass
class DeadReviewSession:
    """A session in reviewing status with no running container."""

    project: str
    session_id: str
    status: str
    session_dir: Path
    container_missing: bool


def check_sessions(
    project_path: Path,
    project_name: str,
    threshold_minutes: int = STUCK_THRESHOLD_MINUTES,
) -> Iterator[StuckSession]:
    """Check for stuck sessions in a project."""
    sessions_dir = project_path / ".nightshift" / "sessions"
    if not sessions_dir.exists():
        return

    now = datetime.now(timezone.utc)

    for session_dir in sessions_dir.iterdir():
        if not session_dir.is_dir():
            continue

        state_file = session_dir / "state.json"
        if not state_file.exists():
            continue

        try:
            state = json.loads(state_file.read_text())
        except (json.JSONDecodeError, OSError) as e:
            log.debug("Failed to read state for %s: %s", session_dir.name, e)
            continue

        status = state.get("status", "")
        if status not in ACTIVE_STATUSES:
            continue

        mtime = datetime.fromtimestamp(state_file.stat().st_mtime, tz=timezone.utc)
        age = now - mtime
        minutes = int(age.total_seconds() / 60)

        if minutes >= threshold_minutes:
            yield StuckSession(
                project=project_name,
                session_id=session_dir.name[:12],
                status=status,
                last_update=mtime,
                minutes_stuck=minutes,
                session_dir=session_dir,
            )


def find_stuck_sessions(
    projects_d: Path,
    threshold_minutes: int = STUCK_THRESHOLD_MINUTES,
) -> Iterator[StuckSession]:
    """Find stuck sessions across all registered projects."""
    if not projects_d.exists():
        return

    for reg_file in projects_d.glob("*.yaml"):
        try:
            data = yaml.safe_load(reg_file.read_text())
            if not data:
                continue
            project_path = Path(data.get("path", ""))
            if not project_path.exists():
                continue
            yield from check_sessions(project_path, reg_file.stem, threshold_minutes)
        except (yaml.YAMLError, OSError) as e:
            log.debug("Failed to check project %s: %s", reg_file.stem, e)


def check_dead_review_containers(
    project_path: Path,
    project_name: str,
) -> Iterator[DeadReviewSession]:
    """Check for sessions in reviewing status with no running container."""
    sessions_dir = project_path / ".nightshift" / "sessions"
    if not sessions_dir.exists():
        return

    for session_dir in sessions_dir.iterdir():
        if not session_dir.is_dir():
            continue

        state_file = session_dir / "state.json"
        if not state_file.exists():
            continue

        try:
            state = json.loads(state_file.read_text())
        except (json.JSONDecodeError, OSError) as e:
            log.debug("Failed to read state for %s: %s", session_dir.name, e)
            continue

        status = state.get("status", "")
        if status != "reviewing":
            continue

        short_id = state.get("short_id", session_dir.name[:8])
        container_name = f"nightshift-review-{short_id}"
        container_status = docker_container_status(container_name)

        if container_status is None:
            yield DeadReviewSession(
                project=project_name,
                session_id=session_dir.name[:12],
                status=status,
                session_dir=session_dir,
                container_missing=True,
            )
