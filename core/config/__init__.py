"""Configuration package — re-exports all public symbols for backward compatibility."""

from core.config.models import (
    AgentConfig,
    AutoStartConfig,
    HooksConfig,
    MergeConfig,
    NotifierConfig,
    OverflowConfig,
    OverflowProfile,
    PricingConfig,
    ReviewConfig,
    TrackerConfig,
    WorkflowConfig,
    WorkspaceConfig,
)
from core.config.loader import load_profiles, load_workflow, resolve_overflow_config, split_front_matter
from core.config.factories import (
    AGENT_REGISTRY,
    NOTIFIER_REGISTRY,
    TRACKER_REGISTRY,
    WORKSPACE_REGISTRY,
    create_agent,
    create_notifiers,
    create_tracker,
    create_workspace_mgr,
)

__all__ = [
    "AgentConfig",
    "AutoStartConfig",
    "HooksConfig",
    "MergeConfig",
    "NotifierConfig",
    "OverflowConfig",
    "OverflowProfile",
    "PricingConfig",
    "ReviewConfig",
    "TrackerConfig",
    "WorkflowConfig",
    "WorkspaceConfig",
    "load_profiles",
    "load_workflow",
    "resolve_overflow_config",
    "split_front_matter",
    "AGENT_REGISTRY",
    "NOTIFIER_REGISTRY",
    "TRACKER_REGISTRY",
    "WORKSPACE_REGISTRY",
    "create_agent",
    "create_notifiers",
    "create_tracker",
    "create_workspace_mgr",
]
