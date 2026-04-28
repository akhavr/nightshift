"""Tests for core/config/loader.py."""

import pytest

from core.config.loader import load_profiles, load_workflow


def test_load_profiles_from_file(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-openai")
    profiles_dir = tmp_path / ".nightshift"
    profiles_dir.mkdir()
    (profiles_dir / "profiles.yaml").write_text("""\
codex-gpt54:
  agent_kind: codex
  skip_oauth: true
  env:
    CODEX_MODEL: gpt-5.4
opencode-gpt54mini:
  agent_kind: opencode
  extra_args: ["-m", "openai/gpt-5.4-mini"]
  env:
    OPENAI_API_KEY: $OPENAI_API_KEY
  prompt_snippet: |
    ## Signal Protocol
    Run `touch /session/signal/done` when finished.
""")

    profiles = load_profiles(tmp_path)

    assert profiles["codex-gpt54"].agent_kind == "codex"
    assert profiles["codex-gpt54"].skip_oauth is True
    assert profiles["codex-gpt54"].env["CODEX_MODEL"] == "gpt-5.4"
    assert profiles["opencode-gpt54mini"].extra_args == ["-m", "openai/gpt-5.4-mini"]
    assert profiles["opencode-gpt54mini"].env["OPENAI_API_KEY"] == "sk-openai"
    assert profiles["opencode-gpt54mini"].prompt_snippet == (
        "## Signal Protocol\nRun `touch /session/signal/done` when finished.\n"
    )


def test_profiles_file_overridden_by_workflow(tmp_path):
    profiles_dir = tmp_path / ".nightshift"
    profiles_dir.mkdir()
    (profiles_dir / "profiles.yaml").write_text("""\
codex-gpt54:
  agent_kind: codex
  env:
    CODEX_MODEL: gpt-5.4
""")

    workflow = tmp_path / "WORKFLOW.md"
    workflow.write_text("""\
---
overflow_profiles:
  codex-gpt54:
    agent_kind: codex
    extra_args: ["--model", "workflow"]
    env:
      CODEX_MODEL: gpt-5.4-override
overflow: codex-gpt54
---
Prompt.
""")

    config = load_workflow(workflow, repo_root=tmp_path)

    assert config.overflow.profile_name == "codex-gpt54"
    assert config.overflow.extra_args == ["--model", "workflow"]
    assert config.overflow.env["CODEX_MODEL"] == "gpt-5.4-override"
    assert config.overflow.profiles["codex-gpt54"].extra_args == ["--model", "workflow"]


def test_missing_profile_error_message(tmp_path):
    profiles_dir = tmp_path / ".nightshift"
    profiles_dir.mkdir()
    (profiles_dir / "profiles.yaml").write_text("""\
codex-gpt54:
  agent_kind: codex
""")

    workflow = tmp_path / "WORKFLOW.md"
    workflow.write_text("""\
---
overflow: missing-profile
---
Prompt.
""")

    with pytest.raises(ValueError) as excinfo:
        load_workflow(workflow, repo_root=tmp_path)

    message = str(excinfo.value)
    assert "Unknown overflow profile 'missing-profile'" in message
    assert ".nightshift/profiles.yaml" in message


def test_profiles_file_used_when_workflow_lacks_profile(tmp_path):
    profiles_dir = tmp_path / ".nightshift"
    profiles_dir.mkdir()
    (profiles_dir / "profiles.yaml").write_text("""\
codex-gpt54:
  agent_kind: codex
  env:
    CODEX_MODEL: gpt-5.4
""")

    workflow = tmp_path / "WORKFLOW.md"
    workflow.write_text("""\
---
overflow: codex-gpt54
---
Prompt.
""")

    config = load_workflow(workflow, repo_root=tmp_path)

    assert config.overflow.profile_name == "codex-gpt54"
    assert config.overflow.agent_kind == "codex"
    assert config.overflow.env["CODEX_MODEL"] == "gpt-5.4"


def test_prompt_snippet_empty_by_default():
    from core.config.models import OverflowProfile

    profile = OverflowProfile()

    assert profile.prompt_snippet is None
