# Git Worktree Corruption by Containers

This document explains the git worktree corruption issue discovered during nightshift development and the fix.

## Problem

When containers run git operations, they corrupt host git state in two places:

### Corruption Point 1: Worktree `.git` file

`docker-entrypoint.sh` rewrites `/workspace/.git`:
```
gitdir: /repo-git/worktrees/agent-abc123
```

This is intentional (git needs container paths to work), but corrupts host state.

### Corruption Point 2: Worktree metadata `gitdir` file

When git runs inside the container with the rewritten `.git`, it auto-updates the reverse pointer:
```
.git/worktrees/agent-abc123/gitdir = /workspace/.git
```

This is **not** intentional - git does this automatically to maintain bidirectional links.

## Why the Original Fix (88acf5d) Was Incomplete

The original fix only addressed Corruption Point 1:
- `host/rebase.py:_fix_container_gitdir()` rewrites worktree `.git` to host paths

But Corruption Point 2 remained unfixed, causing:
```
fatal: not a git repository: /repo-git/worktrees/agent-abc123
```

The error is misleading - the worktree `.git` was fixed, but git follows the chain to `.git/worktrees/<name>/gitdir` which still points to `/workspace/.git`.

## The Rebase Loop

With multiple concurrent sessions:
1. Session completes -> `done:pending-review`
2. Watcher tries pre-review rebase -> fails (gitdir corruption)
3. Watcher relaunches coder -> container re-corrupts gitdir
4. Repeat forever

No session could "win" because:
- Fix applied to worktree `.git`
- Rebase fails on metadata `gitdir`
- Coder relaunched, re-corrupts everything
- Loop continues

## The Clean Fix: Environment Variables

Instead of rewriting files (which git then "fixes" by corrupting reverse pointers), use environment variables:

```bash
# In docker-entrypoint.sh, REPLACE file rewriting with:
if [ -d /repo-git ] && [ -n "$WORKTREE_NAME" ]; then
    export GIT_DIR="/repo-git/worktrees/${WORKTREE_NAME}"
    export GIT_WORK_TREE="/workspace"
fi
```

### Why This Works

1. **No file modifications** -> nothing to corrupt
2. **No cleanup needed** -> no race with container crash
3. **Git respects env vars** for all operations
4. **No defensive fixes needed** on host side

### Tested Operations

All work correctly with env vars:
- `git status`, `git branch`, `git log`
- `git diff`, `git remote`
- `git add`, `git commit`
- `git fetch`, `git rebase`

## Migration

1. Update `docker-entrypoint.sh` to use env vars instead of file rewriting
2. Remove the cleanup trap (no longer needed)
3. Host-side `_fix_container_gitdir()` becomes defense-in-depth for old containers
4. Optionally extend `_fix_container_gitdir()` to also fix metadata gitdir as belt-and-suspenders

## Corruption Point 3: `core.worktree=/workspace` in main config

### Symptom

All host-side git commands fail with "not a git repository" or behave unexpectedly because they think `/workspace` is the working directory.

```
$ git status
fatal: /workspace: No such file or directory
```

### Detection

`ConfigWatchdog` in `host/watcher/config_watchdog.py` polls `.git/config` and logs:
```
ALERT: core.worktree=/workspace detected in .git/config!
```

**Bug:** ConfigWatchdog only LOGS the problem but doesn't auto-fix. Filed as issue to add auto-sanitization.

### Root Cause: Git Config Resolution in Worktrees

When `GIT_DIR` points to a worktree, `git config` writes go to the **main repo's config**, not a worktree-specific config:

```bash
# Container sets:
export GIT_DIR=/repo-git/worktrees/agent-xxx
export GIT_WORK_TREE=/workspace

# Any git config write:
git config core.worktree /workspace
# Writes to /repo-git/config (main repo!) not worktree config
```

This happens because:
1. Worktrees don't have their own config file by default
2. Git follows `commondir` (which contains `../..`) to find the main repo
3. Config writes go to the main repo's `.git/config`

**The read-only mount `/repo-git/config:ro` should block this**, but evidence shows writes still occur:
- Watcher logs show "Sanitized core.worktree=/workspace (exit)" - container successfully UNSETS the config
- Container runs `git config --unset core.worktree` which requires write access

**Tested scenarios that DON'T write core.worktree**:
- Normal git operations (status, fetch, merge, rebase) in worktrees
- Git commands with GIT_WORK_TREE env var set

**The main repo config mount stays read-only**, while the worktree pointer mount must remain writable so transactional cleanup can restore it:
```
-v /repo/.git:/repo-git:rw
-v /repo/.git/config:/repo-git/config:ro
```
Result: direct config writes fail as expected, while the `.git` pointer can still be restored by the cleanup transaction.

**Bug in sanitize function**: `docker-entrypoint.sh` prints "Sanitized" even when unset FAILS (due to `|| true`), giving false confidence that cleanup succeeded.

**Timeline pattern from logs**:
```
19:07:54 Pre-review rebase onto master...
19:08:10 .git/config modified     <-- No ALERT yet
19:08:11 .git/config modified     
19:09:48 .git/config modified     <-- ALERT: core.worktree=/workspace
```

Config is modified multiple times before the pollution appears. Something between normal config writes and 19:09:48 sets core.worktree.

**Root cause still under investigation**. Possibilities:
1. Host-side rebase somehow triggers it (but tests don't reproduce)
2. Git-bug webui (holding lock for 1100+s) interfering
3. Some race condition in multi-session scenario

### Current Mitigations

1. **WT-1.6**: Container startup calls `sanitize_core_worktree()` in `docker-entrypoint.sh`
2. **WT-1.7**: Container EXIT trap restores the saved `.git` pointer and calls `sanitize_core_worktree()`
3. **Host sanitization**: `sanitize_git_config()` in `host/rebase.py` called during rebase/merge/accept

### Gap

If container is killed (SIGKILL) without EXIT trap, and no host operation runs, the pollution persists. `ConfigWatchdog` detects but doesn't fix.

### Manual Fix

```bash
git config --unset core.worktree
```

## Related Issues

- 88acf5d: Original gitdir fix (partial) - amended to use env var approach
- WT-1.5 in docs/hidden-modules.md: Host-side defense (still useful as fallback)
- ConfigWatchdog auto-fix: Pending issue to make watchdog call `sanitize_git_config()` on detection
