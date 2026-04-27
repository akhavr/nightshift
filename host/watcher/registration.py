"""Watcher auto-registration in projects.d for global watchdog discovery."""

import logging
import os
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger("watcher")

PROJECTS_D = Path.home() / ".nightshift" / "projects.d"


def register(project_name: str, repo_path: Path, log_path: Path) -> Path:
    """Register this watcher instance in projects.d.

    Creates a YAML file with metadata for watchdog discovery.
    Fire-and-forget: logs errors but doesn't raise.

    Returns the path to the registration file.
    """
    try:
        PROJECTS_D.mkdir(parents=True, exist_ok=True)
        reg_file = PROJECTS_D / f"{project_name}.yaml"
        reg_file.write_text(f"""\
path: {repo_path}
log: {log_path}
pid: {os.getpid()}
started: {datetime.now(timezone.utc).isoformat()}
""")
        log.info(f"Registered in {reg_file}")
        return reg_file
    except OSError as e:
        log.warning(f"Failed to register watcher: {e}")
        return PROJECTS_D / f"{project_name}.yaml"


def unregister(project_name: str) -> None:
    """Remove registration file on clean shutdown.

    Fire-and-forget: logs errors but doesn't raise.
    """
    reg_file = PROJECTS_D / f"{project_name}.yaml"
    try:
        reg_file.unlink(missing_ok=True)
        log.info(f"Unregistered from {reg_file}")
    except OSError as e:
        log.warning(f"Failed to unregister watcher: {e}")
