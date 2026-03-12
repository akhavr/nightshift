---
name: nightshift-watch
description: Monitor nightshift session statuses, review finished agent work, and take action (accept/reject/revise). Use when asked to watch nightshift, check status, or review agent output.
disable-model-invocation: true
---

# Nightshift Watch

Monitor nightshift sessions and act on results. Run periodically until told to stop.

## Loop

1. Run `nightshift status` to get all sessions
2. For each session in `waiting:review` status:
   - Run `nightshift history <id>` to see conversation timeline
   - Run `git diff main...<branch>` to review the actual code changes (get branch from the worktree)
   - Evaluate: does the work match the issue? Is it complete? Are there regressions?
   - Decide: **accept**, **reject**, or **revise**
3. For each session in `waiting:answer`:
   - Show the question to the user and relay their answer via `nightshift answer <id> "..."`
4. For sessions that are `running` — just report status, no action needed
5. For sessions that are `failed` or stuck — investigate logs with `nightshift logs <id>`

## Actions

- **Accept**: `nightshift accept <id>` — merges agent branch, cleans up. Use when work is correct and complete.
- **Reject**: `nightshift reject <id>` — discards work. Use when approach is fundamentally wrong.
- **Revise**: `nightshift revise <id> "feedback"` — sends back with feedback. Use when work is partially correct but needs fixes.
- **Follow-up**: If work is partially done but acceptable, accept it and file a new focused git-bug issue for remaining work. Label it `nightshift`.

## Rules

- Always read the diff before accepting. Never accept blindly.
- Check that CLAUDE.md, docs/requirements.md, and existing tests are not broken or reverted.
- If an issue has been through 3+ review rounds, consider accepting partial work + filing follow-ups rather than sending back again.
- Report status to the user at each check cycle.
- Wait 60 seconds between poll cycles unless the user asks for a different interval.
