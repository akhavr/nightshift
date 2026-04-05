"""Tests for git-bug lamport clock corruption detection and repair."""

import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from adapters.trackers.git_bug import (
    detect_corrupt_clocks,
    repair_lamport_clocks,
    _scan_max_clocks,
)


@pytest.fixture
def fake_repo(tmp_path):
    """Create a fake repo dir with .git/git-bug/clocks/ structure."""
    clock_dir = tmp_path / ".git" / "git-bug" / "clocks"
    clock_dir.mkdir(parents=True)
    return tmp_path


def _write_clock(repo: Path, name: str, value: str):
    path = repo / ".git" / "git-bug" / "clocks" / name
    path.write_text(value)


def _make_empty_clock(repo: Path, name: str):
    path = repo / ".git" / "git-bug" / "clocks" / name
    path.write_text("")


class TestDetectCorruptClocks:
    def test_detect_empty_clock_file(self, fake_repo):
        _make_empty_clock(fake_repo, "bugs-edit")
        _write_clock(fake_repo, "bugs-create", "5")

        result = detect_corrupt_clocks(fake_repo)
        assert result == ["bugs-edit"]

    def test_detect_both_empty(self, fake_repo):
        _make_empty_clock(fake_repo, "bugs-edit")
        _make_empty_clock(fake_repo, "bugs-create")

        result = detect_corrupt_clocks(fake_repo)
        assert set(result) == {"bugs-edit", "bugs-create"}

    def test_healthy_clocks_not_touched(self, fake_repo):
        _write_clock(fake_repo, "bugs-edit", "42")
        _write_clock(fake_repo, "bugs-create", "10")

        result = detect_corrupt_clocks(fake_repo)
        assert result == []

    def test_missing_clocks_not_reported(self, fake_repo):
        """Missing clock files (no git-bug initialized) should not be flagged."""
        result = detect_corrupt_clocks(fake_repo)
        assert result == []


class TestScanMaxClocks:
    def _mock_refs_and_trees(self, refs_output, tree_outputs):
        """Build side_effect for subprocess.run that returns refs then trees."""
        call_count = [0]

        def side_effect(cmd, **kwargs):
            result = subprocess.CompletedProcess(cmd, 0, "", "")
            if "for-each-ref" in cmd:
                result.stdout = refs_output
                return result
            if "ls-tree" in cmd:
                idx = call_count[0]
                call_count[0] += 1
                result.stdout = tree_outputs[idx] if idx < len(tree_outputs) else ""
                return result
            return result

        return side_effect

    def test_scan_finds_max_edit_clock(self, tmp_path):
        tree1 = "100644 blob abc\tedit-clock-100\n100644 blob abc\tcreate-clock-5\n"
        tree2 = "100644 blob abc\tedit-clock-200\n100644 blob abc\tcreate-clock-3\n"
        side_effect = self._mock_refs_and_trees("ref1\nref2\n", [tree1, tree2])

        with patch("adapters.trackers.git_bug.subprocess.run", side_effect=side_effect):
            result = _scan_max_clocks(tmp_path)

        assert result["edit-clock"] == 200
        assert result["create-clock"] == 5

    def test_scan_empty_refs(self, tmp_path):
        side_effect = self._mock_refs_and_trees("", [])
        with patch("adapters.trackers.git_bug.subprocess.run", side_effect=side_effect):
            result = _scan_max_clocks(tmp_path)
        assert result == {}


class TestRepairLamportClocks:
    def test_repair_edit_clock_from_refs(self, fake_repo):
        _make_empty_clock(fake_repo, "bugs-edit")
        _write_clock(fake_repo, "bugs-create", "5")

        tree1 = "100644 blob abc\tedit-clock-150\n"
        tree2 = "100644 blob abc\tedit-clock-300\n"

        def side_effect(cmd, **kwargs):
            result = subprocess.CompletedProcess(cmd, 0, "", "")
            if "for-each-ref" in cmd:
                result.stdout = "ref1\nref2\n"
            elif "ls-tree" in cmd:
                ref = cmd[-1]
                result.stdout = tree1 if ref == "ref1" else tree2
            return result

        with patch("adapters.trackers.git_bug.subprocess.run", side_effect=side_effect):
            repaired = repair_lamport_clocks(fake_repo)

        assert "bugs-edit" in repaired
        assert "bugs-create" not in repaired
        content = (fake_repo / ".git" / "git-bug" / "clocks" / "bugs-edit").read_text()
        assert content == "300"

    def test_repair_create_clock_from_refs(self, fake_repo):
        _write_clock(fake_repo, "bugs-edit", "42")
        _make_empty_clock(fake_repo, "bugs-create")

        tree1 = "100644 blob abc\tedit-clock-10\n100644 blob abc\tcreate-clock-20\n"
        tree2 = "100644 blob abc\tedit-clock-30\n100644 blob abc\tcreate-clock-50\n"

        def side_effect(cmd, **kwargs):
            result = subprocess.CompletedProcess(cmd, 0, "", "")
            if "for-each-ref" in cmd:
                result.stdout = "ref1\nref2\n"
            elif "ls-tree" in cmd:
                ref = cmd[-1]
                result.stdout = tree1 if ref == "ref1" else tree2
            return result

        with patch("adapters.trackers.git_bug.subprocess.run", side_effect=side_effect):
            repaired = repair_lamport_clocks(fake_repo)

        assert "bugs-create" in repaired
        assert "bugs-edit" not in repaired
        content = (fake_repo / ".git" / "git-bug" / "clocks" / "bugs-create").read_text()
        assert content == "50"

    def test_healthy_clocks_no_repair(self, fake_repo):
        _write_clock(fake_repo, "bugs-edit", "42")
        _write_clock(fake_repo, "bugs-create", "10")

        repaired = repair_lamport_clocks(fake_repo)
        assert repaired == []
        # Values unchanged
        assert (fake_repo / ".git" / "git-bug" / "clocks" / "bugs-edit").read_text() == "42"
        assert (fake_repo / ".git" / "git-bug" / "clocks" / "bugs-create").read_text() == "10"

    def test_repair_with_no_refs(self, fake_repo):
        """When no refs exist, repair writes 0."""
        _make_empty_clock(fake_repo, "bugs-edit")

        def side_effect(cmd, **kwargs):
            result = subprocess.CompletedProcess(cmd, 0, "", "")
            if "for-each-ref" in cmd:
                result.stdout = ""
            return result

        with patch("adapters.trackers.git_bug.subprocess.run", side_effect=side_effect):
            repaired = repair_lamport_clocks(fake_repo)

        assert "bugs-edit" in repaired
        content = (fake_repo / ".git" / "git-bug" / "clocks" / "bugs-edit").read_text()
        assert content == "0"
