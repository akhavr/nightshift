"""Codebase audits to ensure architectural invariants."""

import re
from pathlib import Path


def test_no_direct_status_writes():
    """Ensure all status changes go through SSM-validated methods.

    Direct status writes bypass the SessionStateMachine validation.
    All status changes must use StateManager.update_status(), update_status()
    from host/session_utils.py, or update_state_fields() (which now validates).

    Allowed patterns:
    - st.status = self._ssm.state  (SSM-validated write in StateManager)
    - st.status = ssm.state  (SSM-validated write)
    - state["status"] = ssm.state  (SSM-validated write in host/session_utils.py)
    - state["status"] = status in force_update_status (intentional bypass for recovery)
    - Reading status: if st.status == ..., state["status"], state.get("status")
    - update_state_fields(..., status=...)  (now SSM-validated)
    - fields["status"] = ssm.state  (SSM-validated intermediate in update_state_fields)

    Forbidden patterns:
    - st.status = <literal string>  (bypasses SSM)
    - state["status"] = <literal string>  (bypasses SSM, outside of SSM-validated functions)
    """
    repo_root = Path(__file__).parent.parent

    # Directories to audit
    audit_dirs = [
        repo_root / "core",
        repo_root / "host",
        repo_root / "adapters",
    ]

    violations = []

    for audit_dir in audit_dirs:
        if not audit_dir.exists():
            continue
        for py_file in audit_dir.rglob("*.py"):
            content = py_file.read_text()
            rel_path = py_file.relative_to(repo_root)

            # Skip test files
            if "test_" in py_file.name:
                continue

            # Track current function for allowed bypasses
            current_func = None
            for line_no, line in enumerate(content.splitlines(), 1):
                # Track function definitions
                func_match = re.match(r'^def (\w+)\(', line)
                if func_match:
                    current_func = func_match.group(1)

                # Skip comments
                stripped = line.strip()
                if stripped.startswith("#"):
                    continue

                # Pattern 1: .status = ... (direct attribute assignment)
                # Match: st.status = X, self.status = X, obj.status = X
                # Skip: st.status == X (comparison)
                # Skip: st.status = self._ssm.state or ssm.state (SSM-validated)
                if re.search(r"\.status\s*=(?!=)", line):
                    # Allow SSM-validated writes
                    if "self._ssm.state" in line or "ssm.state" in line:
                        continue
                    violations.append(f"{rel_path}:{line_no}: {stripped}")

                # Pattern 2: state["status"] = ... (dict named 'state', not filters/etc)
                # Must be assignment (=) not comparison (==)
                # Allow: state["status"] = ssm.state (SSM-validated)
                # Allow: force_update_status (intentional bypass for manual recovery)
                if re.search(r'state\s*\[\s*["\']status["\']\s*\]\s*=(?!=)', line):
                    # Allow SSM-validated writes
                    if "ssm.state" in line:
                        continue
                    # Allow intentional bypass in force_update_status
                    if current_func == "force_update_status":
                        continue
                    violations.append(f"{rel_path}:{line_no}: {stripped}")

                # Pattern 3: fields["status"] = ... (common intermediate variable)
                # Must be assignment (=) not comparison (==)
                # Allow: fields["status"] = ssm.state (SSM-validated)
                if re.search(r'fields\s*\[\s*["\']status["\']\s*\]\s*=(?!=)', line):
                    # Allow SSM-validated writes
                    if "ssm.state" in line:
                        continue
                    violations.append(f"{rel_path}:{line_no}: {stripped}")

    assert not violations, (
        "Direct status writes bypass SSM validation.\n"
        "Use StateManager.update_status() or update_status() from host/session_utils.py:\n"
        + "\n".join(f"  - {v}" for v in violations)
    )
