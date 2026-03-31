"""Tests for template versioning and upgrade logic (REQ-024)."""

import subprocess
import sys
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.upgrade import (
    read_template_version,
    get_canonical_version,
    get_canonical_review_version,
    get_prompt_section,
    get_yaml_section,
    diff_prompt_sections,
    apply_upgrade,
    load_canonical_template,
    load_canonical_review_template,
    _set_version_in_yaml,
    TemplateVersion,
    CANONICAL_TEMPLATE,
    CANONICAL_REVIEW_TEMPLATE,
    DEFAULT_VERSION,
    VERSION_KEY,
)


# ── read_template_version ────────────────────────────────────────────────────


class TestReadTemplateVersion:
    def test_reads_version_from_front_matter(self):
        text = "---\ntemplate_version: 3\nagent:\n  kind: claude-code\n---\nprompt"
        assert read_template_version(text) == TemplateVersion(3, 0)

    def test_reads_dotted_version(self):
        text = "---\ntemplate_version: 2.1\nagent:\n  kind: claude-code\n---\nprompt"
        assert read_template_version(text) == TemplateVersion(2, 1)

    def test_missing_version_returns_zero(self):
        text = "---\nagent:\n  kind: claude-code\n---\nprompt"
        assert read_template_version(text) == TemplateVersion(0, 0)

    def test_no_front_matter_returns_zero(self):
        text = "Just a plain prompt with no YAML."
        assert read_template_version(text) == TemplateVersion(0, 0)

    def test_malformed_yaml_returns_zero(self):
        text = "---\n: invalid: yaml: [[[[\n---\nprompt"
        assert read_template_version(text) == TemplateVersion(0, 0)

    def test_non_dict_front_matter_returns_zero(self):
        text = "---\n- list\n- item\n---\nprompt"
        assert read_template_version(text) == TemplateVersion(0, 0)

    def test_non_int_version_returns_zero(self):
        text = "---\ntemplate_version: abc\n---\nprompt"
        assert read_template_version(text) == TemplateVersion(0, 0)


# ── get_canonical_version ────────────────────────────────────────────────────


class TestGetCanonicalVersion:
    def test_reads_from_shipped_template(self):
        version = get_canonical_version()
        assert version >= TemplateVersion(1, 0)

    def test_missing_template_returns_zero(self):
        with patch("core.upgrade.CANONICAL_TEMPLATE", Path("/nonexistent/WORKFLOW.md")):
            assert get_canonical_version() == TemplateVersion(0, 0)


# ── get_prompt_section / get_yaml_section ────────────────────────────────────


class TestSections:
    def test_prompt_section_extracted(self):
        text = "---\nkey: val\n---\nthe prompt body"
        assert get_prompt_section(text) == "\nthe prompt body"

    def test_yaml_section_extracted(self):
        text = "---\nkey: val\n---\nthe prompt body"
        assert get_yaml_section(text) == "\nkey: val\n"

    def test_no_front_matter(self):
        text = "just a prompt"
        assert get_prompt_section(text) == "just a prompt"
        assert get_yaml_section(text) == ""


# ── load_canonical_template ───────────────────────────────────────────────────


class TestLoadCanonicalTemplate:
    def test_returns_canonical_content(self):
        result = load_canonical_template()
        assert "template_version:" in result
        assert "base_branch: main" in result

    def test_substitutes_base_branch(self):
        result = load_canonical_template("develop")
        assert "base_branch: develop" in result
        assert "base_branch: main" not in result

    def test_missing_template_returns_empty(self):
        with patch("core.upgrade.CANONICAL_TEMPLATE", Path("/nonexistent/WORKFLOW.md")):
            assert load_canonical_template() == ""


# ── diff_prompt_sections ─────────────────────────────────────────────────────


class TestDiffPromptSections:
    def test_identical_prompts_empty_diff(self):
        text = "---\nkey: val\n---\nsame prompt"
        assert diff_prompt_sections(text, text) == ""

    def test_different_prompts_show_diff(self):
        project = "---\nkey: val\n---\nold prompt line"
        canonical = "---\nkey: val\n---\nnew prompt line"
        diff = diff_prompt_sections(project, canonical)
        assert "-old prompt line" in diff
        assert "+new prompt line" in diff
        assert "---" in diff  # unified diff header

    def test_diff_ignores_yaml_differences(self):
        project = "---\nagent: foo\n---\nsame prompt"
        canonical = "---\nagent: bar\n---\nsame prompt"
        assert diff_prompt_sections(project, canonical) == ""

    def test_label_appears_in_diff_header(self):
        project = "---\nkey: val\n---\nold prompt"
        canonical = "---\nkey: val\n---\nnew prompt"
        diff = diff_prompt_sections(project, canonical, label="REVIEW.md")
        assert "current REVIEW.md (prompt section)" in diff
        assert "WORKFLOW.md" not in diff


# ── _set_version_in_yaml ─────────────────────────────────────────────────────


class TestSetVersionInYaml:
    def test_update_existing_version(self):
        yaml_text = "\ntemplate_version: 1\nagent:\n  kind: claude-code\n"
        result = _set_version_in_yaml(yaml_text, 2)
        assert "template_version: 2\n" in result
        assert "template_version: 1" not in result

    def test_insert_missing_version(self):
        yaml_text = "\nagent:\n  kind: claude-code\n"
        result = _set_version_in_yaml(yaml_text, 1)
        assert "template_version: 1\n" in result

    def test_preserves_other_fields(self):
        yaml_text = "\ntemplate_version: 1\nagent:\n  kind: claude-code\ntracker:\n  kind: git-bug\n"
        result = _set_version_in_yaml(yaml_text, 5)
        assert "agent:" in result
        assert "tracker:" in result
        assert "template_version: 5" in result


# ── apply_upgrade ─────────────────────────────────────────────────────────────


class TestApplyUpgrade:
    def test_preserves_yaml_config(self):
        project = "---\nagent:\n  kind: claude-code\n  max_turns: 99\n---\nold prompt"
        canonical = "---\ntemplate_version: 2\nagent:\n  kind: claude-code\n---\nnew prompt"
        result = apply_upgrade(project, canonical)
        assert "max_turns: 99" in result
        assert "template_version: 2.0" in result
        assert "new prompt" in result
        assert "old prompt" not in result

    def test_bumps_version(self):
        project = "---\ntemplate_version: 1\nagent:\n  kind: claude-code\n---\nold"
        canonical = "---\ntemplate_version: 3\nagent:\n  kind: claude-code\n---\nnew"
        result = apply_upgrade(project, canonical)
        assert "template_version: 3.0" in result
        assert "template_version: 1" not in result

    def test_adds_version_when_missing(self):
        project = "---\nagent:\n  kind: claude-code\n---\nold"
        canonical = "---\ntemplate_version: 1\nagent:\n  kind: claude-code\n---\nnew"
        result = apply_upgrade(project, canonical)
        assert "template_version: 1.0" in result

    def test_prompt_section_fully_replaced(self):
        project = "---\nv: 0\n---\nold alpha\nold beta\nold gamma"
        canonical = "---\ntemplate_version: 1\n---\nnew alpha\nnew beta"
        result = apply_upgrade(project, canonical)
        assert "old alpha" not in result
        assert "old beta" not in result
        assert "new alpha" in result
        assert "new beta" in result

    def test_round_trip_structure(self):
        """Upgraded file should still have valid ---yaml---prompt structure."""
        project = "---\nagent:\n  kind: claude-code\n---\nold prompt"
        canonical = "---\ntemplate_version: 1\n---\nnew prompt"
        result = apply_upgrade(project, canonical)
        assert result.startswith("---")
        parts = result.split("---", 2)
        assert len(parts) == 3


# ── CLI cmd_upgrade ──────────────────────────────────────────────────────────


class TestCmdUpgrade:
    def test_dry_run_shows_diff(self, tmp_path, capsys):
        from host.cli import cmd_upgrade

        workflow = tmp_path / "WORKFLOW.md"
        workflow.write_text("---\nagent:\n  kind: claude-code\n---\nold prompt")

        canonical = tmp_path / "canonical.md"
        canonical.write_text("---\ntemplate_version: 1\n---\nnew prompt")

        args = MagicMock()
        args.apply = False
        args.workflow = str(workflow)

        with patch("host.cli.repo_root", return_value=tmp_path), \
             patch("host.cli._resolve_workflow", return_value=workflow), \
             patch("host.cli.CANONICAL_TEMPLATE", canonical):
            cmd_upgrade(args)

        out = capsys.readouterr().out
        assert "0.0 -> 1.0" in out
        assert "old prompt" in out or "new prompt" in out

    def test_apply_writes_file(self, tmp_path, capsys):
        from host.cli import cmd_upgrade

        workflow = tmp_path / "WORKFLOW.md"
        workflow.write_text("---\nagent:\n  kind: claude-code\n---\nold prompt")

        canonical = tmp_path / "canonical.md"
        canonical.write_text("---\ntemplate_version: 1\n---\nnew prompt")

        args = MagicMock()
        args.apply = True
        args.force = True
        args.workflow = str(workflow)

        with patch("host.cli.repo_root", return_value=tmp_path), \
             patch("host.cli._resolve_workflow", return_value=workflow), \
             patch("host.cli.CANONICAL_TEMPLATE", canonical):
            cmd_upgrade(args)

        updated = workflow.read_text()
        assert "template_version: 1.0" in updated
        assert "new prompt" in updated
        assert "max_turns" not in updated  # canonical's yaml not used
        assert "kind: claude-code" in updated  # project's yaml preserved

    def test_not_in_git_repo(self, tmp_path, capsys):
        from host.cli import cmd_upgrade

        args = MagicMock()
        args.apply = False
        args.workflow = str(tmp_path / "WORKFLOW.md")

        with patch("host.cli.repo_root", side_effect=subprocess.CalledProcessError(1, "git")):
            with pytest.raises(SystemExit) as exc_info:
                cmd_upgrade(args)
            assert exc_info.value.code == 1

        err = capsys.readouterr().err
        assert "Not inside a git repository" in err

    def test_workflow_not_found(self, tmp_path, capsys):
        from host.cli import cmd_upgrade

        workflow = tmp_path / "WORKFLOW.md"  # does not exist

        args = MagicMock()
        args.apply = False
        args.workflow = str(workflow)

        with patch("host.cli.repo_root", return_value=tmp_path), \
             patch("host.cli._resolve_workflow", return_value=workflow):
            with pytest.raises(SystemExit) as exc_info:
                cmd_upgrade(args)
            assert exc_info.value.code == 1

        err = capsys.readouterr().err
        assert "not found" in err

    def test_canonical_template_not_found(self, tmp_path, capsys):
        from host.cli import cmd_upgrade

        workflow = tmp_path / "WORKFLOW.md"
        workflow.write_text("---\nagent:\n  kind: claude-code\n---\nprompt")

        args = MagicMock()
        args.apply = False
        args.workflow = str(workflow)

        with patch("host.cli.repo_root", return_value=tmp_path), \
             patch("host.cli._resolve_workflow", return_value=workflow), \
             patch("host.cli.CANONICAL_TEMPLATE", Path("/nonexistent/WORKFLOW.md")):
            with pytest.raises(SystemExit) as exc_info:
                cmd_upgrade(args)
            assert exc_info.value.code == 1

        err = capsys.readouterr().err
        assert "Canonical template not found" in err

    def test_up_to_date(self, tmp_path, capsys):
        from host.cli import cmd_upgrade

        workflow = tmp_path / "WORKFLOW.md"
        workflow.write_text("---\ntemplate_version: 1\n---\nprompt")

        canonical = tmp_path / "canonical.md"
        canonical.write_text("---\ntemplate_version: 1\n---\nprompt")

        args = MagicMock()
        args.apply = False
        args.workflow = str(workflow)

        with patch("host.cli.repo_root", return_value=tmp_path), \
             patch("host.cli._resolve_workflow", return_value=workflow), \
             patch("host.cli.CANONICAL_TEMPLATE", canonical):
            cmd_upgrade(args)

        out = capsys.readouterr().out
        assert "up to date" in out


# ── CLI cmd_init version hint ────────────────────────────────────────────────


class TestInitVersionHint:
    def test_init_warns_when_behind(self, tmp_path, capsys):
        from host.cli import cmd_init

        repo = tmp_path / "repo"
        repo.mkdir()
        # Set up a git repo
        import subprocess
        subprocess.run(["git", "init"], cwd=str(repo), capture_output=True)
        subprocess.run(["git", "config", "user.email", "t@t.com"], cwd=str(repo), capture_output=True)
        subprocess.run(["git", "config", "user.name", "T"], cwd=str(repo), capture_output=True)
        (repo / "f.txt").write_text("x")
        subprocess.run(["git", "add", "."], cwd=str(repo), capture_output=True)
        subprocess.run(["git", "commit", "-m", "init"], cwd=str(repo), capture_output=True)

        # Create existing WORKFLOW.md with no version
        (repo / "WORKFLOW.md").write_text("---\nagent:\n  kind: claude-code\n---\nprompt")

        args = MagicMock()
        args.force = False
        args.workflow_path = None
        args.workflow = None

        with patch("host.cli.repo_root", return_value=repo), \
             patch("host.cli.get_canonical_version", return_value=TemplateVersion(1, 0)):
            cmd_init(args)

        out = capsys.readouterr().out
        assert "nightshift upgrade" in out

    def test_init_no_warn_when_current(self, tmp_path, capsys):
        from host.cli import cmd_init

        repo = tmp_path / "repo"
        repo.mkdir()
        import subprocess
        subprocess.run(["git", "init"], cwd=str(repo), capture_output=True)
        subprocess.run(["git", "config", "user.email", "t@t.com"], cwd=str(repo), capture_output=True)
        subprocess.run(["git", "config", "user.name", "T"], cwd=str(repo), capture_output=True)
        (repo / "f.txt").write_text("x")
        subprocess.run(["git", "add", "."], cwd=str(repo), capture_output=True)
        subprocess.run(["git", "commit", "-m", "init"], cwd=str(repo), capture_output=True)

        # Create existing WORKFLOW.md with current version
        (repo / "WORKFLOW.md").write_text("---\ntemplate_version: 1\nagent:\n  kind: claude-code\n---\nprompt")

        args = MagicMock()
        args.force = False
        args.workflow_path = None
        args.workflow = None

        with patch("host.cli.repo_root", return_value=repo), \
             patch("host.cli.get_canonical_version", return_value=TemplateVersion(1, 0)):
            cmd_init(args)

        out = capsys.readouterr().out
        assert "nightshift upgrade" not in out


# ── get_canonical_review_version ──────────────────────────────────────────


class TestGetCanonicalReviewVersion:
    def test_reads_from_shipped_template(self):
        version = get_canonical_review_version()
        assert version >= TemplateVersion(1, 0)

    def test_missing_template_returns_zero(self):
        with patch("core.upgrade.CANONICAL_REVIEW_TEMPLATE", Path("/nonexistent/REVIEW.md")):
            assert get_canonical_review_version() == TemplateVersion(0, 0)


# ── load_canonical_review_template ────────────────────────────────────────


class TestLoadCanonicalReviewTemplate:
    def test_returns_canonical_content(self):
        result = load_canonical_review_template()
        assert "template_version:" in result
        assert "code reviewer" in result.lower()

    def test_missing_template_returns_empty(self):
        with patch("core.upgrade.CANONICAL_REVIEW_TEMPLATE", Path("/nonexistent/REVIEW.md")):
            assert load_canonical_review_template() == ""


# ── cmd_upgrade with REVIEW.md ────────────────────────────────────────────


class TestCmdUpgradeReview:
    def test_upgrades_review_md_alongside_workflow(self, tmp_path, capsys):
        from host.cli import cmd_upgrade

        workflow = tmp_path / "WORKFLOW.md"
        workflow.write_text("---\ntemplate_version: 1\n---\nprompt")

        review = tmp_path / "REVIEW.md"
        review.write_text("---\nagent:\n  kind: claude-code\n---\nold review prompt")

        canonical_wf = tmp_path / "canonical_wf.md"
        canonical_wf.write_text("---\ntemplate_version: 1\n---\nprompt")

        canonical_rv = tmp_path / "canonical_rv.md"
        canonical_rv.write_text("---\ntemplate_version: 1\n---\nnew review prompt")

        args = MagicMock()
        args.apply = True
        args.force = True
        args.workflow = str(workflow)

        with patch("host.cli.repo_root", return_value=tmp_path), \
             patch("host.cli._resolve_workflow", return_value=workflow), \
             patch("host.cli.CANONICAL_TEMPLATE", canonical_wf), \
             patch("host.cli.CANONICAL_REVIEW_TEMPLATE", canonical_rv):
            cmd_upgrade(args)

        updated = review.read_text()
        assert "template_version: 1.0" in updated
        assert "new review prompt" in updated
        assert "old review prompt" not in updated

    def test_review_dry_run_shows_diff(self, tmp_path, capsys):
        from host.cli import cmd_upgrade

        workflow = tmp_path / "WORKFLOW.md"
        workflow.write_text("---\ntemplate_version: 1\n---\nprompt")

        review = tmp_path / "REVIEW.md"
        review.write_text("---\nagent:\n  kind: claude-code\n---\nold review")

        canonical_wf = tmp_path / "canonical_wf.md"
        canonical_wf.write_text("---\ntemplate_version: 1\n---\nprompt")

        canonical_rv = tmp_path / "canonical_rv.md"
        canonical_rv.write_text("---\ntemplate_version: 1\n---\nnew review")

        args = MagicMock()
        args.apply = False
        args.workflow = str(workflow)

        with patch("host.cli.repo_root", return_value=tmp_path), \
             patch("host.cli._resolve_workflow", return_value=workflow), \
             patch("host.cli.CANONICAL_TEMPLATE", canonical_wf), \
             patch("host.cli.CANONICAL_REVIEW_TEMPLATE", canonical_rv):
            cmd_upgrade(args)

        out = capsys.readouterr().out
        assert "REVIEW.md" in out
        assert "0.0 -> 1.0" in out
        assert "nightshift upgrade --apply" in out

    def test_review_skipped_when_no_review_file(self, tmp_path, capsys):
        """When REVIEW.md does not exist, upgrade skips it silently."""
        from host.cli import cmd_upgrade

        workflow = tmp_path / "WORKFLOW.md"
        workflow.write_text("---\ntemplate_version: 1\n---\nprompt")

        canonical_wf = tmp_path / "canonical_wf.md"
        canonical_wf.write_text("---\ntemplate_version: 1\n---\nprompt")

        canonical_rv = tmp_path / "canonical_rv.md"
        canonical_rv.write_text("---\ntemplate_version: 1\n---\nnew review")

        args = MagicMock()
        args.apply = False
        args.workflow = str(workflow)

        with patch("host.cli.repo_root", return_value=tmp_path), \
             patch("host.cli._resolve_workflow", return_value=workflow), \
             patch("host.cli.CANONICAL_TEMPLATE", canonical_wf), \
             patch("host.cli.CANONICAL_REVIEW_TEMPLATE", canonical_rv):
            cmd_upgrade(args)

        out = capsys.readouterr().out
        # REVIEW.md should not appear in output since the file doesn't exist
        assert "REVIEW.md" not in out

    def test_review_preserves_yaml_config(self, tmp_path, capsys):
        """Upgrade preserves project-specific YAML in REVIEW.md."""
        from host.cli import cmd_upgrade

        workflow = tmp_path / "WORKFLOW.md"
        workflow.write_text("---\ntemplate_version: 1\n---\nprompt")

        review = tmp_path / "REVIEW.md"
        review.write_text("---\nagent:\n  max_turns: 99\nreview:\n  max_rounds: 10\n---\nold")

        canonical_wf = tmp_path / "canonical_wf.md"
        canonical_wf.write_text("---\ntemplate_version: 1\n---\nprompt")

        canonical_rv = tmp_path / "canonical_rv.md"
        canonical_rv.write_text("---\ntemplate_version: 1\n---\nnew")

        args = MagicMock()
        args.apply = True
        args.force = True
        args.workflow = str(workflow)

        with patch("host.cli.repo_root", return_value=tmp_path), \
             patch("host.cli._resolve_workflow", return_value=workflow), \
             patch("host.cli.CANONICAL_TEMPLATE", canonical_wf), \
             patch("host.cli.CANONICAL_REVIEW_TEMPLATE", canonical_rv):
            cmd_upgrade(args)

        updated = review.read_text()
        assert "max_turns: 99" in updated
        assert "max_rounds: 10" in updated
        assert "new" in updated
        assert "old" not in updated

    def test_both_up_to_date(self, tmp_path, capsys):
        """When both files are up to date, no --apply hint is shown."""
        from host.cli import cmd_upgrade

        workflow = tmp_path / "WORKFLOW.md"
        workflow.write_text("---\ntemplate_version: 1\n---\nprompt")

        review = tmp_path / "REVIEW.md"
        review.write_text("---\ntemplate_version: 1\n---\nreview prompt")

        canonical_wf = tmp_path / "canonical_wf.md"
        canonical_wf.write_text("---\ntemplate_version: 1\n---\nprompt")

        canonical_rv = tmp_path / "canonical_rv.md"
        canonical_rv.write_text("---\ntemplate_version: 1\n---\nreview prompt")

        args = MagicMock()
        args.apply = False
        args.workflow = str(workflow)

        with patch("host.cli.repo_root", return_value=tmp_path), \
             patch("host.cli._resolve_workflow", return_value=workflow), \
             patch("host.cli.CANONICAL_TEMPLATE", canonical_wf), \
             patch("host.cli.CANONICAL_REVIEW_TEMPLATE", canonical_rv):
            cmd_upgrade(args)

        out = capsys.readouterr().out
        assert "up to date" in out
        assert "--apply" not in out


# ── cmd_init with canonical review template ───────────────────────────────


class TestInitReviewTemplate:
    def test_init_uses_canonical_review_template(self, tmp_path, capsys):
        from host.cli import cmd_init

        repo = tmp_path / "repo"
        repo.mkdir()
        import subprocess
        subprocess.run(["git", "init"], cwd=str(repo), capture_output=True)
        subprocess.run(["git", "config", "user.email", "t@t.com"], cwd=str(repo), capture_output=True)
        subprocess.run(["git", "config", "user.name", "T"], cwd=str(repo), capture_output=True)
        (repo / "f.txt").write_text("x")
        subprocess.run(["git", "add", "."], cwd=str(repo), capture_output=True)
        subprocess.run(["git", "commit", "-m", "init"], cwd=str(repo), capture_output=True)

        args = MagicMock()
        args.force = False
        args.workflow_path = None
        args.workflow = None

        with patch("host.cli.repo_root", return_value=repo):
            cmd_init(args)

        review_content = (repo / "REVIEW.md").read_text()
        assert "template_version:" in review_content

    def test_init_warns_review_behind(self, tmp_path, capsys):
        from host.cli import cmd_init

        repo = tmp_path / "repo"
        repo.mkdir()
        import subprocess
        subprocess.run(["git", "init"], cwd=str(repo), capture_output=True)
        subprocess.run(["git", "config", "user.email", "t@t.com"], cwd=str(repo), capture_output=True)
        subprocess.run(["git", "config", "user.name", "T"], cwd=str(repo), capture_output=True)
        (repo / "f.txt").write_text("x")
        subprocess.run(["git", "add", "."], cwd=str(repo), capture_output=True)
        subprocess.run(["git", "commit", "-m", "init"], cwd=str(repo), capture_output=True)

        # Create existing REVIEW.md with no version
        (repo / "REVIEW.md").write_text("---\nagent:\n  kind: claude-code\n---\nold review")

        args = MagicMock()
        args.force = False
        args.workflow_path = None
        args.workflow = None

        with patch("host.cli.repo_root", return_value=repo), \
             patch("host.cli.get_canonical_review_version", return_value=TemplateVersion(1, 0)):
            cmd_init(args)

        out = capsys.readouterr().out
        assert "REVIEW.md template is behind" in out


# ── TemplateVersion ────────────────────────────────────────────────────────


class TestTemplateVersion:
    def test_parse_int(self):
        v = TemplateVersion.parse(3)
        assert v == TemplateVersion(3, 0)

    def test_parse_dotted_string(self):
        v = TemplateVersion.parse("2.1")
        assert v == TemplateVersion(2, 1)

    def test_parse_none(self):
        v = TemplateVersion.parse(None)
        assert v == TemplateVersion(0, 0)

    def test_parse_float(self):
        v = TemplateVersion.parse(1.2)
        assert v == TemplateVersion(1, 2)

    def test_str(self):
        assert str(TemplateVersion(2, 1)) == "2.1"
        assert str(TemplateVersion(0, 0)) == "0.0"

    def test_comparison(self):
        assert TemplateVersion(1, 0) < TemplateVersion(2, 0)
        assert TemplateVersion(1, 0) < TemplateVersion(1, 1)
        assert TemplateVersion(2, 0) > TemplateVersion(1, 9)
        assert TemplateVersion(1, 0) >= TemplateVersion(1, 0)
        assert TemplateVersion(1, 1) <= TemplateVersion(2, 0)

    def test_is_major_bump_from(self):
        assert TemplateVersion(2, 0).is_major_bump_from(TemplateVersion(1, 0))
        assert TemplateVersion(2, 0).is_major_bump_from(TemplateVersion(1, 5))
        assert not TemplateVersion(1, 1).is_major_bump_from(TemplateVersion(1, 0))
        assert not TemplateVersion(1, 0).is_major_bump_from(TemplateVersion(1, 0))

    def test_backward_compat_int_version(self):
        """Integer versions (legacy) should be treated as N.0."""
        text = "---\ntemplate_version: 1\n---\nprompt"
        v = read_template_version(text)
        assert v == TemplateVersion(1, 0)
        assert v < TemplateVersion(1, 1)
        assert v < TemplateVersion(2, 0)


# ── Major bump blocking ─────────────────────────────────────────────────────


class TestMajorBumpBlocking:
    def test_major_bump_requires_force(self, tmp_path, capsys):
        """Major version bump with --apply but no --force should not write."""
        from host.cli import cmd_upgrade

        workflow = tmp_path / "WORKFLOW.md"
        workflow.write_text("---\ntemplate_version: 1.0\n---\nold prompt")

        canonical = tmp_path / "canonical.md"
        canonical.write_text("---\ntemplate_version: 2.0\n---\nnew prompt")

        args = MagicMock()
        args.apply = True
        args.force = False
        args.workflow = str(workflow)

        with patch("host.cli.repo_root", return_value=tmp_path), \
             patch("host.cli._resolve_workflow", return_value=workflow), \
             patch("host.cli.CANONICAL_TEMPLATE", canonical):
            cmd_upgrade(args)

        # File should NOT be modified
        assert "old prompt" in workflow.read_text()
        err = capsys.readouterr().err
        assert "--force" in err

    def test_major_bump_with_force_applies(self, tmp_path, capsys):
        """Major version bump with --apply --force should write."""
        from host.cli import cmd_upgrade

        workflow = tmp_path / "WORKFLOW.md"
        workflow.write_text("---\ntemplate_version: 1.0\n---\nold prompt")

        canonical = tmp_path / "canonical.md"
        canonical.write_text("---\ntemplate_version: 2.0\n---\nnew prompt")

        args = MagicMock()
        args.apply = True
        args.force = True
        args.workflow = str(workflow)

        with patch("host.cli.repo_root", return_value=tmp_path), \
             patch("host.cli._resolve_workflow", return_value=workflow), \
             patch("host.cli.CANONICAL_TEMPLATE", canonical):
            cmd_upgrade(args)

        updated = workflow.read_text()
        assert "new prompt" in updated
        assert "template_version: 2.0" in updated

    def test_minor_bump_applies_without_force(self, tmp_path, capsys):
        """Minor version bump should apply with just --apply."""
        from host.cli import cmd_upgrade

        workflow = tmp_path / "WORKFLOW.md"
        workflow.write_text("---\ntemplate_version: 1.0\n---\nold prompt")

        canonical = tmp_path / "canonical.md"
        canonical.write_text("---\ntemplate_version: 1.1\n---\nnew prompt")

        args = MagicMock()
        args.apply = True
        args.force = False
        args.workflow = str(workflow)

        with patch("host.cli.repo_root", return_value=tmp_path), \
             patch("host.cli._resolve_workflow", return_value=workflow), \
             patch("host.cli.CANONICAL_TEMPLATE", canonical):
            cmd_upgrade(args)

        updated = workflow.read_text()
        assert "new prompt" in updated
        assert "template_version: 1.1" in updated

    def test_major_bump_shows_warning(self, tmp_path, capsys):
        """Major version bump should show a WARNING in output."""
        from host.cli import cmd_upgrade

        workflow = tmp_path / "WORKFLOW.md"
        workflow.write_text("---\ntemplate_version: 1.0\n---\nold prompt")

        canonical = tmp_path / "canonical.md"
        canonical.write_text("---\ntemplate_version: 2.0\n---\nnew prompt")

        args = MagicMock()
        args.apply = False
        args.force = False
        args.workflow = str(workflow)

        with patch("host.cli.repo_root", return_value=tmp_path), \
             patch("host.cli._resolve_workflow", return_value=workflow), \
             patch("host.cli.CANONICAL_TEMPLATE", canonical):
            cmd_upgrade(args)

        out = capsys.readouterr().out
        assert "MAJOR" in out
        assert "WARNING" in out


# ── Consolidation trigger ────────────────────────────────────────────────────


class TestConsolidationTrigger:
    def test_upgrade_warns_when_over_soft_cap(self, tmp_path, capsys):
        """Upgrade should warn about consolidation when exceeding soft cap."""
        from host.cli import cmd_upgrade
        from core.upstream import PROMPT_SOFT_CAP_LINES

        prompt_lines = "\n".join(f"line {i}" for i in range(PROMPT_SOFT_CAP_LINES + 5))
        workflow = tmp_path / "WORKFLOW.md"
        workflow.write_text("---\ntemplate_version: 1.0\n---\nold")

        canonical = tmp_path / "canonical.md"
        canonical.write_text(f"---\ntemplate_version: 1.1\n---\n{prompt_lines}")

        args = MagicMock()
        args.apply = False
        args.force = False
        args.workflow = str(workflow)

        with patch("host.cli.repo_root", return_value=tmp_path), \
             patch("host.cli._resolve_workflow", return_value=workflow), \
             patch("host.cli.CANONICAL_TEMPLATE", canonical):
            cmd_upgrade(args)

        out = capsys.readouterr().out
        assert "consider consolidation" in out.lower()
