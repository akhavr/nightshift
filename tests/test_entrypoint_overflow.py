"""Tests for entrypoint.py overflow profile handling."""

import os
import sys
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


class TestOverflowProfileResolution:
    """Test that OVERFLOW_PROFILE env var triggers profile resolution."""

    @patch("entrypoint.load_workflow")
    @patch("entrypoint._create_adapters")
    @patch("entrypoint.search_related_issues")
    @patch("entrypoint.SessionRunner")
    def test_overflow_profile_resolved_from_env(
        self, mock_runner, mock_search, mock_adapters, mock_load
    ):
        """When OVERFLOW_PROFILE is set, resolve_overflow_config uses the profile name."""
        from core.config.models import (
            WorkflowConfig, AgentConfig, OverflowConfig, OverflowProfile, PricingConfig
        )

        # Create a config with profiles
        profile = OverflowProfile(
            agent_kind="openhands",
            env={"LLM_API_KEY": "profile-key"},
            pricing=PricingConfig(input_per_1m=0.5, output_per_1m=1.5),
        )
        config = WorkflowConfig(
            agent=AgentConfig(kind="claude-code"),
            overflow=OverflowConfig(
                agent_kind="codex",  # default overflow agent_kind
                profiles={"openhands-qwen": profile},
            ),
        )
        mock_load.return_value = config

        # Set up mock adapters
        mock_tracker = MagicMock()
        mock_issue = MagicMock()
        mock_issue.identifier = "test-123"
        mock_tracker.get_issue.return_value = mock_issue
        mock_tracker.list_issues.return_value = []

        mock_state_mgr = MagicMock()
        mock_state = MagicMock()
        mock_state.step = 0
        mock_state_mgr.load_state.return_value = mock_state
        mock_state_mgr.read_resume_prompt.return_value = None

        mock_notifier = MagicMock()
        mock_adapters.return_value = (
            mock_tracker,  # tracker
            MagicMock(),   # agent
            MagicMock(),   # workspace_mgr
            MagicMock(),   # workspace
            mock_state_mgr,
            mock_notifier,
        )
        mock_search.return_value = ""

        with patch.dict(os.environ, {
            "ISSUE_ID": "test-123",
            "OVERFLOW_ACTIVE": "1",
            "OVERFLOW_PROFILE": "openhands-qwen",  # Select the profile
        }, clear=False):
            # Need to patch resolve_overflow_config to capture the call
            with patch("entrypoint.resolve_overflow_config") as mock_resolve:
                resolved_overflow = OverflowConfig(
                    agent_kind="openhands",
                    profile_name="openhands-qwen",
                    env={"LLM_API_KEY": "profile-key"},
                    pricing=PricingConfig(input_per_1m=0.5, output_per_1m=1.5),
                )
                mock_resolve.return_value = resolved_overflow

                # Import and run main
                from entrypoint import main
                main()

                # Verify resolve_overflow_config was called with the profile name
                mock_resolve.assert_called_once_with(
                    config, "openhands-qwen", repo_root=Path("/workspace")
                )

    @patch("entrypoint.load_workflow")
    @patch("entrypoint._create_adapters")
    @patch("entrypoint.search_related_issues")
    @patch("entrypoint.SessionRunner")
    def test_overflow_without_profile_uses_default(
        self, mock_runner, mock_search, mock_adapters, mock_load
    ):
        """When OVERFLOW_ACTIVE but no OVERFLOW_PROFILE, uses config.overflow directly."""
        from core.config.models import WorkflowConfig, AgentConfig, OverflowConfig

        config = WorkflowConfig(
            agent=AgentConfig(kind="claude-code"),
            overflow=OverflowConfig(
                agent_kind="codex",
                env={"CODEX_API_KEY": "default-key"},
            ),
        )
        mock_load.return_value = config

        mock_tracker = MagicMock()
        mock_issue = MagicMock()
        mock_issue.identifier = "test-123"
        mock_tracker.get_issue.return_value = mock_issue
        mock_tracker.list_issues.return_value = []

        mock_state_mgr = MagicMock()
        mock_state = MagicMock()
        mock_state.step = 0
        mock_state_mgr.load_state.return_value = mock_state
        mock_state_mgr.read_resume_prompt.return_value = None

        mock_notifier = MagicMock()
        mock_adapters.return_value = (
            mock_tracker,
            MagicMock(),
            MagicMock(),
            MagicMock(),
            mock_state_mgr,
            mock_notifier,
        )
        mock_search.return_value = ""

        with patch.dict(os.environ, {
            "ISSUE_ID": "test-123",
            "OVERFLOW_ACTIVE": "1",
            # No OVERFLOW_PROFILE set
        }, clear=False):
            # Remove OVERFLOW_PROFILE if it exists
            os.environ.pop("OVERFLOW_PROFILE", None)

            with patch("entrypoint.resolve_overflow_config") as mock_resolve:
                mock_resolve.return_value = config.overflow

                from entrypoint import main
                main()

                # Called with profile_name=None since env var not set
                mock_resolve.assert_called_once_with(
                    config, None, repo_root=Path("/workspace")
                )

    @patch("entrypoint.load_workflow")
    @patch("entrypoint._create_adapters")
    @patch("entrypoint.search_related_issues")
    @patch("entrypoint.SessionRunner")
    def test_no_overflow_active_skips_resolve(
        self, mock_runner, mock_search, mock_adapters, mock_load
    ):
        """When OVERFLOW_ACTIVE is not set, resolve_overflow_config is not called."""
        from core.config.models import WorkflowConfig, AgentConfig, OverflowConfig

        config = WorkflowConfig(
            agent=AgentConfig(kind="claude-code"),
            overflow=OverflowConfig(),
        )
        mock_load.return_value = config

        mock_tracker = MagicMock()
        mock_issue = MagicMock()
        mock_issue.identifier = "test-123"
        mock_tracker.get_issue.return_value = mock_issue
        mock_tracker.list_issues.return_value = []

        mock_state_mgr = MagicMock()
        mock_state = MagicMock()
        mock_state.step = 0
        mock_state_mgr.load_state.return_value = mock_state
        mock_state_mgr.read_resume_prompt.return_value = None

        mock_notifier = MagicMock()
        mock_adapters.return_value = (
            mock_tracker,
            MagicMock(),
            MagicMock(),
            MagicMock(),
            mock_state_mgr,
            mock_notifier,
        )
        mock_search.return_value = ""

        with patch.dict(os.environ, {
            "ISSUE_ID": "test-123",
            # OVERFLOW_ACTIVE not set
        }, clear=False):
            os.environ.pop("OVERFLOW_ACTIVE", None)
            os.environ.pop("OVERFLOW_PROFILE", None)

            with patch("entrypoint.resolve_overflow_config") as mock_resolve:
                from entrypoint import main
                main()

                # resolve_overflow_config should NOT be called when overflow not active
                mock_resolve.assert_not_called()

    @patch("entrypoint.load_workflow")
    @patch("entrypoint._create_adapters")
    @patch("entrypoint.search_related_issues")
    @patch("entrypoint.SessionRunner")
    def test_runner_uses_overflow_signal_method_when_set(
        self, mock_runner, mock_search, mock_adapters, mock_load
    ):
        """SessionRunner should receive overflow.signal_method when the profile sets one."""
        from core.config.models import WorkflowConfig, AgentConfig, OverflowConfig

        config = WorkflowConfig(
            agent=AgentConfig(kind="claude-code", signal_method="text"),
            overflow=OverflowConfig(signal_method="file"),
        )
        mock_load.return_value = config

        mock_tracker = MagicMock()
        mock_issue = MagicMock()
        mock_issue.identifier = "test-123"
        mock_tracker.get_issue.return_value = mock_issue
        mock_tracker.list_issues.return_value = []

        mock_state_mgr = MagicMock()
        mock_state = MagicMock()
        mock_state.step = 0
        mock_state_mgr.load_state.return_value = mock_state
        mock_state_mgr.read_resume_prompt.return_value = None

        mock_notifier = MagicMock()
        mock_adapters.return_value = (
            mock_tracker,
            MagicMock(),
            MagicMock(),
            MagicMock(),
            mock_state_mgr,
            mock_notifier,
        )
        mock_search.return_value = ""

        with patch.dict(os.environ, {
            "ISSUE_ID": "test-123",
            "OVERFLOW_ACTIVE": "1",
        }, clear=False):
            os.environ.pop("OVERFLOW_PROFILE", None)

            with patch("entrypoint.resolve_overflow_config") as mock_resolve:
                mock_resolve.return_value = config.overflow

                from entrypoint import main
                main()

        assert mock_runner.call_args.kwargs["signal_method"] == "file"

    @patch("entrypoint.load_workflow")
    @patch("entrypoint._create_adapters")
    @patch("entrypoint.search_related_issues")
    @patch("entrypoint.SessionRunner")
    def test_runner_falls_back_to_agent_signal_method_when_overflow_missing(
        self, mock_runner, mock_search, mock_adapters, mock_load
    ):
        """SessionRunner should use config.agent.signal_method when overflow does not set one."""
        from core.config.models import WorkflowConfig, AgentConfig, OverflowConfig

        config = WorkflowConfig(
            agent=AgentConfig(kind="claude-code", signal_method="text"),
            overflow=OverflowConfig(),
        )
        mock_load.return_value = config

        mock_tracker = MagicMock()
        mock_issue = MagicMock()
        mock_issue.identifier = "test-123"
        mock_tracker.get_issue.return_value = mock_issue
        mock_tracker.list_issues.return_value = []

        mock_state_mgr = MagicMock()
        mock_state = MagicMock()
        mock_state.step = 0
        mock_state_mgr.load_state.return_value = mock_state
        mock_state_mgr.read_resume_prompt.return_value = None

        mock_notifier = MagicMock()
        mock_adapters.return_value = (
            mock_tracker,
            MagicMock(),
            MagicMock(),
            MagicMock(),
            mock_state_mgr,
            mock_notifier,
        )
        mock_search.return_value = ""

        with patch.dict(os.environ, {
            "ISSUE_ID": "test-123",
            "OVERFLOW_ACTIVE": "1",
        }, clear=False):
            os.environ.pop("OVERFLOW_PROFILE", None)

            with patch("entrypoint.resolve_overflow_config") as mock_resolve:
                mock_resolve.return_value = config.overflow

                from entrypoint import main
                main()

        assert mock_runner.call_args.kwargs["signal_method"] == "text"
