"""Shared .env file loader for host-side scripts."""

import os
from pathlib import Path


def load_dotenv(path: Path) -> None:
    """Parse a .env file and inject into os.environ.

    Handles KEY=VALUE, KEY="VALUE", KEY='VALUE', and export KEY=VALUE.
    Skips comments and blank lines. Does NOT override existing entries.
    """
    if not path.is_file():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):]
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        # Strip matching quotes
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ('"', "'"):
            value = value[1:-1]
        if key not in os.environ:
            os.environ[key] = value
