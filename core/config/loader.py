"""YAML front matter parsing and environment variable resolution."""

import logging
import os
from pathlib import Path
from typing import Any

import yaml

from core.config.models import (
    WorkflowConfig, AgentConfig, TrackerConfig, WorkspaceConfig,
    NotifierConfig, MergeConfig, HooksConfig, ReviewConfig, AutoStartConfig,
    OverflowConfig,
)

log = logging.getLogger(__name__)


def load_workflow(path: Path | str = "WORKFLOW.md") -> WorkflowConfig:
    """Load and parse WORKFLOW.md. Returns defaults if file doesn't exist."""
    path = Path(path)

    if not path.exists():
        log.info(f"{path} not found — using defaults")
        return WorkflowConfig()

    text = path.read_text()
    front_matter, prompt_body = split_front_matter(text)

    if front_matter:
        try:
            raw = yaml.safe_load(front_matter)
            if not isinstance(raw, dict):
                raise ValueError(f"WORKFLOW.md front matter must be a mapping, got {type(raw)}")
        except yaml.YAMLError as e:
            raise ValueError(f"WORKFLOW.md YAML parse error: {e}")
    else:
        raw = {}

    raw = _resolve_env_vars(raw)
    config = WorkflowConfig(prompt_template=prompt_body.strip())

    _parse_agent(raw, config)
    _parse_tracker(raw, config)
    _parse_workspace(raw, config)
    _parse_notifications(raw, config)
    _parse_merge(raw, config)
    _parse_hooks(raw, config)
    _parse_review(raw, config)
    _parse_auto_start(raw, config)
    _parse_overflow(raw, config)

    if "terminal_statuses" in raw:
        config.terminal_statuses = [str(s) for s in raw["terminal_statuses"]]

    return config


def _parse_agent(raw: dict, config: WorkflowConfig):
    if "agent" not in raw:
        return
    a = raw["agent"]
    known = {"kind", "max_turns", "stall_timeout_s", "extra_args"}
    config.agent = AgentConfig(
        kind=a.get("kind", "claude-code"),
        max_turns=int(a.get("max_turns", 50)),
        stall_timeout_s=int(a.get("stall_timeout_s", 300)),
        extra_args=a.get("extra_args", []),
        extra={k: v for k, v in a.items() if k not in known},
    )


def _parse_tracker(raw: dict, config: WorkflowConfig):
    if "tracker" not in raw:
        return
    t = raw["tracker"]
    config.tracker = TrackerConfig(
        kind=t.get("kind", "git-bug"),
        extra={k: v for k, v in t.items() if k != "kind"},
    )


def _parse_workspace(raw: dict, config: WorkflowConfig):
    if "workspace" not in raw:
        return
    w = raw["workspace"]
    config.workspace = WorkspaceConfig(
        kind=w.get("kind", "worktree"),
        base_branch=w.get("base_branch", "master"),
        root=w.get("root", ".worktrees"),
        test_command=w.get("test_command"),
    )


def _parse_notifications(raw: dict, config: WorkflowConfig):
    if "notifications" not in raw:
        return
    known_keys = {"kind", "level"}
    for n in raw["notifications"]:
        config.notifications.append(NotifierConfig(
            kind=n.get("kind", "webhook"),
            level=n.get("level", "all"),
            extra={k: v for k, v in n.items() if k not in known_keys},
        ))


def _parse_merge(raw: dict, config: WorkflowConfig):
    if "merge" not in raw:
        return
    m = raw["merge"]
    config.merge = MergeConfig(
        require_review=m.get("require_review", True),
        review_label=m.get("review_label", "reviewed"),
        auto_merge_label=m.get("auto_merge_label", "auto-merge"),
    )


def _parse_hooks(raw: dict, config: WorkflowConfig):
    if "hooks" not in raw:
        return
    h = raw["hooks"]
    config.hooks = HooksConfig(
        after_create=h.get("after_create"),
        before_run=h.get("before_run"),
        after_run=h.get("after_run"),
        timeout_s=int(h.get("timeout_s", 60)),
    )


def _parse_review(raw: dict, config: WorkflowConfig):
    if "review" not in raw:
        return
    rv = raw["review"]
    config.review = ReviewConfig(
        max_rounds=int(rv.get("max_rounds", 3)),
    )


def _parse_auto_start(raw: dict, config: WorkflowConfig):
    if "auto_start" not in raw:
        return
    asc = raw["auto_start"]
    config.auto_start = AutoStartConfig(
        enabled=bool(asc.get("enabled", False)),
        label=str(asc.get("label", "nightshift")),
        poll_interval_s=int(asc.get("poll_interval_s", 30)),
        max_concurrent=int(asc.get("max_concurrent", 1)),
    )


def _parse_overflow(raw: dict, config: WorkflowConfig):
    if "overflow" not in raw:
        return
    o = raw["overflow"]
    config.overflow = OverflowConfig(
        extra_args=o.get("extra_args", []),
        env=o.get("env", {}),
        litellm_config=o.get("litellm_config"),
        agent_kind=o.get("agent_kind"),
    )


def split_front_matter(text: str) -> tuple[str, str]:
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
