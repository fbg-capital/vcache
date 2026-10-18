#!/usr/bin/env bash
# SPDX-FileCopyrightText: 2026 Unto Labs
# SPDX-License-Identifier: Apache-2.0
#
# Preprocess candidate harness. Not part of `make test`.
# Reused by later adoption work: it refuses instrumented timing rows, counts
# compile and link invocations separately, and will not touch a production store.
#
#   tests/cxx_preprocess_bench.sh guard
#   tests/cxx_preprocess_bench.sh check-row
#
# Corpus and case runs live in the task scratch and append results.jsonl.

set -euo pipefail

TOP="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SCRATCH="${AXB_SCRATCH:-/build/${USER:-$(id -un)}/claude-tmp/forgrok/vcache-axb}"
export PATH="$(printf '%s' "$PATH" | tr ':' '\n' | grep -vx /usr/lib64/ccache | paste -sd:)"
unset LD_PRELOAD

production="/build/${USER:-$(id -un)}/cache/vcache"
case "${VCACHE_DIR:-}" in
  "$production"|"$production"/*)
    printf 'refusing production VCACHE_DIR %s\n' "$VCACHE_DIR" >&2
    exit 2
    ;;
esac

cmd="${1:-}"
shift || true
case "$cmd" in
  guard)
    mkdir -p "$SCRATCH"
    PYTHONDONTWRITEBYTECODE=1 python3 "$TOP/tests/cxx_preprocess_eval.py" guard "$SCRATCH"
    ;;
  check-row)
    # Stdin is one JSON object. Exit 0 when it is not disqualified.
    PYTHONDONTWRITEBYTECODE=1 python3 - "$TOP" << 'PY'
import json, sys
sys.path.insert(0, sys.argv[1] + "/tests")
import cxx_preprocess_eval as ev
row = json.load(sys.stdin)
reasons = ev.disqualify_reasons(
    row["compiler_before"], row["compiler_after"],
    row["candidate_before"], row["candidate_after"],
    row.get("compiler_absent_before", []), row.get("compiler_absent_after", []),
    row.get("candidate_absent_before", []), row.get("candidate_absent_after", []))
if row.get("instrumented"):
    ev.accept_timing_row(row)
if reasons:
    print("; ".join(reasons))
    sys.exit(1)
PY
    ;;
  *)
    printf 'usage: %s guard|check-row\n' "$0" >&2
    exit 2
    ;;
esac
