# nightshift-client: Client Library for Nightshift Integration

## Motivation

Multiple projects need to submit work to nightshift and track results. Each
currently reimplements the same patterns: create git-bug issue, push to remote,
poll for state changes, read results. A shared client library eliminates this
duplication and provides a stable integration contract.

This is the **client-side counterpart** to nightshift's host-side watcher.
Nightshift is the server/executor. The client library is how applications submit
work and track results.

## Core Capabilities

### 1. Issue creation

Create a git-bug issue in a task repo with proper labels for nightshift pickup.

```python
from nightshift_client import NightshiftClient

client = NightshiftClient(repo_path="/path/to/local/clone")
issue_id = client.create_issue(
    title="Research: compare DeFi fee structures",
    body="Compare Derive vs dYdX fees, focusing on...",
    labels=["nightshift"],  # triggers watcher auto-start
)
client.push()  # push to gitolite remote
```

### 2. State monitoring via git fetch

Poll for nightshift state changes by fetching git-bug refs. Lightweight — no data
transfer if nothing changed.

```python
state = client.check_state(issue_id)
# Returns: "pending", "running", "question", "completed", "failed", "cancelled"

# With details:
info = client.get_issue_info(issue_id)
# {
#     "state": "question",
#     "labels": ["nightshift", "needs-human-input"],
#     "last_comment": "Should I compare spot prices or futures?",
#     "updated_at": "2026-04-28T10:05:00",
# }
```

### 3. Question/answer flow

Read questions from git-bug comments, post answers back.

```python
question = client.get_pending_question(issue_id)
# "Should I compare spot prices or futures?"

client.post_answer(issue_id, "Spot prices only")
client.push()
```

### 4. Result retrieval

After completion, pull the repo and read output files.

```python
client.pull()  # git pull to get merged output
files = client.read_output()
# {"findings.md": "## Summary\n...", "comparison-table.md": "| Exchange | ..."}
```

### 5. Cancellation

```python
client.cancel(issue_id)  # adds status:cancelled label + comment
client.push()
```

### 6. Batch monitoring

Check multiple repos/issues efficiently (for applications tracking many tasks).

```python
from nightshift_client import monitor_all

updates = monitor_all([
    {"repo_path": "/path/to/repo-a", "issue_id": "abc123"},
    {"repo_path": "/path/to/repo-b", "issue_id": "def456"},
])
# [{"issue_id": "abc123", "state": "completed"}, {"issue_id": "def456", "state": "running"}]
```

## git-bug Label Protocol

The client maps nightshift states to git-bug labels:

| Nightshift state | git-bug label | Client state |
|---|---|---|
| starting / working | `status:running` | `"running"` |
| waiting:question | `needs-human-input` | `"question"` |
| done:pending-review / accepted | `status:completed` | `"completed"` |
| suspended:* / rejected | `status:failed` | `"failed"` |
| (cancelled by client) | `status:cancelled` | `"cancelled"` |
| issue created, not yet picked up | `nightshift` (no status label) | `"pending"` |

The `needs-human-input` label already exists in nightshift (qa_flow.py:30).
Other `status:*` labels are new — require nightshift enhancement to push labels
on state transitions.

## git-bug Python Bindings (internal)

Thin wrapper around `git-bug` CLI. Not exposed as public API initially, but could
be extracted later if other projects need direct git-bug access.

```python
from nightshift_client._gitbug import GitBug

gb = GitBug(repo_path="/path/to/repo")
issue_id = gb.add(title="...", body="...", labels=["nightshift"])
gb.comment(issue_id, "answer text")
gb.label(issue_id, "status:completed")
gb.push()
gb.pull()
issues = gb.list(labels=["needs-human-input"])
```

## Gitolite Admin Helpers (optional module)

For applications that auto-provision task repos. Wraps gitolite-admin operations
with validation guardrails.

```python
from nightshift_client.gitolite import GitoliteAdmin

admin = GitoliteAdmin(admin_repo_path="/path/to/gitolite-admin")
admin.add_repo("tasks/trading", owner="app")
errors = admin.validate()  # parse gitolite.conf, check syntax
if not errors:
    admin.push()  # safe push after validation
```

Guardrails:
- Validate gitolite.conf syntax before every push
- Dry-run mode (validate without pushing)
- Backup current conf before modifying
- If provisioning logic grows complex, use a state machine

## What the Library Does NOT Include

- **WORKFLOW.md templates** — project-specific, owned by the consuming application
- **Monitoring cadence/scheduling** — application decides how often to poll
- **Result interpretation** — depends on task type (research findings vs code diff)
- **Task lifecycle state machine** — application-specific (each consumer has its own)

## Nightshift Enhancements Required

The client library depends on nightshift pushing state to git-bug. Current
nightshift only does this for questions (`needs-human-input`). Needed:

1. **Git-bug state push on transitions** — add `status:running`, `status:completed`,
   `status:failed` labels and push to remote on state changes
2. **Git-bug answer detection** — watcher detects new comments on issues with
   `needs-human-input` label, treats as answers (alternative to file-based
   `answer.txt`)
3. **Cancellation support** — detect `status:cancelled` label, kill running session
4. **Multi-repo watcher** — single watcher monitors multiple repos

## Package Structure

```
nightshift-client/
  src/nightshift_client/
    __init__.py          # NightshiftClient, monitor_all
    _gitbug.py           # git-bug CLI wrapper
    _state.py            # label-to-state mapping, state detection
    gitolite.py          # optional: gitolite admin helpers
  tests/
  pyproject.toml
```

Sibling project to nightshift. Consumed by any application that integrates with
nightshift for task execution.

## References

- Nightshift session lifecycle: `docs/session-lifecycle.md`
- Nightshift Q&A flow: `core/qa_flow.py` (existing `needs-human-input` label)
- Nightshift state machine: `core/state_machine.py`
