# Worktree Metadata Corruption by Container Tests

## Problem

When pytest runs inside a nightshift container, tests that call `git init` can corrupt the host's git worktree metadata, making the worktree unusable.

## Root Cause

1. Container sets `GIT_DIR=/repo-git/worktrees/agent-<id>` (docker-entrypoint.sh:47)
2. `/repo-git` is mounted from host's `.git` directory
3. When `git init` runs with `GIT_DIR` set, it writes to `$GIT_DIR` instead of cwd
4. This overwrites the proper worktree metadata with a fresh standalone git repo

### Demonstration

```bash
$ export GIT_DIR=/tmp/target
$ mkdir -p $GIT_DIR
$ git init -b master /some/other/path
Initialized empty Git repository in /tmp/target/  # NOT /some/other/path!
```

## Discovery

Worktree `agent-2c1cf5ea6bbc` was corrupted. Investigation revealed:

```
$ ls .git/worktrees/agent-2c1cf5ea6bbc/
branches/  config  description  FETCH_HEAD  HEAD  hooks/  index  info/  
logs/  objects/  ORIG_HEAD  packed-refs  refs/  worktrees/
```

This is a **full git repo structure**, not worktree metadata (which should only have HEAD, gitdir, commondir, index, logs/).

The config file contained:
```
[user]
    email = test@test.com
    name = Test
```

These are the exact values set by `tests/test_workspace_setup.py::_init_repo()`.

A nested `worktrees/worktree/gitdir` pointed to:
```
/tmp/pytest-of-ubuntu/pytest-0/test_clean_merge_preserves_mas0/worktree/.git
```

Confirming the corruption came from pytest running `test_clean_merge_preserves_master_changes`.

## Vulnerable Tests

Tests that call `git init` without the `clean_git_environ` fixture:

- `tests/test_cli_commands.py`
- `tests/test_config_discovery.py`
- `tests/test_entrypoint_git.py`
- `tests/test_git_bug_clock_repair.py`
- `tests/test_host_rebase.py`
- `tests/test_upgrade.py`

The `clean_git_environ` fixture exists but is defined per-module (not in conftest.py), so it doesn't protect these files.

## Symptoms

When a worktree is corrupted:

```bash
$ git -C .worktrees/agent-xxx log master
# Shows wrong commits (from test repo, not main repo)

$ git worktree list
# Doesn't show the corrupted worktree

$ git -C .worktrees/agent-xxx status
fatal: not a git repository: /path/to/.git/worktrees/agent-xxx
```

The reviewer reports "branch is behind master" but rebase can't fix it because the worktree sees a different repo history.

## Fix Options

### Quick Fixes (low effort, partial protection)

#### 1A: Global pytest fixture

Move `clean_git_environ` to `tests/conftest.py` with `autouse=True`:

```python
@pytest.fixture(autouse=True)
def clean_git_environ(monkeypatch):
    """Clear GIT_DIR/GIT_WORK_TREE so git init doesn't corrupt host metadata."""
    monkeypatch.delenv("GIT_DIR", raising=False)
    monkeypatch.delenv("GIT_WORK_TREE", raising=False)
```

| Pros | Cons |
|------|------|
| Minimal change | Only protects pytest, not other git calls |
| Protects all test modules | Agent could still corrupt via direct git commands |

#### 1B: Git wrapper function

Add to docker-entrypoint.sh:

```bash
git() {
    case "$1" in
        init|clone)
            # These commands should use cwd, not GIT_DIR
            env -u GIT_DIR -u GIT_WORK_TREE command git "$@"
            ;;
        *)
            command git "$@"
            ;;
    esac
}
export -f git
```

| Pros | Cons |
|------|------|
| Protects specific dangerous commands | May miss edge cases |
| No architecture change | Wrapper overhead |

#### 1C: Alias for pytest

Add to docker-entrypoint.sh:

```bash
alias pytest='env -u GIT_DIR -u GIT_WORK_TREE pytest'
alias python='env -u GIT_DIR -u GIT_WORK_TREE python'
```

| Pros | Cons |
|------|------|
| Simple | Only works in interactive shells |
| | Doesn't protect subprocess calls |

### Medium Effort (better isolation)

#### 2A: Read-only .git with commit extraction

```bash
# Mount .git read-only
docker run -v /repo/.git:/repo-git:ro ...

# Container uses local objects dir for new commits
export GIT_OBJECT_DIRECTORY=/session/git-objects
mkdir -p $GIT_OBJECT_DIRECTORY

# Agent commits go to local objects
git commit -m "work"

# Host extracts commits after container exits
GIT_ALTERNATE_OBJECT_DIRECTORIES=/session/git-objects \
  git cherry-pick <commit-hash>
```

| Pros | Cons |
|------|------|
| Container can't corrupt host .git | Complex object extraction |
| Commits are real git objects | Requires post-processing |

#### 2B: Bundle/patch workflow

```bash
# Host: create bundle before launch
git bundle create /session/repo.bundle master

# Container: clone from bundle (completely isolated)
git clone /session/repo.bundle /workspace
cd /workspace
git checkout -b agent/work

# Agent works and commits locally
git commit -m "work"

# Export patches on exit
git format-patch origin/master..HEAD -o /session/patches/

# Host: apply patches after container exits
cd /repo
git checkout -b agent/<issue-id> master
git am /session/patches/*.patch
```

| Pros | Cons |
|------|------|
| Complete isolation | Loses original commit hashes |
| Simple to understand | Extra steps on host |
| Works with any git version | Bundle must include full history or shallow clone |

### Full Isolation (architecture changes)

#### 3A: Overlay filesystem

```bash
# Host: create overlay mount
OVERLAY_UPPER=$(mktemp -d)
OVERLAY_WORK=$(mktemp -d)
OVERLAY_MERGED=$(mktemp -d)

mount -t overlay overlay \
  -o lowerdir=/repo/.git,upperdir=$OVERLAY_UPPER,workdir=$OVERLAY_WORK \
  $OVERLAY_MERGED

# Container sees merged view, writes go to upper layer
docker run -v $OVERLAY_MERGED:/repo-git:rw ...

# After container exits, extract new objects from upper layer
rsync -a $OVERLAY_UPPER/objects/ /repo/.git/objects/

# Import commits
git fsck --unreachable | grep commit | cut -d' ' -f3 | \
  xargs -I{} git cherry-pick {}

# Cleanup
umount $OVERLAY_MERGED
rm -rf $OVERLAY_UPPER $OVERLAY_WORK $OVERLAY_MERGED
```

| Pros | Cons |
|------|------|
| Container sees full .git, can commit normally | Requires root for mount |
| Original .git completely untouched | Complex setup/teardown |
| Preserves commit hashes | Overlay management overhead |

#### 3B: Git daemon / SSH access

```bash
# Host: start git daemon
git daemon --reuseaddr --base-path=/repo --export-all --enable=receive-pack &

# Container: clone via git protocol
git clone git://host.docker.internal/repo /workspace

# Agent works and pushes
git push origin HEAD:refs/heads/agent/<issue-id>

# Host: merge the branch
git merge agent/<issue-id>
```

| Pros | Cons |
|------|------|
| Clean network separation | Requires daemon management |
| Standard git workflow | Network overhead |
| No filesystem sharing | Firewall/port configuration |

#### 3C: Separate git repo per container

```bash
# Host: create bare clone for container
git clone --bare /repo /session/repo.git

# Container: clone from session-local bare repo
git clone /session/repo.git /workspace

# Agent works and pushes to local bare
git push origin HEAD:refs/heads/agent/work

# Host: fetch from session bare repo
cd /repo
git fetch /session/repo.git agent/work:agent/<issue-id>
git merge agent/<issue-id>
```

| Pros | Cons |
|------|------|
| Complete isolation | Disk space for bare clone |
| Familiar git workflow | Clone time on large repos |
| No special mounts | Must sync back to main repo |

## Recommendation

**Primary solution: Overlay filesystem (fuse-overlayfs)**

Overlay is the simplest and most universal solution because:
- No code changes inside container
- Blocks ALL corruption vectors, not just `git init`
- Preserves commit hashes
- Simple mental model: "container can't touch original"

Implementation in `host/launch.py`:

```bash
# Setup before container launch
fuse-overlayfs \
  -o lowerdir=/repo/.git,upperdir=/session/git-upper,workdir=/session/git-work \
  /session/git-merged

# Mount the overlay instead of real .git
docker run -v /session/git-merged:/repo-git:rw ...

# Teardown after container exits
fusermount -u /session/git-merged

# Import new objects to real repo
cp -a /session/git-upper/objects/* /repo/.git/objects/
# Update refs as needed
```

**Fallback (if fuse-overlayfs unavailable):** Copy .git before launch:

```bash
cp -a /repo/.git /session/git-copy
docker run -v /session/git-copy:/repo-git:rw ...
# After: extract commits from copy
```

**Quick fix (interim):** Global pytest fixture (1A) provides partial protection while overlay is implemented.

## Recovery

To fix a corrupted worktree:

```bash
# Remove corrupted metadata
rm -rf .git/worktrees/agent-xxx

# Remove worktree directory
rm -rf .worktrees/agent-xxx

# Reject the session and restart
nightshift reject <issue-id>
nightshift start <issue-id>
```

## Related

- `docs/git-worktree-corruption.md` — Container git corruption (different issue: gitdir/core.worktree)
- `docker-entrypoint.sh:47` — Where GIT_DIR is set
- `tests/conftest.py` — Should contain global fixture
