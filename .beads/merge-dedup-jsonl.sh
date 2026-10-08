#!/usr/bin/env bash
# Git merge driver for beads issues.jsonl.
#
# br rewrites the whole file in canonical order on every change, so two branches
# that both touched it don't produce a small conflict hunk — they produce large
# overlapping regions. Plain `merge=union` stitches both copies together and
# duplicates every id. This driver takes the union of both sides and resolves each
# id that differs: the newer updated_at wins; on an updated_at tie the side that
# changed from the merge base wins; a tie where both sides changed is a conflict.
#
# The merge base matters because records are rewritten without an updated_at bump
# (comment id renumbering, source_repo_path, text fixes). Without it a tie can only
# be broken blindly, and during a rebase that silently restores the replayed
# commit's stale copy over upstream's rewrite.
#
# Registered as merge driver "beads-union-dedup" (see .gitattributes). devtools'
# install-git-hooks.py copies this file to .beads/merge-dedup-jsonl.sh in a beads
# repository and registers the driver. The driver path is repo-relative, so one
# --global registration covers every clone (git runs merge drivers from each repo's root):
#   git config --global merge.beads-union-dedup.name "beads issues.jsonl three-way union by updated_at"
#   git config --global merge.beads-union-dedup.driver ".beads/merge-dedup-jsonl.sh %A %B %O"
#
# Args: $1 = ours/%A (also the output path), $2 = theirs/%B, $3 = merge base/%O.
# The base is last so a two-argument registration still runs; without it every
# differing tie is a conflict. Exit 1 = conflict: ours is kept for those ids.
set -euo pipefail

ours="$1"
theirs="$2"
base="${3:-}"
has_base=false
if [[ -n "$base" ]]; then
    has_base=true
else
    base=/dev/null
fi

merged="$(mktemp)"
conflicts="$(mktemp)"
trap 'rm -f "$merged" "$conflicts"' EXIT

# runMerge <filter>: resolve every id, then apply filter to the [{record, conflict?}] list.
runMerge() {
    jq -n -c --slurpfile ours "$ours" --slurpfile theirs "$theirs" --slurpfile base "$base" \
        --argjson has_base "$has_base" "
        INDEX(\$ours[]; .id) as \$a | INDEX(\$theirs[]; .id) as \$b | INDEX(\$base[]; .id) as \$o
        | [\$a, \$b | keys[]] | unique
        | map(. as \$id | \$a[\$id] as \$x | \$b[\$id] as \$y
            | if \$x == null then {record: \$y}
              elif \$y == null or \$x == \$y then {record: \$x}
              elif \$x.updated_at != \$y.updated_at then {record: (if \$x.updated_at > \$y.updated_at then \$x else \$y end)}
              elif \$has_base and \$x == \$o[\$id] then {record: \$y}
              elif \$has_base and \$y == \$o[\$id] then {record: \$x}
              else {record: \$x, conflict: \$id} end)
        | $1"
}

runMerge '.[].record' >"$merged"
runMerge '.[].conflict // empty' >"$conflicts"

cat "$merged" >"$ours"
if [[ -s "$conflicts" ]]; then
    printf 'beads merge: both sides changed these issues with the same updated_at; kept ours, resolve by hand:\n' >&2
    jq -r . "$conflicts" | sed 's/^/  /' >&2
    exit 1
fi
