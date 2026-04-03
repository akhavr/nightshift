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
    TrackerConfig,
    WorkflowConfig,
    WorkspaceConfig,
)


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
        )
        assert result == mock_cls.return_value

    def test_unknown_agent_kind_raises(self):
        cfg = WorkflowConfig(agent=AgentConfig(kind="unknown-agent"))
        with pytest.raises(ValueError, match="Unknown adapter kind 'unknown-agent'"):
            create_agent(cfg)


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

    def test_unknown_tracker_kind_raises(self):
        cfg = WorkflowConfig(tracker=TrackerConfig(kind="jira"))
        with pytest.raises(ValueError, match="Unknown adapter kind 'jira'"):
            create_tracker(cfg)


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
