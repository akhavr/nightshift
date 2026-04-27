"""Tests for core/workspace_transaction.py."""

import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from core.workspace_transaction import (
    WorktreeCorruptError,
    check_worktree_integrity,
    TransactionError,
    RebaseConflictError,
    WorkspaceTransaction,
)


@pytest.fixture(autouse=True)
def clean_git_environ(monkeypatch):
    """Clear git worktree env so subprocess calls use the temp repo."""
    monkeypatch.delenv("GIT_DIR", raising=False)
    monkeypatch.delenv("GIT_WORK_TREE", raising=False)


def _make_worktree(tmp_path: Path, name: str = "agent-abc123") -> Path:
    """Create a minimal git worktree layout for integrity tests."""
    repo_root = tmp_path / "repo"
    metadata_dir = repo_root / ".git" / "worktrees" / name
    metadata_dir.mkdir(parents=True)

    worktree = tmp_path / name
    worktree.mkdir()
    (worktree / ".git").write_text(f"gitdir: {metadata_dir}\n")
    return worktree


def _init_repo(tmp_path: Path) -> tuple[Path, Path]:
    """Create a git repo and a worktree for transaction tests."""
    repo = tmp_path / "repo"
    repo.mkdir()

    def run(*args: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            list(args), cwd=str(repo), capture_output=True, text=True, check=True
        )

    run("git", "init", "-b", "main")
    run("git", "config", "user.email", "test@example.com")
    run("git", "config", "user.name", "Test User")
    (repo / "file.txt").write_text("initial\n")
    run("git", "add", "file.txt")
    run("git", "commit", "-m", "initial")

    worktree = tmp_path / "worktree"
    run("git", "worktree", "add", "-b", "txn-base", str(worktree), "main")
    return repo, worktree


def _branch_exists(repo: Path, branch: str) -> bool:
    """Return True when the branch exists in the given repo."""
    result = subprocess.run(
        ["git", "rev-parse", "--verify", branch],
        cwd=str(repo),
        capture_output=True,
        text=True,
    )
    return result.returncode == 0


def _current_branch(repo: Path) -> str:
    """Return the current branch name for a repo or worktree."""
    result = subprocess.run(
        ["git", "branch", "--show-current"],
        cwd=str(repo),
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.strip()


def test_check_worktree_integrity_valid(tmp_path):
    """A healthy worktree passes integrity checks."""
    worktree = _make_worktree(tmp_path)

    assert check_worktree_integrity(worktree) is True


def test_check_worktree_integrity_missing_metadata(tmp_path):
    """Missing worktree metadata raises a corruption error."""
    worktree = _make_worktree(tmp_path)
    metadata_dir = tmp_path / "repo" / ".git" / "worktrees" / worktree.name

    metadata_dir.rmdir()

    with pytest.raises(WorktreeCorruptError):
        check_worktree_integrity(worktree)


def test_check_worktree_integrity_missing_git_file(tmp_path):
    """A worktree without a .git file is rejected immediately."""
    worktree = tmp_path / "agent-abc123"
    worktree.mkdir()

    with pytest.raises(WorktreeCorruptError):
        check_worktree_integrity(worktree)


def test_check_worktree_integrity_auto_repair(tmp_path):
    """Auto-repair shells out to git worktree repair when metadata is missing."""
    worktree = _make_worktree(tmp_path)
    metadata_dir = tmp_path / "repo" / ".git" / "worktrees" / worktree.name
    metadata_dir.rmdir()

    with patch("core.workspace_transaction.subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
        assert check_worktree_integrity(worktree, auto_repair=True) is True

    mock_run.assert_called_once_with(
        ["git", "worktree", "repair"],
        cwd=str(worktree),
        capture_output=True,
        text=True,
    )


def test_context_manager_restores_git_pointer(tmp_path):
    """WorkspaceTransaction restores .git content on normal exit."""
    worktree = _make_worktree(tmp_path)
    git_file = worktree / ".git"
    original = git_file.read_text()

    with WorkspaceTransaction(worktree) as txn:
        txn.rewrite_git_pointer("gitdir: /tmp/other-metadata\n")
        assert git_file.read_text() == "gitdir: /tmp/other-metadata\n"

    assert git_file.read_text() == original


def test_exception_triggers_restore(tmp_path):
    """WorkspaceTransaction restores .git content even if the body raises."""
    worktree = _make_worktree(tmp_path)
    git_file = worktree / ".git"
    original = git_file.read_text()

    with pytest.raises(RuntimeError):
        with WorkspaceTransaction(worktree) as txn:
            txn.rewrite_git_pointer("gitdir: /tmp/temporary-metadata\n")
            raise RuntimeError("boom")

    assert git_file.read_text() == original


def test_rewrite_git_pointer(tmp_path):
    """rewrite_git_pointer updates the .git file in place."""
    worktree = _make_worktree(tmp_path)
    git_file = worktree / ".git"

    with WorkspaceTransaction(worktree) as txn:
        txn.rewrite_git_pointer("gitdir: /tmp/new-pointer\n")
        assert git_file.read_text() == "gitdir: /tmp/new-pointer\n"


def test_restore_git_pointer(tmp_path):
    """restore_git_pointer keeps the restored pointer after exit."""
    worktree = _make_worktree(tmp_path)
    git_file = worktree / ".git"
    original = git_file.read_text()

    with WorkspaceTransaction(worktree) as txn:
        txn.restore_git_pointer(original)
        assert git_file.read_text() == original

    assert git_file.read_text() == original


def test_nested_transactions_error(tmp_path):
    """Nested WorkspaceTransaction usage is rejected."""
    worktree = _make_worktree(tmp_path)

    with WorkspaceTransaction(worktree):
        with pytest.raises(TransactionError):
            with WorkspaceTransaction(worktree):
                pass


def test_create_branch_with_rollback(tmp_path):
    """Branches created in a transaction are removed on rollback."""
    repo, worktree = _init_repo(tmp_path)
    created_branch = "agent/rollback-create"

    with pytest.raises(RuntimeError):
        with WorkspaceTransaction(worktree) as txn:
            txn.create_branch(created_branch)
            assert _branch_exists(repo, created_branch)
            raise RuntimeError("boom")

    assert not _branch_exists(repo, created_branch)


def test_checkout_with_rollback(tmp_path):
    """checkout() restores the original branch when the transaction rolls back."""
    repo, worktree = _init_repo(tmp_path)
    original_branch = _current_branch(worktree)

    subprocess.run(
        ["git", "checkout", "-b", "agent/target"],
        cwd=str(repo),
        capture_output=True,
        text=True,
        check=True,
    )

    with pytest.raises(RuntimeError):
        with WorkspaceTransaction(worktree) as txn:
            txn.checkout("agent/target")
            assert _current_branch(worktree) == "agent/target"
            raise RuntimeError("boom")

    assert _current_branch(worktree) == original_branch


def test_rollback_deletes_created_branch(tmp_path):
    """Multiple branches created in a failed transaction are cleaned up."""
    repo, worktree = _init_repo(tmp_path)
    branches = ["agent/rollback-a", "agent/rollback-b"]

    with pytest.raises(RuntimeError):
        with WorkspaceTransaction(worktree) as txn:
            txn.create_branch(branches[0])
            txn.create_branch(branches[1])
            assert _branch_exists(repo, branches[0])
            assert _branch_exists(repo, branches[1])
            raise RuntimeError("boom")

    assert not _branch_exists(repo, branches[0])
    assert not _branch_exists(repo, branches[1])


def test_merge_returns_result(tmp_path):
    """merge() returns a MergeResult with success and conflict metadata."""
    repo, worktree = _init_repo(tmp_path)

    subprocess.run(
        ["git", "checkout", "-b", "feature/merge-ok"],
        cwd=str(repo),
        capture_output=True,
        text=True,
        check=True,
    )
    (repo / "merge.txt").write_text("feature branch\n")
    subprocess.run(
        ["git", "add", "merge.txt"],
        cwd=str(repo),
        capture_output=True,
        text=True,
        check=True,
    )
    subprocess.run(
        ["git", "commit", "-m", "feature commit"],
        cwd=str(repo),
        capture_output=True,
        text=True,
        check=True,
    )

    with WorkspaceTransaction(worktree) as txn:
        result = txn.merge("feature/merge-ok")

    assert result.success is True
    assert result.has_conflicts is False
    assert result.conflicting_files == []


def test_merge_conflict_detected(tmp_path):
    """merge() reports conflict files when git merge hits a conflict."""
    repo, worktree = _init_repo(tmp_path)

    subprocess.run(
        ["git", "checkout", "-b", "feature/merge-conflict"],
        cwd=str(repo),
        capture_output=True,
        text=True,
        check=True,
    )
    (repo / "file.txt").write_text("feature side\n")
    subprocess.run(
        ["git", "add", "file.txt"],
        cwd=str(repo),
        capture_output=True,
        text=True,
        check=True,
    )
    subprocess.run(
        ["git", "commit", "-m", "feature conflict commit"],
        cwd=str(repo),
        capture_output=True,
        text=True,
        check=True,
    )

    (worktree / "file.txt").write_text("base side\n")
    subprocess.run(
        ["git", "add", "file.txt"],
        cwd=str(worktree),
        capture_output=True,
        text=True,
        check=True,
    )
    subprocess.run(
        ["git", "commit", "-m", "base conflict commit"],
        cwd=str(worktree),
        capture_output=True,
        text=True,
        check=True,
    )

    with WorkspaceTransaction(worktree) as txn:
        result = txn.merge("feature/merge-conflict")

    assert result.success is False
    assert result.has_conflicts is True
    assert "file.txt" in result.conflicting_files


def test_merge_non_conflict_failure_returns_result(tmp_path):
    """merge() returns a failure result instead of raising on non-conflicts."""
    _, worktree = _init_repo(tmp_path)

    with patch("core.workspace_transaction.subprocess.run") as mock_run:
        mock_run.side_effect = [
            MagicMock(returncode=1, stderr="fatal: unknown revision"),
            MagicMock(returncode=0, stdout="", stderr=""),
        ]
        with WorkspaceTransaction(worktree) as txn:
            result = txn.merge("missing-branch")

    assert result.success is False
    assert result.has_conflicts is False
    assert result.conflicting_files == []
    assert "unknown revision" in result.stderr


def test_rebase_with_conflict_aborts(tmp_path):
    """rebase() aborts on conflict and restores the original worktree state."""
    repo, worktree = _init_repo(tmp_path)

    (repo / "file.txt").write_text("main side\n")
    subprocess.run(
        ["git", "add", "file.txt"],
        cwd=str(repo),
        capture_output=True,
        text=True,
        check=True,
    )
    subprocess.run(
        ["git", "commit", "-m", "main conflict commit"],
        cwd=str(repo),
        capture_output=True,
        text=True,
        check=True,
    )

    (worktree / "file.txt").write_text("txn side\n")
    subprocess.run(
        ["git", "add", "file.txt"],
        cwd=str(worktree),
        capture_output=True,
        text=True,
        check=True,
    )
    subprocess.run(
        ["git", "commit", "-m", "txn conflict commit"],
        cwd=str(worktree),
        capture_output=True,
        text=True,
        check=True,
    )

    original_head = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=str(worktree),
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    original_status = subprocess.run(
        ["git", "status", "--short"],
        cwd=str(worktree),
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()

    with WorkspaceTransaction(worktree) as txn:
        with pytest.raises(RebaseConflictError):
            txn.rebase("main")

    restored_head = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=str(worktree),
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    restored_status = subprocess.run(
        ["git", "status", "--short"],
        cwd=str(worktree),
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()

    assert restored_head == original_head
    assert restored_status == original_status
