---
template_version: 4

agent:
  kind: claude-code
  max_turns: 50
  stall_timeout_s: 300
  signal_method: file
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
  profile: codex

overflow_profiles:
  codex-gpt54:
    agent_kind: codex
    env:
      CODEX_MODEL: gpt-5.4
  openhands-qwen:
    agent_kind: openhands
    env:
      LLM_MODEL: openrouter/qwen/qwen3.6-plus
      LLM_API_KEY: $OPENROUTER_API_KEY
      LLM_BASE_URL: https://openrouter.ai/api/v1
  opencode-gpt54mini:
    agent_kind: opencode
    extra_args: ["-m", "openai/gpt-5.4-mini"]
    env:
      OPENAI_API_KEY: $OPENAI_API_KEY

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
2. Before starting work, check if your branch is behind the base branch:
   `git log --oneline HEAD..master | head -5`
   If commits are shown, rebase first: `git fetch origin && git rebase origin/master`
   Resolve any conflicts before proceeding.
3. If you have a blocking question, include all relevant context IN the question
   itself (code snippets, file paths, what you did, options you see) — the human
   reads ONLY the question text, they cannot see your other output.
4. Commit frequently. Write tests where appropriate.

For bug fixes, follow this protocol:
1. Reproduce the bug — run the failing scenario and confirm the symptom.
2. Minimize — isolate the smallest code surface that triggers it.
3. Write a failing test that captures the exact bug.
4. Fix the code and verify the test passes.
5. Confirm the original reproduction scenario no longer fails.
6. Search for similar patterns elsewhere in the codebase and fix them too.

## Feedback Logging

When you receive a `@nightshift revise` with reviewer feedback, verify each claim:
1. Read the file and line mentioned
2. Determine if the reviewer's claim is accurate

Append your assessment to `.nightshift/reviewer-issues.yaml`:
```yaml
- category: <false_positive|partial|valid>
  session: {{ session_id }}
  date: {{ date }}
  file: <path>
  claimed: <what reviewer said>
  actual: <what the code actually does>
  verdict: <agree|false_positive|partial>
  reason: <one-line explanation>
```
This logs patterns in reviewer feedback quality for later analysis.

Begin by reading the codebase, then plan your approach.
