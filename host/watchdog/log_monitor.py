"""Monitor watcher log files for error patterns."""

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator

log = logging.getLogger("watchdog")

ERROR_PATTERNS = [
    (re.compile(r"Traceback \(most recent call last\)", re.IGNORECASE), "traceback"),
    (re.compile(r"Exception:.*", re.IGNORECASE), "exception"),
    (re.compile(r"\bERROR\b", re.IGNORECASE), "error"),
    (re.compile(r"bug doesn't exist", re.IGNORECASE), "missing_bug"),
    (re.compile(r"CRITICAL", re.IGNORECASE), "critical"),
]

IGNORED_PATTERNS = [
    re.compile(r"no label added or removed", re.IGNORECASE),
]


@dataclass
class ErrorMatch:
    """An error detected in a log file."""

    project: str
    log_path: Path
    line_number: int
    line: str
    error_type: str


@dataclass
class LogMonitor:
    """Monitor log files for errors, tracking read positions."""

    positions: dict[Path, int] = field(default_factory=dict)
    error_counts: dict[tuple[str, str], int] = field(default_factory=dict)

    def tail(self, log_path: Path, project: str, max_lines: int = 1000) -> Iterator[ErrorMatch]:
        """Read new lines from log and yield any errors detected."""
        if not log_path.exists():
            return

        try:
            current_size = log_path.stat().st_size
        except OSError as e:
            log.warning("Cannot stat log file %s: %s", log_path, e)
            return

        last_pos = self.positions.get(log_path, 0)

        if current_size < last_pos:
            last_pos = 0

        if current_size == last_pos:
            return

        try:
            with open(log_path, "r", errors="replace") as f:
                f.seek(last_pos)
                lines_read = 0
                for line_num, line in enumerate(f, start=1):
                    lines_read += 1
                    if lines_read > max_lines:
                        break
                    match = self._check_line(line)
                    if match:
                        yield ErrorMatch(
                            project=project,
                            log_path=log_path,
                            line_number=line_num,
                            line=line.strip(),
                            error_type=match,
                        )
                self.positions[log_path] = f.tell()
        except OSError as e:
            log.warning("Failed to read log %s: %s", log_path, e)

    def _check_line(self, line: str) -> str | None:
        """Check if line matches any error pattern, return error type or None."""
        for ignored in IGNORED_PATTERNS:
            if ignored.search(line):
                return None

        for pattern, error_type in ERROR_PATTERNS:
            if pattern.search(line):
                return error_type
        return None

    def count_error(self, project: str, error_type: str) -> int:
        """Increment and return the count for this error type."""
        key = (project, error_type)
        self.error_counts[key] = self.error_counts.get(key, 0) + 1
        return self.error_counts[key]

    def reset_count(self, project: str, error_type: str) -> None:
        """Reset the count for an error type."""
        self.error_counts.pop((project, error_type), None)
