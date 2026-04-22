# Git-Bug Lamport Clock Corruption

## Symptom

All `git bug` commands that read or write bugs fail with `Error: EOF`:
```
$ git bug bug show 297115a
Error: EOF

$ git bug bug new -t "test" -m "body"
Error: EOF
```

`git bug bug ls` may partially work (listing titles) but `show`, `new`, `comment`, `label` all fail.

`git bug pull` shows `invalid data: remote bug is not readable: EOF` for all bugs.

## Root Cause

The lamport clock file `.git/git-bug/clocks/bugs-edit` is empty (0 bytes). Git-bug reads this file on every operation that touches bugs. An empty file causes `io.EOF` when parsing, which propagates as `Error: EOF`.

**Location:** `.git/git-bug/clocks/bugs-edit` (and potentially `bugs-create`)

**How it happens:** Likely a crash or kill -9 during a git-bug write operation. The file is written non-atomically — if the process dies mid-write, the file can be truncated to 0 bytes.

## Diagnosis

```bash
# Check clock files
cat .git/git-bug/clocks/bugs-create
cat .git/git-bug/clocks/bugs-edit

# If either is empty, that's the problem
```

## Fix

Reconstruct the clock value from the bug data. Each bug's git tree has `edit-clock-N` and `create-clock-N` entries:

```bash
# Find max edit clock across all bugs
git for-each-ref --format='%(objectname)' refs/bugs/ | while read sha; do
  tree=$(git cat-file -p "$sha" | grep "^tree " | awk '{print $2}')
  [ -n "$tree" ] && git ls-tree "$tree" | grep "edit-clock-" | sed 's/.*edit-clock-//'
done | sort -n | tail -1

# Write it to the clock file (example: 6089)
echo "6089" > .git/git-bug/clocks/bugs-edit
```

Same for `bugs-create` if that's also empty:
```bash
git for-each-ref --format='%(objectname)' refs/bugs/ | while read sha; do
  tree=$(git cat-file -p "$sha" | grep "^tree " | awk '{print $2}')
  [ -n "$tree" ] && git ls-tree "$tree" | grep "create-clock-" | sed 's/.*create-clock-//'
done | sort -n | tail -1

echo "<value>" > .git/git-bug/clocks/bugs-create
```

## Red Herrings

- **Empty `author <>`  in git commit headers:** Git-bug doesn't use git commit author/committer fields. It stores authors in the ops JSON blob. Empty author fields are cosmetic — they don't cause EOF.
- **Rewriting commits:** Rewriting commit headers to add proper author fields is harmless but doesn't fix the problem.

## Prevention

The watcher should check lamport clock file integrity on startup and repair if needed. See issue for implementation.

## Affected Instances

- jessica-ng: `bugs-edit` was empty. Fixed 2026-04-05 by writing `6089` to the file.
