"""Hook execution for workspace lifecycle events.

Extracted from SessionRunner to isolate shell-execution concern.
"""

import logging
import subprocess
from pathlib import Path

log = logging.getLogger(__name__)

DEFAULT_HOOK_TIMEOUT_S = 60


def run_hook(
    workspace_mgr,
    workspace_path: Path | None,
    script: str | None,
    name: str,
    timeout_s: int = DEFAULT_HOOK_TIMEOUT_S,
    fatal: bool = False,
) -> bool:
    """Execute a hook script. Returns True on success."""
    if not script or workspace_path is None:
        return True
    log.info(f"Running {name} hook...")
    if hasattr(workspace_mgr, "run_hook"):
        ok = workspace_mgr.run_hook(workspace_path, script, timeout_s)
    else:
        try:
            subprocess.run(
                ["sh", "-c", script], cwd=str(workspace_path),
                timeout=timeout_s, check=True, capture_output=True,
            )
            ok = True
        except Exception as e:
            log.warning(f"{name} hook failed: {e}")
            ok = False
    if not ok and fatal:
        log.error(f"{name} hook failed (fatal)")
    return ok
