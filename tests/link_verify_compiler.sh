#!/usr/bin/env bash
# SPDX-FileCopyrightText: 2026 Unto Labs
# SPDX-License-Identifier: Apache-2.0
set -euo pipefail
compiler=$1
shift
for argument in "$@"; do
  if [[ $argument == -c ]]; then
    # Normalize even compiles vcache would decline, so the link experiment
    # receives identical objects from independently built worktrees.
    exec "$compiler" "-ffile-prefix-map=$LINK_VERIFY_ROOT=/vcache/link-verify-project" "$@"
  fi
done
exec "$VCACHE" "$compiler" "$@"
