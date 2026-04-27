# Per-Session Overflow Profile Selection

## Current Behavior

Overflow profile selection is global, controlled by a flag file at `.nightshift/overflow.flag`:

```bash
nightshift overflow on                    # enable default overflow
nightshift overflow --profile codex-gpt54 # enable specific profile
nightshift overflow off                   # disable overflow
```

All sessions started while overflow is enabled use the selected profile.

## Limitation

No way to run a single session with a specific profile without affecting other concurrent sessions or forgetting to reset.

Current workaround:
```bash
nightshift overflow --profile codex-gpt54
nightshift start <issue>
nightshift overflow off  # must remember to reset
```

## Proposed Feature

Add `--profile` flag to `start` and `resume` commands:

```bash
nightshift start <issue> --profile codex-gpt54
nightshift resume <issue> --profile codex-gpt54
```

This would:
1. Override the global flag file for this session only
2. Not modify the flag file
3. Allow concurrent sessions with different profiles

## Implementation Notes

- `cmd_start` and `cmd_resume` in `host/cli.py` would accept `--profile`
- Pass profile name to `launch.py` via new `--overflow-profile` arg
- `launch.py` would load the named profile from `overflow_profiles` in WORKFLOW.md
- Global flag file remains for default behavior when `--profile` not specified

## Use Cases

1. **Review with stronger model**: Run review step with gpt-5.4 while coder uses gpt-5.4-mini
2. **One-off capability boost**: Single complex issue needs stronger model
3. **A/B testing**: Compare agent performance across profiles without global toggle
