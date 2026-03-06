# Autonomous Coding Agent Worker — Implementation Spec

## Open Questions (Resolve Before or During Phase 1)

These are unresolved design and technical questions. Each must be answered
before the affected code is written. They are ordered by severity — blocking
questions first, then design decisions, then gaps.

### Coder Prerequisites — Verify Before Writing Code

**OQ-1: RESOLVED — `-p` mode is fire-and-forget. Use `--resume` for multi-turn.**

Tested with Claude Code 2.1.70 (2026-03-06). Results:

- `-p` mode exits (code 0) after responding. Does NOT read stdin.
- `--input-format stream-json` does not enable multi-turn with `-p`.
- `--continue -p "follow-up"` and `--resume <session_id> -p "follow-up"`
  WORK: full conversation context is preserved across invocations.
- `--output-format stream-json` requires `--verbose` with `-p`.
- The stream-json `init` event contains `session_id` for `--resume`.

Implementation: `ClaudeCodeAgent` extracts `session_id` from the init event.
Subsequent `start()` calls use `--resume <session_id>`. For Q&A:
`SessionRunner` collects the answer after the agent exits, then restarts
with the answer as the prompt. No PTY or stdin piping needed.

See `tests/oq1_results.txt` and `tests/oq1_stdin_test.py` for test details.

---

**OQ-2: CODER PREREQUISITE — What is the actual `--output-format stream-json` schema?**

The stream processor assumes events like `{"type": "assistant", "content": "..."}`.
The real field names, nesting, and event types are unverified. A schema mismatch
will cause silent data loss or parse crashes.

Test:
```bash
claude --dangerously-skip-permissions --output-format stream-json \
  -p "List 3 files in the current directory" 2>/dev/null | head -50
```

Capture the output. Update `adapters/agents/claude_code.py._parse_line()`.

**Do this second. 30 min. Needed before stream processor works.**

---

**OQ-3: PARTIALLY RESOLVED — Marker reliability mitigations are in code.**

Mitigations already implemented:
- Agent exit without `@@DONE@@`: `_post_run()` treats status `working` as
  max-turns → auto-resume with review gate. Never auto-merges without
  explicit `@@DONE@@`.
- `@@DONE@@` itself goes through review gate (OQ-6) — double safety net.
- Markers in tool results: stream processor only scans `assistant` events.

Remaining empirical risk: Claude may not output `@@CHECKPOINT@@` often
enough, making resume prompts stale. Acceptable — git diff stat always
captures actual changes. Prompt tuning after real-world testing.

---

**OQ-4: RESOLVED — Two-step with 30s timeout fallback.**

`@@QUESTION@@` + `@@WAITING@@` kept as protocol. `QUESTION_WAIT_TIMEOUT_S = 30`
in event loop auto-triggers `_on_waiting()` if `@@WAITING@@` never arrives.
See `_event_loop()` in `core/session.py`.

---

**OQ-5: RESOLVED — Auth paths verified.**

Claude Code on Linux (including Docker containers):
```
~/.claude/
  ├── .credentials.json    # OAuth tokens (primary auth file)
  ├── settings.json        # User settings
  └── settings.local.json  # Local overrides
~/.claude.json             # MCP server config (optional)
```

Docker mounts needed:
```
-v "$HOME/.claude:/root/.claude:ro"         # credentials + settings
-v "$HOME/.claude.json:/root/.claude.json:ro"  # MCP config (optional)
```

No `~/.config/claude-code` path exists — removed from `launch.py`.
macOS uses Keychain, but Linux containers always use `.credentials.json`.
Alternative: set `ANTHROPIC_API_KEY` env var instead of mounting.

CAUTION: macOS Claude Code may delete `.credentials.json` (known bug
anthropics/claude-code#1414). If sharing `~/.claude` between macOS host
and Linux container via volume mount, copy `.credentials.json` to a
separate location and mount that instead.

---

### Design Decisions — Resolved

**OQ-6: RESOLVED — Review gate by default.**

`_on_done()` posts proof-of-work and adds `needs-review` label. Merge only
happens after human adds `reviewed` label. Auto-merge is opt-in via
`auto-merge` label on the issue. See `_on_done()`, `_request_review()`,
`_wait_for_review()`, `_do_merge()` in `core/session.py`.

---

**OQ-7: RESOLVED — Queue multiple questions.**

`_pending_questions` is a `list[str]`. Each `@@QUESTION@@` appends. Each
`@@WAITING@@` pops the oldest. See `_on_question()` and `_on_waiting()` in
`core/session.py`.

---

**OQ-8: RESOLVED — Host watcher is fully decoupled from tracker.**

`host/watcher.py` has zero tracker imports. It communicates with containers
ONLY via files (`waiting.json` / `answer.txt`) and Docker pause/unpause.
Telegram polling is self-contained in the watcher. Same watcher works with
any tracker adapter.

Answer sources while container is paused:
1. Telegram reply (watcher polls, writes `answer.txt`)
2. CLI: `agent-worker answer <id> "text"` writes `answer.txt` directly

Tracker-based answers (git-bug comment, GitHub comment) are collected by the
container itself before being paused. If the answer arrives after pause via
tracker only (no Telegram, no CLI), the container stays paused until the user
uses CLI or Telegram. This is documented behavior.

---

**OQ-9: RESOLVED — Worktree merge runs from repo_root.**

`finalize()` commits in the worktree, then runs `git merge --no-ff` from
`repo_root` (the main working tree), not the worktree. Ensures the target
branch is checked out in the main tree first. See
`adapters/workspaces/git_worktree.py`.

---

**OQ-10: RESOLVED — Error handling: best-effort for non-critical, raise for critical.**

Policy:
- **Critical (must succeed, raise on failure):** `get_issue()`,
  `create()` workspace, `agent.start()`, `agent.send_input()`
- **Best-effort (log and swallow):** `add_comment()`, `add_label()`,
  `remove_label()`, `set_status()`, `sync()`, `notify()`

Implementation: adapters log and swallow in best-effort methods (already
the case in `GitBugTracker._run()`). Core `SessionRunner` wraps critical
calls only. Notifier methods already catch `RequestException`.

The `IssueTracker` Protocol docstring should document which methods are
critical vs best-effort so future adapter authors know.

---

### Gaps — Address During Development

**OQ-11: RESOLVED — Test structure defined.**

```
tests/
  ├── conftest.py           # Protocol mocks (MockAgent, MockTracker, etc.)
  ├── test_session.py       # SessionRunner against mocks: markers, Q&A, stall, review
  ├── test_state.py         # Atomic writes, checkpoint/QA, signal files
  ├── test_stream.py        # Stream processor: marker parsing, event routing
  ├── test_prompts.py       # Prompt construction, resume prompt building
  ├── test_search.py        # Keyword extraction, scoring, context truncation
  ├── adapters/
  │   ├── test_claude_code.py   # Real Claude Code output parsing (recorded fixtures)
  │   ├── test_git_bug.py       # Git-bug CLI wrapper (mock subprocess)
  │   └── test_git_worktree.py  # Worktree create/commit/finalize (temp git repos)
  └── integration/
      └── test_end_to_end.py    # Full flow with real adapters (skip in CI without creds)
```

Start by writing `conftest.py` with protocol mocks. Test `SessionRunner` in
isolation before touching real adapters.

**OQ-12: RESOLVED — launch.py and cli.py read WORKFLOW.md.**

`launch.py` reads `WORKFLOW.md` from the repo root to determine workspace
kind, Docker image, and environment. `cli.py` delegates to `launch.py`.
Both use `core/config.py` to parse the file. See those modules below.

**OQ-13: RESOLVED — WORKFLOW.md parsing implemented.**

`core/config.py` parses WORKFLOW.md (YAML front matter + prompt body),
validates required fields, and provides typed access. `entrypoint.py`
uses it to instantiate the correct adapters. `launch.py` uses it on
the host side for workspace and Docker config. See `core/config.py`,
updated `entrypoint.py`, and `host/launch.py` below.

**OQ-14: RESOLVED — Summarization is already conditional.**

`_maybe_summarize_checkpoints()` only fires when `len(checkpoints) > 10`.
Cost is one short Claude call (~500 tokens). Acceptable for sessions that
have done 10+ checkpoints. If the summarization call fails (timeout, error),
it's caught and the raw checkpoints are used — no data loss.

---

## Phase 1 Checklist

1. Resolve OQ-1 and OQ-2 (terminal experiments, 30 min each).
2. Implement `core/protocols.py` and `core/state.py` (pure data, no deps).
3. Implement `core/config.py` (WORKFLOW.md parser).
4. Write protocol mocks and test `SessionRunner` event loop in isolation.
5. Implement `adapters/agents/claude_code.py` against real stream format.
6. Implement `adapters/trackers/git_bug.py`.
7. Implement `adapters/workspaces/git_worktree.py`.
8. Wire in `entrypoint.py` (config-driven adapter instantiation).
9. Add Telegram notifier, host watcher, pause/unpause.
10. Implement `host/launch.py` (reads WORKFLOW.md, creates workspace, runs Docker).
11. Implement `host/cli.py` (delegates to launch.py, reads WORKFLOW.md).

---

# Spec Begins Here

## Overview

A pluggable system for running coding agents against issue tracker tasks, with
thought logging, human-in-the-loop Q&A, session serialization, and container
pause/unpause for idle waiting.

The core is agent-agnostic and tracker-agnostic. Claude Code and git-bug are
provided as reference adapters.

## Architecture

```
core/                              ← Agent/tracker-agnostic
  ├── protocols.py                 ← Protocol (interface) definitions
  ├── config.py                    ← WORKFLOW.md parser + typed config
  ├── session.py                   ← Session runner (PTY, pause, stall detection)
  ├── stream.py                    ← Output stream processor
  ├── state.py                     ← Atomic session state
  ├── prompts.py                   ← Prompt construction
  ├── search.py                    ← Issue search (keyword-scored)
  ├── orchestrator.py              ← (future) central daemon with concurrency

adapters/
  ├── agents/
  │   ├── claude_code.py           ← Claude Code adapter
  │   └── codex.py                 ← (future) OpenAI Codex adapter
  ├── trackers/
  │   ├── git_bug.py               ← git-bug adapter
  │   ├── github_issues.py         ← (future) GitHub Issues adapter
  │   └── linear.py                ← (future) Linear adapter
  ├── notifiers/
  │   ├── telegram.py              ← Telegram with force_reply round-trip
  │   ├── slack.py                 ← (future) Slack adapter
  │   └── webhook.py               ← Generic webhook
  └── workspaces/
      ├── git_worktree.py          ← Git worktree workspace
      └── directory.py             ← (future) Plain directory copy

host/
  ├── watcher.py                   ← Pause/unpause containers, collect answers
  ├── launch.py                    ← Reads WORKFLOW.md, creates workspace, runs Docker
  └── cli.py                       ← CLI wrapper (delegates to launch.py)

WORKFLOW.md                        ← Repo-owned config: adapter selection, prompt, hooks
Dockerfile
requirements.txt
entrypoint.py                      ← Container: reads WORKFLOW.md, instantiates adapters
```

**Package structure**: each directory (`core/`, `adapters/`, `adapters/agents/`, etc.)
needs an `__init__.py` (can be empty) for Python imports to work.

### Design Principles

- **Protocol-first**: Every external boundary (agent, tracker, notifier, workspace) is a Python `Protocol`. Core code never imports concrete adapters.
- **Pure Python**: No shell scripts. PTY via `pty` module. Git via `subprocess` in typed adapters. No `unbuffer`, no `bash -lc`.
- **Live continuation**: Questions answered via PTY stdin — no restart, no context loss.
- **Container pause**: Host watcher freezes idle containers (zero CPU), collects answers externally, unfreezes.
- **Serialization as fallback**: Checkpoint/resume exists only for hard restarts (max-turns, context limit, stalls). Questions don't need it.

### Two Kinds of Interruption

| Interruption | Process alive? | How handled |
|---|---|---|
| **Question** | Yes — agent waiting on stdin | Write answer via PTY. Same thread, full context. |
| **Review gate** | No — agent exited after @@DONE@@ | Container paused. Waits for `reviewed` label or CLI approval. |
| **Max turns / context limit / stall** | No — must restart | Serialize state. Build resume prompt. New process. |

### Container Pause/Unpause Flow

Works for both questions and review gates. The host watcher doesn't know
which — it just sees `waiting.json` and waits for an answer.

```
Container (Docker)                     Host watcher
──────────────────                     ────────────
Agent outputs @@QUESTION@@             
Agent outputs @@WAITING@@              
  ─── OR ───                           
Agent outputs @@DONE@@                 
Post proof-of-work, wait for review    

Write /session/waiting.json            
Enter poll loop (sleep 1s)             
  │                             ──→  detect waiting.json
  │                                  docker pause <container>
  │ ◄── FROZEN ───────────────       poll Telegram for reply
  │                                  ... hours pass, zero CPU ...
  │                                  answer arrives (Telegram or CLI)
  │                                  write /session/answer.txt
  │ ◄── UNFROZEN ─────────────       docker unpause <container>
  │
  poll finds answer.txt
  For questions: pipe to agent stdin
  For reviews: proceed to merge
```

### Marker Protocol

| Marker | Purpose | Effect |
|---|---|---|
| `@@LOG@@ <thought>` | Log a decision | Tracker comment, conversation log |
| `@@CHECKPOINT@@ <desc>` | Save progress | Commit, update state, build resume prompt |
| `@@QUESTION@@ <question>` | Needs human input | Post to tracker, send via notifier |
| `@@WAITING@@` | Idle, waiting for stdin | Signal host watcher, enter poll loop |
| `@@DONE@@` | Work complete | Post proof-of-work, wait for review (or auto-merge if labeled) |

---

## WORKFLOW.md — Repository-Owned Configuration

Every project gets a `WORKFLOW.md` in its root. This file defines which adapters
to use, runtime settings, hooks, and the prompt template. Change the file to
change the system's behavior — no code edits, no redeployment.

### Example WORKFLOW.md

```yaml
---
agent:
  kind: claude-code            # or: codex, aider
  max_turns: 50
  stall_timeout_s: 300
  # kind-specific options passed to adapter constructor
  extra_args: []

tracker:
  kind: git-bug                # or: github, linear
  # kind-specific options
  # github: {repo: "owner/repo", token: "$GITHUB_TOKEN"}
  # linear: {project_slug: "my-project", api_key: "$LINEAR_API_KEY"}

workspace:
  kind: worktree               # or: directory
  base_branch: master

notifications:
  - kind: telegram
    token: $TELEGRAM_BOT_TOKEN
    chat_id: $TELEGRAM_CHAT_ID
  - kind: webhook
    url: $SLACK_WEBHOOK

merge:
  require_review: true         # false = auto-merge on @@DONE@@
  review_label: reviewed       # label that triggers merge
  auto_merge_label: auto-merge # issues with this label skip review

hooks:
  after_create: |
    cargo build 2>/dev/null || true
  before_run: |
    cargo check
  after_run: |
    cargo test --no-fail-fast 2>&1 | tail -20 > /session/test-results.txt

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
```

### Schema

```yaml
agent:
  kind: string           # required: "claude-code", "codex", "aider"
  max_turns: int          # default: 50
  stall_timeout_s: int    # default: 300
  extra_args: list[str]   # default: []
  # Additional keys passed as kwargs to the adapter constructor

tracker:
  kind: string           # required: "git-bug", "github", "linear"
  # Additional keys are kind-specific, passed to adapter constructor
  # Values starting with $ are resolved from environment variables

workspace:
  kind: string           # default: "worktree" (or: "directory")
  base_branch: string    # default: "master"
  root: string           # default: ".worktrees" (relative to repo root)

notifications:           # list of notifier configs (all optional)
  - kind: string         # "telegram", "webhook", "slack"
    # kind-specific keys

merge:
  require_review: bool   # default: true
  review_label: string   # default: "reviewed"
  auto_merge_label: string  # default: "auto-merge"

hooks:
  after_create: string   # shell script, runs once on new workspace
  before_run: string     # shell script, runs before each agent session
  after_run: string      # shell script, runs after each session
  timeout_s: int         # default: 60

terminal_statuses:       # default: ["closed"]
  - string
```

### Resolution Rules

- Values starting with `$` are resolved from environment variables.
- If `$VAR` resolves to empty string, treated as missing.
- `~` in path values is expanded to home directory.
- Unknown top-level keys are ignored (forward compatibility).
- Missing `WORKFLOW.md` → all defaults (Claude Code + git-bug + worktree).
- Invalid YAML → fail startup with clear error message.
- Prompt body (after front matter) is a Jinja2-compatible template.
- Template variables: `issue` (TrackerIssue fields), `attempt` (int|None),
  `related_context` (string).

---

## Protocols

### protocols.py

```python
"""Protocol definitions for all external boundaries.

Core code imports ONLY from this module for external interactions.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum, auto
from pathlib import Path
from typing import Protocol, Optional, Iterator, Any, runtime_checkable


# ── Issue Tracker ─────────────────────────────────────────

@dataclass
class TrackerIssue:
    id: str
    identifier: str          # human-readable key
    title: str
    body: str
    status: str              # normalized: "open", "closed", etc.
    labels: list[str] = field(default_factory=list)
    url: str | None = None
    priority: int | None = None
    created_at: str | None = None
    updated_at: str | None = None


@dataclass
class TrackerComment:
    author: str
    body: str
    created_at: str | None = None


@runtime_checkable
class IssueTracker(Protocol):
    def get_issue(self, issue_id: str) -> Optional[TrackerIssue]: ...
    def list_issues(self, status: str | list[str] | None = None) -> list[TrackerIssue]: ...
    def get_comments(self, issue_id: str) -> list[TrackerComment]: ...
    def add_comment(self, issue_id: str, body: str) -> None: ...
    def set_status(self, issue_id: str, status: str) -> None: ...
    def add_label(self, issue_id: str, label: str) -> None: ...
    def remove_label(self, issue_id: str, label: str) -> None: ...
    def sync(self) -> None:
        """Bidirectional sync — pull remote state AND push local changes."""
        ...


# ── Coding Agent ──────────────────────────────────────────

class AgentEventType(Enum):
    TEXT = auto()
    TOOL_CALL = auto()
    TOOL_RESULT = auto()
    SYSTEM = auto()
    TURN_COMPLETED = auto()
    TURN_FAILED = auto()
    PROCESS_EXIT = auto()
    STALL = auto()
    UNKNOWN = auto()


@dataclass
class AgentEvent:
    type: AgentEventType
    content: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)
    raw: str = ""


@runtime_checkable
class CodingAgent(Protocol):
    """Interface for a coding agent.

    Contract: implementations must be re-startable after terminate().
    start() must reinitialize all internal state so the same instance
    can be used for multiple sequential sessions (e.g. main run +
    summarization call).
    """
    def start(self, prompt: str, workspace: Path, max_turns: int = 50) -> None: ...
    def stream_events(self) -> Iterator[AgentEvent]: ...
    def send_input(self, text: str) -> None: ...
    def is_alive(self) -> bool: ...
    def terminate(self) -> None: ...
    @property
    def pid(self) -> int | None: ...


# ── Workspace ─────────────────────────────────────────────

@dataclass
class Workspace:
    path: Path
    branch: str | None = None
    is_new: bool = False


@runtime_checkable
class WorkspaceManager(Protocol):
    def create(self, issue: TrackerIssue) -> Workspace: ...
    def cleanup(self, issue: TrackerIssue) -> None: ...
    def finalize(self, issue: TrackerIssue, target_branch: str = "master") -> None: ...
    def commit(self, workspace: Path, message: str) -> None: ...
    def has_changes(self, workspace: Path) -> bool: ...
    def diff_stat(self, workspace: Path, base: str = "master") -> str: ...
    def get_current_commit(self, workspace: Path) -> str: ...


# ── Notifications ─────────────────────────────────────────

@runtime_checkable
class Notifier(Protocol):
    def notify(self, message: str) -> None: ...
    def send_question(self, issue_id: str, question: str, short_id: str = "") -> bool: ...
    def check_answer(self, issue_id: str) -> Optional[str]: ...
    def clear_pending(self, issue_id: str) -> None: ...


# ── Markers ───────────────────────────────────────────────

class MarkerType(Enum):
    LOG = auto()
    CHECKPOINT = auto()
    QUESTION = auto()
    WAITING = auto()
    DONE = auto()


@dataclass
class Marker:
    type: MarkerType
    content: str = ""


DEFAULT_MARKERS: dict[str, MarkerType] = {
    "@@LOG@@": MarkerType.LOG,
    "@@CHECKPOINT@@": MarkerType.CHECKPOINT,
    "@@QUESTION@@": MarkerType.QUESTION,
    "@@WAITING@@": MarkerType.WAITING,
    "@@DONE@@": MarkerType.DONE,
}


def parse_marker(
    text: str, markers: dict[str, MarkerType] | None = None,
) -> Marker | None:
    for token, mt in (markers or DEFAULT_MARKERS).items():
        if token in text:
            return Marker(type=mt, content=text.split(token, 1)[1].strip())
    return None
```

---

## Adapters

### adapters/agents/claude_code.py

```python
"""Claude Code adapter — PTY-based, pure Python."""

import json
import logging
import os
import pty
import select
import subprocess
import time
from pathlib import Path
from typing import Iterator, Optional

from core.protocols import CodingAgent, AgentEvent, AgentEventType

log = logging.getLogger(__name__)

READ_TIMEOUT_S = 10.0
STALL_TIMEOUT_S = 300.0


class ClaudeCodeAgent:
    def __init__(
        self,
        command: str = "claude",
        stall_timeout_s: float = STALL_TIMEOUT_S,
        extra_args: list[str] | None = None,
    ):
        self.command = command
        self.stall_timeout_s = stall_timeout_s
        self.extra_args = extra_args or []
        self._pid: int | None = None
        self._master_fd: int | None = None
        self._process: subprocess.Popen | None = None
        self._last_event: float = 0

    def start(self, prompt: str, workspace: Path, max_turns: int = 50) -> None:
        master_fd, slave_fd = pty.openpty()
        self._master_fd = master_fd
        self._process = subprocess.Popen(
            [self.command, "--dangerously-skip-permissions",
             "--output-format", "stream-json",
             "--max-turns", str(max_turns),
             *self.extra_args, "-p", prompt],
            stdin=slave_fd, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, text=True,
            cwd=str(workspace), bufsize=1,
        )
        os.close(slave_fd)
        self._pid = self._process.pid
        self._last_event = time.monotonic()

    def stream_events(self) -> Iterator[AgentEvent]:
        if not self._process:
            return
        stdout = self._process.stdout
        while True:
            if self._process.poll() is not None:
                for line in stdout:
                    ev = self._parse(line.rstrip("\n"))
                    if ev: yield ev
                yield AgentEvent(type=AgentEventType.PROCESS_EXIT)
                return

            ready, _, _ = select.select([stdout], [], [], READ_TIMEOUT_S)
            if ready:
                line = stdout.readline()
                if not line:
                    yield AgentEvent(type=AgentEventType.PROCESS_EXIT); return
                self._last_event = time.monotonic()
                ev = self._parse(line.rstrip("\n"))
                if ev: yield ev
            else:
                elapsed = time.monotonic() - self._last_event
                if self.stall_timeout_s > 0 and elapsed > self.stall_timeout_s:
                    yield AgentEvent(type=AgentEventType.STALL,
                                     content=f"No output for {elapsed:.0f}s")
                    return

    def send_input(self, text: str) -> None:
        if self._master_fd is None:
            raise RuntimeError("No live process")
        os.write(self._master_fd, (text + "\n").encode())

    def is_alive(self) -> bool:
        return self._process is not None and self._process.poll() is None

    def terminate(self) -> None:
        if self._process and self._process.poll() is None:
            self._process.terminate()
            try: self._process.wait(timeout=10)
            except Exception: self._process.kill(); self._process.wait()
        if self._master_fd is not None:
            try: os.close(self._master_fd)
            except OSError: pass
            self._master_fd = None
        self._process = None; self._pid = None

    @property
    def pid(self) -> int | None:
        return self._pid

    def _parse(self, raw: str) -> Optional[AgentEvent]:
        if not raw.strip(): return None
        try: ev = json.loads(raw)
        except json.JSONDecodeError: return None
        t = ev.get("type", "")
        # OQ-2: These field names are assumed. Verify against real output.
        if t == "assistant":
            return AgentEvent(type=AgentEventType.TEXT, content=ev.get("content",""), raw=raw)
        elif t == "tool_use":
            return AgentEvent(type=AgentEventType.TOOL_CALL,
                              content=f"{ev.get('tool','?')}: {str(ev.get('input',''))[:300]}", raw=raw)
        elif t == "tool_result":
            return AgentEvent(type=AgentEventType.TOOL_RESULT,
                              content=str(ev.get("content",""))[:200], raw=raw)
        elif t == "system":
            return AgentEvent(type=AgentEventType.SYSTEM, content=ev.get("message",""), raw=raw)
        return AgentEvent(type=AgentEventType.UNKNOWN, raw=raw)
```

### adapters/agents/codex.py (sketch)

```python
"""OpenAI Codex app-server adapter (sketch).

Uses JSON-RPC over stdio: initialize → thread/start → turn/start → stream.
See: https://developers.openai.com/codex/app-server/
"""

from pathlib import Path
from typing import Iterator
from core.protocols import CodingAgent, AgentEvent


class CodexAgent:
    def start(self, prompt: str, workspace: Path, max_turns: int = 50) -> None:
        raise NotImplementedError("Codex adapter not yet implemented")

    def stream_events(self) -> Iterator[AgentEvent]:
        raise NotImplementedError

    def send_input(self, text: str) -> None:
        # Send continuation turn/start on the existing thread
        raise NotImplementedError

    def is_alive(self) -> bool:
        raise NotImplementedError

    def terminate(self) -> None:
        raise NotImplementedError

    @property
    def pid(self) -> int | None:
        return None
```

---

### adapters/trackers/git_bug.py

```python
"""git-bug adapter."""

import json
import logging
import subprocess
from pathlib import Path
from typing import Optional

from core.protocols import IssueTracker, TrackerIssue, TrackerComment

log = logging.getLogger(__name__)


class GitBugTracker:
    def __init__(self, repo_dir: str | Path = "/workspace"):
        self.cwd = str(repo_dir)

    def _run(self, *args: str, timeout: int = 30) -> str:
        try:
            r = subprocess.run(
                ["git-bug", *args], cwd=self.cwd,
                capture_output=True, text=True, timeout=timeout,
            )
            return r.stdout.strip()
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as e:
            log.warning(f"git-bug {args[0]} failed: {e}")
            return ""

    def get_issue(self, issue_id: str) -> Optional[TrackerIssue]:
        raw = self._run("show", issue_id, "--format", "json")
        if not raw: return None
        try:
            d = json.loads(raw)
            comments = d.get("comments", [])
            return TrackerIssue(
                id=issue_id, identifier=issue_id[:12],
                title=d.get("title", "Unknown"),
                body=comments[0].get("message", "") if comments else "",
                status=d.get("status", "unknown"),
                labels=[l.lower() for l in d.get("labels", [])],
                created_at=d.get("created_at"),
            )
        except json.JSONDecodeError:
            return None

    def list_issues(self, status=None) -> list[TrackerIssue]:
        args = ["ls", "--format", "json"]
        if isinstance(status, str):
            args.extend(["--status", status])
        raw = self._run(*args)
        if not raw: return []
        try:
            return [i for item in json.loads(raw)
                    if (i := self.get_issue(item.get("id", ""))) is not None]
        except json.JSONDecodeError:
            return []

    def get_comments(self, issue_id: str) -> list[TrackerComment]:
        raw = self._run("show", issue_id, "--format", "json")
        if not raw: return []
        try:
            return [
                TrackerComment(
                    author=c.get("author", {}).get("name", "unknown"),
                    body=c.get("message", ""), created_at=c.get("timestamp"),
                )
                for c in json.loads(raw).get("comments", [])
            ]
        except json.JSONDecodeError:
            return []

    def add_comment(self, issue_id: str, body: str) -> None:
        self._run("comment", "add", issue_id, "-m", body)

    def set_status(self, issue_id: str, status: str) -> None:
        cmd = "close" if status == "closed" else "open"
        self._run("status", cmd, issue_id)

    def add_label(self, issue_id: str, label: str) -> None:
        self._run("label", "add", issue_id, label)

    def remove_label(self, issue_id: str, label: str) -> None:
        self._run("label", "remove", issue_id, label)

    def sync(self) -> None:
        self._run("pull")
        self._run("push")
```

### adapters/trackers/github_issues.py (sketch)

```python
"""GitHub Issues adapter (sketch). Would use requests or PyGithub."""

from typing import Optional
from core.protocols import IssueTracker, TrackerIssue, TrackerComment


class GitHubIssuesTracker:
    def __init__(self, repo: str, token: str):
        # repo = "owner/repo", token = GitHub PAT
        self.repo = repo
        self.token = token

    def get_issue(self, issue_id: str) -> Optional[TrackerIssue]:
        # GET /repos/{owner}/{repo}/issues/{number}
        raise NotImplementedError

    def add_comment(self, issue_id: str, body: str) -> None:
        # POST /repos/{owner}/{repo}/issues/{number}/comments
        raise NotImplementedError

    # etc.
```

---

### adapters/workspaces/git_worktree.py

```python
"""Git worktree workspace manager."""

import logging
import re
import subprocess
from pathlib import Path
from typing import Optional

from core.protocols import WorkspaceManager, Workspace, TrackerIssue

log = logging.getLogger(__name__)


class GitWorktreeManager:
    def __init__(self, repo_root: Path, worktree_root: Path | None = None,
                 base_branch: str = "master"):
        self.repo_root = Path(repo_root)
        self.worktree_root = Path(worktree_root) if worktree_root else self.repo_root / ".worktrees"
        self.base_branch = base_branch

    def create(self, issue: TrackerIssue) -> Workspace:
        sid = self._sanitize(issue.identifier)
        branch = f"agent/{sid}"
        wt = self.worktree_root / f"agent-{sid}"
        is_new = not wt.exists()
        if is_new:
            self._git("branch", branch, self.base_branch)
            self._git("worktree", "add", str(wt), branch)
        return Workspace(path=wt, branch=branch, is_new=is_new)

    def cleanup(self, issue: TrackerIssue) -> None:
        sid = self._sanitize(issue.identifier)
        wt = self.worktree_root / f"agent-{sid}"
        if wt.exists():
            self._git("worktree", "remove", str(wt), "--force")
        self._git("branch", "-D", f"agent/{sid}")

    def finalize(self, issue: TrackerIssue, target_branch: str = "master") -> None:
        """Merge the agent branch into target.

        IMPORTANT: The merge runs from repo_root (the main working tree),
        NOT from the worktree. You cannot checkout a branch in a worktree
        if it's already checked out elsewhere. The main working tree should
        have the target branch checked out.
        """
        sid = self._sanitize(issue.identifier)
        branch = f"agent/{sid}"
        wt = self.worktree_root / f"agent-{sid}"

        # Commit any remaining changes in the worktree
        self.commit(wt, f"fix: resolve {sid} — {issue.title}")

        # Merge from repo_root (main working tree)
        # Ensure we're on the target branch in the main tree
        current = self._git("branch", "--show-current")
        if current != target_branch:
            self._git("checkout", target_branch)

        self._git(
            "merge", "--no-ff", branch, "-m",
            f"Merge {branch}: {issue.title}\n\nResolved {issue.id}",
        )

    def commit(self, workspace: Path, message: str) -> None:
        self._git_in(workspace, "add", "-A")
        if self.has_changes(workspace):
            self._git_in(workspace, "commit", "-m", message)

    def has_changes(self, workspace: Path) -> bool:
        return subprocess.run(
            ["git", "diff", "--cached", "--quiet"], cwd=str(workspace),
        ).returncode != 0

    def diff_stat(self, workspace: Path, base: str = "master") -> str:
        try:
            return subprocess.check_output(
                ["git", "diff", base, "--stat"],
                cwd=str(workspace), stderr=subprocess.DEVNULL,
            ).decode().strip() or "No changes"
        except subprocess.CalledProcessError:
            return "No changes"

    def get_current_commit(self, workspace: Path) -> str:
        try:
            return subprocess.check_output(
                ["git", "rev-parse", "HEAD"],
                cwd=str(workspace), stderr=subprocess.DEVNULL,
            ).decode().strip()
        except (subprocess.CalledProcessError, FileNotFoundError):
            return "none"

    def run_hook(self, workspace: Path, script: str | None,
                 timeout_s: int = 60) -> bool:
        """Run a hook script in the workspace. Returns True on success."""
        if not script:
            return True
        try:
            subprocess.run(
                ["sh", "-c", script], cwd=str(workspace),
                timeout=timeout_s, check=True,
                capture_output=True, text=True,
            )
            return True
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as e:
            log.warning(f"Hook failed: {e}")
            return False

    def _sanitize(self, s: str) -> str:
        return re.sub(r"[^A-Za-z0-9._-]", "_", s)

    def _git(self, *args: str) -> str:
        return subprocess.run(
            ["git", *args], cwd=str(self.repo_root),
            capture_output=True, text=True,
        ).stdout.strip()

    def _git_in(self, cwd: Path, *args: str) -> str:
        return subprocess.run(
            ["git", *args], cwd=str(cwd), capture_output=True, text=True,
        ).stdout.strip()
```

---

### adapters/notifiers/telegram.py

```python
"""Telegram notifier with force_reply Q&A."""

import json
import logging
import os
import threading
import time
from typing import Optional

import requests
from core.protocols import Notifier, IssueTracker

log = logging.getLogger(__name__)


class TelegramNotifier:
    def __init__(self, tracker: IssueTracker,
                 token: str | None = None, chat_id: str | None = None):
        self.tracker = tracker
        self.token = token or os.environ.get("TELEGRAM_BOT_TOKEN", "")
        self.chat_id = chat_id or os.environ.get("TELEGRAM_CHAT_ID", "")
        self.enabled = bool(self.token and self.chat_id)
        self._pending: dict[str, dict] = {}
        self._lock = threading.Lock()
        self._offset = 0
        self._running = False

    def start(self):
        if not self.enabled: return
        self._running = True
        threading.Thread(target=self._poll, daemon=True).start()

    def stop(self):
        self._running = False

    def notify(self, message: str) -> None:
        if not self.enabled: return
        try:
            requests.post(
                f"https://api.telegram.org/bot{self.token}/sendMessage",
                json={"chat_id": self.chat_id, "text": f"🤖 {message}",
                      "parse_mode": "Markdown"}, timeout=10)
        except requests.RequestException: pass

    def send_question(self, issue_id: str, question: str, short_id: str = "") -> bool:
        if not self.enabled: return False
        try:
            resp = requests.post(
                f"https://api.telegram.org/bot{self.token}/sendMessage",
                json={
                    "chat_id": self.chat_id,
                    "text": (f"❓ *Question*\n*Issue:* `{short_id or issue_id[:12]}`\n"
                             f"*Q:* {question}\n\n_Reply to answer._"),
                    "parse_mode": "Markdown",
                    "reply_markup": {"force_reply": True, "selective": True,
                                     "input_field_placeholder": "Answer..."},
                }, timeout=10)
            d = resp.json()
            if not d.get("ok"): return False
            with self._lock:
                self._pending[issue_id] = {
                    "msg_id": d["result"]["message_id"],
                    "answer": None, "event": threading.Event(),
                }
            return True
        except requests.RequestException: return False

    def check_answer(self, issue_id: str) -> Optional[str]:
        with self._lock:
            p = self._pending.get(issue_id)
            if p and p["event"].is_set():
                self._pending.pop(issue_id, None)
                return p["answer"]
        return None

    def clear_pending(self, issue_id: str) -> None:
        with self._lock: self._pending.pop(issue_id, None)

    def _poll(self):
        while self._running:
            try:
                resp = requests.get(
                    f"https://api.telegram.org/bot{self.token}/getUpdates",
                    params={"offset": self._offset, "timeout": 2,
                            "allowed_updates": json.dumps(["message"])}, timeout=7)
                for u in resp.json().get("result", []):
                    self._offset = u["update_id"] + 1
                    self._handle(u)
            except Exception as e:
                log.warning(f"Telegram: {e}"); time.sleep(5)

    def _handle(self, u: dict):
        msg = u.get("message", {}); text = msg.get("text", "").strip()
        rt = msg.get("reply_to_message", {})
        if not text or not rt: return
        if str(msg.get("chat",{}).get("id")) != str(self.chat_id): return
        rid = rt.get("message_id")
        with self._lock:
            for iid, p in self._pending.items():
                if p["msg_id"] == rid:
                    self.tracker.add_comment(iid, f"👤 [via Telegram]: {text}")
                    p["answer"] = text; p["event"].set()
                    return
```

### adapters/notifiers/webhook.py

```python
"""Generic webhook notifier."""

import os
from typing import Optional
import requests
from core.protocols import Notifier


class WebhookNotifier:
    def __init__(self, url: str | None = None):
        self.url = url or os.environ.get("NOTIFY_WEBHOOK_URL", "")

    def notify(self, message: str) -> None:
        if self.url:
            try: requests.post(self.url, json={"text": message}, timeout=10)
            except Exception: pass

    def send_question(self, issue_id: str, question: str, short_id: str = "") -> bool:
        self.notify(f"❓ [{short_id}] {question}"); return False

    def check_answer(self, issue_id: str) -> Optional[str]:
        return None

    def clear_pending(self, issue_id: str) -> None:
        pass
```

### adapters/notifiers/composite.py — Broadcasts + Primary Q&A

```python
"""Composite notifier — broadcasts to all, Q&A through primary."""

from typing import Optional
from core.protocols import Notifier


class CompositeNotifier:
    """Wraps multiple notifiers. First with round-trip Q&A is primary."""

    def __init__(self, notifiers: list):
        self.notifiers = notifiers
        # Primary = first notifier that can do Q&A (Telegram, not webhook)
        # Webhooks return False from send_question, indicating no round-trip
        self._primary = notifiers[0] if notifiers else None

    def notify(self, message: str) -> None:
        for n in self.notifiers:
            n.notify(message)

    def send_question(self, issue_id: str, question: str, short_id: str = "") -> bool:
        # Send through primary (Telegram etc.) for Q&A
        sent = False
        if self._primary:
            sent = self._primary.send_question(issue_id, question, short_id)
        # Also broadcast to all others (one-way)
        for n in self.notifiers:
            if n is not self._primary:
                n.send_question(issue_id, question, short_id)
        return sent

    def check_answer(self, issue_id: str) -> Optional[str]:
        if self._primary:
            return self._primary.check_answer(issue_id)
        return None

    def clear_pending(self, issue_id: str) -> None:
        if self._primary:
            self._primary.clear_pending(issue_id)

    def start(self):
        for n in self.notifiers:
            if hasattr(n, "start"):
                n.start()

    def stop(self):
        for n in self.notifiers:
            if hasattr(n, "stop"):
                n.stop()
```

---

## Dockerfile and requirements.txt

### Dockerfile

```dockerfile
FROM ubuntu:24.04

RUN apt-get update && apt-get install -y \
    git curl jq nodejs npm openssh-client python3 python3-pip python3-venv \
    && rm -rf /var/lib/apt/lists/*

RUN npm install -g @anthropic-ai/claude-code

RUN curl -sL https://github.com/git-bug/git-bug/releases/latest/download/git-bug_linux_amd64 \
    -o /usr/local/bin/git-bug && chmod +x /usr/local/bin/git-bug

COPY requirements.txt /opt/agent-worker/
RUN pip install --break-system-packages -r /opt/agent-worker/requirements.txt

COPY core/ /opt/agent-worker/core/
COPY adapters/ /opt/agent-worker/adapters/
COPY entrypoint.py /opt/agent-worker/

ENV PYTHONPATH=/opt/agent-worker
WORKDIR /workspace

ENTRYPOINT ["python3", "/opt/agent-worker/entrypoint.py"]
```

### requirements.txt

```
requests>=2.31
pyyaml>=6.0
jinja2>=3.1
```

---

## Core

### core/state.py

```python
"""Atomic session state."""

import json
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional


@dataclass
class Checkpoint:
    step: int
    description: str
    timestamp: str
    commit: str


@dataclass
class QAExchange:
    question: str
    answer: str


@dataclass
class SessionState:
    issue_id: str
    branch: str
    status: str = "starting"
    step: int = 0
    started_at: str = ""
    checkpoints: list[Checkpoint] = field(default_factory=list)
    human_answers: list[QAExchange] = field(default_factory=list)

    def __post_init__(self):
        if not self.started_at:
            self.started_at = datetime.now(timezone.utc).isoformat()
        self.checkpoints = [Checkpoint(**c) if isinstance(c, dict) else c for c in self.checkpoints]
        self.human_answers = [QAExchange(**q) if isinstance(q, dict) else q for q in self.human_answers]


class StateManager:
    def __init__(self, session_dir: str | Path):
        self.session_dir = Path(session_dir)
        self.state_file = self.session_dir / "state.json"
        self.conversation_log = self.session_dir / "conversation.jsonl"
        self.raw_output_log = self.session_dir / "raw-output.log"
        self.resume_prompt_file = self.session_dir / "resume-prompt.md"
        self.waiting_signal = self.session_dir / "waiting.json"
        self.answer_file = self.session_dir / "answer.txt"
        self.session_dir.mkdir(parents=True, exist_ok=True)

    def load_state(self) -> SessionState:
        with open(self.state_file) as f: return SessionState(**json.load(f))

    def update_status(self, s: str):
        st = self.load_state(); st.status = s; self._write(st)

    def increment_step(self) -> int:
        st = self.load_state(); st.step += 1; self._write(st); return st.step

    def add_checkpoint(self, desc: str, step: int, commit: str = "none"):
        st = self.load_state()
        st.checkpoints.append(Checkpoint(
            step=step, description=desc,
            timestamp=datetime.now(timezone.utc).isoformat(), commit=commit))
        self._write(st)

    def add_qa(self, q: str, a: str):
        st = self.load_state()
        st.human_answers.append(QAExchange(question=q, answer=a))
        self._write(st)

    def append_conversation(self, role: str, content: str):
        with open(self.conversation_log, "a") as f:
            f.write(json.dumps({"role": role, "content": content,
                                "timestamp": datetime.now(timezone.utc).isoformat()}) + "\n")

    def append_raw(self, line: str):
        with open(self.raw_output_log, "a") as f: f.write(line + "\n")

    def get_recent_conversation(self, n: int = 50) -> str:
        if not self.conversation_log.exists(): return ""
        lines = self.conversation_log.read_text().strip().splitlines()
        parts = []
        for l in lines[-n:]:
            try:
                e = json.loads(l); parts.append(f"[{e['role']}]: {e['content'][:500]}")
            except Exception: continue
        return "\n".join(parts)

    def write_resume_prompt(self, c: str): self.resume_prompt_file.write_text(c)
    def read_resume_prompt(self) -> Optional[str]:
        return self.resume_prompt_file.read_text() if self.resume_prompt_file.exists() else None

    def signal_waiting(self, question: str):
        self.waiting_signal.write_text(json.dumps({
            "question": question, "issue_id": self.load_state().issue_id,
            "timestamp": datetime.now(timezone.utc).isoformat()}))

    def clear_waiting(self): self.waiting_signal.unlink(missing_ok=True)

    def check_answer(self) -> Optional[str]:
        if self.answer_file.exists():
            a = self.answer_file.read_text().strip()
            self.answer_file.unlink(); self.clear_waiting(); return a
        return None

    def _write(self, st: SessionState):
        tmp = self.state_file.with_suffix(".tmp")
        with open(tmp, "w") as f: json.dump(asdict(st), f, indent=2)
        tmp.rename(self.state_file)
```

---

### core/config.py — WORKFLOW.md Parser

Parses YAML front matter + prompt body. Resolves `$VAR` references. Provides
typed access to all configuration. Used by both `entrypoint.py` (container)
and `host/launch.py` (host).

```python
"""WORKFLOW.md parser — typed config from YAML front matter + prompt template."""

import logging
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

import yaml

log = logging.getLogger(__name__)


@dataclass
class AgentConfig:
    kind: str = "claude-code"
    max_turns: int = 50
    stall_timeout_s: int = 300
    extra_args: list[str] = field(default_factory=list)
    extra: dict[str, Any] = field(default_factory=dict)  # kind-specific kwargs


@dataclass
class TrackerConfig:
    kind: str = "git-bug"
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class WorkspaceConfig:
    kind: str = "worktree"
    base_branch: str = "master"
    root: str = ".worktrees"


@dataclass
class NotifierConfig:
    kind: str
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class MergeConfig:
    require_review: bool = True
    review_label: str = "reviewed"
    auto_merge_label: str = "auto-merge"


@dataclass
class HooksConfig:
    after_create: str | None = None
    before_run: str | None = None
    after_run: str | None = None
    timeout_s: int = 60


@dataclass
class WorkflowConfig:
    """Fully parsed, typed configuration from WORKFLOW.md."""
    agent: AgentConfig = field(default_factory=AgentConfig)
    tracker: TrackerConfig = field(default_factory=TrackerConfig)
    workspace: WorkspaceConfig = field(default_factory=WorkspaceConfig)
    notifications: list[NotifierConfig] = field(default_factory=list)
    merge: MergeConfig = field(default_factory=MergeConfig)
    hooks: HooksConfig = field(default_factory=HooksConfig)
    terminal_statuses: list[str] = field(default_factory=lambda: ["closed"])
    prompt_template: str = ""


def load_workflow(path: Path | str = "WORKFLOW.md") -> WorkflowConfig:
    """Load and parse WORKFLOW.md. Returns defaults if file doesn't exist."""
    path = Path(path)

    if not path.exists():
        log.info(f"{path} not found — using defaults")
        return WorkflowConfig()

    text = path.read_text()
    front_matter, prompt_body = _split_front_matter(text)

    if front_matter:
        try:
            raw = yaml.safe_load(front_matter)
            if not isinstance(raw, dict):
                raise ValueError(f"WORKFLOW.md front matter must be a mapping, got {type(raw)}")
        except yaml.YAMLError as e:
            raise ValueError(f"WORKFLOW.md YAML parse error: {e}")
    else:
        raw = {}

    # Resolve $VAR references in all string values
    raw = _resolve_env_vars(raw)

    config = WorkflowConfig(prompt_template=prompt_body.strip())

    # Agent
    if "agent" in raw:
        a = raw["agent"]
        known = {"kind", "max_turns", "stall_timeout_s", "extra_args"}
        config.agent = AgentConfig(
            kind=a.get("kind", "claude-code"),
            max_turns=int(a.get("max_turns", 50)),
            stall_timeout_s=int(a.get("stall_timeout_s", 300)),
            extra_args=a.get("extra_args", []),
            extra={k: v for k, v in a.items() if k not in known},
        )

    # Tracker
    if "tracker" in raw:
        t = raw["tracker"]
        config.tracker = TrackerConfig(
            kind=t.get("kind", "git-bug"),
            extra={k: v for k, v in t.items() if k != "kind"},
        )

    # Workspace
    if "workspace" in raw:
        w = raw["workspace"]
        config.workspace = WorkspaceConfig(
            kind=w.get("kind", "worktree"),
            base_branch=w.get("base_branch", "master"),
            root=w.get("root", ".worktrees"),
        )

    # Notifications
    if "notifications" in raw:
        for n in raw["notifications"]:
            config.notifications.append(NotifierConfig(
                kind=n.get("kind", "webhook"),
                extra={k: v for k, v in n.items() if k != "kind"},
            ))

    # Merge
    if "merge" in raw:
        m = raw["merge"]
        config.merge = MergeConfig(
            require_review=m.get("require_review", True),
            review_label=m.get("review_label", "reviewed"),
            auto_merge_label=m.get("auto_merge_label", "auto-merge"),
        )

    # Hooks
    if "hooks" in raw:
        h = raw["hooks"]
        config.hooks = HooksConfig(
            after_create=h.get("after_create"),
            before_run=h.get("before_run"),
            after_run=h.get("after_run"),
            timeout_s=int(h.get("timeout_s", 60)),
        )

    # Terminal statuses
    if "terminal_statuses" in raw:
        config.terminal_statuses = [str(s) for s in raw["terminal_statuses"]]

    return config


def _split_front_matter(text: str) -> tuple[str, str]:
    """Split YAML front matter from markdown body."""
    if not text.startswith("---"):
        return "", text
    parts = text.split("---", 2)
    if len(parts) < 3:
        return "", text
    return parts[1], parts[2]


def _resolve_env_vars(obj: Any) -> Any:
    """Recursively resolve $VAR references in string values."""
    if isinstance(obj, str):
        if obj.startswith("$"):
            var_name = obj[1:]
            val = os.environ.get(var_name, "")
            if not val:
                log.warning(f"Environment variable ${var_name} is empty/missing")
            return val
        return obj
    elif isinstance(obj, dict):
        return {k: _resolve_env_vars(v) for k, v in obj.items()}
    elif isinstance(obj, list):
        return [_resolve_env_vars(v) for v in obj]
    return obj


# ── Adapter Factory ──────────────────────────────────────

# Registry: kind → (module_path, class_name)
AGENT_REGISTRY: dict[str, tuple[str, str]] = {
    "claude-code": ("adapters.agents.claude_code", "ClaudeCodeAgent"),
    "codex": ("adapters.agents.codex", "CodexAgent"),
}

TRACKER_REGISTRY: dict[str, tuple[str, str]] = {
    "git-bug": ("adapters.trackers.git_bug", "GitBugTracker"),
    "github": ("adapters.trackers.github_issues", "GitHubIssuesTracker"),
    "linear": ("adapters.trackers.linear", "LinearTracker"),
}

WORKSPACE_REGISTRY: dict[str, tuple[str, str]] = {
    "worktree": ("adapters.workspaces.git_worktree", "GitWorktreeManager"),
    "directory": ("adapters.workspaces.directory", "DirectoryManager"),
}

NOTIFIER_REGISTRY: dict[str, tuple[str, str]] = {
    "telegram": ("adapters.notifiers.telegram", "TelegramNotifier"),
    "webhook": ("adapters.notifiers.webhook", "WebhookNotifier"),
    "slack": ("adapters.notifiers.slack", "SlackNotifier"),
}


def _instantiate(registry: dict, kind: str, **kwargs) -> Any:
    """Dynamically import and instantiate an adapter by kind."""
    if kind not in registry:
        available = ", ".join(registry.keys())
        raise ValueError(f"Unknown adapter kind '{kind}'. Available: {available}")
    module_path, class_name = registry[kind]
    import importlib
    mod = importlib.import_module(module_path)
    cls = getattr(mod, class_name)
    return cls(**kwargs)


def create_agent(config: WorkflowConfig) -> "CodingAgent":
    kwargs = {
        "stall_timeout_s": config.agent.stall_timeout_s,
        "extra_args": config.agent.extra_args,
        **config.agent.extra,
    }
    return _instantiate(AGENT_REGISTRY, config.agent.kind, **kwargs)


def create_tracker(config: WorkflowConfig, **overrides) -> "IssueTracker":
    kwargs = {**config.tracker.extra, **overrides}
    return _instantiate(TRACKER_REGISTRY, config.tracker.kind, **kwargs)


def create_workspace_mgr(config: WorkflowConfig, repo_root: Path) -> "WorkspaceManager":
    kwargs = {
        "repo_root": repo_root,
        "base_branch": config.workspace.base_branch,
    }
    if config.workspace.root:
        kwargs["worktree_root"] = repo_root / config.workspace.root
    return _instantiate(WORKSPACE_REGISTRY, config.workspace.kind, **kwargs)


def create_notifiers(config: WorkflowConfig, tracker=None) -> list:
    """Create all configured notifiers."""
    notifiers = []
    for nc in config.notifications:
        kwargs = {**nc.extra}
        # Telegram needs tracker reference for posting answers to issues
        if nc.kind == "telegram" and tracker:
            kwargs["tracker"] = tracker
        try:
            notifiers.append(_instantiate(NOTIFIER_REGISTRY, nc.kind, **kwargs))
        except Exception as e:
            log.warning(f"Failed to create {nc.kind} notifier: {e}")
    return notifiers
```

---

### core/session.py

```python
"""Session runner — agent-agnostic, tracker-agnostic.

Works with any adapters implementing the protocols.
"""

import logging
import time
from pathlib import Path

from core.protocols import (
    CodingAgent, IssueTracker, Notifier, WorkspaceManager,
    AgentEventType, MarkerType, parse_marker, TrackerIssue, Workspace,
)
from core.state import StateManager

log = logging.getLogger(__name__)

BOT_PREFIXES = ("💭", "🤖", "❓", "📌", "⚠️", "✅", "⏸️", "🔄", "👤", "💬", "🛑")
RECONCILE_S = 60
ANSWER_POLL_S = 1
QUESTION_WAIT_TIMEOUT_S = 30  # OQ-4: fallback if @@WAITING@@ never arrives
MAX_RESUMES = 10  # prevent infinite context-limit loops


class SessionRunner:
    def __init__(
        self, agent: CodingAgent, tracker: IssueTracker,
        notifier: Notifier, workspace_mgr: WorkspaceManager,
        state_mgr: StateManager, issue: TrackerIssue, prompt: str,
        max_turns: int = 50,
        terminal_statuses: tuple[str, ...] = ("closed",),
        merge_config: "MergeConfig | None" = None,
        hooks_config: "HooksConfig | None" = None,
    ):
        self.agent = agent
        self.tracker = tracker
        self.notifier = notifier
        self.workspace_mgr = workspace_mgr
        self.state_mgr = state_mgr
        self.issue = issue
        self.prompt = prompt
        self.max_turns = max_turns
        self.terminal_statuses = terminal_statuses
        self._workspace: Workspace | None = None
        self._pending_questions: list[str] = []  # OQ-7: queue, not overwrite
        self._question_sent_via_notifier = False

        # Merge policy from WORKFLOW.md (defaults if not provided)
        if merge_config is None:
            from core.config import MergeConfig
            merge_config = MergeConfig()
        self.merge_config = merge_config

        # Hooks from WORKFLOW.md
        self.hooks_config = hooks_config

    def run(self):
        """Main entry. Loops on auto-resume (no recursion → no stack overflow)."""
        self._workspace = self.workspace_mgr.create(self.issue)

        # Run after_create hook on new workspaces
        if self._workspace.is_new and self.hooks_config:
            self._run_hook(self.hooks_config.after_create, "after_create", fatal=True)

        prompt = self.prompt
        resume_count = 0
        while True:
            # Guard against infinite resume loops
            if resume_count >= MAX_RESUMES:
                log.error(f"Hit max resumes ({MAX_RESUMES}). Stopping.")
                self.state_mgr.update_status("suspended:max-resumes")
                self.notifier.notify(
                    f"⚠️ {self.issue.identifier} hit {MAX_RESUMES} resumes. Manual --resume needed.")
                break

        prompt = self.prompt
        while True:
            # Run before_run hook
            if self.hooks_config:
                if not self._run_hook(self.hooks_config.before_run, "before_run", fatal=True):
                    self.state_mgr.update_status("suspended:hook-failure")
                    self.notifier.notify(
                        f"⚠️ {self.issue.identifier}: before_run hook failed.")
                    break

            self.state_mgr.append_conversation("user", prompt)
            self.agent.start(prompt, self._workspace.path, self.max_turns)
            log.info(f"Agent started (pid={self.agent.pid})")
            try:
                self._event_loop()
            finally:
                self.agent.terminate()

            # Run after_run hook (best-effort)
            if self.hooks_config:
                self._run_hook(self.hooks_config.after_run, "after_run", fatal=False)

            # Check if we need to auto-resume or stop
            resume_prompt = self._post_run()
            if resume_prompt is None:
                break  # terminal state — exit the loop
            resume_count += 1
            prompt = resume_prompt  # auto-resume with new prompt

    def _run_hook(self, script: str | None, name: str, fatal: bool = False) -> bool:
        """Execute a hook script. Returns True on success."""
        if not script:
            return True
        if not self._workspace:
            return True
        log.info(f"Running {name} hook...")
        timeout = self.hooks_config.timeout_s if self.hooks_config else 60
        # Use workspace manager's hook runner if available, else subprocess
        if hasattr(self.workspace_mgr, "run_hook"):
            ok = self.workspace_mgr.run_hook(self._workspace.path, script, timeout)
        else:
            import subprocess
            try:
                subprocess.run(
                    ["sh", "-c", script], cwd=str(self._workspace.path),
                    timeout=timeout, check=True, capture_output=True,
                )
                ok = True
            except Exception as e:
                log.warning(f"{name} hook failed: {e}")
                ok = False
        if not ok and fatal:
            log.error(f"{name} hook failed (fatal)")
        return ok

    def _event_loop(self):
        last_reconcile = time.monotonic()
        question_time: float | None = None  # OQ-4: track when question was asked

        for event in self.agent.stream_events():
            self.state_mgr.append_raw(event.raw)

            if event.type == AgentEventType.TEXT:
                result = self._handle_text(event.content)
                if result == "STOP":
                    break
                if result == "QUESTION_ASKED":
                    question_time = time.monotonic()

            elif event.type == AgentEventType.TOOL_CALL:
                self.state_mgr.append_conversation("tool_call", event.content)

            elif event.type == AgentEventType.TOOL_RESULT:
                self.state_mgr.append_conversation("tool_result", event.content)

            elif event.type == AgentEventType.SYSTEM:
                if "context window" in event.content or "token limit" in event.content:
                    self._commit_wip("context limit")
                    self.state_mgr.update_status("suspended:context-limit")
                    self._build_resume()
                    break
                self.state_mgr.append_conversation("system", event.content)

            elif event.type == AgentEventType.STALL:
                log.warning(f"Stall: {event.content}")
                self._commit_wip("stalled")
                self.state_mgr.update_status("suspended:stall")
                break

            elif event.type == AgentEventType.PROCESS_EXIT:
                break

            # OQ-4: if @@QUESTION@@ was seen but @@WAITING@@ hasn't arrived
            if (question_time and self._pending_questions
                    and time.monotonic() - question_time > QUESTION_WAIT_TIMEOUT_S):
                log.warning("@@WAITING@@ not received — forcing wait")
                self._on_waiting()
                question_time = None

            # Reconciliation
            if time.monotonic() - last_reconcile > RECONCILE_S:
                last_reconcile = time.monotonic()
                if self._issue_is_terminal():
                    self.state_mgr.update_status("cancelled:external")
                    break

    def _handle_text(self, text: str) -> str | None:
        for line in text.splitlines():
            marker = parse_marker(line)
            if not marker:
                continue

            if marker.type == MarkerType.LOG:
                self.tracker.add_comment(self.issue.id, f"💭 {marker.content}")
                self.state_mgr.append_conversation("thought", marker.content)

            elif marker.type == MarkerType.CHECKPOINT:
                step = self.state_mgr.increment_step()
                commit = self._commit_checkpoint(marker.content, step)
                self.state_mgr.add_checkpoint(marker.content, step, commit)
                self._build_resume()
                self.tracker.add_comment(
                    self.issue.id, f"📌 Checkpoint {step}: {marker.content}")

            elif marker.type == MarkerType.QUESTION:
                self._on_question(marker.content)
                return "QUESTION_ASKED"

            elif marker.type == MarkerType.WAITING:
                self._on_waiting()

            elif marker.type == MarkerType.DONE:
                self._on_done()
                return "STOP"

        return None

    def _on_question(self, question: str):
        step = self.state_mgr.load_state().step
        self.state_mgr.update_status("waiting:question")
        self.tracker.add_comment(
            self.issue.id, f"❓ **Question (step {step}):** {question}")
        self.tracker.add_label(self.issue.id, "needs-human-input")
        self.state_mgr.append_conversation("question", question)

        self._question_sent_via_notifier = self.notifier.send_question(
            self.issue.id, question, self.issue.identifier)
        if not self._question_sent_via_notifier:
            self.notifier.notify(f"❓ [{self.issue.identifier}]: {question}")

        self._pending_questions.append(question)  # OQ-7: queue

    def _on_waiting(self):
        if not self._pending_questions:
            log.warning("@@WAITING@@ without pending question — ignoring")
            return

        question = self._pending_questions.pop(0)  # OQ-7: pop oldest
        self.state_mgr.signal_waiting(question)
        log.info("Waiting for answer. Container may be paused.")

        answer = self._collect_answer()

        self.state_mgr.clear_waiting()
        self.state_mgr.add_qa(question, answer)
        self.tracker.remove_label(self.issue.id, "needs-human-input")
        self.tracker.add_comment(self.issue.id, f"💬 Answer: {answer[:200]}")

        # Check agent is still alive before piping to stdin.
        # If it died while we were waiting, the answer is saved in state
        # and will be available via resume prompt on next run.
        if not self.agent.is_alive():
            log.warning("Agent died while waiting for answer. Answer saved in state.")
            return

        self.agent.send_input(answer)
        self.state_mgr.update_status("working")
        log.info("Answer sent to agent stdin. Same thread continues.")

    def _on_done(self):
        """Mark as done. Review/merge happens in _post_run after agent exits."""
        self._commit_wip(f"resolve {self.issue.identifier}")
        self.state_mgr.update_status("done:pending-review")

    def _request_review(self, state):
        """Post proof-of-work and wait for review."""
        mc = self.merge_config
        self.state_mgr.update_status("waiting:review")
        diff = self.workspace_mgr.diff_stat(self._workspace.path) if self._workspace else "N/A"
        proof = (
            f"🏁 **Work complete — awaiting review**\n\n"
            f"**Checkpoints:** {len(state.checkpoints)}\n"
            f"**Q&A exchanges:** {len(state.human_answers)}\n"
            f"**Changes:**\n```\n{diff}\n```\n\n"
            f"To merge: add label `{mc.review_label}`, "
            f"reply in Telegram, or run `agent-worker answer <id> approve`.\n"
            f"To reject: close the issue."
        )
        self.tracker.add_comment(self.issue.id, proof)
        self.tracker.add_label(self.issue.id, "needs-review")
        self.tracker.remove_label(self.issue.id, "agent-in-progress")
        self.notifier.notify(
            f"🏁 {self.issue.identifier} done. Reply to approve or close issue to reject."
        )
        self.state_mgr.signal_waiting(f"review:{self.issue.id}")
        self._wait_for_review()

    def _wait_for_review(self):
        """Poll tracker until review label appears or issue is closed.

        Approval sources:
        - answer.txt: ANY non-empty content = approval (rejecting = close issue)
        - Tracker: `review_label` label added to issue
        - Tracker: issue closed = rejection
        """
        mc = self.merge_config
        while True:
            # Source 1: answer.txt — any content = approval
            if a := self.state_mgr.check_answer():
                if a.strip():  # any non-empty answer = approved
                    break

            # Source 2: tracker labels/status
            self.tracker.sync()
            issue = self.tracker.get_issue(self.issue.id)
            if not issue:
                break

            if issue.status in self.terminal_statuses:
                self.state_mgr.update_status("cancelled:review-rejected")
                self.tracker.add_comment(self.issue.id, "🛑 Closed without merge.")
                self.notifier.notify(f"🛑 {self.issue.identifier} closed without merge.")
                return

            if mc.review_label in issue.labels:
                break

            time.sleep(ANSWER_POLL_S)

        self.state_mgr.clear_waiting()
        state = self.state_mgr.load_state()
        self._do_merge(state)

    def _do_merge(self, state):
        """Merge and close — after review or with auto-merge."""
        self.state_mgr.update_status("completed")
        self.workspace_mgr.finalize(self.issue)
        self.tracker.set_status(self.issue.id, "closed")
        self.tracker.add_comment(
            self.issue.id,
            f"✅ Merged ({len(state.checkpoints)} checkpoints, "
            f"{len(state.human_answers)} Q&A).")
        self.tracker.remove_label(self.issue.id, "agent-in-progress")
        self.tracker.remove_label(self.issue.id, "needs-review")
        self.tracker.sync()
        self.notifier.notify(f"✅ {self.issue.identifier} merged.")

    def _collect_answer(self) -> str:
        last_count = len(self.tracker.get_comments(self.issue.id))
        gb_counter = 0
        while True:
            # Source 1: answer.txt from host watcher
            if a := self.state_mgr.check_answer():
                self.notifier.clear_pending(self.issue.id); return a
            # Source 2: notifier (Telegram)
            if a := self.notifier.check_answer(self.issue.id):
                return a
            # Source 3: tracker comments
            gb_counter += 1
            if gb_counter >= 30:
                gb_counter = 0; self.tracker.sync()
                comments = self.tracker.get_comments(self.issue.id)
                if len(comments) > last_count:
                    latest = comments[-1].body
                    if not any(latest.startswith(p) for p in BOT_PREFIXES):
                        self.notifier.clear_pending(self.issue.id); return latest
                    last_count = len(comments)
            time.sleep(ANSWER_POLL_S)  # container may be paused here

    def _post_run(self) -> str | None:
        """Returns resume prompt if auto-resuming, None if terminal.

        Called after agent is terminated — safe to do blocking I/O.
        """
        st = self.state_mgr.load_state()

        if st.status == "done:pending-review":
            # Agent completed. Now handle review gate (agent is terminated,
            # so blocking here is safe — no stdout buffer deadlock).
            state = st
            mc = self.merge_config
            skip_review = (
                not mc.require_review
                or mc.auto_merge_label in self.issue.labels
            )
            if skip_review:
                self._do_merge(state)
            else:
                self._request_review(state)
            return None

        if st.status in ("completed", "cancelled:review-rejected"):
            return None
        if st.status == "cancelled:external":
            self.tracker.add_comment(self.issue.id, "🛑 Stopped: closed externally.")
            self.notifier.notify(f"🛑 {self.issue.identifier} stopped.")
            return None
        if st.status in ("suspended:context-limit", "suspended:stall"):
            return self._prepare_resume(st.status.split(":")[1])
        if st.status == "working":
            self._commit_wip("max-turns")
            return self._prepare_resume("max-turns")
        # Unexpected
        self._commit_wip("unexpected exit")
        self.state_mgr.update_status("suspended:unexpected")
        self._build_resume()
        self.notifier.notify(f"⚠️ {self.issue.identifier} ended unexpectedly.")
        return None  # unexpected = manual --resume needed

    def _prepare_resume(self, reason: str) -> str:
        """Build resume prompt and notify. Returns the prompt for the loop."""
        self._build_resume()
        self._maybe_summarize_checkpoints()
        self.tracker.add_comment(self.issue.id, f"🔄 {reason} — auto-resuming...")
        self.notifier.notify(f"{reason} for {self.issue.identifier}. Resuming.")
        self.state_mgr.update_status("working")
        return self.state_mgr.read_resume_prompt()

    def _maybe_summarize_checkpoints(self):
        """Compress checkpoint history when it gets long (>10 entries).

        Uses a one-shot agent call to summarize. Requires the agent binary
        to be available in the container. Non-critical — if it fails,
        raw checkpoints are used.
        """
        state = self.state_mgr.load_state()
        if len(state.checkpoints) <= 10:
            return
        cp_text = "\n".join(
            f"Step {c.step}: {c.description}" for c in state.checkpoints
        )
        try:
            self.agent.start(
                prompt=f"Summarize these checkpoints to 5 key decisions. "
                       f"Output only the summary:\n\n{cp_text}",
                workspace=self._workspace.path if self._workspace else Path("/tmp"),
                max_turns=1,
            )
            summary_parts = []
            for event in self.agent.stream_events():
                if event.type == AgentEventType.TEXT:
                    summary_parts.append(event.content)
                elif event.type in (AgentEventType.PROCESS_EXIT, AgentEventType.STALL):
                    break
            self.agent.terminate()

            summary = " ".join(summary_parts).strip()
            if summary:
                self._build_resume(checkpoint_summary=summary)
        except Exception:
            pass  # non-critical — raw checkpoints are fine

    def _issue_is_terminal(self) -> bool:
        self.tracker.sync()
        issue = self.tracker.get_issue(self.issue.id)
        return issue is not None and issue.status in self.terminal_statuses

    def _commit_wip(self, reason: str):
        if self._workspace:
            step = self.state_mgr.load_state().step
            self.workspace_mgr.commit(
                self._workspace.path,
                f"wip: {reason} at step {step} [{self.issue.identifier}]")

    def _commit_checkpoint(self, desc: str, step: int) -> str:
        if self._workspace:
            self.workspace_mgr.commit(
                self._workspace.path,
                f"checkpoint({step}): {desc[:60]} [{self.issue.identifier}]")
            return self.workspace_mgr.get_current_commit(self._workspace.path)
        return "none"

    def _build_resume(self, checkpoint_summary: str | None = None):
        from core.prompts import build_resume_prompt
        build_resume_prompt(
            self.issue.title, self.issue.body, "", self.state_mgr,
            checkpoint_summary=checkpoint_summary,
            diff_fn=lambda: self.workspace_mgr.diff_stat(
                self._workspace.path) if self._workspace else "N/A")
```

---

*Note: `core/prompts.py` is defined in the updated version below (includes
`render_template()` for WORKFLOW.md Jinja2 templates).*

---

### core/search.py

```python
"""Search related issues — tracker-agnostic."""

import re
from collections import Counter
from core.protocols import TrackerIssue, IssueTracker

STOP_WORDS = frozenset({
    "this","that","with","from","have","been","will","would","should","could",
    "about","their","there","which","other","than","then","when","what","into",
    "more","some","very","just","also","only","does","done","each","like",
    "make","made","need","work","used","using","want",
})

def search_related_issues(
    target: TrackerIssue, all_issues: list[TrackerIssue],
    tracker: IssueTracker, min_score: int = 3, max_chars: int = 3000,
) -> str:
    words = re.findall(r"\b[a-z]{4,}\b", f"{target.title} {target.body}".lower())
    keywords = [w for w, _ in Counter(
        w for w in words if w not in STOP_WORDS).most_common(15)]
    if not keywords: return ""
    scored = []
    for issue in all_issues:
        if issue.id == target.id: continue
        comments = tracker.get_comments(issue.id)
        text = (issue.title + " " + " ".join(c.body for c in comments)).lower()
        score = sum(text.count(k) for k in keywords)
        if score >= min_score:
            res = comments[-1].body[:800] if comments else "No resolution"
            scored.append((score, issue, res))
    scored.sort(key=lambda x: x[0], reverse=True)
    parts, total = [], 0
    for score, issue, res in scored:
        e = f"### [{issue.status}] {issue.title} (relevance: {score})\n**Resolution:** {res}\n---"
        if total + len(e) > max_chars: break
        parts.append(e); total += len(e)
    return "\n".join(parts)
```

---

## Host Watcher (Runs on Host, Tracker-Agnostic)

### host/watcher.py

The watcher is completely decoupled from both the tracker and the agent. It only
interacts via:
- **Files**: reads `waiting.json`, writes `answer.txt` (shared volume)
- **Docker**: `docker pause` / `docker unpause`
- **Telegram**: polls for replies to forwarded questions

The watcher never calls `git-bug`, `git`, or any tracker CLI. The container
handles all tracker interaction. This means:
- Same watcher works with git-bug, GitHub Issues, Linear, or any future tracker.
- The watcher has zero knowledge of what's inside the container.

```python
#!/usr/bin/env python3
"""Host watcher — pauses idle containers, collects answers via Telegram + file.

Tracker-agnostic: communicates with containers ONLY via files in the shared
session directory (waiting.json / answer.txt). Optionally polls Telegram
for replies. Never imports or calls any tracker.

    python host/watcher.py --sessions-dir .agent-worker/sessions
"""

import argparse
import json
import logging
import os
import subprocess
import time
from pathlib import Path
from typing import Optional

logging.basicConfig(level=logging.INFO, format="%(asctime)s [watcher] %(message)s")
log = logging.getLogger("watcher")

try:
    import requests
    HAS_REQUESTS = True
except ImportError:
    HAS_REQUESTS = False


class HostWatcher:
    """Monitors session dirs, pauses containers, polls Telegram, writes answers.

    Zero tracker coupling. The contract is:
    - Container writes /session/waiting.json when it needs an answer.
    - Watcher writes /session/answer.txt when it has one.
    - Container reads answer.txt and continues.

    Answer sources (checked in order):
    1. Telegram reply (if configured)
    2. Manual: user runs `cli.py answer <id> "text"` which writes answer.txt directly
    """

    def __init__(self, sessions_dir: Path):
        self.sessions_dir = sessions_dir

        # Telegram config (optional)
        self.tg_token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
        self.tg_chat = os.environ.get("TELEGRAM_CHAT_ID", "")
        self.tg_enabled = HAS_REQUESTS and bool(self.tg_token and self.tg_chat)
        self._tg_offset = 0

        # Track paused sessions: session_id -> metadata
        self._paused: dict[str, dict] = {}

    def run(self):
        log.info(f"Watching {self.sessions_dir}")
        if self.tg_enabled:
            log.info("Telegram polling enabled")
        else:
            log.info("Telegram not configured — answers via CLI only")

        while True:
            self._scan_for_waiting()
            self._check_for_answers()
            time.sleep(2)

    def _scan_for_waiting(self):
        """Detect new waiting.json files → pause those containers."""
        if not self.sessions_dir.exists():
            return

        for session_dir in self.sessions_dir.iterdir():
            if not session_dir.is_dir():
                continue
            sid = session_dir.name
            waiting_file = session_dir / "waiting.json"

            if waiting_file.exists() and sid not in self._paused:
                try:
                    data = json.loads(waiting_file.read_text())
                except (json.JSONDecodeError, OSError):
                    continue

                container = f"agent-worker-{sid}"

                # Brief delay to let container finish writing state
                time.sleep(1)

                if self._docker_pause(container):
                    self._paused[sid] = {
                        "question": data.get("question", ""),
                        "issue_id": data.get("issue_id", ""),
                        "container": container,
                        "dir": session_dir,
                        "paused_at": time.time(),
                        "tg_msg_id": None,
                    }
                    log.info(f"[{sid}] Paused. Question: {data.get('question', '')[:60]}")

                    # Forward question to Telegram if not already sent by container
                    if self.tg_enabled and data.get("question"):
                        msg_id = self._tg_send_question(
                            sid, data["question"], data.get("issue_id", "")[:12]
                        )
                        self._paused[sid]["tg_msg_id"] = msg_id
                else:
                    log.warning(f"[{sid}] Pause failed — container will poll internally")

    def _check_for_answers(self):
        """Check Telegram for replies, write answer.txt, unpause."""
        tg_replies = self._poll_telegram() if self.tg_enabled else {}

        for sid, info in list(self._paused.items()):
            answer_file = info["dir"] / "answer.txt"

            # Check if someone wrote answer.txt directly (via CLI)
            if answer_file.exists():
                log.info(f"[{sid}] answer.txt found (via CLI). Unpausing.")
                self._docker_unpause(info["container"])
                del self._paused[sid]
                continue

            # Check Telegram replies
            if sid in tg_replies:
                answer = tg_replies[sid]
                log.info(f"[{sid}] Telegram reply: {answer[:60]}")
                answer_file.write_text(answer)
                self._docker_unpause(info["container"])
                log.info(f"[{sid}] Unpaused.")
                del self._paused[sid]
                continue

            # Log periodic status
            elapsed = time.time() - info["paused_at"]
            if int(elapsed) % 300 == 0 and int(elapsed) > 0:
                log.info(f"[{sid}] Still waiting ({elapsed/60:.0f}m)")

    # --- Docker ---

    def _docker_pause(self, container: str) -> bool:
        return subprocess.run(
            ["docker", "pause", container], capture_output=True,
        ).returncode == 0

    def _docker_unpause(self, container: str) -> bool:
        return subprocess.run(
            ["docker", "unpause", container], capture_output=True,
        ).returncode == 0

    # --- Telegram (self-contained, no tracker imports) ---

    def _tg_send_question(self, sid: str, question: str, short_id: str) -> Optional[int]:
        """Send question to Telegram with force_reply. Returns message_id."""
        try:
            resp = requests.post(
                f"https://api.telegram.org/bot{self.tg_token}/sendMessage",
                json={
                    "chat_id": self.tg_chat,
                    "text": (
                        f"❓ *Question*\n"
                        f"*Issue:* `{short_id}`\n"
                        f"*Q:* {question}\n\n"
                        f"_Reply to answer._"
                    ),
                    "parse_mode": "Markdown",
                    "reply_markup": {
                        "force_reply": True, "selective": True,
                        "input_field_placeholder": "Answer...",
                    },
                }, timeout=10,
            )
            d = resp.json()
            return d["result"]["message_id"] if d.get("ok") else None
        except Exception as e:
            log.warning(f"Telegram send failed: {e}")
            return None

    def _poll_telegram(self) -> dict[str, str]:
        """Fetch Telegram updates, match replies to paused sessions."""
        replies: dict[str, str] = {}
        try:
            resp = requests.get(
                f"https://api.telegram.org/bot{self.tg_token}/getUpdates",
                params={"offset": self._tg_offset, "timeout": 1}, timeout=5,
            )
            for u in resp.json().get("result", []):
                self._tg_offset = u["update_id"] + 1
                msg = u.get("message", {})
                text = msg.get("text", "").strip()
                rt = msg.get("reply_to_message", {})

                if not text or not rt:
                    continue
                if str(msg.get("chat", {}).get("id")) != str(self.tg_chat):
                    continue

                reply_msg_id = rt.get("message_id")

                # Match by Telegram message_id
                for sid, info in self._paused.items():
                    if info.get("tg_msg_id") == reply_msg_id:
                        replies[sid] = text
                        self._tg_ack(msg.get("message_id"), sid)
                        break

        except Exception as e:
            log.warning(f"Telegram poll: {e}")

        return replies

    def _tg_ack(self, reply_to: int, sid: str):
        try:
            requests.post(
                f"https://api.telegram.org/bot{self.tg_token}/sendMessage",
                json={
                    "chat_id": self.tg_chat,
                    "text": f"✅ Answer received for `{sid}`. Unpausing.",
                    "parse_mode": "Markdown",
                    "reply_to_message_id": reply_to,
                }, timeout=10,
            )
        except Exception:
            pass


def main():
    p = argparse.ArgumentParser(description="Host watcher — pause/unpause containers")
    p.add_argument("--sessions-dir", required=True, help=".agent-worker/sessions path")
    a = p.parse_args()
    HostWatcher(Path(a.sessions_dir)).run()

if __name__ == "__main__":
    main()
```

---

## Entrypoint (Config-Driven)

No hardcoded adapter imports. Reads `WORKFLOW.md`, dynamically instantiates
the adapters specified in config.

```python
#!/usr/bin/env python3
"""Container entrypoint — reads WORKFLOW.md, instantiates adapters, runs session.

No hardcoded adapter imports. Adapter selection is driven by WORKFLOW.md.
"""

import logging
import os
import sys
from pathlib import Path

from core.config import (
    load_workflow, create_agent, create_tracker,
    create_workspace_mgr, create_notifiers,
)
from core.state import StateManager
from core.session import SessionRunner
from core.search import search_related_issues

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s")
log = logging.getLogger("entrypoint")


def main():
    issue_id = os.environ["ISSUE_ID"]
    resume = os.environ.get("RESUME") == "--resume"

    # Load config from WORKFLOW.md (mounted into container at /workspace)
    workflow_path = Path("/workspace/WORKFLOW.md")
    config = load_workflow(workflow_path)

    # Override max_turns from env if set (CLI takes precedence over WORKFLOW.md)
    max_turns = int(os.environ.get("MAX_TURNS", config.agent.max_turns))

    # Instantiate adapters from config
    tracker = create_tracker(config, repo_dir="/workspace")
    agent = create_agent(config)
    workspace_mgr = create_workspace_mgr(config, repo_root=Path("/workspace"))
    state_mgr = StateManager("/session")

    # Notifiers (may include Telegram, webhook, etc.)
    notifiers = create_notifiers(config, tracker=tracker)

    # Wrap in composite: broadcasts to all, Q&A through primary
    from adapters.notifiers.composite import CompositeNotifier
    notifier = CompositeNotifier(notifiers)
    notifier.start()

    # Load issue
    issue = tracker.get_issue(issue_id)
    if not issue:
        log.error(f"Issue {issue_id} not found")
        sys.exit(1)

    # Search related issues
    related = search_related_issues(issue, tracker.list_issues(), tracker)

    # Build or load prompt
    if resume and (p := state_mgr.read_resume_prompt()):
        tracker.add_comment(issue_id, f"🤖 Resuming from step {state_mgr.load_state().step}...")
        prompt = p
    else:
        tracker.add_label(issue_id, "agent-in-progress")
        tracker.add_comment(issue_id, f"🤖 Starting on {issue.identifier}")
        state_mgr.update_status("working")

        if config.prompt_template:
            from core.prompts import render_template
            prompt = render_template(
                config.prompt_template, issue=issue,
                related_context=related, attempt=None,
            )
        else:
            from core.prompts import build_initial_prompt
            prompt = build_initial_prompt(issue.title, issue.body, related)

    runner = SessionRunner(
        agent=agent, tracker=tracker, notifier=notifier,
        workspace_mgr=workspace_mgr, state_mgr=state_mgr,
        issue=issue, prompt=prompt, max_turns=max_turns,
        terminal_statuses=tuple(config.terminal_statuses),
        merge_config=config.merge,
        hooks_config=config.hooks,
    )

    try:
        runner.run()
    finally:
        notifier.stop()


if __name__ == "__main__":
    main()
```

---

## Host Scripts (Config-Driven)

### host/launch.py

Reads `WORKFLOW.md` from the repo root. Uses config for workspace creation,
Docker image name, and environment. No hardcoded adapter references.

```python
#!/usr/bin/env python3
"""Host-side launcher — reads WORKFLOW.md, creates workspace, runs Docker."""

import argparse
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

# host/launch.py runs on the host, so it adds the project root to sys.path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from core.config import load_workflow


def get_repo_root() -> Path:
    return Path(subprocess.run(
        ["git", "rev-parse", "--show-toplevel"],
        capture_output=True, text=True, check=True,
    ).stdout.strip())


def main():
    parser = argparse.ArgumentParser(description="Launch agent worker")
    parser.add_argument("issue_id")
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--max-turns", type=int, default=None)
    parser.add_argument("--workflow", default=None, help="Path to WORKFLOW.md")
    parser.add_argument("--image", default="agent-worker:latest", help="Docker image")
    args = parser.parse_args()

    repo = get_repo_root()
    config = load_workflow(args.workflow or repo / "WORKFLOW.md")

    issue_id = args.issue_id
    short_id = issue_id[:12]
    max_turns = args.max_turns or config.agent.max_turns

    # Session dir (always under repo)
    session_dir = repo / ".agent-worker" / "sessions" / short_id
    branch = f"agent/{short_id}"

    # Create workspace based on config
    if config.workspace.kind == "worktree":
        wt_root = repo / config.workspace.root
        wt_path = wt_root / f"agent-{short_id}"

        if not args.resume:
            session_dir.mkdir(parents=True, exist_ok=True)
            subprocess.run(["git", "branch", branch, config.workspace.base_branch],
                           capture_output=True, cwd=str(repo))
            subprocess.run(["git", "worktree", "add", str(wt_path), branch],
                           capture_output=True, cwd=str(repo))

            (session_dir / "state.json").write_text(json.dumps({
                "issue_id": issue_id, "branch": branch,
                "status": "starting", "step": 0,
                "started_at": datetime.now(timezone.utc).isoformat(),
                "checkpoints": [], "human_answers": [],
            }, indent=2))
            print(f"Created worktree at {wt_path}")
        else:
            if not (session_dir / "state.json").exists():
                print(f"No session state at {session_dir}", file=sys.stderr)
                sys.exit(1)
            print(f"Resuming session for {short_id}")

        workspace_mount = str(wt_path)
    else:
        # directory mode — just use a subdirectory
        workspace_mount = str(repo / config.workspace.root / f"agent-{short_id}")
        if not args.resume:
            session_dir.mkdir(parents=True, exist_ok=True)
            Path(workspace_mount).mkdir(parents=True, exist_ok=True)
            (session_dir / "state.json").write_text(json.dumps({
                "issue_id": issue_id, "branch": "",
                "status": "starting", "step": 0,
                "started_at": datetime.now(timezone.utc).isoformat(),
                "checkpoints": [], "human_answers": [],
            }, indent=2))

    # Auth mounts (OQ-5: verified paths)
    home = Path.home()
    auth_mounts = []
    if (home / ".claude").is_dir():
        auth_mounts += ["-v", f"{home / '.claude'}:/root/.claude:ro"]
    if (home / ".claude.json").exists():
        auth_mounts += ["-v", f"{home / '.claude.json'}:/root/.claude.json:ro"]

    # Build env vars for notifications (pass through from host)
    notify_env = []
    for var in ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID",
                "NOTIFY_WEBHOOK_URL", "SLACK_WEBHOOK",
                "ANTHROPIC_API_KEY", "LINEAR_API_KEY", "GITHUB_TOKEN"):
        val = os.environ.get(var, "")
        if val:
            notify_env += ["-e", f"{var}={val}"]

    docker_cmd = [
        "docker", "run", "--rm", "-it",
        "--name", f"agent-worker-{short_id}",
        "-v", f"{workspace_mount}:/workspace:rw",
        "-v", f"{session_dir}:/session:rw",
        "-v", f"{repo / '.git'}:/repo-git:ro",
        # Mount WORKFLOW.md so container can read it
        "-v", f"{repo / 'WORKFLOW.md'}:/workspace/WORKFLOW.md:ro",
        *auth_mounts,
        "-v", f"{home / '.gitconfig'}:/root/.gitconfig:ro",
        "-e", f"ISSUE_ID={issue_id}",
        "-e", f"RESUME={'--resume' if args.resume else ''}",
        "-e", f"MAX_TURNS={max_turns}",
        *notify_env,
        args.image,
    ]

    # SSH agent forwarding (if available)
    ssh_sock = os.environ.get("SSH_AUTH_SOCK", "")
    if ssh_sock:
        docker_cmd.insert(-1, "-v")
        docker_cmd.insert(-1, f"{ssh_sock}:/ssh-agent")
        docker_cmd.insert(-1, "-e")
        docker_cmd.insert(-1, "SSH_AUTH_SOCK=/ssh-agent")

    print(f"Launching container agent-worker-{short_id}...")
    os.execvp("docker", docker_cmd)


if __name__ == "__main__":
    main()
```

### host/cli.py

```python
#!/usr/bin/env python3
"""CLI — reads WORKFLOW.md for config, delegates to launch.py and watcher."""

import argparse
import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from core.config import load_workflow


def repo_root() -> Path:
    return Path(subprocess.run(
        ["git", "rev-parse", "--show-toplevel"],
        capture_output=True, text=True, check=True,
    ).stdout.strip())


def sessions_dir() -> Path:
    return repo_root() / ".agent-worker" / "sessions"


def cmd_start(a):
    cmd = [sys.executable, str(Path(__file__).parent / "launch.py"), a.issue_id]
    if a.max_turns:
        cmd += ["--max-turns", str(a.max_turns)]
    if a.workflow:
        cmd += ["--workflow", a.workflow]
    subprocess.run(cmd)


def cmd_resume(a):
    cmd = [sys.executable, str(Path(__file__).parent / "launch.py"),
           a.issue_id, "--resume"]
    if a.workflow:
        cmd += ["--workflow", a.workflow]
    subprocess.run(cmd)


def cmd_answer(a):
    """Write answer.txt directly — works even if container is paused."""
    sid = a.issue_id[:12]
    sd = sessions_dir() / sid
    if sd.exists():
        (sd / "answer.txt").write_text(a.message)
        print(f"Answer written for {sid}")
    else:
        print(f"No session found for {sid}", file=sys.stderr)


def cmd_watcher(a):
    subprocess.run([
        sys.executable, str(Path(__file__).parent / "watcher.py"),
        "--sessions-dir", str(sessions_dir()),
    ])


def cmd_status(a):
    sd = sessions_dir()
    if not sd.exists():
        print("No sessions."); return
    print(f"{'SESSION':<14} {'STATUS':<26} {'STEP':>5} {'CPS':>4}")
    print("-" * 54)
    for f in sorted(sd.glob("*/state.json")):
        sid = f.parent.name
        try:
            s = json.loads(f.read_text())
            print(f"{sid:<14} {s.get('status','?'):<26} "
                  f"{s.get('step',0):>5} {len(s.get('checkpoints',[])):>4}")
        except Exception:
            print(f"{sid:<14} {'<error>':<26}")


def cmd_logs(a):
    log_file = sessions_dir() / a.issue_id[:12] / "raw-output.log"
    if not log_file.exists():
        print("No log file.", file=sys.stderr); return
    subprocess.run(["tail", "-f", str(log_file)])


def cmd_history(a):
    cf = sessions_dir() / a.issue_id[:12] / "conversation.jsonl"
    if not cf.exists():
        print("No history.", file=sys.stderr); return
    icons = {"thought":"💭","checkpoint":"📌","question":"❓","human_answer_sent":"👤",
             "tool_call":"🔧","tool_result":"📄","system":"⚙️","user":"📝"}
    for line in cf.read_text().strip().splitlines():
        try:
            e = json.loads(line)
            print(f"  {e['timestamp'][:19]}  {icons.get(e['role'],'•')} "
                  f"[{e['role']}] {e['content'][:120]}")
        except Exception:
            continue


def cmd_cleanup(a):
    r = repo_root()
    sid = a.issue_id[:12]

    # Read config to know workspace kind
    config = load_workflow(r / "WORKFLOW.md")

    if config.workspace.kind == "worktree":
        wt = r / config.workspace.root / f"agent-{sid}"
        if wt.exists():
            subprocess.run(["git", "worktree", "remove", str(wt), "--force"])
        subprocess.run(["git", "branch", "-D", f"agent/{sid}"], capture_output=True)
    else:
        import shutil
        ws = r / config.workspace.root / f"agent-{sid}"
        if ws.exists():
            shutil.rmtree(ws)

    ss = sessions_dir() / sid
    if ss.exists() and not a.keep_session:
        import shutil
        shutil.rmtree(ss)
    print(f"Cleaned up {sid}")


def main():
    p = argparse.ArgumentParser(prog="agent-worker")
    p.add_argument("--workflow", default=None, help="Path to WORKFLOW.md")
    s = p.add_subparsers(dest="cmd", required=True)

    sp = s.add_parser("start")
    sp.add_argument("issue_id")
    sp.add_argument("--max-turns", type=int, default=None)
    sp.set_defaults(func=cmd_start)

    sp = s.add_parser("resume")
    sp.add_argument("issue_id")
    sp.set_defaults(func=cmd_resume)

    sp = s.add_parser("answer")
    sp.add_argument("issue_id")
    sp.add_argument("message")
    sp.set_defaults(func=cmd_answer)

    sp = s.add_parser("watcher")
    sp.set_defaults(func=cmd_watcher)

    sp = s.add_parser("status")
    sp.set_defaults(func=cmd_status)

    sp = s.add_parser("logs")
    sp.add_argument("issue_id")
    sp.set_defaults(func=cmd_logs)

    sp = s.add_parser("history")
    sp.add_argument("issue_id")
    sp.set_defaults(func=cmd_history)

    sp = s.add_parser("cleanup")
    sp.add_argument("issue_id")
    sp.add_argument("--keep-session", action="store_true")
    sp.set_defaults(func=cmd_cleanup)

    a = p.parse_args()
    a.func(a)


if __name__ == "__main__":
    main()
```

---

### core/prompts.py (updated — add template rendering)

Add `render_template()` for WORKFLOW.md prompt templates alongside the existing
hardcoded `build_initial_prompt()` as fallback.

```python
"""Prompt construction — supports WORKFLOW.md Jinja2 templates and fallback."""

import re
from typing import Callable
from core.state import StateManager
from core.protocols import TrackerIssue


def render_template(
    template: str,
    issue: TrackerIssue,
    related_context: str = "",
    attempt: int | None = None,
) -> str:
    """Render a WORKFLOW.md prompt template with issue context.

    Supports simple {{ var }} and {% if %} / {% endif %} syntax.
    For full Jinja2 support, install jinja2 and switch to that engine.
    """
    try:
        import jinja2
        env = jinja2.Environment(undefined=jinja2.StrictUndefined)
        tmpl = env.from_string(template)
        return tmpl.render(
            issue=issue, related_context=related_context, attempt=attempt,
        )
    except ImportError:
        # Fallback: simple {{ var }} replacement (no conditionals)
        result = template
        result = result.replace("{{ issue.title }}", issue.title)
        result = result.replace("{{ issue.body }}", issue.body)
        result = result.replace("{{ issue.identifier }}", issue.identifier)
        result = result.replace("{{ related_context }}", related_context)
        result = result.replace("{{ attempt }}", str(attempt) if attempt else "")
        # Strip unresolved {% %} blocks
        result = re.sub(r"\{%.*?%\}", "", result, flags=re.DOTALL)
        return result.strip()


def build_initial_prompt(
    issue_title: str, issue_body: str, related_context: str,
    markers: dict[str, str] | None = None,
) -> str:
    """Fallback prompt when WORKFLOW.md has no prompt template."""
    m = markers or {"log": "@@LOG@@", "checkpoint": "@@CHECKPOINT@@",
                    "question": "@@QUESTION@@", "waiting": "@@WAITING@@", "done": "@@DONE@@"}
    return f"""You are working on the following issue:

**Title:** {issue_title}
**Description:**
{issue_body}

**Related previous issues:**
{related_context}

RULES:
1. Work on the current branch. The repo is already checked out.
2. For every significant thought: {m['log']} <your thought>
3. After meaningful work: {m['checkpoint']} <description>
4. If you have a blocking question:
   a. Output: {m['question']} <your question>
   b. Then output: {m['waiting']}
   c. The answer will appear as your next input.
5. When done: {m['done']}
6. Commit frequently. Write tests where appropriate.

Begin by reading the codebase, then plan your approach."""


def build_resume_prompt(
    issue_title: str, issue_body: str, related_context: str,
    state_mgr: StateManager, checkpoint_summary: str | None = None,
    diff_fn: Callable[[], str] | None = None,
) -> str:
    state = state_mgr.load_state()
    cp = checkpoint_summary or ("\n".join(
        f"- Step {c.step}: {c.description} [{c.commit[:8]}]"
        for c in state.checkpoints) or "None")
    qa = "\n".join(f"Q: {q.question}\nA: {q.answer}"
                   for q in state.human_answers) or "None"
    diff = diff_fn() if diff_fn else "N/A"
    recent = state_mgr.get_recent_conversation(50)

    prompt = f"""Resuming work. Session was interrupted.

## Issue
**Title:** {issue_title}
**Description:** {issue_body}

## Work Done
{cp}

## Changes vs base
```
{diff}
```

## Q&A
{qa}

## Recent
{recent}

## Instructions
Continue. Same marker rules apply."""
    state_mgr.write_resume_prompt(prompt)
    return prompt
```

---

## Adapter Compatibility Matrix

| Component | Provided | Planned |
|---|---|---|
| **Agent** | Claude Code (PTY, stream-json) | Codex (JSON-RPC), Aider, custom |
| **Tracker** | git-bug (CLI) | GitHub Issues, Linear, Jira, plain files |
| **Notifier** | Telegram (force_reply), Webhook | Slack, Discord, ntfy.sh, email |
| **Workspace** | Git worktree | Plain directory, Docker volume, SSH |

### Writing a New Adapter

Implement the Protocol. No registration — structural typing.

```python
# Example: GitHub Issues
from core.protocols import IssueTracker, TrackerIssue

class GitHubTracker:
    def get_issue(self, issue_id: str) -> TrackerIssue | None:
        # GET /repos/{owner}/{repo}/issues/{number}
        ...
    def add_comment(self, issue_id: str, body: str) -> None:
        # POST .../comments
        ...
    # ... rest of protocol
```

Then in `entrypoint.py`: swap one import, one constructor call. Everything else unchanged.
