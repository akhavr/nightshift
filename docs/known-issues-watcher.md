# Known Watcher Issues

This document tracks known issues with the nightshift watcher and their resolution status.

## Resolved Issues

### 1. Rebase Fails Inside Container

**Problem:** Git rebase inside the container fails with `unable to unlink old 'WORKFLOW.md': Device or resource busy` because bind-mounted files cannot be unlinked by git during rebase operations.

**Impact:** Pre-review rebase would fail, leaving sessions stuck.

**Solution:** Moved rebase logic to host-side (`host/rebase.py`). The watcher's `review_orchestrator.py` now calls `attempt_pre_review_rebase()` on the host before launching the review container.

**Status:** Resolved (merged to master)

---

### 2. Outdated Branches Cause False "Deletions"

**Problem:** When an agent branch is created before recent master commits, the diff shows those commits as "deletions". Reviewers would ask agents to "restore" code that was never in the branch, instead of requesting a rebase.

**Impact:** Review cycles wasted on impossible requests. Agents would fail to satisfy reviews.

**Solution:** Added item 9 to REVIEW.md instructing reviewers to check `git log --oneline HEAD..{{ base_branch }}` and request rebase instead of manual restoration.

**Status:** Resolved (commit 26c28220)

---

### 3. Infinite Container Relaunch Loop

**Problem:** Watcher failed to detect when agent containers exited after completing work, leading to infinite relaunch cycles. Container would exit, watcher would relaunch, new container found no work, exited, relaunch repeated.

**Root cause:** Race between container exit detection and status transition.

**Solution:** Fixed in commit 7086158 - improved container exit detection and status transition handling.

**Status:** Resolved (merged to master)

**Related issue:** c0483cd5 was a victim of this bug (session ran before fix merged).

---

### 4. Socket Server Health Check

**Issue ID:** 93495dacbe1c

**Problem:** If the tracker socket server crashes, CLI commands and external processes fail to communicate with the watcher's single-writer tracker.

**Solution:** Added `is_alive()` and `restart()` methods to `TrackerSocketServer`, plus `_check_socket_server_health()` with exponential backoff in the watcher main loop.

**Status:** Resolved (merged to master)

---

### 5. Signal Method Configuration

**Issue ID:** f81c2aed2986

**Problem:** Different agent types (Claude Code, OpenHands, Codex) use different signal mechanisms (MCP tools, text markers, file signals). No way to configure which mechanism to use per agent.

**Solution:** Added `signal_method` config option to WORKFLOW.md (`auto`, `mcp`, `text`, `file`). SessionRunner checks the configured method.

**Status:** Resolved (merged to master)

---

### 6. GraphQL Tracker Lock Conflict

**Issue ID:** 2e1f041

**Problem:** `run_raw()` (CLI passthrough) shells out to git-bug CLI while the watcher's webui holds the repository lock. Commands fail with lock errors.

**Solution:** Added "label add/rm" alias parsing to route through tracker methods.

**Status:** Resolved (merged to master)

---

## Open Issues

### 7. Orphan Review Refs After Failed Copy-Back

**Problem:** Review session refs can be left pointing to non-existent commits, corrupting git operations across the repo.

**Symptoms:**
- `git show-ref` fails with "bad ref refs/heads/review/xxx"
- `git fsck` reports "invalid sha1 pointer"
- git-bug GraphQL queries fail with cascading errors ("bug doesn't exist")

**Root cause:** Review branches are created **directly on host** via `workspace_setup.create_worktree()`:

```python
subprocess.run(["git", "branch", branch, base_branch], ...)
# branch = "review/{coder_sid}"
# base_branch = "agent/{short_id}"
```

Git creates the ref without verifying the base commit exists. If the agent branch points to a missing commit (due to failed copy-back from overlay), the review branch also points to that missing commit.

**Sequence:**
1. Coder session creates commits in git overlay
2. Copy-back fails (fsck error, race condition, etc.)
3. Agent branch ref exists but points to non-existent commit
4. Review session starts
5. `create_worktree()` creates `review/{coder_sid}` from `agent/{short_id}`
6. Review branch now also points to non-existent commit
7. Git operations fail across the repo

**Manual fix:**
```bash
# Find orphan refs
git fsck 2>&1 | grep "invalid sha1 pointer"

# Delete each orphan ref
git update-ref -d refs/heads/review/<session-id>
git update-ref -d refs/heads/agent/<session-id>
```

**Proper fix needed:** `workspace_setup.create_worktree()` should verify base commit exists before creating branch:
```python
result = subprocess.run(["git", "cat-file", "-t", base_branch], ...)
if result.returncode != 0:
    raise ValueError(f"Base branch {base_branch} points to missing commit")
```

**Issue ID:** 93b2a3a0

**Solution:** Added `detect_orphan_refs()` and `cleanup_orphan_refs()` in `host/watcher/host_watcher.py`. The watcher now detects and cleans orphan refs on startup and periodically during the main loop.

**Status:** Resolved (merged to master, session 93b2a3a0cc18)

---

### 8. Review Branch Created From Missing Commit

**Issue ID:** 72f3989b3e93

**Problem:** `create_worktree()` doesn't verify the base commit exists before creating a branch. If the agent branch points to a missing commit (copy-back failed), the review branch creation succeeds but points to garbage.

**Location:** `host/workspace_setup.py:create_worktree()`

**Solution:** The orphan ref detection in issue #7 catches and cleans these refs. Additionally, tests were added in `tests/test_workspace_setup.py::test_create_worktree_rejects_missing_base_commit`.

**Status:** Resolved (merged to master, session 93b2a3a0cc18)

---

### 9. Ref Whitelist Regex Mismatch Loses Agent Commits

**Issue ID:** e091cd7c2b90

**Problem:** The ref whitelist regex in `host/git_overlay.py` uses the wrong branch naming pattern, causing `extract_commits()` to skip copying agent branch refs back to the host repo. Commits are lost.

**Symptoms:**
- Agent completes work, commits inside container, tests pass
- On host, files appear as untracked/modified, no commit exists
- Session shows 0 CPS and step 0 despite successful work
- `git log` in worktree shows no agent commits

**Root cause:** Branch naming convention mismatch:
```python
# launch.py:43 - actual branch names use slash
"branch": f"{prefix}/{short_id}"  # produces "agent/abc123"

# git_overlay.py:12 - regex expects hyphen
_ALLOWED_AGENT_REF_RE = re.compile(r"^refs/heads/agent-[^/]+$")  # expects "agent-abc123"
```

The regex was written based on `worktree_name` (which uses hyphens: `agent-abc123`) instead of `branch` (which uses slashes: `agent/abc123`).

**Why objects don't help:** Git objects ARE copied back (no filtering). But without the ref pointing to the commit, git doesn't know about the commit chain. Working directory changes persist (direct mount), but `git status` shows them as uncommitted.

**Introduced:** Commit f7c75219 (GAP-001 security hardening, 2026-04-27)

**Fix:** Changed regex to match actual branch naming:
```python
_ALLOWED_AGENT_REF_RE = re.compile(r"^refs/heads/(agent|review)/[^/]+$")
```

Security property preserved: `[^/]+` still blocks nested paths and traversal.

**Status:** Resolved (merged to master)

---

### 10. Git-bug Cache Auto-Recovery

**Issue ID:** 4010068

**Problem:** The git-bug GraphQL tracker caches bug excerpts in `.git/git-bug/cache/bugs`. If a bug ref is deleted while the watcher is running (manual deletion, push/pull conflict, etc.), the cache becomes stale. `list_issues()` fails with "bug doesn't exist" on every poll cycle, blocking auto-start.

**Symptoms:**
- Watcher log shows repeated `list_issues failed: git-bug GraphQL error: [{'message': "bug doesn't exist", 'path': ['repository', 'allBugs', 'nodes', N, 'comments']}]`
- Auto-start stops picking up new issues
- Direct GraphQL queries to specific bugs may work while `allBugs` fails

**Root cause:** `lazyBug.load()` calls `cache.Bugs().Resolve(id)` which does a fresh git read. If the ref was deleted after cache build, the read fails. The persisted cache file at `.git/git-bug/cache/bugs` survives webui restarts.

**Diagnosis:**
```bash
# Compare counts - mismatch indicates stale cache
git show-ref | grep refs/bugs | wc -l  # actual refs
curl -s 'http://localhost:<port>/graphql' -d '{"query":"{ repository { allBugs { nodes { id } } } }"}' | jq '.data.repository.allBugs.nodes | length'  # cached
```

**Manual fix:**
```bash
rm .git/git-bug/cache/bugs
# Restart watcher (or webui if running standalone)
```

**Fix:** Implemented a three-layer recovery path:
1. On-error recovery in `adapters/trackers/git_bug_graphql.py`: catch "bug doesn't exist", clear `.git/git-bug/cache/bugs`, restart webui, retry once.
2. Periodic health check in `host/watcher/host_watcher.py`: compare `refs/bugs` count with cached GraphQL issue count and rebuild on mismatch.
3. SIGHUP handling in `host/watcher/main.py`: mark git-bug cache for clearing before config reload restarts the tracker.

**Status:** Resolved (session 4010068a8fa6)

---

## Monitoring Notes

- `git-bug bug timed out after 30s` in logs indicates CLI tracker fallback with lock retry (expected when watcher not running)
- Review sessions should complete within 5 minutes; longer indicates potential issues
- Sessions stuck at `reviewing` without a running container may indicate orphaned state
