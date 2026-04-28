# Host-Container Interface

This document describes the formal interface between the nightshift host and container, including all communication channels, guardrails, and known gaps.

## Design Principle

**File-based only** — no network calls, no IPC sockets between host and container. All communication goes through the session directory:

```
Host:      .nightshift/sessions/<id>/
Container: /session/
```

## Mount Points

| Host Path | Container Path | Mode | Purpose |
|-----------|---------------|------|---------|
| `<worktree>` | `/workspace` | rw | Agent edits code here |
| `<session-dir>` | `/session` | rw | State, logs, IPC files |
| `<session-dir>/git-merged` | `/repo-git` | rw | Isolated git overlay |
| `~/.claude` | `/claude-auth` | **ro** | Auth credentials |
| `WORKFLOW.md` | `/workspace/WORKFLOW.md` | **ro** | Prompt template |
| `REVIEW.md` | `/workspace/REVIEW.md` | **ro** | Review template (if exists) |

## Communication Channels

### Host → Container

| File | Purpose | Writer | Reader | Format |
|------|---------|--------|--------|--------|
| `issue.json` | Current issue data | `issue_dump.py` | `StaticTracker` | JSON |
| `issues.json` | All open issues | `issue_dump.py` | `StaticTracker` | JSON |
| `answer.txt` | Q&A answer from human | watcher/CLI | `SessionRunner` | Plain text |
| `merge-needed.txt` | Merge conflict instructions | `workspace_setup.py` | `entrypoint.py` | Plain text with `---` separator |
| `litellm-config.yaml` | LLM proxy config | `docker_cmd.py` | `docker-entrypoint.sh` | YAML |

### Container → Host

| File | Purpose | Writer | Reader | Format |
|------|---------|--------|--------|--------|
| `state.json` | Session state | `StateManager` | watcher, CLI | JSON |
| `conversation.jsonl` | Conversation log | `StateManager` | watcher, CLI | JSON Lines |
| `raw-output.log` | Raw agent stdout | `SessionRunner` | CLI | Plain text |
| `waiting.json` | Q&A question pending | `StateManager` | watcher | JSON |
| `tracker-outbox.jsonl` | Tracker write ops | `StaticTracker` | `issue_sync.py` | JSON Lines |
| `resume-prompt.md` | Resume context | `StateManager` | debugging | Markdown |

### Signal Files (Container-internal)

| File | Purpose | Writer | Reader |
|------|---------|--------|--------|
| `/session/signal/done` | Task completion | agent | `SessionRunner` |
| `/session/signal/checkpoint` | Progress checkpoint | agent | `SessionRunner` |
| `/session/signal/question.json` | Question for human | agent | `SessionRunner` |

Note: Signal files are read by `SessionRunner` inside the container, not by the host directly.

## Guardrails

### 1. File Locking (state.json)

All read-modify-write operations on `state.json` use fcntl file locking:

```python
# core/state.py
@contextmanager
def state_lock():
    with open(STATE_LOCK_FILE, 'w') as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        yield
```

Both container (`StateManager`) and host (`session_utils`) use this lock.

### 2. SSM Validation

Every status change passes through `SessionStateMachine`:

```python
# Container side
StateManager.update_status(new_status)  # validates via SSM

# Host side
session_utils.update_status(session_dir, new_status)  # validates via SSM
```

Invalid transitions raise `InvalidTransition`. Direct `state["status"] = "foo"` is forbidden.

### 3. Atomic Outbox Processing

```python
# host/watcher/issue_sync.py
os.rename(outbox_path, processing_path)  # atomic
for entry in read_jsonl(processing_path):
    execute_op(entry)
os.unlink(processing_path)
```

Atomic rename prevents losing entries the container appends concurrently.

### 4. Read-Only StaticTracker

Container cannot modify the real tracker — writes are queued:

```python
# adapters/trackers/static.py
def add_comment(self, issue_id, body):
    self._append_outbox({"op": "add_comment", "issue_id": issue_id, "body": body})
```

### 5. UID/GID Matching

```bash
docker run --user $(id -u):$(id -g) ...
```

Container runs as host user, can read 600-permission auth files without privilege escalation.

### 6. Read-Only Mounts

Auth credentials and config templates are mounted read-only:

```bash
-v ~/.claude:/claude-auth:ro
-v WORKFLOW.md:/workspace/WORKFLOW.md:ro
```

### 7. Git Isolation

Container uses an overlay/copy of `.git`, not the live repo:

```bash
-v <session-dir>/git-merged:/repo-git:rw
```

Prevents container from corrupting host git state during operation.

### 8. Mtime-Based Live Sync

```python
# StaticTracker.reload()
if os.path.getmtime(self._issue_path) > self._mtime:
    self._load_issue()  # re-read
    return new_comments
```

Cheap stat() checks — no inotify dependency.

## Known Gaps

### GAP-001: Git Object Copy-Back (HIGH) — IMPLEMENTED

**Location:** `host/launch.py` — `_copy_git_changes()`

**Problem:** After container exit, git objects and refs are copied from the overlay back to the real `.git/` directory without validation. A compromised agent could:
- Inject malicious commits
- Corrupt refs
- Write bad pack files

**Implementation:** 
1. Run `git fsck --connectivity-only` before copy-back
2. Whitelist refs: only `refs/heads/agent-*` (no subpaths) are copied
3. Filter packed-refs, materialize allowed refs as loose refs

**Note on fsck flags:** Must use `--connectivity-only`, not `--no-dangling`. The git-bug tracker stores issues as refs under `refs/remotes/origin/bugs/*` with non-standard reflog formats. Plain `git fsck` or `--no-dangling` reports these as "invalid reflog entry" errors and blocks copy-back. `--connectivity-only` checks object graph integrity without validating reflogs.

### GAP-002: Outbox Schema Validation (MEDIUM)

**Location:** `host/watcher/issue_sync.py` — `process_outbox()`

**Problem:** Outbox entries are executed without schema validation. Malformed entries could cause errors; malicious `issue_id` values rely on git-bug's internal sanitization.

**Missing guardrail:** JSON schema validation, issue_id format check.

**Mitigation:**
```python
VALID_OPS = {"add_comment", "set_status", "add_label", "remove_label"}
ISSUE_ID_PATTERN = re.compile(r'^[a-f0-9]{8,64}$')

def validate_outbox_entry(entry):
    if entry.get("op") not in VALID_OPS:
        raise ValueError(f"Unknown op: {entry.get('op')}")
    if not ISSUE_ID_PATTERN.match(entry.get("issue_id", "")):
        raise ValueError(f"Invalid issue_id: {entry.get('issue_id')}")
```

### GAP-003: state.json Partial Validation (MEDIUM)

**Location:** `host/session_utils.py`, `host/watcher/*.py`

**Problem:** SSM validates status transitions, but other fields are unchecked:
- `usage.cost_usd` — could be set to arbitrary values
- `checkpoints` — no format validation
- `orphan_resumes` — no bounds check

**Missing guardrail:** Full schema validation on host reads.

**Mitigation:** Define a JSON schema for state.json; validate on every host read.

### GAP-004: Session Directory Size (LOW)

**Location:** Docker run configuration

**Problem:** Container can write unlimited data to `/session/`:
- `raw-output.log` — could be gigabytes
- `conversation.jsonl` — unbounded growth

**Missing guardrail:** Disk quota or size limits.

**Mitigation:**
- Use `--storage-opt size=1G` if supported by storage driver
- Use tmpfs with size limit: `--mount type=tmpfs,destination=/session,tmpfs-size=1G`
- Periodic size check in watcher with alert

### GAP-005: Symlink Escape (LOW)

**Location:** Worktree mount

**Problem:** Container could create symlinks in `/workspace/` pointing outside the worktree. If host git operations follow symlinks, this could leak or modify external files.

**Missing guardrail:** Symlink audit before merge/accept.

**Mitigation:**
- Use `git add --no-dereference`
- Audit worktree for symlinks before `accept`
- Reject symlinks pointing outside worktree

## Outbox Protocol

### Supported Operations

| Operation | Fields | Description |
|-----------|--------|-------------|
| `add_comment` | `issue_id`, `body` | Add comment to issue |
| `set_status` | `issue_id`, `status` | Open/close issue |
| `add_label` | `issue_id`, `label` | Add label to issue |
| `remove_label` | `issue_id`, `label` | Remove label from issue |

### Entry Format

```json
{"op": "add_comment", "issue_id": "abc123def", "body": "Work completed"}
{"op": "add_label", "issue_id": "abc123def", "label": "done"}
```

### Processing Guarantees

1. **Atomicity:** Rename-based processing prevents partial reads
2. **Ordering:** Entries processed in append order
3. **Crash recovery:** `.processing` file is re-processed on restart
4. **Idempotency:** Not guaranteed — duplicate comments possible on crash

## State Machine Transitions

See `core/state_machine.py` for the full SSM. Key transitions:

```
starting → working → waiting:review → reviewing → [merged|rejected]
                  → waiting:answer → working (after answer)
                  → suspended:* → working (after resume)
```

## Security Considerations

### Threat Model

The primary threat is a **compromised or misbehaving agent** (not a malicious user). The agent runs arbitrary code inside the container and could:
- Exfiltrate API keys (inherent — keys are needed for agent to function)
- Corrupt git state (GAP-001)
- Spam tracker (GAP-002)
- DoS via disk fill (GAP-004)

### Trust Boundaries

1. **Host → Container:** Host trusts that container will produce valid state updates
2. **Container → Host:** Host should NOT blindly trust container output (gaps exist)
3. **Agent → Container:** Container trusts agent (no sandbox within container)

### Recommendations

1. Run containers with minimal capabilities (`--cap-drop=ALL`)
2. Use read-only root filesystem where possible
3. Implement GAP-001 fix (git fsck) as priority
4. Consider seccomp profiles for additional isolation
