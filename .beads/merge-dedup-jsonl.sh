#!/usr/bin/env bash
# Git merge driver for beads issues.jsonl.
#
# br rewrites the whole export in id order, so two branches that both touched it
# overlap everywhere. This driver unions the ids. A row on one side only is kept.
# Rows that match once comment ids and source_repo_path are ignored, and once
# updated_at and created_at are normalized, collapse to one row: the side that
# is not an exact copy of the base, otherwise ours. A row changed on one side
# only is taken from that side.
#
# A row changed on both sides is merged field by field against the base: a
# field changed on one side wins, the same change on both sides is accepted,
# different changes to one field conflict. status, closed_at and close_reason
# are one field, so a close on one side never mixes with the other side's
# status. labels and dependencies are three-way sets, so a removal on one side
# holds; dependencies are keyed by type and depends_on_id, and one whose other
# fields changed differently on both sides conflicts. comments are the union by
# author, created_at and text, ours' ids first. updated_at is the newer of the
# two; created_at and source_repo_path are ours.
#
# Before that, a row changed on both sides whose one side is a snapshot the other
# has moved past is taken whole from the newer side. That side is newer by
# updated_at and holds every comment of the older side, and the older side added
# a comment since the base (any comment, for a row new on both sides). This is an
# export of a shared beads DB taken mid-run; a comment both sides hold keeps
# ours' id, as above. A field merge would take its stale status as a one-sided
# change wherever the newer side moved it back to the base value, and reopen the
# issue without a conflict. Without such a comment nothing shows the newer side
# saw the older one, and the field merge runs. The comments show only what the
# newer side saw up to the older side's last comment: an older side whose
# updated_at is a later second than that comment changed again after it, perhaps
# unseen (a separate DB that imported the comment). If it also changed a field
# from the base that the newer side does not hold, the row conflicts, naming the
# fields, instead of a silent pick of either side.
#
# Comment ids are unique in the output: a later duplicate gets a fresh id above
# every input's maximum.
#
# Anything else conflicts. %A stays valid JSONL (ours kept for that id; br
# refuses conflict markers). Both rows are written to
# .beads/merge-conflict.ours.jsonl and .beads/merge-conflict.theirs.jsonl,
# and only when this run has a conflict, so a clean run leaves an earlier pair
# in place.
#
# A different normalized created_at is two issues under one id. A one-sided
# edit older than the base is a stale export being replayed. With no base, a
# one-sided edit cannot be told from a two-sided one, so any real difference
# conflicts. Fractions are padded to nine digits before compare: a six-digit
# fraction sorts after a later nine-digit fraction as a string.
#
# Registered as merge driver "beads-union-dedup" (see .gitattributes). devtools'
# install-git-hooks.py copies this file to .beads/merge-dedup-jsonl.sh in a beads
# repository and registers the driver. Git runs this from the work tree root, so
# the side files are relative to that root:
#   git config --global merge.beads-union-dedup.driver ".beads/merge-dedup-jsonl.sh %A %B %O"
#
# Args: $1 = ours/%A (also the output path), $2 = theirs/%B, $3 = merge base/%O.
# The base is last so a two-argument registration still runs. Exit 1 = conflict
# or unreadable input. Unreadable input leaves %A untouched.
set -euo pipefail

ours=$1
theirs=$2
base=${3:-}
has_base=false
if [[ -n $base ]]; then
    has_base=true
else
    base=/dev/null
fi

validate_jsonl() {
    local path=$1
    if ! jq empty "$path" >/dev/null 2>&1; then
        printf 'beads merge: %s: not valid JSON\n' "$path" >&2
        exit 1
    fi
}

validate_jsonl "$ours"
validate_jsonl "$theirs"
if [[ $has_base == true ]]; then
    validate_jsonl "$base"
fi

# shellcheck disable=SC2016
JQ_PROGRAM=$(
    cat <<'JQ'
def stamp: ((. // "") | capture("^(?<base>[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2})(\\.(?<frac>[0-9]+))?")) // null
    | if . == null then "" else .base + "." + ((.frac // "") + "000000000")[0:9] end;
def canon:
    del(.source_repo_path)
    | if (.comments | type) == "array"
        then .comments |= map(if type == "object" then del(.id) else . end)
        else . end
    | .created_at = (.created_at | stamp)
    | .updated_at = (.updated_at | stamp);
def same($left; $right): ($left | canon) == ($right | canon);
def older_than($row; $other): ($row.updated_at | stamp) < ($other.updated_at | stamp);
def clash($id; $left; $right; $why): {record: $left, conflict: $id, why: $why, ours: $left, theirs: $right};

# A field with its presence: null when the key is absent, {v: value} when present, so a present null is a value.
def slot($row; $key): if ($row | has($key)) then {v: $row[$key]} else null end;
def put_slot($key; $slot): if $slot == null then del(.[$key]) else .[$key] = $slot.v end;
# One-sided change wins, the same change on both sides is accepted, different changes conflict.
def pick3($o; $a; $b):
    if $a == $o then {slot: $b}
    elif $b == $o then {slot: $a}
    elif $a == $b then {slot: $a}
    else {conflict: true}
    end;

def dedupe_keys: reduce .[] as $key ({seen: {}, out: []};
    if .seen[$key] then . else .seen[$key] = true | .out += [$key] end) | .out;
def label_entries: (. // []) | map({key: tostring, v: .});
def dep_entries: (. // []) | map({key: (if type == "object" then "\(.type)\u001f\(.depends_on_id)" else tojson end), v: .});
def is_list_or_absent($key): all(.[$key] == null or (.[$key] | type) == "array");
# Three-way merge of keyed entries: base order, then ours' additions, then theirs'.
def merge_keyed($o; $a; $b):
    (INDEX($o[]; .key) | map_values(.v)) as $oi
    | (INDEX($a[]; .key) | map_values(.v)) as $ai
    | (INDEX($b[]; .key) | map_values(.v)) as $bi
    | reduce ([$o, $a, $b] | map(.[].key) | dedupe_keys)[] as $key ({merged: [], conflicts: []};
        pick3(slot($oi; $key); slot($ai; $key); slot($bi; $key)) as $pick
        | if $pick.conflict then .conflicts += [$key]
          elif $pick.slot == null then .
          else .merged += [$pick.slot.v]
          end);

def comment_key: if type == "object" then "\(.author)\u001f\(.created_at)\u001f\(.text)" else tojson end;
def merge_comments($a; $b):
    ($a // []) as $ac | ($b // []) as $bc
    | INDEX($ac[]; comment_key) as $ai
    | $ac + [$bc[] | select($ai[comment_key] == null)];

# $stale is older than $fresh, $fresh holds every comment of $stale, and $stale added one since the base: $fresh
# has seen $stale, so $stale is a snapshot of a shared DB exported mid-run, not an independent edit.
def comment_list: if type == "array" then . else [] end;
def superseded($stale; $fresh; $base_row):
    if ($stale.comments | type) != "array" or ($stale.updated_at | stamp) == "" or (older_than($stale; $fresh) | not)
    then false
    else
        INDEX($fresh.comments | comment_list | .[]; comment_key) as $fi
        | INDEX($base_row.comments? | comment_list | .[]; comment_key) as $oi
        | ($stale.comments | map(comment_key)) as $sk
        | all($sk[]; $fi[.] != null) and any($sk[]; $oi[.] == null)
    end;

# br stamps updated_at when it adds a comment; comment created_at has whole seconds.
def edited_after_last_comment($row):
    ($row.updated_at | stamp | .[0:19]) > ([$row.comments[] | objects | .created_at | stamp | .[0:19]] | max // "");
# Fields $stale changed from the base that $fresh does not hold: what taking $fresh would lose.
def lost_fields($stale; $fresh; $base_row):
    ($base_row // {}) as $o
    | [[$stale, $fresh, $o] | map(keys_unsorted[]) | dedupe_keys | .[]
        | select(IN("comments", "updated_at", "created_at", "source_repo_path") | not)
        | . as $key | select(slot($stale; $key) != slot($o; $key) and slot($stale; $key) != slot($fresh; $key))];
def take_newer($side; $stale; $fresh; $base_row; $row):
    lost_fields($stale; $fresh; $base_row) as $lost
    | if edited_after_last_comment($stale) and ($lost | length) > 0 then
          clash($row.id; $row.ours; $row.theirs; "\($side) is older and was edited after its last comment: \($lost | join(", "))")
      else {record: ($fresh | .comments = merge_comments($row.ours.comments; $row.theirs.comments))}
      end;

def newer_updated_at($x; $y):
    if ($y.updated_at | stamp) > ($x.updated_at | stamp) then $y.updated_at
    elif ($x | has("updated_at")) then $x.updated_at
    else $y.updated_at
    end;

def CLOSE_KEYS: ["status", "closed_at", "close_reason"];
def close_state($row): {status: slot($row; "status"), closed_at: slot($row; "closed_at"), close_reason: slot($row; "close_reason")};
def SPECIAL_KEYS: CLOSE_KEYS + ["labels", "dependencies", "comments", "updated_at", "created_at", "source_repo_path"];

# Field-level three-way merge of one row changed on both sides: {row, conflicts: [field names]}.
def merge_fields($o; $x; $y):
    ([$o, $x, $y] | map(keys_unsorted[]) | dedupe_keys | map(select(IN(SPECIAL_KEYS[]) | not))) as $plain_keys
    | reduce $plain_keys[] as $key ({row: $x, conflicts: []};
        pick3(slot($o; $key); slot($x; $key); slot($y; $key)) as $pick
        | if $pick.conflict then .conflicts += [$key] else .row |= put_slot($key; $pick.slot) end)
    | pick3(close_state($o); close_state($x); close_state($y)) as $close
    | if $close.conflict then .conflicts += ["status/closed_at/close_reason"]
      else reduce CLOSE_KEYS[] as $key (.; .row |= put_slot($key; $close.slot[$key]))
      end
    | reduce ("labels", "dependencies") as $key (.;
        if ([$o, $x, $y] | is_list_or_absent($key) | not) then
            pick3(slot($o; $key); slot($x; $key); slot($y; $key)) as $pick
            | if $pick.conflict then .conflicts += [$key] else .row |= put_slot($key; $pick.slot) end
        elif ([$o, $x, $y] | any(has($key)) | not) then .
        else
            (if $key == "labels" then merge_keyed($o.labels | label_entries; $x.labels | label_entries; $y.labels | label_entries)
             else merge_keyed($o.dependencies | dep_entries; $x.dependencies | dep_entries; $y.dependencies | dep_entries)
             end) as $set
            | .row[$key] = $set.merged
            | if ($set.conflicts | length) > 0 then .conflicts += [$key] else . end
        end)
    | if ([$x, $y] | is_list_or_absent("comments") | not) then
          pick3(slot($o; "comments"); slot($x; "comments"); slot($y; "comments")) as $pick
          | if $pick.conflict then .conflicts += ["comments"] else .row |= put_slot("comments"; $pick.slot) end
      elif ([$x, $y] | any(has("comments"))) then .row.comments = merge_comments($x.comments; $y.comments)
      else .
      end
    | if ([$x, $y] | any(has("updated_at"))) then .row.updated_at = newer_updated_at($x; $y) else . end;

def renumber_comment_ids($max_id):
    reduce range(0; length) as $i ({plan: ., seen: {}, next: ($max_id + 1)};
        if (.plan[$i].record.comments | type) != "array" then .
        else reduce range(0; .plan[$i].record.comments | length) as $j (.;
            (.plan[$i].record.comments[$j] | if type == "object" then .id else null end) as $cid
            | if ($cid | type) != "number" then .
              elif .seen[$cid | tostring] then
                  .plan[$i].record.comments[$j].id = .next | .seen[.next | tostring] = true | .next += 1
              else .seen[$cid | tostring] = true
              end)
        end)
    | .plan;

INDEX($ours[]; .id) as $a
| INDEX($theirs[]; .id) as $b
| INDEX($base[]; .id) as $o
| ([$ours[], $theirs[], $base[] | .comments? // empty | arrays | .[] | objects | .id | numbers] | max // 0) as $max_comment_id
| [($a, $b) | keys[]]
| unique
| map(
    . as $id
    | $a[$id] as $x
    | $b[$id] as $y
    | $o[$id] as $base_row
    | if $x == null then {record: $y}
        elif $y == null then {record: $x}
        elif (($x.created_at | stamp) != ($y.created_at | stamp)) then clash($id; $x; $y; "created_at")
        elif same($x; $y) then
            if $has_base and ($x == $base_row) and ($y != $base_row) then {record: $y}
            elif $has_base and ($y == $base_row) and ($x != $base_row) then {record: $x}
            else {record: $x}
            end
        elif ($has_base | not) then clash($id; $x; $y; "no merge base")
        elif superseded($y; $x; $base_row) then take_newer("theirs"; $y; $x; $base_row; {id: $id, ours: $x, theirs: $y})
        elif superseded($x; $y; $base_row) then take_newer("ours"; $x; $y; $base_row; {id: $id, ours: $x, theirs: $y})
        elif $base_row == null then clash($id; $x; $y; "no base row")
        elif ((same($x; $base_row) | not) and (same($y; $base_row) | not)) then
            merge_fields($base_row; $x; $y) as $merge
            | if ($merge.conflicts | length) > 0 then clash($id; $x; $y; ($merge.conflicts | join(", ")))
              else {record: $merge.row}
              end
        elif (same($x; $base_row) and older_than($y; $base_row)) then clash($id; $x; $y; "older than the base")
        elif (same($y; $base_row) and older_than($x; $base_row)) then clash($id; $x; $y; "older than the base")
        elif same($y; $base_row) then {record: $x}
        else {record: $y}
        end
)
| renumber_comment_ids($max_comment_id)
JQ
)

plan=$(mktemp)
merged=$(mktemp)
ours_side=$(mktemp)
theirs_side=$(mktemp)
ids=$(mktemp)
trap 'rm -f "$plan" "$merged" "$ours_side" "$theirs_side" "$ids"' EXIT

jq -n -c \
    --slurpfile ours "$ours" \
    --slurpfile theirs "$theirs" \
    --slurpfile base "$base" \
    --argjson has_base "$has_base" \
    "$JQ_PROGRAM" >"$plan"
jq -c '.[].record' "$plan" >"$merged"
jq -c '.[] | select(has("conflict")) | .ours' "$plan" >"$ours_side"
jq -c '.[] | select(has("conflict")) | .theirs' "$plan" >"$theirs_side"
jq -r '.[] | select(has("conflict")) | "\(.conflict): \(.why)"' "$plan" >"$ids"

if [[ -s $ids ]]; then
    mkdir -p .beads
    cp "$ours_side" .beads/merge-conflict.ours.jsonl
    cp "$theirs_side" .beads/merge-conflict.theirs.jsonl
fi
cp "$merged" "$ours"
if [[ -s $ids ]]; then
    printf 'beads merge: kept ours for these issues. Both rows are in .beads/merge-conflict.ours.jsonl and .beads/merge-conflict.theirs.jsonl (br sync --merge on 0.7.x):\n' >&2
    sed 's/^/  /' "$ids" >&2
    exit 1
fi
