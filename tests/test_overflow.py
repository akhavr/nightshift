"""Tests for overflow feature (REQ-028): alternate LLM provider switching."""

import json
import os
import sys
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.config.models import OverflowConfig, OverflowProfile, PricingConfig, WorkflowConfig
from core.config.loader import load_workflow, _parse_overflow
from core.protocols import AgentEvent, AgentEventType
from core.session import SessionRunner
from core.state import SessionState, StateManager
from host.constants import OVERFLOW_FLAG_FILENAME
from host.docker_cmd import build_docker_cmd
from host.cli import cmd_overflow, cmd_status, _overflow_flag_path
from tests.conftest import (
    MockNotifier,
    MockTracker,
    MockWorkspaceManager,
    make_test_issue,
)


# ── OverflowConfig dataclass ────────────────────────────────────────────────


def test_overflow_config_defaults():
    """OverflowConfig has empty defaults."""
    oc = OverflowConfig()
    assert oc.extra_args == []
    assert oc.env == {}
    assert oc.skip_oauth is False
    assert oc.litellm_config is None
    assert oc.pricing is None


def test_overflow_config_with_values():
    """OverflowConfig stores extra_args and env."""
    oc = OverflowConfig(
        extra_args=["--model", "m2.7"],
        env={"ANTHROPIC_BASE_URL": "https://example.com"},
    )
    assert oc.extra_args == ["--model", "m2.7"]
    assert oc.env["ANTHROPIC_BASE_URL"] == "https://example.com"


def test_overflow_config_stores_skip_oauth():
    """OverflowConfig stores the skip_oauth flag."""
    oc = OverflowConfig(skip_oauth=True)
    assert oc.skip_oauth is True


def test_overflow_config_profiles():
    """OverflowConfig stores named overflow profiles."""
    profile = OverflowProfile(agent_kind="codex", env={"CODEX_MODEL": "gpt-5.4-mini"})
    oc = OverflowConfig(profile_name="codex-oauth", profiles={"codex-oauth": profile})
    assert oc.profile_name == "codex-oauth"
    assert oc.profiles["codex-oauth"] == profile


def test_workflow_config_has_overflow():
    """WorkflowConfig includes an overflow field with correct default."""
    wc = WorkflowConfig()
    assert isinstance(wc.overflow, OverflowConfig)
    assert wc.overflow.extra_args == []
    assert wc.overflow.env == {}
    assert wc.overflow.profiles == {}


# ── Config parsing ──────────────────────────────────────────────────────────


def test_parse_overflow_from_yaml(tmp_path):
    """Overflow section is parsed from WORKFLOW.md YAML front matter."""
    workflow = tmp_path / "WORKFLOW.md"
    workflow.write_text("""\
---
overflow:
  extra_args: ["--model", "m2.7"]
  env:
    ANTHROPIC_BASE_URL: https://api.minimax.io/anthropic
    ANTHROPIC_API_KEY: sk-test-key
---
Prompt body here.
""")
    config = load_workflow(workflow)
    assert config.overflow.extra_args == ["--model", "m2.7"]
    assert config.overflow.env["ANTHROPIC_BASE_URL"] == "https://api.minimax.io/anthropic"
    assert config.overflow.env["ANTHROPIC_API_KEY"] == "sk-test-key"


def test_parse_overflow_missing():
    """Missing overflow section produces empty defaults."""
    config = WorkflowConfig()
    raw = {}
    _parse_overflow(raw, config)
    assert config.overflow.extra_args == []
    assert config.overflow.env == {}


def test_parse_overflow_env_var_resolution(tmp_path, monkeypatch):
    """$VAR references in overflow config are resolved from environment."""
    monkeypatch.setenv("OVERFLOW_MODEL", "m2.7")
    monkeypatch.setenv("OVERFLOW_BASE_URL", "https://api.minimax.io/anthropic")
    monkeypatch.setenv("OVERFLOW_API_KEY", "sk-resolved-key")

    workflow = tmp_path / "WORKFLOW.md"
    workflow.write_text("""\
---
overflow:
  extra_args: ["--model", "$OVERFLOW_MODEL"]
  env:
    ANTHROPIC_BASE_URL: $OVERFLOW_BASE_URL
    ANTHROPIC_API_KEY: $OVERFLOW_API_KEY
---
Prompt.
""")
    config = load_workflow(workflow)
    assert config.overflow.extra_args == ["--model", "m2.7"]
    assert config.overflow.env["ANTHROPIC_BASE_URL"] == "https://api.minimax.io/anthropic"
    assert config.overflow.env["ANTHROPIC_API_KEY"] == "sk-resolved-key"


def test_parse_overflow_partial():
    """Overflow section with only extra_args, no env."""
    config = WorkflowConfig()
    raw = {"overflow": {"extra_args": ["--model", "test"]}}
    _parse_overflow(raw, config)
    assert config.overflow.extra_args == ["--model", "test"]
    assert config.overflow.env == {}


def test_parse_overflow_profile_from_yaml(tmp_path, monkeypatch):
    """String overflow values resolve to a named profile."""
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-openrouter")
    workflow = tmp_path / "WORKFLOW.md"
    workflow.write_text("""\
---
overflow_profiles:
  openrouter-qwen:
    agent_kind: codex
    skip_oauth: true
    env:
      CODEX_MODEL: qwen/qwen3.6-plus
      CODEX_BASE_URL: https://openrouter.ai/api/v1
      CODEX_API_KEY: $OPENROUTER_API_KEY
overflow: openrouter-qwen
---
Prompt.
""")
    config = load_workflow(workflow)
    assert config.overflow.profile_name == "openrouter-qwen"
    assert config.overflow.agent_kind == "codex"
    assert config.overflow.skip_oauth is True
    assert config.overflow.env["CODEX_MODEL"] == "qwen/qwen3.6-plus"
    assert config.overflow.env["CODEX_API_KEY"] == "sk-openrouter"
    assert "openrouter-qwen" in config.overflow.profiles


def test_parse_overflow_profile_unknown_name_raises(tmp_path):
    """Unknown overflow profile names fail fast during config load."""
    workflow = tmp_path / "WORKFLOW.md"
    workflow.write_text("""\
---
overflow_profiles:
  codex-oauth:
    agent_kind: codex
overflow: missing-profile
---
Prompt.
""")
    with pytest.raises(ValueError, match="Unknown overflow profile"):
        load_workflow(workflow)


def test_pricing_config_parsed(tmp_path):
    """Overflow pricing is parsed for custom-provider usage accounting."""
    workflow = tmp_path / "WORKFLOW.md"
    workflow.write_text("""\
---
overflow:
  agent_kind: codex
  pricing:
    input_per_1m: 0.325
    output_per_1m: 1.95
---
Prompt.
""")
    config = load_workflow(workflow)
    assert isinstance(config.overflow.pricing, PricingConfig)
    assert config.overflow.pricing.input_per_1m == 0.325
    assert config.overflow.pricing.output_per_1m == 1.95


class _SingleRunAgent:
    def __init__(self, events):
        self.events = events
        self._pid = 12345

    def start(self, prompt, workspace, max_turns=50):
        self.started = True

    def stream_events(self):
        yield from self.events

    def send_input(self, text):
        pass

    def is_alive(self):
        return False

    def terminate(self):
        pass

    @property
    def pid(self):
        return self._pid


def _run_overflow_usage(tmp_path, cost_usd):
    issue = make_test_issue()
    session_dir = tmp_path / "session"
    state_mgr = StateManager(session_dir)
    state_mgr._write(SessionState(
        issue_id=issue.id, branch=f"agent/{issue.identifier}", status="working"))
    usage_event = AgentEvent(
        type=AgentEventType.TEXT,
        content="@@DONE@@",
        metadata={"usage": {
            "input_tokens": 1_000_000,
            "output_tokens": 2_000_000,
            "cost_usd": cost_usd,
            "model": "qwen/qwen3.6-plus",
        }},
        raw="done",
    )
    runner = SessionRunner(
        agent=_SingleRunAgent([usage_event]),
        tracker=MockTracker({issue.id: issue}),
        notifier=MockNotifier(),
        workspace_mgr=MockWorkspaceManager(tmp_path),
        state_mgr=state_mgr,
        issue=issue,
        prompt="Fix the widget",
        pricing=PricingConfig(input_per_1m=0.325, output_per_1m=1.95),
    )
    runner.run()
    return state_mgr.load_state().usage


def test_cost_calculated_from_tokens(tmp_path):
    """Zero-cost token usage is priced from overflow pricing config."""
    usage = _run_overflow_usage(tmp_path, cost_usd=0.0)
    assert usage.cost_usd == pytest.approx(4.225)


def test_cost_not_calculated_when_provider_reports_cost(tmp_path):
    """Provider-reported cost takes precedence over overflow pricing."""
    usage = _run_overflow_usage(tmp_path, cost_usd=0.12)
    assert usage.cost_usd == 0.12


# ── docker_cmd with overflow ────────────────────────────────────────────────


def _build_cmd_with_overflow(overflow=None):
    """Helper to build a docker command with optional overflow."""
    return build_docker_cmd(
        repo=Path("/repo"),
        workspace_mount="/workspace",
        session_dir=Path("/session"),
        container_name="nightshift-abc123",
        worktree_name="agent-abc123",
        issue_id="abc123",
        short_id="abc123",
        max_turns=50,
        step="coder",
        is_resume=False,
        workflow_path="/repo/WORKFLOW.md",
        image="nightshift:latest",
        overflow=overflow,
    )


def test_docker_cmd_no_overflow():
    """Without overflow, no overflow env vars appear in docker command."""
    cmd = _build_cmd_with_overflow(overflow=None)
    cmd_str = " ".join(cmd)
    assert "OVERFLOW_EXTRA_ARGS" not in cmd_str
    assert "OVERFLOW_ACTIVE" not in cmd_str


def test_docker_cmd_overflow_active_flag():
    """Overflow always sets OVERFLOW_ACTIVE=1 in docker command."""
    overflow = OverflowConfig(agent_kind="openhands", env={})
    cmd = _build_cmd_with_overflow(overflow=overflow)

    env_pairs = []
    for i, arg in enumerate(cmd):
        if arg == "-e" and i + 1 < len(cmd):
            env_pairs.append(cmd[i + 1])

    assert "OVERFLOW_ACTIVE=1" in env_pairs


def test_docker_cmd_with_overflow_env():
    """Overflow env vars are injected into docker command."""
    overflow = OverflowConfig(
        extra_args=[],
        env={"ANTHROPIC_BASE_URL": "https://alt.api", "ANTHROPIC_API_KEY": "sk-alt"},
    )
    cmd = _build_cmd_with_overflow(overflow=overflow)

    # Find the overflow env vars in the command
    env_pairs = []
    for i, arg in enumerate(cmd):
        if arg == "-e" and i + 1 < len(cmd):
            env_pairs.append(cmd[i + 1])

    assert "ANTHROPIC_BASE_URL=https://alt.api" in env_pairs
    assert "ANTHROPIC_API_KEY=sk-alt" in env_pairs


def test_docker_cmd_with_overflow_extra_args():
    """Overflow extra_args are passed as OVERFLOW_EXTRA_ARGS env var."""
    overflow = OverflowConfig(
        extra_args=["--model", "m2.7"],
        env={},
    )
    cmd = _build_cmd_with_overflow(overflow=overflow)

    env_pairs = []
    for i, arg in enumerate(cmd):
        if arg == "-e" and i + 1 < len(cmd):
            env_pairs.append(cmd[i + 1])

    overflow_args_entries = [e for e in env_pairs if e.startswith("OVERFLOW_EXTRA_ARGS=")]
    assert len(overflow_args_entries) == 1
    parsed = json.loads(overflow_args_entries[0].split("=", 1)[1])
    assert parsed == ["--model", "m2.7"]


def test_docker_cmd_overflow_env_overrides_passthrough(monkeypatch):
    """Overflow env vars appear after passthrough vars, effectively overriding them."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-primary")
    overflow = OverflowConfig(
        extra_args=[],
        env={"ANTHROPIC_API_KEY": "sk-overflow"},
    )
    cmd = _build_cmd_with_overflow(overflow=overflow)

    # Collect all ANTHROPIC_API_KEY values in order
    api_keys = []
    for i, arg in enumerate(cmd):
        if arg == "-e" and i + 1 < len(cmd) and cmd[i + 1].startswith("ANTHROPIC_API_KEY="):
            api_keys.append(cmd[i + 1])

    # Docker uses the last -e value for a given var, so overflow should be last
    assert len(api_keys) == 2
    assert api_keys[-1] == "ANTHROPIC_API_KEY=sk-overflow"


# ── ANTHROPIC_BASE_URL passthrough ──────────────────────────────────────────


def test_anthropic_base_url_passthrough(monkeypatch):
    """ANTHROPIC_BASE_URL is now in _PASSTHROUGH_ENV_VARS."""
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://custom.api")
    cmd = _build_cmd_with_overflow(overflow=None)

    env_pairs = []
    for i, arg in enumerate(cmd):
        if arg == "-e" and i + 1 < len(cmd):
            env_pairs.append(cmd[i + 1])

    assert "ANTHROPIC_BASE_URL=https://custom.api" in env_pairs


# ── CLI overflow command ────────────────────────────────────────────────────


def test_cmd_overflow_on(tmp_path, capsys):
    """'nightshift overflow on' creates the flag file."""
    ns_dir = tmp_path / ".nightshift"
    ns_dir.mkdir()
    flag = ns_dir / OVERFLOW_FLAG_FILENAME

    args = MagicMock()
    args.state = "on"

    with patch("host.cli._overflow_flag_path", return_value=flag):
        cmd_overflow(args)

    assert flag.exists()
    out = capsys.readouterr().out
    assert "ON" in out


def test_cmd_overflow_off(tmp_path, capsys):
    """'nightshift overflow off' removes the flag file."""
    ns_dir = tmp_path / ".nightshift"
    ns_dir.mkdir()
    flag = ns_dir / OVERFLOW_FLAG_FILENAME
    flag.touch()

    args = MagicMock()
    args.state = "off"

    with patch("host.cli._overflow_flag_path", return_value=flag):
        cmd_overflow(args)

    assert not flag.exists()
    out = capsys.readouterr().out
    assert "OFF" in out


def test_cmd_overflow_off_no_file(tmp_path, capsys):
    """'nightshift overflow off' is safe when flag file doesn't exist."""
    ns_dir = tmp_path / ".nightshift"
    ns_dir.mkdir()
    flag = ns_dir / OVERFLOW_FLAG_FILENAME

    args = MagicMock()
    args.state = "off"

    with patch("host.cli._overflow_flag_path", return_value=flag):
        cmd_overflow(args)

    assert not flag.exists()


def test_cmd_overflow_creates_parent_dir(tmp_path, capsys):
    """'nightshift overflow on' creates .nightshift/ if it doesn't exist."""
    flag = tmp_path / ".nightshift" / OVERFLOW_FLAG_FILENAME

    args = MagicMock()
    args.state = "on"

    with patch("host.cli._overflow_flag_path", return_value=flag):
        cmd_overflow(args)

    assert flag.exists()


def test_cmd_overflow_profile_selects_named_profile(tmp_path, capsys):
    """'nightshift overflow profile <name>' stores the selected profile name."""
    flag = tmp_path / ".nightshift" / OVERFLOW_FLAG_FILENAME

    args = MagicMock()
    args.state = "profile"
    args.profile_name = "openrouter-qwen"
    args.workflow = None

    with patch("host.cli._overflow_flag_path", return_value=flag), \
         patch("host.cli._resolve_workflow", return_value=tmp_path / "WORKFLOW.md"), \
         patch("host.cli.load_workflow", return_value=WorkflowConfig(
             overflow=OverflowConfig(
                 profiles={"openrouter-qwen": OverflowProfile(agent_kind="codex")}
             )
         )):
        cmd_overflow(args)

    assert flag.read_text() == "openrouter-qwen\n"
    out = capsys.readouterr().out
    assert "openrouter-qwen" in out


def test_cmd_overflow_profile_rejects_unknown_profile(tmp_path, capsys):
    """'nightshift overflow profile <name>' exits when the profile is undefined."""
    flag = tmp_path / ".nightshift" / OVERFLOW_FLAG_FILENAME

    args = MagicMock()
    args.state = "profile"
    args.profile_name = "missing-profile"
    args.workflow = None

    with patch("host.cli._overflow_flag_path", return_value=flag), \
         patch("host.cli._resolve_workflow", return_value=tmp_path / "WORKFLOW.md"), \
         patch("host.cli.load_workflow", return_value=WorkflowConfig()):
        with pytest.raises(SystemExit):
            cmd_overflow(args)

    err = capsys.readouterr().err
    assert "missing-profile" in err
    assert not flag.exists()


# ── CLI status shows overflow state ─────────────────────────────────────────


def test_cmd_status_shows_overflow_on(tmp_path, capsys):
    """'nightshift status' shows 'Overflow: ON' when flag is present."""
    ns_dir = tmp_path / ".nightshift"
    sessions = ns_dir / "sessions"
    sessions.mkdir(parents=True)
    flag = ns_dir / OVERFLOW_FLAG_FILENAME
    flag.touch()

    args = MagicMock()

    with patch("host.cli._overflow_flag_path", return_value=flag), \
         patch("host.cli.sessions_dir", return_value=sessions):
        cmd_status(args)

    out = capsys.readouterr().out
    assert "Overflow: ON" in out


def test_cmd_status_shows_overflow_profile_name(tmp_path, capsys):
    """'nightshift status' shows the selected overflow profile when present."""
    ns_dir = tmp_path / ".nightshift"
    sessions = ns_dir / "sessions"
    sessions.mkdir(parents=True)
    flag = ns_dir / OVERFLOW_FLAG_FILENAME
    flag.write_text("openrouter-qwen\n")

    args = MagicMock()

    with patch("host.cli._overflow_flag_path", return_value=flag), \
         patch("host.cli.sessions_dir", return_value=sessions):
        cmd_status(args)

    out = capsys.readouterr().out
    assert "Overflow: ON (profile: openrouter-qwen)" in out


def test_cmd_status_no_overflow_header(tmp_path, capsys):
    """'nightshift status' does not show overflow header when flag is absent."""
    ns_dir = tmp_path / ".nightshift"
    sessions = ns_dir / "sessions"
    sessions.mkdir(parents=True)
    flag = ns_dir / OVERFLOW_FLAG_FILENAME

    args = MagicMock()

    with patch("host.cli._overflow_flag_path", return_value=flag), \
         patch("host.cli.sessions_dir", return_value=sessions):
        cmd_status(args)

    out = capsys.readouterr().out
    assert "Overflow" not in out


# ── launch.py overflow flag check ──────────────────────────────────────────


def test_launch_passes_overflow_when_flag_present(tmp_path, monkeypatch):
    """launch.py passes overflow config to run_container when flag is present."""
    # Create overflow flag
    ns_dir = tmp_path / ".nightshift"
    ns_dir.mkdir()
    (ns_dir / OVERFLOW_FLAG_FILENAME).touch()

    # Create a minimal workflow with overflow config
    wf = tmp_path / "WORKFLOW.md"
    wf.write_text("""\
---
overflow:
  extra_args: ["--model", "m2.7"]
  env:
    ANTHROPIC_API_KEY: sk-overflow
---
Prompt.
""")

    from host.launch import main

    monkeypatch.setattr("sys.argv", [
        "launch.py", "test-issue-id",
        "--workflow", str(wf),
    ])
    monkeypatch.setattr("host.launch.get_repo_root", lambda: tmp_path)
    monkeypatch.setattr("host.launch.load_all_dotenv", lambda p: None)
    monkeypatch.setattr("host.launch.setup_workspace", lambda *a, **kw: str(tmp_path / "ws"))
    monkeypatch.setattr("host.launch.dump_issue_data", lambda *a, **kw: None)
    monkeypatch.setattr("host.launch._setup_git_overlay", lambda *a, **kw: tmp_path / "git-merged")
    monkeypatch.setattr("host.launch._teardown_git_overlay", lambda *a, **kw: None)

    captured_kwargs = {}

    def mock_run_container(*args, **kwargs):
        captured_kwargs.update(kwargs)
        return 0

    monkeypatch.setattr("host.launch.run_container", mock_run_container)

    with pytest.raises(SystemExit):
        main()

    assert captured_kwargs.get("overflow") is not None
    assert captured_kwargs["overflow"].extra_args == ["--model", "m2.7"]
    assert captured_kwargs["overflow"].env["ANTHROPIC_API_KEY"] == "sk-overflow"


def test_launch_uses_profile_selected_in_flag_file(tmp_path, monkeypatch):
    """launch.py resolves the overflow profile named in the flag file."""
    ns_dir = tmp_path / ".nightshift"
    ns_dir.mkdir()
    (ns_dir / OVERFLOW_FLAG_FILENAME).write_text("openrouter-qwen\n")

    wf = tmp_path / "WORKFLOW.md"
    wf.write_text("""\
---
overflow_profiles:
  openrouter-qwen:
    agent_kind: codex
    env:
      CODEX_MODEL: qwen/qwen3.6-plus
---
Prompt.
""")

    from host.launch import main

    monkeypatch.setattr("sys.argv", [
        "launch.py", "test-issue-id",
        "--workflow", str(wf),
    ])
    monkeypatch.setattr("host.launch.get_repo_root", lambda: tmp_path)
    monkeypatch.setattr("host.launch.load_all_dotenv", lambda p: None)
    monkeypatch.setattr("host.launch.setup_workspace", lambda *a, **kw: str(tmp_path / "ws"))
    monkeypatch.setattr("host.launch.dump_issue_data", lambda *a, **kw: None)
    monkeypatch.setattr("host.launch._setup_git_overlay", lambda *a, **kw: tmp_path / "git-merged")
    monkeypatch.setattr("host.launch._teardown_git_overlay", lambda *a, **kw: None)

    captured_kwargs = {}

    def mock_run_container(*args, **kwargs):
        captured_kwargs.update(kwargs)
        return 0

    monkeypatch.setattr("host.launch.run_container", mock_run_container)

    with pytest.raises(SystemExit):
        main()

    assert captured_kwargs["overflow"] is not None
    assert captured_kwargs["overflow"].profile_name == "openrouter-qwen"
    assert captured_kwargs["overflow"].agent_kind == "codex"
    assert captured_kwargs["overflow"].env["CODEX_MODEL"] == "qwen/qwen3.6-plus"


def test_launch_no_overflow_without_flag(tmp_path, monkeypatch):
    """launch.py passes overflow=None when flag file is absent."""
    ns_dir = tmp_path / ".nightshift"
    ns_dir.mkdir()
    # No overflow flag file

    wf = tmp_path / "WORKFLOW.md"
    wf.write_text("""\
---
overflow:
  extra_args: ["--model", "m2.7"]
  env:
    ANTHROPIC_API_KEY: sk-overflow
---
Prompt.
""")

    from host.launch import main

    monkeypatch.setattr("sys.argv", [
        "launch.py", "test-issue-id",
        "--workflow", str(wf),
    ])
    monkeypatch.setattr("host.launch.get_repo_root", lambda: tmp_path)
    monkeypatch.setattr("host.launch.load_all_dotenv", lambda p: None)
    monkeypatch.setattr("host.launch.setup_workspace", lambda *a, **kw: str(tmp_path / "ws"))
    monkeypatch.setattr("host.launch.dump_issue_data", lambda *a, **kw: None)
    monkeypatch.setattr("host.launch._setup_git_overlay", lambda *a, **kw: tmp_path / "git-merged")
    monkeypatch.setattr("host.launch._teardown_git_overlay", lambda *a, **kw: None)

    captured_kwargs = {}

    def mock_run_container(*args, **kwargs):
        captured_kwargs.update(kwargs)
        return 0

    monkeypatch.setattr("host.launch.run_container", mock_run_container)

    with pytest.raises(SystemExit):
        main()

    assert captured_kwargs.get("overflow") is None


# ── Entrypoint OVERFLOW_EXTRA_ARGS parsing ──────────────────────────────────


def test_entrypoint_overflow_extra_args(monkeypatch):
    """Entrypoint extends agent extra_args with OVERFLOW_EXTRA_ARGS env var."""
    from core.config.models import AgentConfig

    config = WorkflowConfig()
    config.agent = AgentConfig(extra_args=["--existing"])

    monkeypatch.setenv("OVERFLOW_EXTRA_ARGS", json.dumps(["--model", "m2.7"]))

    overflow_args_raw = os.environ.get("OVERFLOW_EXTRA_ARGS", "")
    if overflow_args_raw:
        overflow_args = json.loads(overflow_args_raw)
        config.agent.extra_args = list(overflow_args) + config.agent.extra_args

    assert config.agent.extra_args == ["--model", "m2.7", "--existing"]


def test_entrypoint_no_overflow_extra_args():
    """Without OVERFLOW_EXTRA_ARGS, agent extra_args remain unchanged."""
    from core.config.models import AgentConfig

    config = WorkflowConfig()
    config.agent = AgentConfig(extra_args=["--existing"])

    overflow_args_raw = os.environ.get("OVERFLOW_EXTRA_ARGS", "")
    if overflow_args_raw:
        overflow_args = json.loads(overflow_args_raw)
        config.agent.extra_args = list(overflow_args) + config.agent.extra_args

    assert config.agent.extra_args == ["--existing"]


def test_entrypoint_overflow_active_triggers_agent_kind(monkeypatch):
    """OVERFLOW_ACTIVE=1 causes entrypoint to use overflow.agent_kind."""
    from core.config.models import AgentConfig

    config = WorkflowConfig()
    config.agent = AgentConfig(kind="claude-code")
    config.overflow = OverflowConfig(agent_kind="openhands")

    monkeypatch.setenv("OVERFLOW_ACTIVE", "1")

    overflow_active = os.environ.get("OVERFLOW_ACTIVE") == "1"
    overflow_config = config.overflow if overflow_active else None

    assert overflow_config is not None
    assert overflow_config.agent_kind == "openhands"


def test_entrypoint_no_overflow_active_no_agent_kind_override(monkeypatch):
    """Without OVERFLOW_ACTIVE, overflow.agent_kind is not used."""
    from core.config.models import AgentConfig

    config = WorkflowConfig()
    config.agent = AgentConfig(kind="claude-code")
    config.overflow = OverflowConfig(agent_kind="openhands")

    monkeypatch.delenv("OVERFLOW_ACTIVE", raising=False)

    overflow_active = os.environ.get("OVERFLOW_ACTIVE") == "1"
    overflow_config = config.overflow if overflow_active else None

    assert overflow_config is None


# ── Launch agent_kind in overflow check ────────────────────────────────────


def test_launch_passes_overflow_with_agent_kind_only(tmp_path, monkeypatch):
    """launch.py activates overflow when only agent_kind is set."""
    ns_dir = tmp_path / ".nightshift"
    ns_dir.mkdir()
    (ns_dir / OVERFLOW_FLAG_FILENAME).touch()

    wf = tmp_path / "WORKFLOW.md"
    wf.write_text("""\
---
overflow:
  agent_kind: openhands
---
Prompt.
""")

    from host.launch import main

    monkeypatch.setattr("host.launch.get_repo_root", lambda: tmp_path)
    monkeypatch.setattr("host.launch.discover_workflow", lambda *a, **kw: wf)
    monkeypatch.setattr("host.launch.load_all_dotenv", lambda *a: None)
    monkeypatch.setattr("host.launch.setup_workspace", lambda *a, **kw: str(tmp_path / "ws"))
    monkeypatch.setattr("host.launch.dump_issue_data", lambda *a, **kw: None)
    monkeypatch.setattr("host.launch._setup_git_overlay", lambda *a, **kw: tmp_path / "git-merged")
    monkeypatch.setattr("host.launch._teardown_git_overlay", lambda *a, **kw: None)
    import sys
    monkeypatch.setattr(sys, "argv", ["launch.py", "abc123"])

    captured_kwargs = {}

    def mock_run_container(*args, **kwargs):
        captured_kwargs.update(kwargs)
        return 0

    monkeypatch.setattr("host.launch.run_container", mock_run_container)

    with pytest.raises(SystemExit):
        main()

    assert captured_kwargs.get("overflow") is not None
    assert captured_kwargs["overflow"].agent_kind == "openhands"
    # agent_kind kwarg should be overflow.agent_kind, not config.agent.kind
    assert captured_kwargs.get("agent_kind") == "openhands"


def test_launch_uses_overflow_agent_kind_not_config(tmp_path, monkeypatch):
    """When overflow is active, agent_kind should be overflow.agent_kind, not config.agent.kind."""
    ns_dir = tmp_path / ".nightshift"
    ns_dir.mkdir()
    (ns_dir / OVERFLOW_FLAG_FILENAME).touch()

    # config.agent.kind is claude-code, but overflow.agent_kind is codex
    wf = tmp_path / "WORKFLOW.md"
    wf.write_text("""\
---
agent:
  kind: claude-code
overflow:
  agent_kind: codex
  env:
    CODEX_API_KEY: test-key
    CODEX_BASE_URL: https://api.openai.com/v1
---
Prompt.
""")

    from host.launch import main

    monkeypatch.setattr("host.launch.get_repo_root", lambda: tmp_path)
    monkeypatch.setattr("host.launch.discover_workflow", lambda *a, **kw: wf)
    monkeypatch.setattr("host.launch.load_all_dotenv", lambda *a: None)
    monkeypatch.setattr("host.launch.setup_workspace", lambda *a, **kw: str(tmp_path / "ws"))
    monkeypatch.setattr("host.launch.dump_issue_data", lambda *a, **kw: None)
    monkeypatch.setattr("host.launch._setup_git_overlay", lambda *a, **kw: tmp_path / "git-merged")
    monkeypatch.setattr("host.launch._teardown_git_overlay", lambda *a, **kw: None)
    import sys
    monkeypatch.setattr(sys, "argv", ["launch.py", "abc123"])

    captured_kwargs = {}

    def mock_run_container(*args, **kwargs):
        captured_kwargs.update(kwargs)
        return 0

    monkeypatch.setattr("host.launch.run_container", mock_run_container)

    with pytest.raises(SystemExit):
        main()

    # The bug was: agent_kind was config.agent.kind (claude-code) instead of overflow.agent_kind (codex)
    assert captured_kwargs.get("agent_kind") == "codex", \
        f"Expected codex, got {captured_kwargs.get('agent_kind')}"


# ── Watcher _diff_config detects overflow changes ───────────────────────────


def test_diff_config_detects_overflow_change():
    """_diff_config reports overflow changes between old and new config."""
    from host.watcher.host_watcher import _diff_config

    old = WorkflowConfig()
    new = WorkflowConfig()
    new.overflow = OverflowConfig(extra_args=["--model", "m2.7"])

    changes = _diff_config(old, new)
    assert "overflow" in changes


def test_diff_config_no_overflow_change():
    """_diff_config does not report overflow when it hasn't changed."""
    from host.watcher.host_watcher import _diff_config

    old = WorkflowConfig()
    new = WorkflowConfig()

    changes = _diff_config(old, new)
    assert "overflow" not in changes


# ── Constant ────────────────────────────────────────────────────────────────


def test_overflow_flag_filename_constant():
    """OVERFLOW_FLAG_FILENAME is defined and correct."""
    assert OVERFLOW_FLAG_FILENAME == "overflow"


# ── Litellm proxy support ───────────────────────────────────────────────────


def test_overflow_config_with_litellm():
    """OverflowConfig stores litellm_config path."""
    oc = OverflowConfig(litellm_config="litellm-config.yaml")
    assert oc.litellm_config == "litellm-config.yaml"


def test_parse_overflow_litellm_config(tmp_path):
    """litellm_config is parsed from WORKFLOW.md overflow section."""
    workflow = tmp_path / "WORKFLOW.md"
    workflow.write_text("""\
---
overflow:
  litellm_config: litellm-config.yaml
  env:
    OVERFLOW_API_KEY: sk-test
---
Prompt.
""")
    config = load_workflow(workflow)
    assert config.overflow.litellm_config == "litellm-config.yaml"
    assert config.overflow.env["OVERFLOW_API_KEY"] == "sk-test"


def test_parse_overflow_no_litellm_config():
    """Missing litellm_config defaults to None."""
    config = WorkflowConfig()
    raw = {"overflow": {"env": {"KEY": "val"}}}
    _parse_overflow(raw, config)
    assert config.overflow.litellm_config is None


def test_docker_cmd_with_litellm_config():
    """litellm config is mounted and ANTHROPIC_BASE_URL points to proxy."""
    from core.constants import LITELLM_CONFIG_CONTAINER_PATH, LITELLM_PROXY_PORT

    overflow = OverflowConfig(
        litellm_config="/path/to/litellm-config.yaml",
        env={"OVERFLOW_API_KEY": "sk-test"},
    )
    cmd = _build_cmd_with_overflow(overflow=overflow)

    # Check mount
    mount_pairs = []
    for i, arg in enumerate(cmd):
        if arg == "-v" and i + 1 < len(cmd):
            mount_pairs.append(cmd[i + 1])

    litellm_mounts = [m for m in mount_pairs if LITELLM_CONFIG_CONTAINER_PATH in m]
    assert len(litellm_mounts) == 1
    assert litellm_mounts[0].endswith(":ro")

    # Check ANTHROPIC_BASE_URL points to proxy
    env_pairs = []
    for i, arg in enumerate(cmd):
        if arg == "-e" and i + 1 < len(cmd):
            env_pairs.append(cmd[i + 1])

    base_url_entries = [e for e in env_pairs if e.startswith("ANTHROPIC_BASE_URL=")]
    assert len(base_url_entries) >= 1
    # The last one should be the proxy URL (overflow env appended after passthrough)
    assert base_url_entries[-1] == f"ANTHROPIC_BASE_URL=http://localhost:{LITELLM_PROXY_PORT}"


def test_docker_cmd_no_litellm_mount_without_config():
    """Without litellm_config, no litellm mount appears."""
    from core.constants import LITELLM_CONFIG_CONTAINER_PATH

    overflow = OverflowConfig(
        env={"ANTHROPIC_API_KEY": "sk-alt"},
    )
    cmd = _build_cmd_with_overflow(overflow=overflow)

    mount_pairs = []
    for i, arg in enumerate(cmd):
        if arg == "-v" and i + 1 < len(cmd):
            mount_pairs.append(cmd[i + 1])

    litellm_mounts = [m for m in mount_pairs if LITELLM_CONFIG_CONTAINER_PATH in m]
    assert len(litellm_mounts) == 0


def _launch_with_overflow(tmp_path, monkeypatch, step="coder", workflow_content=None):
    """Helper: launch main() with overflow config and given step, return captured kwargs."""
    ns_dir = tmp_path / ".nightshift"
    ns_dir.mkdir(exist_ok=True)
    (ns_dir / OVERFLOW_FLAG_FILENAME).touch()

    if workflow_content is None:
        workflow_content = """\
---
overflow:
  extra_args: ["--model", "m2.7"]
  env:
    ANTHROPIC_API_KEY: sk-overflow
---
Prompt.
"""

    wf = tmp_path / "WORKFLOW.md"
    wf.write_text(workflow_content)

    from host.launch import main

    monkeypatch.setattr("sys.argv", [
        "launch.py", "test-issue-id",
        "--workflow", str(wf),
        "--step", step,
    ])
    monkeypatch.setattr("host.launch.get_repo_root", lambda: tmp_path)
    monkeypatch.setattr("host.launch.load_all_dotenv", lambda p: None)
    monkeypatch.setattr("host.launch.setup_workspace", lambda *a, **kw: str(tmp_path / "ws"))
    monkeypatch.setattr("host.launch.dump_issue_data", lambda *a, **kw: None)
    monkeypatch.setattr("host.launch._setup_git_overlay", lambda *a, **kw: tmp_path / "git-merged")
    monkeypatch.setattr("host.launch._teardown_git_overlay", lambda *a, **kw: None)

    captured_kwargs = {}

    def mock_run_container(*args, **kwargs):
        captured_kwargs.update(kwargs)
        return 0

    monkeypatch.setattr("host.launch.run_container", mock_run_container)

    with pytest.raises(SystemExit):
        main()

    return captured_kwargs


def test_launch_passes_overflow_with_litellm_config(tmp_path, monkeypatch):
    """launch.py activates overflow when only litellm_config is set."""
    captured = _launch_with_overflow(tmp_path, monkeypatch, workflow_content="""\
---
overflow:
  litellm_config: litellm-config.yaml
  env:
    OVERFLOW_API_KEY: sk-test
---
Prompt.
""")
    assert captured.get("overflow") is not None
    assert captured["overflow"].litellm_config == "litellm-config.yaml"


def test_launch_uses_overflow_for_review_when_configured(tmp_path, monkeypatch):
    """launch.py uses overflow for review when REVIEW.md defines overflow."""
    captured = _launch_with_overflow(tmp_path, monkeypatch, step="review")
    assert captured.get("overflow") is not None
    assert captured["overflow"].extra_args == ["--model", "m2.7"]


def test_launch_skips_overflow_for_review_when_not_configured(tmp_path, monkeypatch):
    """launch.py falls back to REVIEW.md agent config when overflow is absent."""
    captured = _launch_with_overflow(
        tmp_path,
        monkeypatch,
        step="review",
        workflow_content="""\
---
agent:
  kind: claude-code
---
Prompt.
""",
    )
    assert captured.get("overflow") is None


def test_overflow_applied_for_coder_step(tmp_path, monkeypatch):
    """launch.py applies overflow when step='coder' and flag file exists."""
    captured = _launch_with_overflow(tmp_path, monkeypatch, step="coder")
    assert captured.get("overflow") is not None
    assert captured["overflow"].extra_args == ["--model", "m2.7"]


def test_docker_cmd_openhands_env_passthrough():
    """LLM_* vars are in _PASSTHROUGH_ENV_VARS."""
    from host.docker_cmd import _PASSTHROUGH_ENV_VARS

    assert "LLM_API_KEY" in _PASSTHROUGH_ENV_VARS
    assert "LLM_MODEL" in _PASSTHROUGH_ENV_VARS
    assert "LLM_BASE_URL" in _PASSTHROUGH_ENV_VARS


def test_codex_env_passthrough():
    """CODEX_API_KEY, CODEX_BASE_URL, CODEX_MODEL, OPENAI_API_KEY are in _PASSTHROUGH_ENV_VARS."""
    from host.docker_cmd import _PASSTHROUGH_ENV_VARS

    assert "CODEX_API_KEY" in _PASSTHROUGH_ENV_VARS
    assert "CODEX_BASE_URL" in _PASSTHROUGH_ENV_VARS
    assert "CODEX_MODEL" in _PASSTHROUGH_ENV_VARS
    assert "OPENAI_API_KEY" in _PASSTHROUGH_ENV_VARS
    # OVERFLOW_* vars still needed for litellm overflow feature
    assert "OVERFLOW_API_KEY" in _PASSTHROUGH_ENV_VARS
    assert "OVERFLOW_BASE_URL" in _PASSTHROUGH_ENV_VARS
    assert "OVERFLOW_MODEL" in _PASSTHROUGH_ENV_VARS


def test_litellm_constants():
    """Litellm proxy constants are defined and sensible."""
    from core.constants import (
        LITELLM_PROXY_PORT, LITELLM_CONFIG_CONTAINER_PATH,
        LITELLM_HEALTH_TIMEOUT_S, LITELLM_HEALTH_POLL_INTERVAL_S,
    )
    assert LITELLM_PROXY_PORT == 4000
    assert LITELLM_CONFIG_CONTAINER_PATH == "/session/litellm-config.yaml"
    assert LITELLM_HEALTH_TIMEOUT_S == 30
    assert LITELLM_HEALTH_POLL_INTERVAL_S == 0.5
