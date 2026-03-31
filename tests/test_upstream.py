"""Tests for upstream proposal logic (REQ-027)."""

import sys
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.upstream import (
    diff_reverse,
    detect_operation,
    extract_jinja2_vars,
    count_prompt_lines,
    validate_no_blocklist_terms,
    validate_jinja2_vars,
    validate_line_count,
    validate_proposal,
    build_proposal,
    load_blocklist,
    UpstreamProposal,
    PROMPT_HARD_CAP_LINES,
    PROMPT_SOFT_CAP_LINES,
    KNOWN_JINJA2_VARS,
)


# ── diff_reverse ─────────────────────────────────────────────────────────────


class TestDiffReverse:
    def test_identical_returns_empty(self):
        text = "---\nkey: val\n---\nsame prompt"
        assert diff_reverse(text, text) == ""

    def test_shows_project_additions(self):
        canonical = "---\nkey: val\n---\ncanonical line"
        project = "---\nkey: val\n---\ncanonical line\nnew project line"
        diff = diff_reverse(project, canonical)
        assert "+new project line" in diff

    def test_shows_project_changes(self):
        canonical = "---\nkey: val\n---\noriginal rule"
        project = "---\nkey: val\n---\nimproved rule"
        diff = diff_reverse(project, canonical)
        assert "-original rule" in diff
        assert "+improved rule" in diff

    def test_ignores_yaml_differences(self):
        canonical = "---\nagent: foo\n---\nsame prompt"
        project = "---\nagent: bar\n---\nsame prompt"
        assert diff_reverse(project, canonical) == ""

    def test_label_appears_in_diff(self):
        canonical = "---\nk: v\n---\nold"
        project = "---\nk: v\n---\nnew"
        diff = diff_reverse(project, canonical, label="REVIEW.md")
        assert "REVIEW.md" in diff


# ── detect_operation ─────────────────────────────────────────────────────────


class TestDetectOperation:
    def test_no_changes_returns_none(self):
        text = "---\nv: 1\n---\nline one\nline two"
        assert detect_operation(text, text) == "none"

    def test_only_additions_returns_add(self):
        canonical = "---\nv: 1\n---\nline one"
        project = "---\nv: 1\n---\nline one\nnew line"
        assert detect_operation(canonical, project) == "add"

    def test_replacement_returns_replace(self):
        canonical = "---\nv: 1\n---\nold rule\nkeep this"
        project = "---\nv: 1\n---\nnew rule\nkeep this"
        assert detect_operation(canonical, project) == "replace"

    def test_fewer_lines_returns_consolidate(self):
        canonical = "---\nv: 1\n---\nrule one\nrule two\nrule three"
        project = "---\nv: 1\n---\ncombined rule"
        assert detect_operation(canonical, project) == "consolidate"

    def test_same_count_with_changes_returns_replace(self):
        canonical = "---\nv: 1\n---\nold a\nold b"
        project = "---\nv: 1\n---\nnew a\nnew b"
        assert detect_operation(canonical, project) == "replace"


# ── extract_jinja2_vars ──────────────────────────────────────────────────────


class TestExtractJinja2Vars:
    def test_extracts_double_brace_vars(self):
        text = "---\nv: 1\n---\n{{ issue.title }} and {{ diff }}"
        assert extract_jinja2_vars(text) == {"issue.title", "diff"}

    def test_extracts_if_block_vars(self):
        text = "---\nv: 1\n---\n{% if attempt %}retry{% endif %}"
        assert extract_jinja2_vars(text) == {"attempt"}

    def test_no_vars_returns_empty(self):
        text = "---\nv: 1\n---\nplain text only"
        assert extract_jinja2_vars(text) == set()

    def test_ignores_yaml_section_vars(self):
        text = "---\nvar: {{ something }}\n---\n{{ issue.body }}"
        # Only prompt section vars should be found
        result = extract_jinja2_vars(text)
        assert "issue.body" in result


# ── count_prompt_lines ───────────────────────────────────────────────────────


class TestCountPromptLines:
    def test_counts_nonempty_lines(self):
        text = "---\nv: 1\n---\nline one\nline two\nline three"
        assert count_prompt_lines(text) == 3

    def test_empty_prompt_returns_zero(self):
        text = "---\nv: 1\n---\n"
        assert count_prompt_lines(text) == 0

    def test_no_front_matter(self):
        text = "line one\nline two"
        assert count_prompt_lines(text) == 2


# ── validate_no_blocklist_terms ──────────────────────────────────────────────


class TestValidateNoBlocklistTerms:
    def test_clean_prompt_returns_empty(self):
        text = "---\nv: 1\n---\ngeneric prompt"
        assert validate_no_blocklist_terms(text, ["my-project"]) == []

    def test_finds_blocklist_terms(self):
        text = "---\nv: 1\n---\nuse my-project framework"
        found = validate_no_blocklist_terms(text, ["my-project", "django"])
        assert "my-project" in found
        assert "django" not in found

    def test_case_insensitive(self):
        text = "---\nv: 1\n---\nuse MY-PROJECT here"
        found = validate_no_blocklist_terms(text, ["my-project"])
        assert "my-project" in found

    def test_empty_blocklist(self):
        text = "---\nv: 1\n---\nanything goes"
        assert validate_no_blocklist_terms(text, []) == []


# ── validate_jinja2_vars ─────────────────────────────────────────────────────


class TestValidateJinja2Vars:
    def test_known_vars_pass(self):
        text = "---\nv: 1\n---\n{{ issue.title }} {{ diff }}"
        assert validate_jinja2_vars(text) == []

    def test_unknown_vars_reported(self):
        text = "---\nv: 1\n---\n{{ custom_var }} {{ issue.title }}"
        unknown = validate_jinja2_vars(text)
        assert "custom_var" in unknown

    def test_all_known_vars_pass(self):
        var_refs = " ".join(f"{{{{ {v} }}}}" for v in KNOWN_JINJA2_VARS)
        text = f"---\nv: 1\n---\n{var_refs}"
        assert validate_jinja2_vars(text) == []


# ── validate_line_count ──────────────────────────────────────────────────────


class TestValidateLineCount:
    def test_under_soft_cap_returns_none(self):
        lines = "\n".join(f"line {i}" for i in range(50))
        text = f"---\nv: 1\n---\n{lines}"
        assert validate_line_count(text, "add") is None

    def test_add_over_hard_cap_returns_error(self):
        lines = "\n".join(f"line {i}" for i in range(PROMPT_HARD_CAP_LINES + 1))
        text = f"---\nv: 1\n---\n{lines}"
        result = validate_line_count(text, "add")
        assert result is not None
        level, message = result
        assert level == "error"
        assert "hard cap" in message

    def test_replace_over_hard_cap_returns_warning_not_error(self):
        lines = "\n".join(f"line {i}" for i in range(PROMPT_HARD_CAP_LINES + 1))
        text = f"---\nv: 1\n---\n{lines}"
        result = validate_line_count(text, "replace")
        assert result is not None
        level, message = result
        assert level == "warning"
        assert "soft cap" in message or "consolidation" in message.lower()

    def test_over_soft_cap_returns_warning(self):
        lines = "\n".join(f"line {i}" for i in range(PROMPT_SOFT_CAP_LINES + 5))
        text = f"---\nv: 1\n---\n{lines}"
        result = validate_line_count(text, "replace")
        assert result is not None
        level, message = result
        assert level == "warning"


# ── validate_proposal ────────────────────────────────────────────────────────


class TestValidateProposal:
    def test_clean_proposal_returns_empty(self):
        project = "---\nv: 1\n---\n{{ issue.title }} prompt"
        canonical = "---\nv: 1\n---\nold prompt"
        with patch("core.upstream.load_blocklist", return_value=[]):
            issues = validate_proposal(project, "add")
        assert issues == []

    def test_blocklist_term_returns_issue(self):
        project = "---\nv: 1\n---\nuse my-project"
        canonical = "---\nv: 1\n---\nold prompt"
        with patch("core.upstream.load_blocklist", return_value=["my-project"]):
            issues = validate_proposal(project, "add")
        assert any("my-project" in i for i in issues)

    def test_unknown_jinja2_var_returns_issue(self):
        project = "---\nv: 1\n---\n{{ custom_thing }}"
        canonical = "---\nv: 1\n---\nold prompt"
        with patch("core.upstream.load_blocklist", return_value=[]):
            issues = validate_proposal(project, "add")
        assert any("custom_thing" in i for i in issues)

    def test_add_over_hard_cap_returns_issue(self):
        lines = "\n".join(f"line {i}" for i in range(PROMPT_HARD_CAP_LINES + 1))
        project = f"---\nv: 1\n---\n{lines}"
        canonical = "---\nv: 1\n---\nold"
        with patch("core.upstream.load_blocklist", return_value=[]):
            issues = validate_proposal(project, "add")
        assert any("hard cap" in i for i in issues)


# ── build_proposal ───────────────────────────────────────────────────────────


class TestBuildProposal:
    def test_builds_proposal_with_correct_fields(self):
        canonical = "---\nv: 1\n---\nold prompt"
        project = "---\nv: 1\n---\nnew prompt"
        op = detect_operation(canonical, project)
        diff = diff_reverse(project, canonical, label="WORKFLOW.md")
        proposal = build_proposal(project, canonical, "WORKFLOW.md", "test-project",
                                  op, diff)
        assert proposal.template_label == "WORKFLOW.md"
        assert proposal.operation == "replace"
        assert proposal.project_name == "test-project"
        assert proposal.new_line_count == 1

    def test_format_issue_body(self):
        canonical = "---\nv: 1\n---\nold prompt"
        project = "---\nv: 1\n---\nnew prompt"
        op = detect_operation(canonical, project)
        diff = diff_reverse(project, canonical, label="WORKFLOW.md")
        proposal = build_proposal(project, canonical, "WORKFLOW.md", "my-proj",
                                  op, diff)
        body = proposal.format_issue_body()
        assert "WORKFLOW.md" in body
        assert "replace" in body
        assert "my-proj" in body
        assert "```diff" in body


# ── load_blocklist ───────────────────────────────────────────────────────────


class TestLoadBlocklist:
    def test_loads_from_file(self, tmp_path):
        bl = tmp_path / "blocklist.txt"
        bl.write_text("# comment\nmy-project\n\ndjango\n")
        with patch("core.upstream.BLOCKLIST_PATH", bl):
            terms = load_blocklist()
        assert terms == ["my-project", "django"]

    def test_missing_file_returns_empty(self):
        with patch("core.upstream.BLOCKLIST_PATH", Path("/nonexistent/blocklist.txt")):
            assert load_blocklist() == []

    def test_ignores_comments_and_blanks(self, tmp_path):
        bl = tmp_path / "blocklist.txt"
        bl.write_text("# header\n\n  # indented comment\nterm1\n  \nterm2\n")
        with patch("core.upstream.BLOCKLIST_PATH", bl):
            terms = load_blocklist()
        assert terms == ["term1", "term2"]


# ── UpstreamProposal ─────────────────────────────────────────────────────────


class TestUpstreamProposal:
    def test_format_issue_body_structure(self):
        p = UpstreamProposal(
            template_label="WORKFLOW.md",
            operation="add",
            diff_text="+new line",
            project_name="test-proj",
            new_line_count=42,
        )
        body = p.format_issue_body()
        assert "## Upstream Template Proposal" in body
        assert "**Template:** WORKFLOW.md" in body
        assert "**Operation:** add" in body
        assert "**Source project:** test-proj" in body
        assert "**New prompt line count:** 42" in body
        assert "+new line" in body


# ── CLI cmd_upstream ─────────────────────────────────────────────────────────


class TestCmdUpstream:
    def test_dry_run_shows_diff(self, tmp_path, capsys):
        from host.cli import cmd_upstream

        workflow = tmp_path / "WORKFLOW.md"
        workflow.write_text("---\ntemplate_version: 1\n---\nnew prompt line")

        canonical = tmp_path / "canonical.md"
        canonical.write_text("---\ntemplate_version: 1\n---\nold prompt line")

        args = MagicMock()
        args.dry_run = True
        args.project_name = "test-proj"
        args.workflow = str(workflow)

        with patch("host.cli.repo_root", return_value=tmp_path), \
             patch("host.cli._resolve_workflow", return_value=workflow), \
             patch("host.cli.CANONICAL_TEMPLATE", canonical), \
             patch("core.upstream.load_blocklist", return_value=[]):
            cmd_upstream(args)

        out = capsys.readouterr().out
        assert "replace" in out
        assert "Dry run complete" in out

    def test_no_differences(self, tmp_path, capsys):
        from host.cli import cmd_upstream

        workflow = tmp_path / "WORKFLOW.md"
        workflow.write_text("---\ntemplate_version: 1\n---\nsame prompt")

        canonical = tmp_path / "canonical.md"
        canonical.write_text("---\ntemplate_version: 1\n---\nsame prompt")

        args = MagicMock()
        args.dry_run = True
        args.project_name = None
        args.workflow = str(workflow)

        with patch("host.cli.repo_root", return_value=tmp_path), \
             patch("host.cli._resolve_workflow", return_value=workflow), \
             patch("host.cli.CANONICAL_TEMPLATE", canonical):
            cmd_upstream(args)

        out = capsys.readouterr().out
        assert "No differences" in out

    def test_not_in_git_repo(self, tmp_path, capsys):
        from host.cli import cmd_upstream
        import subprocess

        args = MagicMock()
        args.dry_run = True
        args.project_name = None
        args.workflow = str(tmp_path / "WORKFLOW.md")

        with patch("host.cli.repo_root",
                    side_effect=subprocess.CalledProcessError(1, "git")):
            with pytest.raises(SystemExit) as exc_info:
                cmd_upstream(args)
            assert exc_info.value.code == 1

    def test_workflow_not_found(self, tmp_path, capsys):
        from host.cli import cmd_upstream

        workflow = tmp_path / "WORKFLOW.md"  # does not exist

        args = MagicMock()
        args.dry_run = True
        args.project_name = None
        args.workflow = str(workflow)

        with patch("host.cli.repo_root", return_value=tmp_path), \
             patch("host.cli._resolve_workflow", return_value=workflow):
            with pytest.raises(SystemExit) as exc_info:
                cmd_upstream(args)
            assert exc_info.value.code == 1

    def test_validation_issues_block_filing(self, tmp_path, capsys):
        from host.cli import cmd_upstream

        workflow = tmp_path / "WORKFLOW.md"
        workflow.write_text("---\ntemplate_version: 1\n---\nuse my-project here")

        canonical = tmp_path / "canonical.md"
        canonical.write_text("---\ntemplate_version: 1\n---\nold prompt")

        args = MagicMock()
        args.dry_run = False
        args.project_name = "test-proj"
        args.workflow = str(workflow)

        with patch("host.cli.repo_root", return_value=tmp_path), \
             patch("host.cli._resolve_workflow", return_value=workflow), \
             patch("host.cli.CANONICAL_TEMPLATE", canonical), \
             patch("core.upstream.load_blocklist",
                   return_value=["my-project"]):
            cmd_upstream(args)

        out = capsys.readouterr()
        # Validation issues block: no confirmation prompt, no filing
        assert "No differences" in out.out or "my-project" in out.err

    def test_user_declines_confirmation(self, tmp_path, capsys):
        from host.cli import cmd_upstream

        workflow = tmp_path / "WORKFLOW.md"
        workflow.write_text("---\ntemplate_version: 1\n---\nnew improved prompt")

        canonical = tmp_path / "canonical.md"
        canonical.write_text("---\ntemplate_version: 1\n---\nold prompt")

        args = MagicMock()
        args.dry_run = False
        args.project_name = "test-proj"
        args.workflow = str(workflow)

        with patch("host.cli.repo_root", return_value=tmp_path), \
             patch("host.cli._resolve_workflow", return_value=workflow), \
             patch("host.cli.CANONICAL_TEMPLATE", canonical), \
             patch("core.upstream.load_blocklist", return_value=[]), \
             patch("builtins.input", return_value="n"):
            cmd_upstream(args)

        out = capsys.readouterr().out
        assert "Aborted" in out

    def test_files_issue_on_valid_proposal(self, tmp_path, capsys):
        from host.cli import cmd_upstream

        workflow = tmp_path / "WORKFLOW.md"
        workflow.write_text("---\ntemplate_version: 1\n---\nnew improved prompt")

        canonical = tmp_path / "canonical.md"
        canonical.write_text("---\ntemplate_version: 1\n---\nold prompt")

        args = MagicMock()
        args.dry_run = False
        args.project_name = "test-proj"
        args.workflow = str(workflow)

        mock_tracker = MagicMock()
        mock_tracker.run_raw.return_value = "abc123"

        with patch("host.cli.repo_root", return_value=tmp_path), \
             patch("host.cli._resolve_workflow", return_value=workflow), \
             patch("host.cli.CANONICAL_TEMPLATE", canonical), \
             patch("host.cli.load_workflow"), \
             patch("host.cli.get_tracker_with_fallback",
                   return_value=mock_tracker), \
             patch("core.upstream.load_blocklist", return_value=[]), \
             patch("builtins.input", return_value="y"):
            cmd_upstream(args)

        out = capsys.readouterr().out
        assert "Filed upstream proposal" in out
        mock_tracker.run_raw.assert_called_once()
        mock_tracker.add_label.assert_called_once()
        mock_tracker.sync.assert_called_once()

    def test_tracker_run_raw_failure(self, tmp_path, capsys):
        """When tracker.run_raw raises, the error is printed and filing continues."""
        from host.cli import cmd_upstream

        workflow = tmp_path / "WORKFLOW.md"
        workflow.write_text("---\ntemplate_version: 1\n---\nnew improved prompt")

        canonical = tmp_path / "canonical.md"
        canonical.write_text("---\ntemplate_version: 1\n---\nold prompt")

        args = MagicMock()
        args.dry_run = False
        args.project_name = "test-proj"
        args.workflow = str(workflow)

        mock_tracker = MagicMock()
        mock_tracker.run_raw.side_effect = RuntimeError("git-bug lock error")

        with patch("host.cli.repo_root", return_value=tmp_path), \
             patch("host.cli._resolve_workflow", return_value=workflow), \
             patch("host.cli.CANONICAL_TEMPLATE", canonical), \
             patch("host.cli.load_workflow"), \
             patch("host.cli.get_tracker_with_fallback",
                   return_value=mock_tracker), \
             patch("core.upstream.load_blocklist", return_value=[]), \
             patch("builtins.input", return_value="y"):
            cmd_upstream(args)

        err = capsys.readouterr().err
        assert "Failed to file upstream proposal" in err
        assert "git-bug lock error" in err
        # sync should not be called since no issue was filed
        mock_tracker.sync.assert_not_called()

    def test_tracker_sync_failure(self, tmp_path, capsys):
        """When tracker.sync raises after filing, a warning is printed."""
        from host.cli import cmd_upstream

        workflow = tmp_path / "WORKFLOW.md"
        workflow.write_text("---\ntemplate_version: 1\n---\nnew improved prompt")

        canonical = tmp_path / "canonical.md"
        canonical.write_text("---\ntemplate_version: 1\n---\nold prompt")

        args = MagicMock()
        args.dry_run = False
        args.project_name = "test-proj"
        args.workflow = str(workflow)

        mock_tracker = MagicMock()
        mock_tracker.run_raw.return_value = "abc123"
        mock_tracker.sync.side_effect = RuntimeError("network error")

        with patch("host.cli.repo_root", return_value=tmp_path), \
             patch("host.cli._resolve_workflow", return_value=workflow), \
             patch("host.cli.CANONICAL_TEMPLATE", canonical), \
             patch("host.cli.load_workflow"), \
             patch("host.cli.get_tracker_with_fallback",
                   return_value=mock_tracker), \
             patch("core.upstream.load_blocklist", return_value=[]), \
             patch("builtins.input", return_value="y"):
            cmd_upstream(args)

        out = capsys.readouterr()
        assert "Filed upstream proposal" in out.out
        assert "tracker sync failed" in out.err
        assert "network error" in out.err

    def test_review_md_upstream_proposal(self, tmp_path, capsys):
        """REVIEW.md changes should also generate an upstream proposal."""
        from host.cli import cmd_upstream

        workflow = tmp_path / "WORKFLOW.md"
        workflow.write_text("---\ntemplate_version: 1\n---\nsame prompt")

        canonical_wf = tmp_path / "canonical_wf.md"
        canonical_wf.write_text("---\ntemplate_version: 1\n---\nsame prompt")

        review = tmp_path / "REVIEW.md"
        review.write_text("---\ntemplate_version: 1\n---\nimproved review prompt")

        canonical_rv = tmp_path / "canonical_rv.md"
        canonical_rv.write_text("---\ntemplate_version: 1\n---\nold review prompt")

        args = MagicMock()
        args.dry_run = True
        args.project_name = "test-proj"
        args.workflow = str(workflow)

        with patch("host.cli.repo_root", return_value=tmp_path), \
             patch("host.cli._resolve_workflow", return_value=workflow), \
             patch("host.cli.CANONICAL_TEMPLATE", canonical_wf), \
             patch("host.cli.CANONICAL_REVIEW_TEMPLATE", canonical_rv), \
             patch("core.upstream.load_blocklist", return_value=[]):
            cmd_upstream(args)

        out = capsys.readouterr().out
        assert "REVIEW.md" in out
        assert "replace" in out
        assert "Dry run complete" in out
