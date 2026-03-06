---
agent:
  kind: claude-code
  max_turns: 50
  stall_timeout_s: 300
  extra_args: []

tracker:
  kind: git-bug

workspace:
  kind: worktree
  base_branch: master
  root: .worktrees

notifications:
  - kind: telegram
    token: $TELEGRAM_BOT_TOKEN
    chat_id: $TELEGRAM_CHAT_ID

merge:
  require_review: true
  review_label: reviewed
  auto_merge_label: auto-merge

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
2. For every significant thought: @@LOG@@ <your thought>
3. After meaningful work: @@CHECKPOINT@@ <description>
4. If you have a blocking question:
   a. Output: @@QUESTION@@ <your question>
   b. Then output: @@WAITING@@
   c. The answer will appear as your next input.
5. When done: @@DONE@@
6. Commit frequently. Write tests where appropriate.

Begin by reading the codebase, then plan your approach.
