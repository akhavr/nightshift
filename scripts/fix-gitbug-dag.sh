#!/bin/bash
# fix-gitbug-dag.sh - Repair git-bug refs with broken DAG (multiple roots)
#
# The issue: Previous authorship fix script broke parent relationships in refs
# with merge commits, creating orphan root commits that should have parents.
#
# This script:
# 1. Identifies refs with multiple root commits
# 2. For each orphan root (except the true root with lowest clock), finds its logical predecessor
# 3. Rewrites commits to restore proper parent chains
# 4. Updates refs to point to the repaired tips

# set -e  # Disabled to continue on errors

AUTHOR_NAME="${GIT_AUTHOR_NAME:-@akhavr}"
AUTHOR_EMAIL="${GIT_AUTHOR_EMAIL:-akhavr@42cc.co}"

# Get clock value from a commit's tree
get_clock() {
    local commit="$1"
    local tree
    tree=$(git cat-file -p "$commit" | head -1 | awk '{print $2}')
    local create edit
    create=$(git ls-tree "$tree" 2>/dev/null | grep -oP 'create-clock-\K\d+' || echo "")
    edit=$(git ls-tree "$tree" 2>/dev/null | grep -oP 'edit-clock-\K\d+' || echo "")
    echo "${edit:-${create:-0}}"
}

# Find all root commits in a ref
find_roots() {
    local ref="$1"
    local roots=()
    for commit in $(git rev-list "$ref"); do
        local parents
        parents=$(git cat-file -p "$commit" | grep -c '^parent' 2>/dev/null || true)
        parents="${parents:-0}"
        if [ "$parents" = "0" ]; then
            roots+=("$commit")
        fi
    done
    printf '%s\n' "${roots[@]}"
}

# Find the true root (lowest create-clock)
find_true_root() {
    local ref="$1"
    local min_clock=999999999
    local true_root=""

    for commit in $(git rev-list "$ref"); do
        local tree parents create_clock
        tree=$(git cat-file -p "$commit" | head -1 | awk '{print $2}')
        parents=$(git cat-file -p "$commit" | grep -c '^parent' 2>/dev/null || true)
        parents="${parents:-0}"

        if [ "$parents" = "0" ]; then
            create_clock=$(git ls-tree "$tree" 2>/dev/null | grep -oP 'create-clock-\K\d+' || echo "999999999")
            if [ "$create_clock" -lt "$min_clock" ]; then
                min_clock="$create_clock"
                true_root="$commit"
            fi
        fi
    done
    echo "$true_root"
}

# Build a clock-to-commit map for a ref
build_clock_map() {
    local ref="$1"
    # Returns lines of: clock commit
    for commit in $(git rev-list "$ref"); do
        local clock
        clock=$(get_clock "$commit")
        echo "$clock $commit"
    done | sort -n
}

# Find the commit with the highest clock value less than or equal to the given clock
# (excluding the orphan itself), preferring commits with fewer parents
find_predecessor() {
    local ref="$1"
    local target_clock="$2"
    local orphan_commit="$3"
    local best_commit=""
    local best_clock=-1

    for commit in $(git rev-list "$ref"); do
        [ "$commit" = "$orphan_commit" ] && continue  # Skip the orphan itself
        local clock
        clock=$(get_clock "$commit")
        # Find commit with clock <= target_clock that has the highest clock
        if [ "$clock" -le "$target_clock" ] && [ "$clock" -gt "$best_clock" ]; then
            best_clock="$clock"
            best_commit="$commit"
        fi
    done
    echo "$best_commit"
}

# Increment the edit-clock in a tree, returning new tree hash
# This is needed when orphan has same clock as predecessor (violates clock ordering)
increment_tree_clock() {
    local old_tree="$1"
    local increment="${2:-1}"

    # Get current edit-clock value
    local old_clock_file old_clock
    old_clock_file=$(git ls-tree "$old_tree" | grep -oP 'edit-clock-\d+' | head -1)
    if [ -z "$old_clock_file" ]; then
        echo "$old_tree"  # No edit-clock, return unchanged
        return
    fi
    old_clock="${old_clock_file#edit-clock-}"
    local new_clock=$((old_clock + increment))
    local new_clock_file="edit-clock-$new_clock"

    # Build new tree with renamed clock file
    # Format: mode SP type SP hash TAB name
    local tree_content=""
    while IFS=$'\t' read -r mode_type_hash name; do
        if [ "$name" = "$old_clock_file" ]; then
            # Rename the clock file
            tree_content+="$mode_type_hash"$'\t'"$new_clock_file"$'\n'
        else
            tree_content+="$mode_type_hash"$'\t'"$name"$'\n'
        fi
    done < <(git ls-tree "$old_tree")

    # Create new tree object
    local new_tree
    new_tree=$(echo -n "$tree_content" | git mktree)
    echo "$new_tree"
}

# Rebuild a ref's commit chain with fixed parent relationships
fix_ref() {
    local ref="$1"
    local short_id="${ref#refs/bugs/}"
    short_id="${short_id:0:12}"

    # Find roots
    local roots
    mapfile -t roots < <(find_roots "$ref")

    if [ ${#roots[@]} -le 1 ]; then
        return 0  # No fix needed
    fi

    echo "Fixing ${short_id} (${#roots[@]} roots found)"

    # Find true root
    local true_root
    true_root=$(find_true_root "$ref")
    echo "  True root: ${true_root:0:12}"

    # Build the set of orphan roots and their needed parents
    # Also track which orphans need clock increment (same clock as predecessor)
    declare -A orphan_parents
    declare -A orphan_needs_clock_bump
    for root in "${roots[@]}"; do
        if [ "$root" = "$true_root" ]; then
            continue
        fi
        local clock predecessor pred_clock
        clock=$(get_clock "$root")
        predecessor=$(find_predecessor "$ref" "$clock" "$root")
        if [ -n "$predecessor" ]; then
            orphan_parents[$root]="$predecessor"
            pred_clock=$(get_clock "$predecessor")
            if [ "$clock" -le "$pred_clock" ]; then
                # Need to increment clock to satisfy ordering constraint
                orphan_needs_clock_bump[$root]=$((pred_clock - clock + 1))
                echo "  Orphan ${root:0:12} (clock $clock) -> parent ${predecessor:0:12} (clock $pred_clock) [BUMP +${orphan_needs_clock_bump[$root]}]"
            else
                echo "  Orphan ${root:0:12} (clock $clock) -> parent ${predecessor:0:12}"
            fi
        else
            echo "  WARNING: Could not find predecessor for orphan ${root:0:12}"
        fi
    done

    # Now we need to rebuild the commit graph
    # Strategy: traverse in reverse topological order (oldest first by clock)
    # For each commit, if it's an orphan, add its parent; if its parent was rewritten, use new hash
    # IMPORTANT: When we bump an orphan's clock, we must also bump all its descendants

    declare -A commit_map  # old_commit -> new_commit
    declare -A clock_offset  # commit -> cumulative clock offset to apply

    # Initialize clock offsets for orphans and propagate to descendants
    for orphan in "${!orphan_needs_clock_bump[@]}"; do
        clock_offset[$orphan]="${orphan_needs_clock_bump[$orphan]}"
    done

    # Sort commits by clock
    local sorted_commits
    mapfile -t sorted_commits < <(build_clock_map "$ref" | awk '{print $2}')

    for old_commit in "${sorted_commits[@]}"; do
        local tree message old_parents new_parent_args=""
        tree=$(git cat-file -p "$old_commit" | head -1 | awk '{print $2}')
        message=$(git cat-file -p "$old_commit" | sed '1,/^$/d')  # Everything after the blank line

        # Check if any parent has a clock offset - if so, inherit it
        for parent in $(git cat-file -p "$old_commit" | grep '^parent' | awk '{print $2}'); do
            if [ -n "${clock_offset[$parent]}" ] && [ -z "${clock_offset[$old_commit]}" ]; then
                clock_offset[$old_commit]="${clock_offset[$parent]}"
            fi
        done

        # Build parent args
        if [ -n "${orphan_parents[$old_commit]}" ]; then
            # This orphan needs a parent added
            local needed_parent="${orphan_parents[$old_commit]}"
            local mapped_parent="${commit_map[$needed_parent]:-$needed_parent}"
            new_parent_args="-p $mapped_parent"
        fi

        # Apply clock offset if needed
        if [ -n "${clock_offset[$old_commit]}" ]; then
            local bump="${clock_offset[$old_commit]}"
            tree=$(increment_tree_clock "$tree" "$bump")
        fi

        # Add existing parents (mapped to new hashes if rewritten)
        while read -r line; do
            if [[ "$line" =~ ^parent\ (.+)$ ]]; then
                local old_parent="${BASH_REMATCH[1]}"
                local new_parent="${commit_map[$old_parent]:-$old_parent}"
                new_parent_args="$new_parent_args -p $new_parent"
            fi
        done < <(git cat-file -p "$old_commit")

        # Check if any rewriting is needed
        local needs_rewrite=false
        if [ -n "${orphan_parents[$old_commit]}" ]; then
            needs_rewrite=true
        fi
        if [ -n "${clock_offset[$old_commit]}" ]; then
            needs_rewrite=true
        fi
        for old_parent in $(git cat-file -p "$old_commit" | grep '^parent' | awk '{print $2}'); do
            if [ -n "${commit_map[$old_parent]}" ]; then
                needs_rewrite=true
                break
            fi
        done

        if [ "$needs_rewrite" = "true" ]; then
            # Create new commit
            local new_commit
            new_commit=$(GIT_AUTHOR_NAME="$AUTHOR_NAME" GIT_AUTHOR_EMAIL="$AUTHOR_EMAIL" \
                         GIT_COMMITTER_NAME="$AUTHOR_NAME" GIT_COMMITTER_EMAIL="$AUTHOR_EMAIL" \
                         git commit-tree $tree $new_parent_args -m "$message")
            commit_map[$old_commit]="$new_commit"
            echo "  Rewrote ${old_commit:0:12} -> ${new_commit:0:12}"
        fi
    done

    # Update ref to new tip
    local old_tip new_tip
    old_tip=$(git rev-parse "$ref")
    new_tip="${commit_map[$old_tip]:-$old_tip}"

    if [ "$old_tip" != "$new_tip" ]; then
        git update-ref "$ref" "$new_tip"
        echo "  Updated $ref: ${old_tip:0:12} -> ${new_tip:0:12}"
    fi
}

# Main
echo "Scanning refs/bugs/ for broken DAGs..."
fixed=0
for ref in $(git for-each-ref --format='%(refname)' refs/bugs/); do
    mapfile -t roots < <(find_roots "$ref")
    if [ ${#roots[@]} -gt 1 ]; then
        fix_ref "$ref"
        ((fixed++))
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
else
    echo "ERROR: git-bug still failing"
    git-bug bug 2>&1 | head -5
fi
