#!/bin/bash
# fix-gitbug-dag-v3.sh - Remove duplicate branches from git-bug refs
#
# The issue: Authorship fix created duplicate branches with identical ops.
# Both branches have the same operations, so we just keep the main branch
# (the one with the true root) and discard the orphan/duplicate branch.
#
# This script:
# 1. Identifies refs with multiple roots (broken DAG)
# 2. Finds the true root (lowest create-clock)
# 3. Finds the main branch tip (the ancestor path from merge to true root)
# 4. Updates ref to point to main branch tip (discards duplicate branch)

fix_ref() {
    local ref="$1"
    local short_id="${ref#refs/bugs/}"
    short_id="${short_id:0:12}"

    # Get all commits
    local all_commits
    mapfile -t all_commits < <(git rev-list "$ref")

    # Find roots
    local roots=()
    for commit in "${all_commits[@]}"; do
        local parent_count
        parent_count=$(git cat-file -p "$commit" | grep -c '^parent' 2>/dev/null || true)
        parent_count="${parent_count:-0}"
        if [ "$parent_count" = "0" ]; then
            roots+=("$commit")
        fi
    done

    if [ ${#roots[@]} -le 1 ]; then
        return 0  # No fix needed
    fi

    echo "Fixing ${short_id} (${#roots[@]} roots found)"

    # Find merge commit (has 2 parents)
    local merge_commit=""
    for commit in "${all_commits[@]}"; do
        local parent_count
        parent_count=$(git cat-file -p "$commit" | grep -c '^parent' 2>/dev/null || true)
        if [ "$parent_count" = "2" ]; then
            merge_commit="$commit"
            break
        fi
    done

    if [ -z "$merge_commit" ]; then
        echo "  No merge commit found - skipping"
        return 1
    fi

    # Get the two parents of merge
    local parents
    mapfile -t parents < <(git cat-file -p "$merge_commit" | grep '^parent' | awk '{print $2}')

    # Find which parent leads to true root (lowest create-clock)
    local main_tip=""
    local min_root_clock=999999999

    for parent_tip in "${parents[@]}"; do
        # Walk back to find root
        local current="$parent_tip"
        while true; do
            local p=$(git cat-file -p "$current" | grep '^parent' | head -1 | awk '{print $2}')
            if [ -z "$p" ]; then
                # current is a root
                local tree=$(git cat-file -p "$current" | head -1 | awk '{print $2}')
                local create_clock=$(git ls-tree "$tree" 2>/dev/null | grep -oP 'create-clock-\K\d+' || echo "999999999")
                if [ "$create_clock" -lt "$min_root_clock" ]; then
                    min_root_clock="$create_clock"
                    main_tip="$parent_tip"
                fi
                break
            fi
            current="$p"
        done
    done

    if [ -z "$main_tip" ]; then
        echo "  Could not determine main branch - skipping"
        return 1
    fi

    # Update ref to point to main branch tip (discard merge and duplicate branch)
    echo "  Main branch tip: ${main_tip:0:12}"
    echo "  Discarding merge ${merge_commit:0:12} and duplicate branch"
    git update-ref "$ref" "$main_tip"
    echo "  Updated ref to ${main_tip:0:12}"
}

# Main
echo "Scanning refs/bugs/ for broken DAGs..."
fixed=0
for ref in $(git for-each-ref --format='%(refname)' refs/bugs/); do
    roots=()
    for commit in $(git rev-list "$ref" 2>/dev/null); do
        parent_count=$(git cat-file -p "$commit" | grep -c '^parent' 2>/dev/null || true)
        parent_count="${parent_count:-0}"
        if [ "$parent_count" = "0" ]; then
            roots+=("$commit")
        fi
    done
    if [ ${#roots[@]} -gt 1 ]; then
        if fix_ref "$ref"; then
            ((fixed++))
        fi
    fi
done

echo ""
echo "Fixed $fixed refs."
echo "Clearing cache..."
rm -rf .git/git-bug

echo ""
echo "Testing git-bug..."
if git-bug bug -f json >/dev/null 2>&1; then
    echo "SUCCESS: git-bug cache builds correctly"
    echo "Bug count: $(git-bug bug -f json | jq '. | length')"
else
    echo "ERROR: git-bug still failing"
    git-bug bug 2>&1 | head -5
fi
