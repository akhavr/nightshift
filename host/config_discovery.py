"""Workflow file discovery for per-repo configuration.

Discovery order (first match wins):
1. Explicit --workflow CLI flag (passed as override parameter)
2. .nightshift.yaml in repo root (workflow: field)
3. WORKFLOW.md in repo root (default fallback)
"""

import logging
import sys
from pathlib import Path

import yaml

log = logging.getLogger(__name__)

LOCAL_CONFIG_FILENAME = ".nightshift.yaml"
DEFAULT_WORKFLOW_FILENAME = "WORKFLOW.md"


def discover_workflow(repo_root: Path, cli_override: str | None = None) -> Path:
    """Find the workflow file using the three-level discovery order.

    Args:
        repo_root: The git repository root directory.
        cli_override: Explicit --workflow path from CLI (highest priority).

    Returns:
        Resolved Path to the workflow file.

    Raises:
        SystemExit: If no workflow file is found at any level.
    """
    # 1. CLI flag (highest priority)
    if cli_override:
        path = Path(cli_override).expanduser().resolve()
        if not path.is_file():
            print(f"Workflow file not found: {path}", file=sys.stderr)
            sys.exit(1)
        return path

    # 2. .nightshift.yaml pointer in repo root
    local_config = repo_root / LOCAL_CONFIG_FILENAME
    if local_config.is_file():
        path = _read_workflow_pointer(local_config, repo_root)
        if path is not None:
            if not path.is_file():
                print(f"Workflow file referenced in {LOCAL_CONFIG_FILENAME} not found: {path}",
                      file=sys.stderr)
                sys.exit(1)
            return path

    # 3. Default WORKFLOW.md in repo root
    default = repo_root / DEFAULT_WORKFLOW_FILENAME
    if default.is_file():
        return default

    print(f"No workflow file found. Looked for:\n"
          f"  1. {LOCAL_CONFIG_FILENAME} pointer in {repo_root}\n"
          f"  2. {DEFAULT_WORKFLOW_FILENAME} in {repo_root}\n"
          f"Run 'nightshift init' to create one.", file=sys.stderr)
    sys.exit(1)


def _read_workflow_pointer(config_path: Path, repo_root: Path) -> Path | None:
    """Read the workflow path from .nightshift.yaml.

    Returns:
        Resolved Path, or None if the file has no workflow key.
    """
    try:
        data = yaml.safe_load(config_path.read_text())
    except Exception as e:
        log.warning(f"Failed to parse {config_path}: {e}")
        return None

    if not isinstance(data, dict):
        return None

    workflow_value = data.get("workflow")
    if not workflow_value:
        return None

    path = Path(workflow_value).expanduser()
    if not path.is_absolute():
        path = repo_root / path
    return path.resolve()


def write_local_config(repo_root: Path, workflow_path: str) -> Path:
    """Write or update .nightshift.yaml with the workflow pointer.

    Args:
        repo_root: The git repository root directory.
        workflow_path: The workflow file path to store.

    Returns:
        Path to the written .nightshift.yaml file.
    """
    config_path = repo_root / LOCAL_CONFIG_FILENAME
    data = {}
    if config_path.is_file():
        try:
            existing = yaml.safe_load(config_path.read_text())
            if isinstance(existing, dict):
                data = existing
        except Exception as e:
            log.warning(f"Failed to parse existing {config_path}, overwriting: {e}")

    data["workflow"] = workflow_path
    config_path.write_text(yaml.dump(data, default_flow_style=False))
    return config_path
