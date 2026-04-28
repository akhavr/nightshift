"""Rule-based anomaly detection for watchdog logs."""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

ERROR_RE = re.compile(r"\b(ERROR|CRITICAL|Traceback|Exception)\b", re.IGNORECASE)


@dataclass
class Anomaly:
    type: str
    message: str
    context: str


def check_stale(log_path: Path, threshold_s: int) -> list[Anomaly]:
    """Flag logs that have not been updated recently."""
    if not log_path.exists():
        return []

    age_s = (datetime.now(timezone.utc) - datetime.fromtimestamp(log_path.stat().st_mtime, tz=timezone.utc)).total_seconds()
    if age_s <= threshold_s:
        return []
    return [
        Anomaly(
            type="stale_log",
            message=f"{log_path} has not been updated for {int(age_s)}s",
            context="",
        )
    ]


def check_errors(lines: Iterable[str], threshold: int) -> list[Anomaly]:
    """Count error-bearing lines and flag if they exceed the threshold."""
    error_lines = [line for line in lines if ERROR_RE.search(line)]
    if len(error_lines) <= threshold:
        return []
    return [
        Anomaly(
            type="error_threshold",
            message=f"Found {len(error_lines)} errors, above threshold {threshold}",
            context="\n".join(error_lines),
        )
    ]


def _normalize_error(line: str) -> str:
    line = line.strip()
    if not line:
        return ""
    # Strip a common timestamp prefix if present.
    parts = line.split(maxsplit=2)
    if len(parts) >= 3 and re.match(r"\d{4}-\d{2}-\d{2}", parts[0]):
        return parts[2]
    return line


def check_repeated(lines: Iterable[str], threshold: int) -> list[Anomaly]:
    """Detect the same error text repeated multiple times."""
    counter = Counter(_normalize_error(line) for line in lines if ERROR_RE.search(line))
    anomalies: list[Anomaly] = []
    for error_text, count in counter.items():
        if error_text and count >= threshold:
            anomalies.append(
                Anomaly(
                    type="repeated_error",
                    message=f"Repeated error '{error_text}' occurred {count} times",
                    context=error_text,
                )
            )
    return anomalies
