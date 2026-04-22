# git-bug Empty Authorship Bug

## Summary

git-bug v0.10.1 creates commits with empty author/committer fields (`<>`). The cache builder silently rejects bugs with empty authorship, causing `git bug ls` to return incomplete results while `git bug show <id>` still works.

## Symptoms

- `git-bug bug ls` returns only a subset of bugs (e.g., 3 out of 153)
- `git-bug bug show <id>` works for bugs not appearing in the list
- New operations (comments, labels, status changes) don't make bugs appear
- Newly created identities also have empty authorship

## Root Cause

In `repository/gogit.go:StoreSignedCommit()` (lines 673-691):

```go
func (repo *GoGitRepo) StoreSignedCommit(...) (Hash, error) {
    cfg, err := repo.r.Config()  // Reads .gitconfig
    
    commit := object.Commit{
        Author: object.Signature{
            Name:  cfg.Author.Name,   // Empty if not in .gitconfig!
            Email: cfg.Author.Email,  // Empty if not in .gitconfig!
            When:  time.Now(),
        },
        Committer: object.Signature{
            Name:  cfg.Committer.Name,
            Email: cfg.Committer.Email,
            When:  time.Now(),
        },
        ...
    }
}
```

git-bug reads author from `.gitconfig` via go-git's `Config()`, **NOT** from the git-bug identity. If `user.name`/`user.email` aren't configured in git, commits have empty authorship:

```
tree d6a275cc850554cf4e9813ed4b7dea2f80699d1e
author  <> 1776855148 +0100
committer  <> 1776855148 +0100
```

The cache builder then validates authorship and fails on these commits.

## Diagnosis

```bash
# Check if a bug ref has empty authorship
git log -1 --format='%an <%ae>' refs/bugs/<full-bug-id>
# If output is " <>" the bug is affected

# Count affected bugs
git for-each-ref --format='%(refname) %(authorname) %(authoremail)' refs/bugs/ | grep '<>' | wc -l
```

## Prevention

go-git reads from `[author]` section, not `[user]` section. Set both:

```bash
# Standard git (used by git CLI)
git config user.name "@akhavr"
git config user.email "akhavr@42cc.co"

# go-git (used by git-bug)
git config author.name "@akhavr"
git config author.email "akhavr@42cc.co"
git config committer.name "@akhavr"
git config committer.email "akhavr@42cc.co"
```

## Fix Existing Refs

Run the fix script to rewrite all commits with proper authorship:

```bash
#!/bin/bash
# fix-git-bug-authors.sh

AUTHOR_NAME="@akhavr"
AUTHOR_EMAIL="akhavr@42cc.co"

fix_ref() {
    local ref="$1"
    local refname="${ref#refs/}"
    
    commits=($(git rev-list --reverse "$ref"))
    [ ${#commits[@]} -eq 0 ] && return
    
    # Check if tip has empty author
    author=$(git log -1 --format='%an' "${commits[-1]}")
    [ -n "$author" ] && return
    
    echo "Fixing $refname (${#commits[@]} commits)"
    
    declare -A commit_map
    
    for old_commit in "${commits[@]}"; do
        tree=$(git cat-file -p "$old_commit" | head -1 | awk '{print $2}')
        
        parent_args=""
        while read -r line; do
            if [[ "$line" =~ ^parent\ (.+)$ ]]; then
                old_parent="${BASH_REMATCH[1]}"
                new_parent="${commit_map[$old_parent]:-$old_parent}"
                parent_args="$parent_args -p $new_parent"
            fi
        done < <(git cat-file -p "$old_commit")
        
        new_commit=$(GIT_AUTHOR_NAME="$AUTHOR_NAME" GIT_AUTHOR_EMAIL="$AUTHOR_EMAIL" \
                     GIT_COMMITTER_NAME="$AUTHOR_NAME" GIT_COMMITTER_EMAIL="$AUTHOR_EMAIL" \
                     git commit-tree $tree $parent_args -m "")
        
        commit_map[$old_commit]=$new_commit
    done
    
    new_tip="${commit_map[${commits[-1]}]}"
    git update-ref "$ref" "$new_tip"
}

for ref in $(git for-each-ref --format='%(refname)' refs/bugs/); do
    fix_ref "$ref"
done

for ref in $(git for-each-ref --format='%(refname)' refs/identities/); do
    fix_ref "$ref"
done

# Clean up remote tracking refs (they have old commit hashes)
git for-each-ref --format='%(refname)' refs/remotes/origin/bugs/ | xargs -I{} git update-ref -d {}
git for-each-ref --format='%(refname)' refs/remotes/origin/identities/ | xargs -I{} git update-ref -d {}

rm -rf .git/git-bug
echo "Done. Run 'git-bug bug' to rebuild cache."
```

**Note:** After setting `author.*` config (see Prevention), new commits will have proper authorship. This script is only needed once for existing refs.

## Note on `git-bug bug ls` Behavior

`git-bug bug ls` has unusual default filtering and may show fewer bugs than expected. Use query syntax instead:

```bash
# List all bugs
git-bug bug

# List open bugs
git-bug bug status:open

# List closed bugs  
git-bug bug status:closed
```

## Cache Builder Error Handling

If a bug ref has issues (empty author, missing identity, etc.), the cache builder stops on first error (`entity/dag/entity.go:ReadAll()` returns early). After fixing refs, delete cache and rebuild:

```bash
rm -rf .git/git-bug
git-bug bug  # triggers rebuild
```

## Related GitHub Issues

Investigated existing issues - none match our exact problem:

- [#445](https://github.com/git-bug/git-bug/issues/445) - Cache not updated after push/pull. Different issue (sync, not authorship).
- [#36](https://github.com/git-bug/git-bug/issues/36) - New bugs not registering. Similar symptoms but was cache excerpts bug, fixed 2018.
- [#426](https://github.com/git-bug/git-bug/issues/426) - Timestamps as 1970-01-01. Root cause: gob serialization ignoring unexported fields. Similar pattern (silent data loss).

The empty authorship issue appears to be unreported. go-git docs say "If Author is empty, Name and Email is read from config" but git-bug may not be passing config correctly.

## Status

**FIXED** (2026-04-22)

All 156 bug refs are now repaired. git-bug cache builds correctly.

### Root Causes
1. **Empty authorship**: `repository/gogit.go:StoreSignedCommit()` reads `cfg.Author.Name` from `[author]` section, not `[user]`
2. **DAG corruption**: Previous authorship fix script created merge commits combining identical duplicate branches
3. **Cache builder**: Stops on first error (`entity/dag/entity.go:ReadAll()` returns early)

### What Happened (2026-04-22)

The original authorship fix script rewrote commits to add proper author/committer fields, but in the process it:
1. Created a new chain of commits with proper authorship (orphan branch)
2. Created a merge commit combining the orphan branch with the original chain
3. Both branches had IDENTICAL ops blobs (same operations, same clocks)

**Corrupted structure:**
```
*   cd176180  (merge, 2 parents)
|\  
| * 9db44308  (orphan branch - DUPLICATE ops)
| * ...
| * f8dcc794  (orphan ROOT - duplicate CreateOp)
* 1a9187b5   (main branch)
* ...
* 685d4883   (main ROOT - original CreateOp)
```

git-bug failed because:
- "multiple leafs" error: Two root commits detected
- "DFS failed" error: Duplicate clocks violate DAG ordering
- "creation lamport time not set": CreateOp found without matching create-clock

### The Fix (v3 script)

Since both branches contained IDENTICAL operations (same ops blob hashes), the fix was simple:

1. For each ref with multiple roots, find the merge commit
2. Identify which parent leads to the "true root" (lowest create-clock)
3. Update the ref to point to that parent's tip, discarding the merge and duplicate branch

**Fixed structure:**
```
* 1a9187b5   (ref now points here - main branch tip)
* ...
* 685d4883   (main ROOT - only root, only CreateOp)
```

Script: `scripts/fix-gitbug-dag-v3.sh`

### Verification
```bash
$ git-bug bug -f json | jq '. | length'
156
$ git-bug bug show 007cd7dc  # works correctly
```

### Affected Refs
143 out of 156 refs were fixed (had 2 roots). The remaining 13 refs were already correct (single root).

### Upstream Fixes Needed
1. `StoreSignedCommit()` should fall back to `user.name`/`user.email` (not just `[author]` section)
2. `ReadAll()` should skip bad refs instead of aborting (or provide better error messages)
3. Entity authorship should use git-bug identity, not git config

### Repair Scripts
- `scripts/fix-gitbug-dag-v3.sh` - **Working fix**: removes duplicate branches
- `scripts/fix-gitbug-dag.sh` - v1 attempt (failed - clock overlap)
- `scripts/fix-gitbug-dag-v2.sh` - v2 attempt (failed - duplicate CreateOp)

## Watchdog Script

Add to watcher startup or cron to detect corruption early:

```bash
#!/bin/bash
# check-gitbug-health.sh - Detect git-bug DAG corruption

check_dag_health() {
    local errors=0
    
    # Check for empty authorship on new commits
    for ref in $(git for-each-ref --format='%(refname)' refs/bugs/ refs/identities/); do
        author=$(git log -1 --format='%an' "$ref" 2>/dev/null)
        if [ -z "$author" ]; then
            echo "WARN: Empty author on $ref"
            ((errors++))
        fi
    done
    
    # Check for broken DAG (commits with no parents that aren't root)
    for ref in $(git for-each-ref --format='%(refname)' refs/bugs/ refs/identities/); do
        local first=true
        for c in $(git rev-list --reverse "$ref" 2>/dev/null); do
            if [ "$first" = "true" ]; then
                first=false
                continue
            fi
            parents=$(git cat-file -p "$c" 2>/dev/null | grep -c '^parent ')
            if [ "$parents" -eq 0 ]; then
                echo "ERROR: Broken DAG in $ref - commit $c has no parents"
                ((errors++))
                break
            fi
        done
    done
    
    # Try cache build
    rm -rf .git/git-bug 2>/dev/null
    if ! git-bug bug -f json >/dev/null 2>&1; then
        echo "ERROR: git-bug cache build failed"
        ((errors++))
    fi
    
    return $errors
}

check_dag_health
exit $?
```

Run before watcher startup:
```bash
./check-gitbug-health.sh || echo "git-bug health check failed"
```

## Related

- git-bug uses go-git: https://github.com/go-git/go-git
- git-bug repo: https://github.com/git-bug/git-bug
- go-git commit handling: https://github.com/go-git/go-git/blob/master/plumbing/object/commit.go
