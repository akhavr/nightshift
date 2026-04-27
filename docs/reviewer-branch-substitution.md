# Reviewer Branch Name Substitution

## Problem

The automated reviewer sometimes substitutes `main` for `master` (or vice versa) when running git commands, even when the prompt explicitly specifies the correct branch name.

**Example:**
- Prompt says: `git log --oneline HEAD..master`
- Reviewer runs: `git log --oneline HEAD..main`

This causes false "branch behind" verdicts when the repo uses `master` but reviewer checks against `main`.

## Root Cause

Instruction-following failure. The model sees `master` in the instructions but substitutes `main` — likely due to training data bias where `main` is more common in recent repos.

This is different from line number hallucination (docs/reviewer-false-positives.md) — the reviewer deliberately changes the command rather than misreading output.

## Current Mitigations

1. **Explicit warning in REVIEW.md** — Added instruction: "Use `{{ base_branch }}` exactly as shown. Do NOT substitute `main` for `master` or vice versa."

2. **Template variable** — REVIEW.md uses `{{ base_branch }}` which injects the correct branch name from config, ensuring the prompt always shows the right value.

## Potential Future Mitigations

### Host-side validation (not implemented)

Parse reviewer's conversation after verdict to check git commands used correct branch:

```python
def validate_reviewer_commands(conversation, base_branch):
    """Check reviewer used correct branch in git commands."""
    for entry in conversation:
        if 'git log' in entry.get('content', ''):
            match = re.search(r'HEAD\.\.(\S+)', entry['content'])
            if match and match.group(1) != base_branch:
                return f"Reviewer used '{match.group(1)}' instead of '{base_branch}'"
    return None  # Valid
```

If validation fails:
- Log the error
- Ignore the revise verdict
- Either auto-approve or re-run review with stronger prompt

### Finetuning

Train on examples where exact instruction-following matters, penalizing substitutions.

## Detection

If reviewer issues `@nightshift revise` claiming "branch behind":

```bash
# Verify actual branch status
git log --oneline HEAD..<actual-base-branch> | head -5
```

If empty, the branch is NOT behind — reviewer used wrong branch name.

## Related

- docs/reviewer-false-positives.md — line number hallucination (different failure mode)
- REVIEW.md — template with explicit warning added
