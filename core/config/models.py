"""Typed dataclass models for WORKFLOW.md configuration."""

from dataclasses import dataclass, field
from typing import Any


@dataclass
class AgentConfig:
    kind: str = "claude-code"
    max_turns: int = 50
    stall_timeout_s: int = 300
    extra_args: list[str] = field(default_factory=list)
    signal_method: str = "auto"  # "auto", "mcp", "text", "file"
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class TrackerConfig:
    kind: str = "git-bug"
    sync: bool = False
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class WorkspaceConfig:
    kind: str = "worktree"
    base_branch: str = "master"
    root: str = ".worktrees"
    test_command: str | None = None  # shell command to verify code after rebase
    test_timeout_s: int = 120  # timeout for test command execution


@dataclass
class NotifierConfig:
    kind: str
    level: str = "all"
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
class ReviewConfig:
    max_rounds: int = 3


@dataclass
class AutoStartConfig:
    enabled: bool = False
    label: str = "nightshift"
    poll_interval_s: int = 30
    max_concurrent: int = 1


@dataclass
class PricingConfig:
    input_per_1m: float = 0.0
    output_per_1m: float = 0.0


@dataclass
class OverflowProfile:
    extra_args: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)
    prompt_snippet: str | None = None
    signal_method: str | None = None
    # When true, keep API keys in the container even if Codex OAuth is present.
    skip_oauth: bool = False
    # Path to litellm-config.yaml for proxy-based model remapping
    litellm_config: str | None = None
    # Optional provider pricing for agents that emit tokens without cost.
    pricing: PricingConfig | None = None
    # Agent kind to use in overflow mode (e.g., "openhands" vs "claude-code")
    # In regular mode, agent.kind is used; in overflow mode, this overrides it
    agent_kind: str | None = None


@dataclass
class OverflowConfig(OverflowProfile):
    # Name of the active overflow profile, if selected via WORKFLOW.md or CLI.
    profile_name: str | None = None
    # All named profiles available for selection.
    profiles: dict[str, OverflowProfile] = field(default_factory=dict)


@dataclass
class WorkflowConfig:
    """Fully parsed, typed configuration from WORKFLOW.md."""
    agent: AgentConfig = field(default_factory=AgentConfig)
    tracker: TrackerConfig = field(default_factory=TrackerConfig)
    workspace: WorkspaceConfig = field(default_factory=WorkspaceConfig)
    notifications: list[NotifierConfig] = field(default_factory=list)
    merge: MergeConfig = field(default_factory=MergeConfig)
    hooks: HooksConfig = field(default_factory=HooksConfig)
    review: ReviewConfig = field(default_factory=ReviewConfig)
    auto_start: AutoStartConfig = field(default_factory=AutoStartConfig)
    overflow: OverflowConfig = field(default_factory=OverflowConfig)
    terminal_statuses: list[str] = field(default_factory=lambda: ["closed"])
    prompt_template: str = ""
