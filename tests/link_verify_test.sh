#!/usr/bin/env bash
# SPDX-FileCopyrightText: 2026 Unto Labs
# SPDX-License-Identifier: Apache-2.0
set -uo pipefail

if [[ $(uname -s) != Linux ]]; then
  echo 'SKIP link verification requires the Linux tracer'
  exit 0
fi
TOP=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
for name in ${!VCACHE_@}; do unset "$name"; done
VCACHE=${1:-$TOP/bin/vcache}
WORK=$(mktemp -d "${TMPDIR:-/tmp}/vcache-link-verify-XXXXXX")
echo "link verification evidence: $WORK"
export VCACHE_CONFIG="$WORK/config.toml" VCACHE_DAEMON=off VCACHE_MAP_CWD=0
export VCACHE_LINK_CACHE=1 VCACHE_LINK_VERIFY=0 VCACHE_CACHE_SIZE=1G
export VCACHE_READONLY=0 VCACHE_RECACHE=0
printf '[vcache]\n' > "$VCACHE_CONFIG"
mkdir -p "$WORK/a" "$WORK/b"
export VCACHE_ROOTS="$WORK/a=project"
REAL_CC=$(command -v gcc)
export REAL_CC
cat > "$WORK/main.c" <<'EOF'
int main(void) { return 0; }
EOF
"$REAL_CC" -c "$WORK/main.c" -o "$WORK/a/main.o" || exit 1
cp "$WORK/a/main.o" "$WORK/b/main.o"
# The environment changes the real link while preserving the cached command,
# tool files and input objects. The resulting stale entry has valid checksums.
cat > "$WORK/gcc" <<'EOF'
#!/usr/bin/env bash
set -eu
if [[ ${1:-} != main.o ]]; then exec "$REAL_CC" "$@"; fi
if [[ ${LINK_VERIFY_EXPECT_OUTPUT:-0} == 1 ]]; then
  grep -qx caller-owned app || { echo 'cached output was placed before linking' >&2; exit 38; }
fi
echo "fresh diagnostic: ${LINK_VERIFY_CASE:-base}" >&2
if [[ ${LINK_VERIFY_CASE:-base} == failure ]]; then exit 37; fi
if [[ ${LINK_VERIFY_DELAY:-0} == 1 ]]; then sleep 1; fi
"$REAL_CC" "$@"
case ${LINK_VERIFY_CASE:-base} in
  main) printf 'stale-main' >> app ;;
  map) printf 'stale-map\n' >> app.map ;;
  mode) chmod a-x app ;;
  missing-output) mv app.map app.map.saved ;;
esac
if [[ ${LINK_VERIFY_INCOMPLETE:-0} == 1 ]]; then
  : > "${VCACHE_TRACE_LOG}.incomplete"
fi
EOF
chmod +x "$WORK/gcc"
PASS=0 FAIL=0 STORE=0
check() {
  if [[ $2 == "$3" ]]; then
    printf 'PASS %s\n' "$1"; PASS=$((PASS + 1))
  else
    printf 'FAIL %s: expected [%s], got [%s]\n' "$1" "$3" "$2"
    FAIL=$((FAIL + 1))
  fi
}
new_store() {
  STORE=$((STORE + 1))
  export VCACHE_DIR="$WORK/store-$STORE"
}
stat_of() {
  "$VCACHE" --show-stats | awk -F '  +' -v key="$1" '$1 == key { print $2 }'
}
run_link() {
  local label=$1 mode=$2 tree=${LINK_VERIFY_TREE:-a}; shift 2
  ( cd "$WORK/$tree" && env LINK_VERIFY_CASE="$mode" "$@" \
      VCACHE_ROOTS="$WORK/$tree=project" \
      VCACHE_LOG="$WORK/$label.log" timeout 30 "$VCACHE" "$WORK/gcc" \
      main.o -Wl,-Map,app.map -o app ) > "$WORK/$label.stdout" 2> "$WORK/$label.stderr"
  local status=$?
  printf '%s status=%s\n' "$label" "$status"
  "$VCACHE" --show-stats > "$WORK/$label.stats"
  return "$status"
}
fill() {
  new_store
  run_link "fill-$STORE" "$1"
  check "fill $STORE succeeds" "$?" 0
  check "fill $STORE stores a valid entry" "$(stat_of 'entries stored')" 1
  "$VCACHE" --zero-stats >/dev/null
}

fill base
cp "$WORK/a/app" "$WORK/expected-app"
cp "$WORK/a/app.map" "$WORK/expected-map"
printf 'caller-owned\n' > "$WORK/a/app"
run_link match base VCACHE_LINK_VERIFY=1 LINK_VERIFY_EXPECT_OUTPUT=1
check 'verification never materializes the candidate before the fresh link' "$?" 0
check 'matching fresh link is verified' "$(stat_of 'link verified')" 1
check 'verification counts a real link miss' "$(stat_of 'cache miss')" 1
check 'verification is not a served hit' "$(stat_of 'cache hit (disk)')" 0
check 'matching binary bytes are fresh' "$(cmp -s "$WORK/a/app" "$WORK/expected-app" && echo yes)" yes
check 'matching map bytes are fresh' "$(cmp -s "$WORK/a/app.map" "$WORK/expected-map" && echo yes)" yes
check 'only fresh diagnostics are printed' "$(cat "$WORK/match.stderr")" 'fresh diagnostic: base'

for mode in main map mode; do
  fill "$mode"
  run_link "mismatch-$mode" base VCACHE_LINK_VERIFY=1
  check "$mode mismatch returns the successful fresh link" "$?" 0
  check "$mode mismatch is counted" "$(stat_of 'link mismatch')" 1
  check "$mode mismatch is not a verified result" "$(stat_of 'link verified')" 0
  check "$mode mismatch keeps fresh binary" "$(cmp -s "$WORK/a/app" "$WORK/expected-app" && echo yes)" yes
  check "$mode mismatch keeps fresh map" "$(cmp -s "$WORK/a/app.map" "$WORK/expected-map" && echo yes)" yes
  check "$mode mismatch keeps executable metadata" "$([[ -x "$WORK/a/app" ]] && echo yes)" yes
  check "$mode mismatch prints fresh diagnostics only" "$(cat "$WORK/mismatch-$mode.stderr")" 'fresh diagnostic: base'
  check "$mode mismatch log includes both digests" \
    "$(grep -c 'link verify: output=.*cached=.*fresh=' "$WORK/mismatch-$mode.log")" 2
  run_link "repaired-$mode" base VCACHE_LINK_VERIFY=1
  check "$mode replacement verifies on the next run" "$(stat_of 'link verified')" 1
done

fill base
printf 'caller-owned\n' > "$WORK/a/app"
run_link failure failure VCACHE_LINK_VERIFY=1 LINK_VERIFY_EXPECT_OUTPUT=1
check 'fresh failure returns its own status' "$?" 37
check 'fresh failure cannot restore a cached executable' "$(cat "$WORK/a/app")" caller-owned
check 'fresh failure has fresh diagnostics' "$(cat "$WORK/failure.stderr")" 'fresh diagnostic: failure'
check 'fresh failure counts a verification error' "$(stat_of 'link verify error')" 1
check 'fresh failure counts a failed compile' "$(stat_of 'compile failed')" 1
check 'fresh failure stores nothing' "$(stat_of 'entries stored')" 0

fill base
run_link missing-output missing-output VCACHE_LINK_VERIFY=1
check 'a missing fresh output preserves the linker status' "$?" 0
check 'a missing fresh output is a verification error' "$(stat_of 'link verify error')" 1
check 'a missing fresh output is never certified' "$(stat_of 'link verified')" 0
check 'a missing fresh output cannot replace the candidate' "$(stat_of 'entries stored')" 0

fill main
run_link readonly1 base VCACHE_LINK_VERIFY=1 VCACHE_READONLY=1
run_link readonly2 base VCACHE_LINK_VERIFY=1 VCACHE_READONLY=1
check 'read-only verification detects the unrepaired mismatch twice' "$(stat_of 'link mismatch')" 2
check 'read-only verification never writes replacements' "$(stat_of 'entries stored')" 0
run_link recache base VCACHE_LINK_VERIFY=1 VCACHE_RECACHE=1
check 'recache bypasses comparison' "$(stat_of 'link mismatch')/$(stat_of 'link verified')" 2/0
check 'recache stores the fresh result' "$(stat_of 'entries stored')" 1

for mode in missing corrupt; do
  fill base
  sidecar=$(ls "$VCACHE_DIR"/??/*.linkout | head -1)
  if [[ $mode == missing ]]; then mv "$sidecar" "$sidecar.saved";
  else printf 'corrupt' > "$sidecar"; fi
  run_link "$mode" base VCACHE_LINK_VERIFY=1
  check "$mode sidecar is an ordinary miss" "$(stat_of 'cache miss')" 1
  check "$mode sidecar is not a semantic comparison" \
    "$(stat_of 'link mismatch')/$(stat_of 'link verified')/$(stat_of 'link verify error')" 0/0/0
done

fill main
run_link incomplete base VCACHE_LINK_VERIFY=1 LINK_VERIFY_INCOMPLETE=1
check 'an incomplete trace still exposes a byte mismatch' "$(stat_of 'link mismatch')" 1
check 'an incomplete trace cannot replace the candidate' "$(stat_of 'entries stored')" 0
run_link incomplete-retry base VCACHE_LINK_VERIFY=1
check 'the next complete trace still sees the old mismatch' "$(stat_of 'link mismatch')" 2
run_link incomplete-match base VCACHE_LINK_VERIFY=1 LINK_VERIFY_INCOMPLETE=1
check 'an equal output with an incomplete trace is a verification error' "$(stat_of 'link verify error')" 1
check 'incomplete equality is not a successful verification' "$(stat_of 'link verified')" 0

fill main
run_link disabled base VCACHE_LINK_CACHE=0 VCACHE_LINK_VERIFY=1
check 'verify with link caching off runs normally' "$?" 0
check 'verify with link caching off does not compare' "$(stat_of 'link mismatch')/$(stat_of 'link verified')" 0/0

for warmth in cold warm; do
  new_store
  export VCACHE_DAEMON=auto VCACHE_DAEMON_SINGLE_FLIGHT=1 VCACHE_DAEMON_ADMISSION=1
  if [[ $warmth == warm ]]; then run_link concurrent-fill base; fi
  "$VCACHE" --zero-stats >/dev/null
  run_link "$warmth-a" base VCACHE_LINK_VERIFY=1 LINK_VERIFY_DELAY=1 & first=$!
  LINK_VERIFY_TREE=b run_link "$warmth-b" base VCACHE_LINK_VERIFY=1 LINK_VERIFY_DELAY=1 & second=$!
  wait "$first"; check "$warmth first real link succeeds" "$?" 0
  wait "$second"; check "$warmth waiting real link succeeds" "$?" 0
  expected=1; [[ $warmth != warm ]] || expected=2
  check "$warmth single-flight waiter verifies instead of serving a hit" "$(stat_of 'link verified')" "$expected"
  check "$warmth concurrent verification runs both links" "$(stat_of 'cache miss')" 2
  check "$warmth concurrent verification never serves cached outputs" "$(stat_of 'cache hit (disk)')" 0
  check "$warmth cross-tree fresh outputs agree" "$(cmp -s "$WORK/a/app" "$WORK/b/app" && echo yes)" yes
  "$VCACHE" --stop-daemon >/dev/null
  export VCACHE_DAEMON=off
done

printf 'link verification: %s passed, %s failed\n' "$PASS" "$FAIL"
(( FAIL == 0 ))
