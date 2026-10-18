# Link-verification observation records

`scripts/link_soak.py` collects live counter snapshots and checks the daily
observation window on host and EL10. It never enables link caching, resets
counters, starts a daemon, or runs a build. Use it from the same environment
and with the same binary as the workload being observed. The collector requires
disk caching, link caching and link verification to be enabled.

The report checks counter evidence only. A complete counter window still needs
workload coverage, audit controls, ordinary-hit output comparisons and the owner's
rollout decision. A snapshot of a configured launcher does not prove that builds
actually used that launcher.

## Collect daily snapshots

Create a context JSON file under `/build/$USER/codex-1000/` containing these
nonempty string fields. Use actual revisions and evidence paths; do not fill them
with inferred successes:

| Field | Record |
|---|---|
| `devtools_revision` | Adapter revision and any local changes |
| `sunbird_revision` | Workload revision and any local changes |
| `compiler` | Resolved compiler path and version |
| `linker` | Resolved linker path and version |
| `image` | Image digest, or native OS version for host |
| `workload_evidence` | Logs naming builds, roots, binary/map outputs and changed inputs; state when no workload ran |
| `reset_deployment_log` | Retained reset, daemon restart and deployment history, including earlier failures |

After loading the workload's vcache environment, collect a new directory each day:

```sh
~/bin/build-limited python3 scripts/link_soak.py snapshot \
  --platform host --host-id zeus.sg.fbg \
  --epoch vcache-1.6-observation-20261018 \
  --vcache "$VCACHE_BINARY" \
  --context /build/$USER/codex-1000/link-soak/context-host.json \
  --output /build/$USER/codex-1000/link-soak/20261018-host
```

Run the EL10 collector **inside its image**, with `--platform el10`, its native
binary and its own store. Use the physical host's stable `--host-id` because
ephemeral container hostnames change. The raw container hostname is also retained.
Mount the collector source, artifact pair and store, and make the evidence
directory writable. Keep compiler-wrapper directories out of `PATH` for any
accompanying compiler tests. All tests/builds go through `build-limited`.

Each directory contains raw `--show-config`, `--show-stats` and `--daemon-status`
output and stderr, plus `snapshot.json`. The snapshot records the collection
start/end times, binary/tracer SHA256 hashes, optional commit stamp, store and
stats-file device/inode identities, context and parsed counters. Configuration,
hashes and identities are checked again before publication. A stopped daemon is
recorded with its status and diagnostic; persisted counters are still usable.
Failed collections retain their partial directory and do not publish a snapshot.
Existing evidence directories are refused.

## Check the observation window

Pass every snapshot for the chosen window, in chronological order **within each
platform**. Platforms may be interleaved. For directories named as above:

```sh
~/bin/build-limited python3 scripts/link_soak.py check \
  /build/$USER/codex-1000/link-soak/2026*-*/snapshot.json
```

The JSON report includes each interval's deltas and separate platform totals.
Exit 0 means `counter_window_complete`; exit 1 means the evidence is incomplete
or failed; exit 2 means a command, input file or collection failed. The thresholds
are fixed:

- Both platforms need a baseline and subsequent observations from separate stores.
- Each needs at least 14 elapsed days and 200 new `link verified` outcomes.
- Consecutive collection intervals must be ordered, with no gap above 36 hours.
- Any counter decrease, new `link mismatch` or new `link verify error` rejects the window.
- Epoch, physical host, store identities, binary/tracer hashes and effective
  configuration must remain unchanged within a window.

Historical mismatch/error counts in the baseline remain visible; their unchanged
values are not new failures. A daemon restart alone does not reset persisted
counters. Workload revisions may change and are retained in the context. A
binary/configuration change conservatively requires a new window and review of
its deployment record. Do not drop failed intermediate observations to make a
window pass. Keep previous windows and failure evidence alongside the new one.

After a corrective deployment or counter reset, choose a new epoch label and
collect a fresh baseline. Do not splice it onto an older window. Investigate
every new mismatch/error, retain command, roots, keys, outputs and trace logs,
and link the repair before starting the required clean observation period again.

Counters have no persistent reset generation. A reset followed by enough activity
to exceed the previous snapshot is invisible to endpoint comparison. The epoch
label and reset/deployment log are therefore required, but their truth still needs
operator review. The collector timestamps current observations; it does not
backfill missing days or authenticate manually edited JSON.

This command is a manual evidence tool, not a scheduled collector. Arrange daily
collection separately. It neither changes `vcache_link=verify` nor authorizes
ordinary link-cache hits.

## Regression evidence

The ordinary `make test` gate includes the checker and real-binary collector
tests. Isolated mutations disable the duration, volume, zero-attempt, reset,
failure-outcome, identity, daily-gap, ordering, baseline, separate-store,
verification-mode, context, raw-stat consistency, timestamp, collection-duration,
counter-type, parser and file-preservation guards. Each produces a named failure.
Collector mutations also prove the idle-daemon, evidence-preservation and disabled
verification regressions fail. Scratch logs and the reproducible mutation runners
are under `/build/richard/codex-1000/link-soak-20261018/`.

Live collector checks on 2026-10-18 selected Anvil vcache 1.6 through Sunbird's
host and EL10 adapters, both in verify mode. Their cumulative snapshots were:

| Platform | Link verified | Link mismatch | Link verify error |
|---|---:|---:|---:|
| Host | 1,048 | 11 | 0 |
| EL10 | 1,121 | 0 | 0 |

These counters are unchanged from the retained 2026-10-17 post-deployment
baseline. No counters were reset, and no new Sunbird workload was run by this
collector check. The report correctly rejects the new single-snapshot series
as incomplete. It does not turn the host's historical mismatches into new
events or declare a clean 14-day window. The parent soak remains open.
