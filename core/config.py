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
