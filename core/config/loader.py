"""YAML front matter parsing and environment variable resolution."""

import logging
import os
from pathlib import Path
from typing import Any

import yaml

from core.config.models import (
    WorkflowConfig, AgentConfig, TrackerConfig, WorkspaceConfig,
    NotifierConfig, MergeConfig, HooksConfig, ReviewConfig, AutoStartConfig,
    OverflowConfig, OverflowProfile, PricingConfig,
)

log = logging.getLogger(__name__)


def load_workflow(path: Path | str = "WORKFLOW.md",
                  repo_root: Path | str | None = None) -> WorkflowConfig:
    """Load and parse WORKFLOW.md. Returns defaults if file doesn't exist."""
    path = Path(path)
    repo_root = Path(repo_root) if repo_root is not None else path.resolve().parent

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

    # Resolve env vars for everything except overflow (deferred to avoid
    # noisy warnings when overflow env vars are not set in the shell).
    overflow_raw = raw.pop("overflow", None)
    overflow_profiles_raw = raw.pop("overflow_profiles", None)
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

    if overflow_profiles_raw is not None:
        raw["overflow_profiles"] = _resolve_env_vars(overflow_profiles_raw, quiet=True)
    if overflow_raw is not None:
        raw["overflow"] = _resolve_env_vars(overflow_raw, quiet=True)
    _parse_overflow(raw, config, repo_root)

    if "terminal_statuses" in raw:
        config.terminal_statuses = [str(s) for s in raw["terminal_statuses"]]

    return config


VALID_SIGNAL_METHODS = {"auto", "mcp", "text", "file"}


def _parse_agent(raw: dict, config: WorkflowConfig):
    if "agent" not in raw:
        return
    a = raw["agent"]
    known = {"kind", "max_turns", "stall_timeout_s", "extra_args", "signal_method"}
    signal_method = str(a.get("signal_method", "auto"))
    if signal_method not in VALID_SIGNAL_METHODS:
        raise ValueError(
            f"Invalid signal_method '{signal_method}'. "
            f"Must be one of: {sorted(VALID_SIGNAL_METHODS)}"
        )
    config.agent = AgentConfig(
        kind=a.get("kind", "claude-code"),
        max_turns=int(a.get("max_turns", 50)),
        stall_timeout_s=int(a.get("stall_timeout_s", 300)),
        extra_args=a.get("extra_args", []),
        signal_method=signal_method,
        extra={k: v for k, v in a.items() if k not in known},
    )


def _parse_tracker(raw: dict, config: WorkflowConfig):
    if "tracker" not in raw:
        return
    t = raw["tracker"]
    known = {"kind", "sync"}
    config.tracker = TrackerConfig(
        kind=t.get("kind", "git-bug"),
        sync=bool(t.get("sync", False)),
        extra={k: v for k, v in t.items() if k not in known},
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
        test_timeout_s=int(w.get("test_timeout_s", 120)),
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


def _parse_overflow_profile(raw_profile: dict[str, Any]) -> OverflowProfile:
    pricing = None
    if isinstance(raw_profile.get("pricing"), dict):
        pricing_raw = raw_profile["pricing"]
        pricing = PricingConfig(
            input_per_1m=float(pricing_raw.get("input_per_1m", 0.0)),
            output_per_1m=float(pricing_raw.get("output_per_1m", 0.0)),
        )
    return OverflowProfile(
        extra_args=raw_profile.get("extra_args", []),
        env=raw_profile.get("env", {}),
        prompt_snippet=raw_profile.get("prompt_snippet"),
        litellm_config=raw_profile.get("litellm_config"),
        pricing=pricing,
        agent_kind=raw_profile.get("agent_kind"),
    )


def _parse_overflow_profiles(raw_profiles: Any, source: str) -> dict[str, OverflowProfile]:
    if not isinstance(raw_profiles, dict):
        raise ValueError(f"{source} must be a mapping of profile names")
    profiles: dict[str, OverflowProfile] = {}
    for name, profile_raw in raw_profiles.items():
        if not isinstance(profile_raw, dict):
            raise ValueError(f"{source}.{name} must be a mapping")
        profiles[str(name)] = _parse_overflow_profile(_resolve_env_vars(profile_raw, quiet=True))
    return profiles


def load_profiles(repo_root: Path | str) -> dict[str, OverflowProfile]:
    repo_root = Path(repo_root)
    profiles_path = repo_root / ".nightshift" / "profiles.yaml"
    if not profiles_path.exists():
        return {}
    try:
        raw = yaml.safe_load(profiles_path.read_text())
    except yaml.YAMLError as e:
        raise ValueError(f"{profiles_path} YAML parse error: {e}")
    if raw is None:
        return {}
    return _parse_overflow_profiles(raw, str(profiles_path))


def resolve_overflow_config(config: WorkflowConfig,
                            profile_name: str | None = None,
                            repo_root: Path | str | None = None) -> OverflowConfig:
    """Return the active overflow config, optionally overriding the profile name."""
    selected_profile = profile_name or config.overflow.profile_name
    if not selected_profile:
        return config.overflow
    profiles = dict(config.overflow.profiles)
    profile = profiles.get(selected_profile)
    if profile is None and repo_root is not None:
        profiles = {**load_profiles(repo_root), **profiles}
        profile = profiles.get(selected_profile)
    if profile is None:
        source = "workflow overflow_profiles"
        if repo_root is not None:
            source = f"{source} or {Path(repo_root) / '.nightshift' / 'profiles.yaml'}"
        raise ValueError(f"Unknown overflow profile '{selected_profile}' in {source}")
    return OverflowConfig(
        extra_args=list(profile.extra_args),
        env=dict(profile.env),
        prompt_snippet=profile.prompt_snippet,
        litellm_config=profile.litellm_config,
        pricing=profile.pricing,
        agent_kind=profile.agent_kind,
        profile_name=selected_profile,
        profiles=profiles,
    )


def _parse_overflow(raw: dict, config: WorkflowConfig, repo_root: Path | None = None):
    profiles: dict[str, OverflowProfile] = {}
    if repo_root is not None:
        profiles = load_profiles(repo_root)
    if "overflow_profiles" in raw:
        profiles = {**profiles, **_parse_overflow_profiles(raw["overflow_profiles"], "overflow_profiles")}

    if "overflow" not in raw:
        if profiles:
            config.overflow = OverflowConfig(profiles=profiles)
        return
    o = raw["overflow"]
    if isinstance(o, str):
        config.overflow = OverflowConfig(profile_name=o, profiles=profiles)
        config.overflow = resolve_overflow_config(config, repo_root=repo_root)
        return
    if not isinstance(o, dict):
        raise ValueError("overflow must be a mapping or profile name")
    profile = _parse_overflow_profile(o)
    config.overflow = OverflowConfig(
        extra_args=profile.extra_args,
        env=profile.env,
        prompt_snippet=profile.prompt_snippet,
        litellm_config=profile.litellm_config,
        pricing=profile.pricing,
        agent_kind=profile.agent_kind,
        profiles=profiles,
    )


def split_front_matter(text: str) -> tuple[str, str]:
    """Split YAML front matter from markdown body."""
    if not text.startswith("---"):
        return "", text
    parts = text.split("---", 2)
    if len(parts) < 3:
        return "", text
    return parts[1], parts[2]


def _resolve_env_vars(obj: Any, quiet: bool = False) -> Any:
    """Recursively resolve $VAR references in string values.

    Args:
        quiet: If True, suppress warnings for missing env vars (used for
               overflow section which is only relevant when overflow is active).
    """
    if isinstance(obj, str):
        if obj.startswith("$"):
            var_name = obj[1:]
            val = os.environ.get(var_name, "")
            if not val and not quiet:
                log.warning(f"Environment variable ${var_name} is empty/missing")
            return val
        return obj
    elif isinstance(obj, dict):
        return {k: _resolve_env_vars(v, quiet) for k, v in obj.items()}
    elif isinstance(obj, list):
        return [_resolve_env_vars(v, quiet) for v in obj]
    return obj
