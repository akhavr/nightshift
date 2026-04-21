---
template_version: 2
agent:
  kind: claude-code
  max_turns: 50
  stall_timeout_s: 300
  extra_args: []

tracker:
  kind: git-bug

workspace:
  kind: worktree
  base_branch: main
  root: .worktrees

notifications:
  - kind: telegram
    token: $TELEGRAM_BOT_TOKEN
    chat_id: $TELEGRAM_CHAT_ID
    level: questions

merge:
  require_review: true
  review_label: reviewed
  auto_merge_label: auto-merge

auto_start:
  enabled: false
  label: nightshift
  poll_interval_s: 30
  max_concurrent: 1

hooks:
  after_create: |
    echo "Workspace created"
  before_run: |
    echo "Starting agent run"
  after_run: |
    echo "Agent run finished"
  timeout_s: 60

terminal_statuses:
  - closed
---

You are working on the following issue:

**Title:** {{ issue.title }}
**Description:**
{{ issue.body }}

{% if attempt %}
This is continuation attempt {{ attempt }}. Review previous work and continue.
{% endif %}

**Related previous issues:**
{{ related_context }}

RULES:
1. Work on the current branch. The repo is already checked out.
2. If you have a blocking question, include all relevant context IN the question
   itself (code snippets, file paths, what you did, options you see) — the human
   reads ONLY the question text, they cannot see your other output.
3. Commit frequently. Write tests where appropriate.

For bug fixes, follow this protocol:
1. Reproduce the bug — run the failing scenario and confirm the symptom.
2. Minimize — isolate the smallest code surface that triggers it.
3. Write a failing test that captures the exact bug.
4. Fix the code and verify the test passes.
5. Confirm the original reproduction scenario no longer fails.
6. Search for similar patterns elsewhere in the codebase and fix them too.

{% if agent_kind == "openhands" %}
## Signal Protocol
When you complete the task, write a file at /session/signal/done containing a one-line summary.
If you have a question, write /session/signal/question.json with {"question": "your question"}.
For progress checkpoints, write /session/signal/checkpoint with a description.
These signal files are REQUIRED in addition to using FinishAction.
{% endif %}
{% if agent_kind == "codex" %}
## Signal Protocol
Use MCP tools from the nightshift-signals server to signal lifecycle events:
- Call `nightshift_done` with a summary when the task is complete.
- Call `nightshift_checkpoint` with a description for progress updates.
- Call `nightshift_question` with your question if you need human input.
These MCP tools are REQUIRED. Do NOT print text markers directly.
{% endif %}

Begin by reading the codebase, then plan your approach.
