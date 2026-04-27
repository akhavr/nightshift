# Dependency Blocking

Nightshift supports dependency blocking via `blocked:<id>` labels. This prevents issues from starting out of order when multiple related issues are labeled for processing.

## Label Format

```
blocked:<git-bug-id-prefix>
```

Example: `blocked:d2000b5c84aa` blocks an issue until the issue with ID prefix `d2000b5c84aa` is accepted.

## Behavior

### Auto-start (watcher)

The watcher's auto-start feature skips issues with `blocked:<id>` labels. The issue remains in the queue and will be started once the blocking label is removed.

### Manual CLI commands

Manual CLI commands (`nightshift start`, `nightshift resume`, `nightshift accept`) ignore blocked labels. This allows operators to intentionally override blocking when needed.

### Auto-cleanup on accept

When an issue is accepted (`nightshift accept`), the system automatically:

1. Scans all open issues for `blocked:<accepted-id>` labels
2. Removes matching labels
3. Logs: "Unblocked <id> (dependency <prefix> closed)"

The unblocked issues become eligible for auto-start on the next watcher poll.

### Startup cleanup

On watcher startup, stale blocked labels are cleaned up:

1. Scans all open issues for `blocked:<id>` labels
2. For each label, checks if the blocking issue is closed
3. Removes stale labels where the blocker is already closed

This handles labels that became stale during watcher downtime.

## Commands

### List blocked issues

```bash
nightshift blocked
```

Output:
```
ISSUE          BLOCKED BY     TITLE
57561f29274b   d2000b5c84aa   SSM-5: Watcher uses SSM...
```

### Add a blocking label

```bash
nightshift issue bug label new <issue-id> blocked:<blocker-id>
```

### Remove a blocking label

```bash
nightshift issue bug label rm <issue-id> blocked:<blocker-id>
```

## Workflow

1. File related issues
2. Add `blocked:<id>` labels to dependent issues
3. Label all issues with `nightshift`
4. Watcher starts unblocked issues first
5. As issues are accepted, dependents are automatically unblocked
6. Watcher picks up newly unblocked issues

## Example

```bash
# File issues
nightshift issue bug add -t "Base feature" -m "..."    # Returns: abc123...
nightshift issue bug add -t "Depends on base" -m "..."  # Returns: def456...

# Mark dependency
nightshift issue bug label new def456 blocked:abc123456789

# Label both for processing
nightshift issue bug label new abc123 nightshift
nightshift issue bug label new def456 nightshift

# Watcher starts abc123 first
# When abc123 is accepted, def456 is unblocked
# Watcher then starts def456
```
