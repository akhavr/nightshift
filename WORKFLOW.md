---
template_version: 3

agent:
  kind: claude-code
  max_turns: 50
  stall_timeout_s: 300
  extra_args: []

tracker:
  kind: git-bug-graphql

workspace:
  kind: worktree
  base_branch: master
  root: .worktrees
  test_command: ".venv/bin/python -m pytest tests/ -v"
  test_timeout_s: 300

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
    python3 -m venv /workspace/.venv 2>/dev/null || true
    /workspace/.venv/bin/pip install -r /workspace/requirements.txt pytest --quiet 2>/dev/null || true
    echo "Starting agent run"
  after_run: |
    echo "Agent run finished"
  timeout_s: 900

terminal_statuses:
  - closed

overflow:
  agent_kind: codex
  extra_args: []
  env:
    CODEX_MODEL: gpt-5.4-mini

overflow_profiles:
  openhands-qwen:
    agent_kind: openhands
    env:
      LLM_MODEL: openrouter/qwen/qwen3.6-plus
      LLM_API_KEY: $OPENROUTER_API_KEY
      LLM_BASE_URL: https://openrouter.ai/api/v1

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

Begin by reading the codebase, then plan your approach.
