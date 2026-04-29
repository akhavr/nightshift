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
| starting | `status:starting` | `"starting"` |
| working | `status:working` | `"working"` |
| waiting:question | `needs-human-input` | `"question"` |
| waiting:review | `status:waiting-review` | `"waiting_review"` |
| waiting:human-review | `status:waiting-human-review` | `"waiting_human_review"` |
| reviewing | `status:reviewing` | `"reviewing"` |
| done:pending-review | `status:pending-review` | `"pending_review"` |
| accepted | `status:accepted` | `"accepted"` |
| suspended:auth-failure | `status:suspended-auth` | `"suspended_auth"` |
| suspended:max-resumes | `status:suspended-max-resumes` | `"suspended_max_resumes"` |
| suspended:* (other) | `status:suspended` | `"suspended"` |
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

1. **Git-bug state push on transitions** — add `status:*` labels (see table above)
   and push to remote on state changes
2. **Git-bug answer detection** — watcher detects new comments on issues with
   `needs-human-input` label from non-bot authors, treats as answers. Requires
   per-repo bot identity setup and authorship check in answer detection logic
3. **Cancellation support** — detect `status:cancelled` label, kill running session
4. **Multi-repo watcher** (optional) — single watcher monitors multiple repos.
   Not a prerequisite; the client works with per-repo watchers but is not limited
   to that model

## Package Structure

```
agent-worker/
  nightshift-client/           # subdirectory, pip-installable
    src/nightshift_client/
      __init__.py              # NightshiftClient, monitor_all
      _gitbug.py               # git-bug CLI wrapper
      _state.py                # label-to-state mapping, state detection
      gitolite.py              # optional: gitolite admin helpers
    tests/
    pyproject.toml
```

Subdirectory of agent-worker, but a complete Python module with its own
`pyproject.toml` for independent installation via pip. Consumed by any
application that integrates with nightshift for task execution.

## Deployment Model

Client and nightshift may run on different machines, coordinating via gitolite
remote. The client pushes issues and answers to gitolite; nightshift polls the
remote, executes work, and pushes state changes back. The client polls via git
fetch to detect state transitions. No direct network connection between client
and nightshift is required.

## Open Questions

### Resolved

1. **Git-bug answer detection**: Additional channel initially, becomes primary later.
   
   **Detection method:** Authorship-based. Nightshift uses a per-repo bot identity
   (e.g., `@nightshift`) for all automated comments. Any comment NOT from bot
   identity on an issue with `needs-human-input` label = answer.
   
   **Implementation:**
   - Create per-repo bot identity: `git-bug user create --name "nightshift" ...`
   - Watcher posts lifecycle/proof-of-work comments with bot identity
   - Telegram answers and client answers attributed to human identity
   - Answer detection: `comment.author != BOT_IDENTITY`
   
   **Benefits:** No prefix convention, natural UX, clear audit trail.

### Deferred (to be discussed)

2. **Gitolite helpers scope**: The optional `GitoliteAdmin` class handles repo
   provisioning. Is auto-provisioning task repos actually needed by consuming
   applications, or is this speculative?

### Design decisions needed

3. **Batch monitoring auth**: `monitor_all()` across multiple repos — same SSH
   key assumed for all gitolite remotes, or does each repo entry need its own
   credentials?

4. **Error handling pattern**: Push fails, git-bug malformed, network timeout —
   should the client raise exceptions, return error objects, or use Result types?

5. **Cancellation semantics**: Client adds `status:cancelled` label. Is this
   fire-and-forget, or does the client wait for confirmation that nightshift
   killed the session?

6. **Output file discovery**: `client.read_output()` returns files. How does the
   client know which files are "output"? Convention (specific directory),
   manifest file, or label in issue?

7. **Polling vs push**: The design uses polling via git fetch. Any consideration
   for lower-latency notification (webhooks, server-sent events) for
   latency-sensitive consumers?

### Resolved

8. **Telegram-to-identity mapping**: Use Telegram username as git-bug author
   name with namespace prefix: `tg:<username>` or `telegram:<username>`.
   
   Example: Answer from `@akhavr` on Telegram → git-bug comment authored by
   `tg:akhavr`. Preserves individual attribution and indicates source channel.

9. **Client library identity**: Client must configure an identity explicitly.
   
   ```python
   client = NightshiftClient(
       repo_path="/path/to/clone",
       identity="alice@example.com"  # Required
   )
   ```
   
   The client uses this identity when posting answers/comments. Ensures
   attribution is explicit, not accidentally inherited from repo config.

## References

- Nightshift session lifecycle: `docs/session-lifecycle.md`
- Nightshift Q&A flow: `core/qa_flow.py` (existing `needs-human-input` label)
- Nightshift state machine: `core/state_machine.py`
