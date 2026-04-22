# Git-Bug DAG Corruption from Authorship Fix

**Date:** 2026-04-22  
**Status:** RESOLVED

## Summary

156 git-bug refs were corrupted with broken DAG structure after running an authorship fix script. The fix was to discard duplicate branches since they contained identical operations.

## Symptoms

```
$ git-bug bug
Building cache...
panic: DFS failed
```

Or:
```
panic: multiple leafs in the entity DAG
```

Or:
```
invalid data: creation lamport time not set
```

## Corruption Pattern

Each corrupted ref had:
- 2 root commits (should be 1)
- A merge commit combining both branches
- Both branches with IDENTICAL `ops` blobs (same operation data, same clock values)

```
*   cd176180  (merge commit, 2 parents)
|\  
| * 9db44308  (orphan branch - duplicate ops)
| * ...
| * f8dcc794  (orphan ROOT with duplicate CreateOp)
* 1a9187b5   (main branch)
* ...
* 685d4883   (main ROOT with original CreateOp)
```

## Root Cause

An authorship fix script (see [gitbug-empty-authorship.md](gitbug-empty-authorship.md)) rewrote commits to add proper author/committer fields. It:
1. Created a new chain of commits with proper authorship (orphan branch)
2. Created a merge commit combining the orphan branch with the original chain
3. Both branches had IDENTICAL ops blobs (same operations, same clocks)

## Why git-bug Failed

Three different errors depending on the state:

1. **"multiple leafs in the entity DAG"**
   - git-bug detected two root commits
   - A valid entity DAG must have exactly one root (the CreateOp commit)

2. **"DFS failed"**
   - After making orphan root a child, clocks still overlapped
   - git-bug's DFS validation requires strictly increasing clocks

3. **"creation lamport time not set"**
   - After removing `create-clock` from orphan root, git-bug couldn't find the creation timestamp
   - Each entity needs exactly one `create-clock` on the CreateOp commit

## Diagnosis

Check for refs with multiple roots:
```bash
for ref in $(git for-each-ref --format='%(refname)' refs/bugs/); do
    roots=0
    for commit in $(git rev-list "$ref" 2>/dev/null); do
        parent_count=$(git cat-file -p "$commit" | grep -c '^parent' 2>/dev/null || echo 0)
        [ "$parent_count" = "0" ] && ((roots++))
    done
    [ "$roots" -gt 1 ] && echo "$ref has $roots roots"
done
```

Compare ops blobs between branches (if identical, branches are duplicates):
```bash
# Extract ops blob hash from a commit's tree
commit="<commit-hash>"
tree=$(git cat-file -p "$commit" | head -1 | awk '{print $2}')
git ls-tree "$tree" | grep '\tops$' | awk '{print $3}'
```

## The Fix

Since both branches contained IDENTICAL operations, the fix was simple:
1. For each ref with multiple roots, find the merge commit
2. Walk each parent branch back to find which leads to the "true root" (lowest `create-clock`)
3. Update the ref to point to main branch tip, discarding merge and duplicate branch

Script: `scripts/fix-gitbug-dag-v3.sh`

```bash
$ bash scripts/fix-gitbug-dag-v3.sh
Scanning refs/bugs/ for broken DAGs...
Fixing 007cd7dc6f61 (2 roots found)
  Main branch tip: 1a9187b52201
  Discarding merge cd1761802d97 and duplicate branch
  Updated ref to 1a9187b52201
...
Fixed 143 refs.
SUCCESS: git-bug cache builds correctly
Bug count: 156
```

## git-bug Entity Data Model

Each commit in a git-bug entity contains a tree with:
- `ops` blob: JSON with operations (`type:1` = CreateOp, `type:3` = AddComment, etc.)
- `create-clock-N` file: Only on root commit, marks creation timestamp
- `edit-clock-M` file: On all commits, lamport clock for ordering
- `version-4` file: Format version marker

### Clock Rules
- Clocks must be unique across the entire DAG
- Clocks must increase along all paths from root to tip
- Each entity has exactly one `create-clock` (on the CreateOp commit)
- Each entity has exactly one `CreateOp` (type:1) operation

## Failed Repair Attempts

1. **v1 script** (`scripts/fix-gitbug-dag.sh`): Made orphan root a child of main branch tip → "DFS failed" (clocks still overlapped)

2. **v2 script** (`scripts/fix-gitbug-dag-v2.sh`): Offset all clocks on orphan branch → Still failed because orphan root still had duplicate `CreateOp` in ops blob

3. **v3 script** (`scripts/fix-gitbug-dag-v3.sh`): Discard orphan branch entirely → SUCCESS (branches were identical anyway)

## Prevention

1. Never run scripts that create merge commits between identical histories
2. Authorship fix scripts should rewrite in-place (update refs directly), not create parallel branches
3. Consider backing up refs before any bulk rewrite operation

## Related

- [gitbug-empty-authorship.md](gitbug-empty-authorship.md) - Root cause: empty authorship from go-git config issue
- [gitbug-clock-corruption.md](gitbug-clock-corruption.md) - Different issue: empty lamport clock files
