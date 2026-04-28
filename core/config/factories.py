"""Adapter registries and dynamic factory functions."""

import importlib
import logging
from pathlib import Path
from typing import Any

from core.config.models import OverflowConfig, WorkflowConfig

log = logging.getLogger(__name__)

# ── Adapter Registries ──────────────────────────────────────
# kind → (module_path, class_name)

AGENT_REGISTRY: dict[str, tuple[str, str]] = {
    "claude-code": ("adapters.agents.claude_code", "ClaudeCodeAgent"),
    "openhands": ("adapters.agents.openhands", "OpenHandsAgent"),
    "codex": ("adapters.agents.codex", "CodexAgent"),
    "opencode": ("adapters.agents.opencode", "OpenCodeAgent"),
}

TRACKER_REGISTRY: dict[str, tuple[str, str]] = {
    "git-bug": ("adapters.trackers.git_bug", "GitBugTracker"),
    "git-bug-graphql": ("adapters.trackers.git_bug_graphql", "GitBugGraphQLTracker"),
    "github": ("adapters.trackers.github_issues", "GitHubIssuesTracker"),
    "static": ("adapters.trackers.static", "StaticTracker"),
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
    mod = importlib.import_module(module_path)
    cls = getattr(mod, class_name)
    return cls(**kwargs)


def create_agent(
    config: WorkflowConfig,
    overflow: OverflowConfig | None = None,
    signal_method: str | None = None,
) -> Any:
    """Create an agent instance.
    
    Args:
        config: The workflow configuration
        overflow: Optional overflow config. If provided and overflow.agent_kind is set,
                  use that instead of config.agent.kind (for overflow mode).
    """
    # Use overflow agent_kind if in overflow mode and set
    agent_kind = config.agent.kind
    if overflow and overflow.agent_kind:
        agent_kind = overflow.agent_kind

    if signal_method is None:
        signal_method = (
            overflow.signal_method
            if overflow and overflow.signal_method
            else config.agent.signal_method
        )

    kwargs = {
        "stall_timeout_s": config.agent.stall_timeout_s,
        "extra_args": config.agent.extra_args,
        **config.agent.extra,
        "signal_method": signal_method,
    }
    return _instantiate(AGENT_REGISTRY, agent_kind, **kwargs)


def create_tracker(config: WorkflowConfig, **overrides) -> Any:
    kwargs = {**config.tracker.extra}
    if config.tracker.sync:
        kwargs["sync"] = True
    kwargs.update(overrides)
    return _instantiate(TRACKER_REGISTRY, config.tracker.kind, **kwargs)


def create_workspace_mgr(config: WorkflowConfig, repo_root: Path) -> Any:
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
        kwargs = {**nc.extra, "level": nc.level}
        if nc.kind == "telegram" and tracker:
            kwargs["tracker"] = tracker
        try:
            notifiers.append(_instantiate(NOTIFIER_REGISTRY, nc.kind, **kwargs))
        except Exception as e:
            log.warning(f"Failed to create {nc.kind} notifier: {e}")
    return notifiers
