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
    level: questions

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

overflow:
  agent_kind: openhands
  extra_args: []
  env:
    # OpenHands uses LLM_* env vars (litellm under the hood)
    LLM_API_KEY: $OVERFLOW_API_KEY
    LLM_MODEL: $OVERFLOW_MODEL
    LLM_BASE_URL: $OVERFLOW_BASE_URL
    # Claude Code uses ANTHROPIC_* env vars
    ANTHROPIC_BASE_URL: $OVERFLOW_BASE_URL
    ANTHROPIC_AUTH_TOKEN: $OVERFLOW_API_KEY
    ANTHROPIC_API_KEY: $OVERFLOW_API_KEY
    ANTHROPIC_MODEL: $OVERFLOW_MODEL
    ANTHROPIC_SMALL_FAST_MODEL: $OVERFLOW_MODEL
    ANTHROPIC_DEFAULT_SONNET_MODEL: $OVERFLOW_MODEL
    ANTHROPIC_DEFAULT_OPUS_MODEL: $OVERFLOW_MODEL
    ANTHROPIC_DEFAULT_HAIKU_MODEL: $OVERFLOW_MODEL
    API_TIMEOUT_MS: "3000000"
    CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC: "1"

auto_start:
  enabled: true
  label: nightshift
  poll_interval_s: 30
  max_concurrent: 1
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
   a. Include all relevant context IN the question itself (code snippets,
      file paths, what you did, options you see) — the human reads ONLY
      the question text, they cannot see your other output.
   b. Output: @@QUESTION@@ <your self-contained question>
   c. Then output: @@WAITING@@
   d. The answer will appear as your next input.
5. When done: @@DONE@@
6. Commit frequently. Write tests where appropriate.

For bug fixes, follow this protocol:
1. Reproduce the bug — run the failing scenario and confirm the symptom.
2. Minimize — isolate the smallest code surface that triggers it.
3. Write a failing test that captures the exact bug.
4. Fix the code and verify the test passes.
5. Confirm the original reproduction scenario no longer fails.
6. Search for similar patterns elsewhere in the codebase and fix them too.

Begin by reading the codebase, then plan your approach.
