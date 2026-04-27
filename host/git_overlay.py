"""Session-local git overlay helpers."""

from __future__ import annotations

import re
import logging
import shutil
import subprocess
from pathlib import Path

logger = logging.getLogger(__name__)
_ALLOWED_AGENT_REF_RE = re.compile(r"^refs/heads/agent-[^/]+$")


def is_fuse_overlayfs_available() -> bool:
    """Return True when fuse-overlayfs is available on the host."""
    return shutil.which("fuse-overlayfs") is not None


def setup_overlay(repo_git: Path, session_dir: Path) -> Path:
    """Mount a fuse-overlayfs overlay for the repo's .git directory."""
    merged = session_dir / "git-merged"
    upper = session_dir / "git-upper"
    work = session_dir / "git-work"

    upper.mkdir(parents=True, exist_ok=True)
    work.mkdir(parents=True, exist_ok=True)
    merged.mkdir(parents=True, exist_ok=True)

    cmd = [
        "fuse-overlayfs",
        "-o",
        f"lowerdir={repo_git},upperdir={upper},workdir={work}",
        str(merged),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f'fuse-overlayfs failed: {result.stderr}')
    return merged


def teardown_overlay(merged_path: Path) -> None:
    """Unmount a fuse-overlayfs mount."""
    subprocess.run(["fusermount", "-u", str(merged_path)],
                   capture_output=True, text=True)


def setup_git_copy(repo_git: Path, session_dir: Path) -> Path:
    """Fallback isolation: copy .git into a session-local directory."""
    copy_dir = session_dir / "git-copy"
    copy_dir.parent.mkdir(parents=True, exist_ok=True)
    if copy_dir.exists():
        shutil.rmtree(copy_dir)
    subprocess.run(["cp", "-a", str(repo_git), str(copy_dir)],
                   capture_output=True, text=True, check=True)
    return copy_dir


def _copy_tree(src: Path, dst: Path, overwrite: bool = False) -> None:
    """Copy files from src into dst, preserving directory structure.

    By default, skips existing files because git objects are immutable.
    When overwrite=True, existing mutable refs/logs are replaced.
    """
    if not src.exists():
        return
    for path in src.rglob("*"):
        rel = path.relative_to(src)
        target = dst / rel
        if path.is_dir():
            target.mkdir(parents=True, exist_ok=True)
        elif not target.exists() or overwrite:
            target.parent.mkdir(parents=True, exist_ok=True)
            if overwrite and target.exists():
                target.unlink()
            shutil.copy2(path, target)


def _copy_file(src: Path, dst: Path, overwrite: bool = False) -> None:
    """Copy a single file, optionally overwriting the target."""
    if not src.exists():
        return
    dst.parent.mkdir(parents=True, exist_ok=True)
    if overwrite and dst.exists():
        dst.unlink()
    elif dst.exists():
        return
    shutil.copy2(src, dst)


def _is_allowed_ref(ref_name: str) -> bool:
    """Return True for refs allowed to copy back into the host repo."""
    return _ALLOWED_AGENT_REF_RE.fullmatch(ref_name) is not None


def _copy_loose_refs(src_refs: Path, dst_git: Path) -> tuple[list[str], list[str]]:
    """Copy loose refs that match the whitelist and report skipped refs."""
    copied: list[str] = []
    skipped: list[str] = []
    if not src_refs.exists():
        return copied, skipped

    dst_refs = dst_git / "refs"
    for path in src_refs.rglob("*"):
        if path.is_dir():
            continue
        rel = path.relative_to(src_refs)
        ref_name = f"refs/{rel.as_posix()}"
        if not _is_allowed_ref(ref_name):
            skipped.append(ref_name)
            continue
        target = dst_refs / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, target)
        copied.append(ref_name)
    return copied, skipped


def _copy_packed_refs(src_git: Path, dst_git: Path, copied_refs: set[str]) -> list[str]:
    """Copy allowed packed refs as loose refs in the host repo."""
    packed_refs = src_git / "packed-refs"
    skipped: list[str] = []
    if not packed_refs.exists():
        return skipped

    dst_refs = dst_git / "refs"
    for raw_line in packed_refs.read_text().splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or line.startswith("^"):
            continue
        try:
            object_id, ref_name = line.split(" ", 1)
        except ValueError:
            continue
        ref_name = ref_name.strip()
        if not _is_allowed_ref(ref_name):
            skipped.append(ref_name)
            continue
        if ref_name in copied_refs:
            continue
        rel = Path(*ref_name.split("/")[1:])
        target = dst_refs / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(f"{object_id}\n")
        copied_refs.add(ref_name)
    return skipped


def extract_commits(upper_dir: Path, repo_git: Path) -> list[str]:
    """Copy git objects and whitelisted refs from the overlay/copy back into .git."""
    repo_git.mkdir(parents=True, exist_ok=True)

    _copy_tree(upper_dir / "objects", repo_git / "objects")
    copied_refs, skipped_refs = _copy_loose_refs(upper_dir / "refs", repo_git)
    skipped_refs.extend(_copy_packed_refs(upper_dir, repo_git, set(copied_refs)))
    _copy_tree(upper_dir / "logs", repo_git / "logs", overwrite=True)
    for name in ("HEAD", "packed-refs", "FETCH_HEAD", "ORIG_HEAD"):
        _copy_file(upper_dir / name, repo_git / name, overwrite=True)
    return skipped_refs
