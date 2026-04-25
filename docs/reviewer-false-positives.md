# Reviewer False Positives: Line Number Hallucination

## Problem

The automated reviewer (REVIEW.md) sometimes cites incorrect line numbers when flagging issues. Example:

- **Claim:** "host/rebase.py:21 has `import os` inside function"
- **Reality:** Line 21 is a docstring; `import os` is at line 9 (module top)

This causes unnecessary revise cycles for code that is actually correct.

## Root Cause

The reviewer reads the diff and makes assumptions about line numbers without reading the actual file. Diff line markers (`+`/`-` lines) don't correspond to actual file line numbers because:

1. Diffs show context around changes, not absolute positions
2. Line numbers shift after edits in the same file
3. Multiple hunks in a diff can confuse line counting

## Solution

REVIEW.md v4 adds:

1. **Pre-verdict verification checkpoint** - Reviewer must read the actual file and quote the line content before citing it as an issue
2. **Anti-pattern warning** - Explicit instruction to never cite line numbers from diff output

## Detection

If a reviewer issues `@nightshift revise` with line number citations:

```bash
# Verify the claim
git show agent/<branch>:<file> | sed -n '<line>p'
```

If the content doesn't match the claim, it's a false positive - accept the code.

## Related

- REVIEW.md template_version: 4 (adds verification)
- Issue pattern: reviewer correctly identifies a problem exists, but cites wrong location
