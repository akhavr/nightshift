#!/bin/bash
# fix-gitbug-dag-v2.sh - Properly repair git-bug refs with broken DAG
#
# The issue: Original authorship fix created two independent chains with
# IDENTICAL clock values. Both branches have clocks 191, 192, 193...
#
# This script:
# 1. Identifies refs with multiple roots
# 2. Keeps the "main" branch (the one NOT being orphaned)
# 3. Offsets ALL clocks on the orphan branch to be > max(main branch clocks)
# 4. Makes orphan root a child of the main branch tip
# 5. Updates refs

AUTHOR_NAME="${GIT_AUTHOR_NAME:-@akhavr}"
AUTHOR_EMAIL="${GIT_AUTHOR_EMAIL:-akhavr@42cc.co}"

# Get clock value from a commit's tree
get_clock() {
    local commit="$1"
    local tree
    tree=$(git cat-file -p "$commit" | head -1 | awk '{print $2}')
    local edit create
    edit=$(git ls-tree "$tree" 2>/dev/null | grep -oP 'edit-clock-\K\d+' || echo "")
    create=$(git ls-tree "$tree" 2>/dev/null | grep -oP 'create-clock-\K\d+' || echo "")
    echo "${edit:-${create:-0}}"
}

# Increment edit-clock and REMOVE create-clock (for non-root commits)
# $3 = "remove_create" to remove create-clock, else keep/increment it
rewrite_tree_clocks() {
    local old_tree="$1"
    local increment="$2"
    local remove_create="${3:-}"

    local tree_content=""
    while IFS=$'\t' read -r mode_type_hash name; do
        if [[ "$name" =~ ^create-clock-([0-9]+)$ ]]; then
            if [ "$remove_create" = "remove_create" ]; then
                # Skip create-clock for non-root commits
                continue
            else
                local old_clock="${BASH_REMATCH[1]}"
                local new_clock=$((old_clock + increment))
                tree_content+="$mode_type_hash"$'\t'"create-clock-${new_clock}"$'\n'
            fi
        elif [[ "$name" =~ ^edit-clock-([0-9]+)$ ]]; then
            local old_clock="${BASH_REMATCH[1]}"
            local new_clock=$((old_clock + increment))
            tree_content+="$mode_type_hash"$'\t'"edit-clock-${new_clock}"$'\n'
        else
            tree_content+="$mode_type_hash"$'\t'"$name"$'\n'
        fi
    done < <(git ls-tree "$old_tree")

    local new_tree
    new_tree=$(echo -n "$tree_content" | git mktree)
    echo "$new_tree"
}

# Get all commits in a branch starting from a given root
get_branch_commits() {
    local ref="$1"
    local root="$2"
    local tip="$3"

    # Walk from tip backwards, collecting commits until we hit root
    local commits=()
    local current="$tip"

    while true; do
        commits+=("$current")
        if [ "$current" = "$root" ]; then
            break
        fi
        # Get first parent (linear branch)
        current=$(git cat-file -p "$current" | grep '^parent' | head -1 | awk '{print $2}')
        if [ -z "$current" ]; then
            break
        fi
    done

    # Reverse to get oldest-first order
    local i
    for (( i=${#commits[@]}-1; i>=0; i-- )); do
        echo "${commits[$i]}"
    done
}

fix_ref() {
    local ref="$1"
    local short_id="${ref#refs/bugs/}"
    short_id="${short_id:0:12}"

    # Get all commits and find roots
    local all_commits roots=()
    mapfile -t all_commits < <(git rev-list "$ref")

    for commit in "${all_commits[@]}"; do
        local parent_count
        parent_count=$(git cat-file -p "$commit" | grep -c '^parent' 2>/dev/null || true)
        parent_count="${parent_count:-0}"
        if [ "$parent_count" = "0" ]; then
            roots+=("$commit")
        fi
    done

    if [ ${#roots[@]} -le 1 ]; then
        return 0
    fi

    echo "Fixing ${short_id} (${#roots[@]} roots)"

    # Find the merge commit (commit with 2 parents)
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

    # Get the two parent branches of the merge
    local parents
    mapfile -t parents < <(git cat-file -p "$merge_commit" | grep '^parent' | awk '{print $2}')
    local branch1_tip="${parents[0]}"
    local branch2_tip="${parents[1]}"

    # Trace each branch back to its root
    local branch1_root branch2_root
    local current="$branch1_tip"
    while true; do
        local parent=$(git cat-file -p "$current" | grep '^parent' | head -1 | awk '{print $2}')
        if [ -z "$parent" ]; then
            branch1_root="$current"
            break
        fi
        current="$parent"
    done

    current="$branch2_tip"
    while true; do
        local parent=$(git cat-file -p "$current" | grep '^parent' | head -1 | awk '{print $2}')
        if [ -z "$parent" ]; then
            branch2_root="$current"
            break
        fi
        current="$parent"
    done

    # Determine which branch is "main" (has lower root clock = created first)
    local root1_clock root2_clock
    root1_clock=$(get_clock "$branch1_root")
    root2_clock=$(get_clock "$branch2_root")

    local main_branch_tip main_branch_root orphan_branch_tip orphan_branch_root
    if [ "$root1_clock" -le "$root2_clock" ]; then
        main_branch_tip="$branch1_tip"
        main_branch_root="$branch1_root"
        orphan_branch_tip="$branch2_tip"
        orphan_branch_root="$branch2_root"
    else
        main_branch_tip="$branch2_tip"
        main_branch_root="$branch2_root"
        orphan_branch_tip="$branch1_tip"
        orphan_branch_root="$branch1_root"
    fi

    echo "  Main branch: ${main_branch_root:0:8} -> ${main_branch_tip:0:8}"
    echo "  Orphan branch: ${orphan_branch_root:0:8} -> ${orphan_branch_tip:0:8}"

    # Find max clock on main branch
    local max_main_clock=0
    current="$main_branch_tip"
    while true; do
        local clock=$(get_clock "$current")
        if [ "$clock" -gt "$max_main_clock" ]; then
            max_main_clock="$clock"
        fi
        local parent=$(git cat-file -p "$current" | grep '^parent' | head -1 | awk '{print $2}')
        if [ -z "$parent" ]; then
            break
        fi
        current="$parent"
    done

    # Calculate offset for orphan branch (all clocks must be > max_main_clock)
    local orphan_min_clock
    orphan_min_clock=$(get_clock "$orphan_branch_root")
    local offset=$((max_main_clock - orphan_min_clock + 1))
    echo "  Max main clock: $max_main_clock, orphan min: $orphan_min_clock, offset: $offset"

    # Rewrite the orphan branch with offset clocks and new parent for root
    declare -A commit_map

    # Get orphan branch commits in order
    local orphan_commits
    mapfile -t orphan_commits < <(get_branch_commits "$ref" "$orphan_branch_root" "$orphan_branch_tip")

    for old_commit in "${orphan_commits[@]}"; do
        local old_tree message new_tree new_parent_args=""
        old_tree=$(git cat-file -p "$old_commit" | head -1 | awk '{print $2}')
        message=$(git cat-file -p "$old_commit" | sed '1,/^$/d')

        # Rewrite clocks - increment both edit-clock and create-clock
        new_tree=$(rewrite_tree_clocks "$old_tree" "$offset")

        # Set parent
        if [ "$old_commit" = "$orphan_branch_root" ]; then
            # Root of orphan branch becomes child of main branch tip
            new_parent_args="-p $main_branch_tip"
        else
            # Other commits point to their rewritten parent
            local old_parent
            old_parent=$(git cat-file -p "$old_commit" | grep '^parent' | head -1 | awk '{print $2}')
            local new_parent="${commit_map[$old_parent]:-$old_parent}"
            new_parent_args="-p $new_parent"
        fi

        # Create new commit
        local new_commit
        new_commit=$(GIT_AUTHOR_NAME="$AUTHOR_NAME" GIT_AUTHOR_EMAIL="$AUTHOR_EMAIL" \
                     GIT_COMMITTER_NAME="$AUTHOR_NAME" GIT_COMMITTER_EMAIL="$AUTHOR_EMAIL" \
                     git commit-tree "$new_tree" $new_parent_args -m "$message")
        commit_map[$old_commit]="$new_commit"
    done

    # Rewrite merge commit with new orphan branch tip
    local new_orphan_tip="${commit_map[$orphan_branch_tip]}"
    local merge_tree merge_message new_merge
    merge_tree=$(git cat-file -p "$merge_commit" | head -1 | awk '{print $2}')
    merge_message=$(git cat-file -p "$merge_commit" | sed '1,/^$/d')

    # Rewrite merge commit clock too (no create-clock on merge commits)
    local merge_clock=$(get_clock "$merge_commit")
    local new_merge_tree=$(rewrite_tree_clocks "$merge_tree" "$offset")

    new_merge=$(GIT_AUTHOR_NAME="$AUTHOR_NAME" GIT_AUTHOR_EMAIL="$AUTHOR_EMAIL" \
                GIT_COMMITTER_NAME="$AUTHOR_NAME" GIT_COMMITTER_EMAIL="$AUTHOR_EMAIL" \
                git commit-tree "$new_merge_tree" -p "$main_branch_tip" -p "$new_orphan_tip" -m "$merge_message")

    # Update ref
    git update-ref "$ref" "$new_merge"
    echo "  Updated: ${merge_commit:0:12} -> ${new_merge:0:12}"
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
