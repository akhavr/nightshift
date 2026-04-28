"""Tests for core.config.factories — adapter registries and dynamic factory functions."""

import types
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from core.config.factories import (
    AGENT_REGISTRY,
    NOTIFIER_REGISTRY,
    TRACKER_REGISTRY,
    WORKSPACE_REGISTRY,
    _instantiate,
    create_agent,
    create_notifiers,
    create_tracker,
    create_workspace_mgr,
)
from core.config.models import (
    AgentConfig,
    NotifierConfig,
    OverflowConfig,
    TrackerConfig,
    WorkflowConfig,
    WorkspaceConfig,
)
from core.config.loader import load_workflow


# ── helpers ───────────────────────────────────────────────────


def _make_mock_module(class_name: str):
    """Return a fake module containing a MagicMock class with the given name."""
    mock_cls = MagicMock()
    mod = types.ModuleType("fake_module")
    setattr(mod, class_name, mock_cls)
    return mod, mock_cls


# ── _instantiate ──────────────────────────────────────────────


class TestInstantiate:
    def test_valid_kind(self):
        registry = {"my-kind": ("some.module", "MyClass")}
        mod, mock_cls = _make_mock_module("MyClass")

        with patch("core.config.factories.importlib.import_module", return_value=mod) as imp:
            result = _instantiate(registry, "my-kind", foo="bar")

        imp.assert_called_once_with("some.module")
        mock_cls.assert_called_once_with(foo="bar")
        assert result == mock_cls.return_value

    def test_invalid_kind_raises_valueerror(self):
        registry = {"a": ("m", "C"), "b": ("m", "C")}
        with pytest.raises(ValueError, match="Unknown adapter kind 'nope'"):
            _instantiate(registry, "nope")

    def test_invalid_kind_message_lists_available(self):
        registry = {"alpha": ("m", "C"), "beta": ("m", "C")}
        with pytest.raises(ValueError, match="alpha") as exc_info:
            _instantiate(registry, "nope")
        assert "beta" in str(exc_info.value)

    def test_import_error_propagates(self):
        registry = {"x": ("bad.module", "X")}
        with patch(
            "core.config.factories.importlib.import_module",
            side_effect=ImportError("no such module"),
        ):
            with pytest.raises(ImportError, match="no such module"):
                _instantiate(registry, "x")

    def test_kwargs_forwarded(self):
        registry = {"k": ("mod", "Cls")}
        mod, mock_cls = _make_mock_module("Cls")

        with patch("core.config.factories.importlib.import_module", return_value=mod):
            _instantiate(registry, "k", a=1, b=2, c="three")

        mock_cls.assert_called_once_with(a=1, b=2, c="three")


# ── create_agent ──────────────────────────────────────────────


class TestCreateAgent:
    def test_creates_agent_with_config(self):
        cfg = WorkflowConfig(
            agent=AgentConfig(
                kind="claude-code",
                stall_timeout_s=120,
                extra_args=["--verbose"],
                extra={"model": "opus"},
            )
        )
        mod, mock_cls = _make_mock_module("ClaudeCodeAgent")

        with patch("core.config.factories.importlib.import_module", return_value=mod):
            result = create_agent(cfg)

        mock_cls.assert_called_once_with(
            stall_timeout_s=120,
            extra_args=["--verbose"],
            model="opus",
            signal_method="auto",
        )
        assert result == mock_cls.return_value

    def test_creates_agent_with_signal_method(self):
        cfg = WorkflowConfig(
            agent=AgentConfig(
                kind="claude-code",
                stall_timeout_s=120,
                extra_args=["--verbose"],
                signal_method="file",
            )
        )
        mod, mock_cls = _make_mock_module("ClaudeCodeAgent")

        with patch("core.config.factories.importlib.import_module", return_value=mod):
            result = create_agent(cfg, signal_method="file")

        mock_cls.assert_called_once_with(
            stall_timeout_s=120,
            extra_args=["--verbose"],
            signal_method="file",
        )
        assert result == mock_cls.return_value

    def test_creates_openhands_agent(self):
        cfg = WorkflowConfig(
            agent=AgentConfig(
                kind="openhands",
                stall_timeout_s=60,
                extra_args=["--debug"],
            )
        )
        mod, mock_cls = _make_mock_module("OpenHandsAgent")

        with patch("core.config.factories.importlib.import_module", return_value=mod):
            result = create_agent(cfg)

        mock_cls.assert_called_once_with(
            stall_timeout_s=60,
            extra_args=["--debug"],
            signal_method="auto",
        )
        assert result == mock_cls.return_value

    def test_unknown_agent_kind_raises(self):
        cfg = WorkflowConfig(agent=AgentConfig(kind="unknown-agent"))
        with pytest.raises(ValueError, match="Unknown adapter kind 'unknown-agent'"):
            create_agent(cfg)

    def test_overflow_agent_kind_overrides_default(self):
        """When overflow.agent_kind is set, it should override config.agent.kind."""
        cfg = WorkflowConfig(
            agent=AgentConfig(kind="claude-code")
        )
        overflow = OverflowConfig(agent_kind="openhands")
        mod, mock_cls = _make_mock_module("OpenHandsAgent")

        with patch("core.config.factories.importlib.import_module", return_value=mod):
            result = create_agent(cfg, overflow)

        mock_cls.assert_called_once()
        assert result == mock_cls.return_value

    def test_regular_mode_uses_agent_kind(self):
        """When overflow is None, use config.agent.kind."""
        cfg = WorkflowConfig(
            agent=AgentConfig(kind="claude-code")
        )
        mod, mock_cls = _make_mock_module("ClaudeCodeAgent")

        with patch("core.config.factories.importlib.import_module", return_value=mod):
            result = create_agent(cfg, None)

        mock_cls.assert_called_once()
        assert result == mock_cls.return_value


# ── create_tracker ────────────────────────────────────────────


class TestCreateTracker:
    def test_creates_tracker_with_defaults(self):
        cfg = WorkflowConfig(
            tracker=TrackerConfig(kind="static", extra={"dir": "/tmp"})
        )
        mod, mock_cls = _make_mock_module("StaticTracker")

        with patch("core.config.factories.importlib.import_module", return_value=mod):
            result = create_tracker(cfg)

        mock_cls.assert_called_once_with(dir="/tmp")
        assert result == mock_cls.return_value

    def test_overrides_merge_with_extra(self):
        cfg = WorkflowConfig(
            tracker=TrackerConfig(kind="git-bug", extra={"repo": "/r"})
        )
        mod, mock_cls = _make_mock_module("GitBugTracker")

        with patch("core.config.factories.importlib.import_module", return_value=mod):
            create_tracker(cfg, repo="/override", verbose=True)

        mock_cls.assert_called_once_with(repo="/override", verbose=True)

    def test_passes_sync_only_when_enabled(self):
        cfg = WorkflowConfig(
            tracker=TrackerConfig(kind="git-bug", sync=True, extra={"repo": "/r"})
        )
        mod, mock_cls = _make_mock_module("GitBugTracker")

        with patch("core.config.factories.importlib.import_module", return_value=mod):
            create_tracker(cfg)

        mock_cls.assert_called_once_with(repo="/r", sync=True)

    def test_unknown_tracker_kind_raises(self):
        cfg = WorkflowConfig(tracker=TrackerConfig(kind="jira"))
        with pytest.raises(ValueError, match="Unknown adapter kind 'jira'"):
            create_tracker(cfg)

    def test_gitbug_graphql_registered(self):
        assert TRACKER_REGISTRY["git-bug-graphql"] == (
            "adapters.trackers.git_bug_graphql",
            "GitBugGraphQLTracker",
        )


# ── create_workspace_mgr ─────────────────────────────────────


class TestCreateWorkspaceMgr:
    def test_creates_with_root(self):
        cfg = WorkflowConfig(
            workspace=WorkspaceConfig(
                kind="worktree", base_branch="main", root=".wt"
            )
        )
        repo = Path("/repo")
        mod, mock_cls = _make_mock_module("GitWorktreeManager")

        with patch("core.config.factories.importlib.import_module", return_value=mod):
            result = create_workspace_mgr(cfg, repo)

        mock_cls.assert_called_once_with(
            repo_root=repo,
            base_branch="main",
            worktree_root=repo / ".wt",
        )
        assert result == mock_cls.return_value

    def test_creates_without_root(self):
        cfg = WorkflowConfig(
            workspace=WorkspaceConfig(kind="worktree", base_branch="develop", root="")
        )
        repo = Path("/repo")
        mod, mock_cls = _make_mock_module("GitWorktreeManager")

        with patch("core.config.factories.importlib.import_module", return_value=mod):
            create_workspace_mgr(cfg, repo)

        mock_cls.assert_called_once_with(
            repo_root=repo,
            base_branch="develop",
        )

    def test_unknown_workspace_kind_raises(self):
        cfg = WorkflowConfig(
            workspace=WorkspaceConfig(kind="docker-volume")
        )
        with pytest.raises(ValueError, match="Unknown adapter kind 'docker-volume'"):
            create_workspace_mgr(cfg, Path("/repo"))


# ── create_notifiers ──────────────────────────────────────────


class TestCreateNotifiers:
    def test_creates_multiple_notifiers(self):
        cfg = WorkflowConfig(
            notifications=[
                NotifierConfig(kind="telegram", extra={"token": "t", "chat_id": "1"}),
                NotifierConfig(kind="webhook", extra={"url": "http://x"}),
            ]
        )
        tg_mod, tg_cls = _make_mock_module("TelegramNotifier")
        wh_mod, wh_cls = _make_mock_module("WebhookNotifier")

        def side_effect(module_path):
            if "telegram" in module_path:
                return tg_mod
            return wh_mod

        with patch(
            "core.config.factories.importlib.import_module", side_effect=side_effect
        ):
            result = create_notifiers(cfg)

        assert len(result) == 2
        tg_cls.assert_called_once_with(token="t", chat_id="1", level="all")
        wh_cls.assert_called_once_with(url="http://x", level="all")

    def test_telegram_receives_tracker(self):
        cfg = WorkflowConfig(
            notifications=[
                NotifierConfig(kind="telegram", extra={"token": "t"}),
            ]
        )
        mod, mock_cls = _make_mock_module("TelegramNotifier")
        tracker = MagicMock()

        with patch("core.config.factories.importlib.import_module", return_value=mod):
            create_notifiers(cfg, tracker=tracker)

        mock_cls.assert_called_once_with(token="t", level="all", tracker=tracker)

    def test_non_telegram_does_not_receive_tracker(self):
        cfg = WorkflowConfig(
            notifications=[
                NotifierConfig(kind="webhook", extra={"url": "http://x"}),
            ]
        )
        mod, mock_cls = _make_mock_module("WebhookNotifier")
        tracker = MagicMock()

        with patch("core.config.factories.importlib.import_module", return_value=mod):
            create_notifiers(cfg, tracker=tracker)

        mock_cls.assert_called_once_with(url="http://x", level="all")

    def test_empty_notifications(self):
        cfg = WorkflowConfig(notifications=[])
        assert create_notifiers(cfg) == []

    def test_failed_notifier_is_skipped_with_warning(self):
        cfg = WorkflowConfig(
            notifications=[
                NotifierConfig(kind="telegram", extra={}),
                NotifierConfig(kind="webhook", extra={"url": "http://ok"}),
            ]
        )
        ok_mod, ok_cls = _make_mock_module("WebhookNotifier")

        def side_effect(module_path):
            if "telegram" in module_path:
                raise RuntimeError("bad token")
            return ok_mod

        with patch(
            "core.config.factories.importlib.import_module", side_effect=side_effect
        ):
            result = create_notifiers(cfg)

        # telegram failed, webhook succeeded
        assert len(result) == 1
        ok_cls.assert_called_once()

    def test_all_notifiers_fail_returns_empty(self):
        cfg = WorkflowConfig(
            notifications=[
                NotifierConfig(kind="telegram", extra={}),
            ]
        )

        with patch(
            "core.config.factories.importlib.import_module",
            side_effect=ImportError("nope"),
        ):
            result = create_notifiers(cfg)

        assert result == []


# ── WorkspaceConfig parsing ──────────────────────────────────


class TestWorkspaceConfigParsing:
    def test_tracker_config_parses_sync(self, tmp_path):
        """TrackerConfig parses sync as a typed field, not extra config."""
        workflow_md = tmp_path / "WORKFLOW.md"
        workflow_md.write_text("""---
tracker:
  kind: git-bug
  sync: true
  repo: /tmp/repo
---
Prompt body
""")
        config = load_workflow(workflow_md)
        assert config.tracker.sync is True
        assert config.tracker.extra == {"repo": "/tmp/repo"}

    def test_workspace_config_parses_test_timeout(self, tmp_path):
        """WorkspaceConfig with test_timeout_s: 300 parses correctly."""
        workflow_md = tmp_path / "WORKFLOW.md"
        workflow_md.write_text("""---
workspace:
  kind: worktree
  base_branch: master
  test_command: ".venv/bin/python -m pytest tests/ -v"
  test_timeout_s: 300
---
Prompt body
""")
        config = load_workflow(workflow_md)
        assert config.workspace.test_timeout_s == 300
        assert config.workspace.test_command == ".venv/bin/python -m pytest tests/ -v"

    def test_workspace_config_defaults_test_timeout(self, tmp_path):
        """WorkspaceConfig without test_timeout_s defaults to 120."""
        workflow_md = tmp_path / "WORKFLOW.md"
        workflow_md.write_text("""---
workspace:
  kind: worktree
  test_command: "pytest"
---
Prompt body
""")
        config = load_workflow(workflow_md)
        assert config.workspace.test_timeout_s == 120

    def test_workspace_config_dataclass_default(self):
        """WorkspaceConfig dataclass has correct default for test_timeout_s."""
        config = WorkspaceConfig()
        assert config.test_timeout_s == 120


class TestSignalMethodConfig:
    """Tests for signal_method configuration parsing (REQ-001)."""

    def test_signal_method_config(self, tmp_path):
        """AgentConfig parses signal_method from YAML."""
        workflow_md = tmp_path / "WORKFLOW.md"
        workflow_md.write_text("""---
agent:
  kind: claude-code
  signal_method: file
---
Prompt body
""")
        config = load_workflow(workflow_md)
        assert config.agent.signal_method == "file"

    def test_signal_method_defaults_to_auto(self, tmp_path):
        """AgentConfig defaults signal_method to 'auto' when not specified."""
        workflow_md = tmp_path / "WORKFLOW.md"
        workflow_md.write_text("""---
agent:
  kind: claude-code
---
Prompt body
""")
        config = load_workflow(workflow_md)
        assert config.agent.signal_method == "auto"

    def test_signal_method_dataclass_default(self):
        """AgentConfig dataclass has correct default for signal_method."""
        config = AgentConfig()
        assert config.signal_method == "auto"

    def test_signal_method_all_values(self, tmp_path):
        """All valid signal_method values are parsed correctly."""
        for method in ["auto", "mcp", "text", "file"]:
            workflow_md = tmp_path / "WORKFLOW.md"
            workflow_md.write_text(f"""---
agent:
  kind: claude-code
  signal_method: {method}
---
Prompt body
""")
            config = load_workflow(workflow_md)
            assert config.agent.signal_method == method

    def test_signal_method_invalid_raises(self, tmp_path):
        """Invalid signal_method values raise ValueError."""
        workflow_md = tmp_path / "WORKFLOW.md"
        workflow_md.write_text("""---
agent:
  kind: claude-code
  signal_method: invalid
---
Prompt body
""")
        with pytest.raises(ValueError) as exc_info:
            load_workflow(workflow_md)
        assert "Invalid signal_method 'invalid'" in str(exc_info.value)
        assert "auto" in str(exc_info.value)


class TestOverflowProfileSignalMethod:
    """Tests for per-profile signal_method support (REQ-001)."""

    def test_overflow_profile_signal_method(self, tmp_path):
        """OverflowProfile with signal_method: file parses correctly."""
        workflow_md = tmp_path / "WORKFLOW.md"
        workflow_md.write_text("""---
overflow_profiles:
  openhands-file:
    agent_kind: openhands
    signal_method: file
overflow: openhands-file
---
Prompt body
""")
        config = load_workflow(workflow_md)
        assert config.overflow.profile_name == "openhands-file"
        assert config.overflow.signal_method == "file"
        assert config.overflow.profiles["openhands-file"].signal_method == "file"

    def test_overflow_profile_signal_method_overrides_agent(self, tmp_path):
        """When overflow profile has signal_method, it overrides agent.signal_method."""
        workflow_md = tmp_path / "WORKFLOW.md"
        workflow_md.write_text("""---
agent:
  kind: claude-code
  signal_method: text
overflow_profiles:
  openhands-file:
    agent_kind: openhands
    signal_method: file
overflow: openhands-file
---
Prompt body
""")
        config = load_workflow(workflow_md)
        assert config.agent.signal_method == "text"
        assert config.overflow.signal_method == "file"

    def test_overflow_profile_signal_method_defaults_to_none(self, tmp_path):
        """OverflowProfile without signal_method has None and inherits from agent."""
        workflow_md = tmp_path / "WORKFLOW.md"
        workflow_md.write_text("""---
agent:
  kind: claude-code
  signal_method: text
overflow_profiles:
  openhands-default:
    agent_kind: openhands
overflow: openhands-default
---
Prompt body
""")
        config = load_workflow(workflow_md)
        assert config.overflow.profile_name == "openhands-default"
        assert config.overflow.signal_method is None
        assert config.overflow.profiles["openhands-default"].signal_method is None
