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

### 7. Orphan Review Refs After Failed Overlay Teardown

**Problem:** Review session refs can be left pointing to non-existent commits, corrupting git operations across the repo.

**Symptoms:**
- `git show-ref` fails with "bad ref refs/heads/review/xxx"
- `git fsck` reports "invalid sha1 pointer"
- git-bug GraphQL queries fail with cascading errors ("bug doesn't exist")

**Root cause:** During git overlay teardown, the ref copy-back can succeed while the object copy-back fails (e.g., due to fsck errors, race conditions, or overlay unmount timing). This leaves a ref pointing to a commit that was never copied to the main repo.

**Sequence:**
1. Review session runs in git overlay (session-dir/git-merged or git-copy)
2. Agent creates commits in the overlay
3. Overlay teardown begins: `_copy_git_changes()` called
4. Ref `refs/heads/review/xxx` is copied to main repo
5. Object copy fails or is skipped (fsck error, missing objects)
6. Orphan ref remains pointing to non-existent commit

**Manual fix:**
```bash
# Find orphan refs
git fsck 2>&1 | grep "invalid sha1 pointer"

# Delete each orphan ref
git update-ref -d refs/heads/review/<session-id>
```

**Proper fix needed:** GAP-001's `extract_commits()` should verify target commits exist before copying refs. Or: copy objects first, verify, then copy refs atomically.

**Status:** Open - manual cleanup required when encountered

---

## Monitoring Notes

- `git-bug bug timed out after 30s` in logs indicates CLI tracker fallback with lock retry (expected when watcher not running)
- Review sessions should complete within 5 minutes; longer indicates potential issues
- Sessions stuck at `reviewing` without a running container may indicate orphaned state
