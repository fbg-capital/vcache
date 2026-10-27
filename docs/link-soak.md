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

devtools' `link-soak-daily.timer` collects them. `host-setup-buildcache.sh` installs
it with `~/bin/link-soak-daily.sh` (source: devtools `scripts/linksoak/`) and
enables it, with the nightly el10 build timer below, only for a soak:
`host-setup-buildcache.sh --link-soak on` starts one and `--link-soak off` ends it.
Once a day at 04:30, up to 30 minutes later and caught up after downtime, it runs
`build-limited link-soak-daily.sh`. The script loads Sunbird's vcache environment
with `FBG_VCACHE_LINK=verify`, on the host and through `container.run.sh el10`, and
runs `link_soak.py snapshot` once per platform. The evidence root is
`/build/$USER/link-soak/`; it is not a pruned scratch path. It holds:

| Path | Content |
|---|---|
| `epoch` | The counter epoch label, written by the operator |
| `reset-deployment.log` | The operator's reset, daemon-restart and failure history |
| `<YYYYMMDD>-host/`, `<YYYYMMDD>-el10/` | One collection each: raw outputs and `snapshot.json` |
| `context/<YYYYMMDD>-<platform>.json` | The context that collection used |
| `collector/link_soak-<hash>.py` | The collector copy each run used; EL10 runs it from there |
| `link-soak-daily.log` | The script's output, appended on every run |

A day that already has a platform's snapshot is skipped, so a second run the same
day creates nothing. A failed attempt keeps its partial directory, and a retry the
same day writes `<YYYYMMDD>-<platform>-2`. The run exits non-zero when a platform
published no snapshot, and the timer's unit then shows as failed.

The script reads each context field at run time. Do not fill them with inferred
successes:

| Field | Record |
|---|---|
| `devtools_revision` | Adapter checkout, revision and uncommitted tracked files |
| `sunbird_revision` | Workload checkout, revision and uncommitted tracked files |
| `compiler` | Sunbird's resolved clang path and version, in the collection's environment |
| `linker` | The `ld.mold` beside it, path and version |
| `image` | The el10 image ID, or the native OS and kernel for host |
| `workload_evidence` | Each link-verification log under the store changed since the previous snapshot, with link, verified, mismatch and not-stored counts, or "No workload ran" |
| `reset_deployment_log` | `reset-deployment.log`, then one `deployment:` line per vcache version in the anvil dist root |

A deployment line names the version, publish time, commit, install time (the dist
directory's mtime), anvil target and that platform's binary and tracer SHA256:

```text
deployment: vcache 1.12 published 2026-10-19T07:31:55.837Z commit 7fe1269ac40f... installed 2026-10-19T07:32:21Z target fedora44 binary_sha256 baa9a5de... tracer_sha256 25f7bc32...
```

A release therefore enters the log automatically once anvil delivers it. Record
counter resets, store changes, daemon restarts and failures in
`reset-deployment.log` by hand, as dated lines.

Read the log with `tail /build/$USER/link-soak/link-soak-daily.log` or
`journalctl --user -u link-soak-daily`. After each published snapshot the log
gives every counter with its change since the platform's previous snapshot,
for example `link mismatch 11 (+0) since 20261019-host`: the total carries the
store's history from before the epoch, and only the change can reject the
window. `systemctl --user list-timers link-soak-daily.timer` shows the next run.

To collect by hand, for example in a new evidence root, use the same script:

```sh
LINK_SOAK_PLATFORMS=host ~/bin/build-limited ~/bin/link-soak-daily.sh --root /build/$USER/link-soak
```

The EL10 collector runs **inside its image**, with `--platform el10`, its native
binary from the mounted anvil dist (`/code/vcache/dist`) and its own store. It uses
the physical host's stable `--host-id` (`hostname -f`) because ephemeral container
hostnames change; the raw container hostname is also retained. The evidence root
is passed as the container's `SCRATCH_ROOT`, the writable mount `container.run.sh`
provides at the same path, and the collector runs from its copy there.

Each directory contains raw `--show-config`, `--show-stats` and `--daemon-status`
output and stderr, plus `snapshot.json`. The snapshot records the collection
start/end times, binary/tracer SHA256 hashes, optional commit stamp, store and
stats-file device/inode identities, context and parsed counters. Configuration,
hashes and identities are checked again before publication. A stopped daemon is
recorded with its status and diagnostic; persisted counters are still usable.
Failed collections retain their partial directory and do not publish a snapshot.
Existing evidence directories are refused.

## Nightly el10 builds

A link is verified only when a later build repeats its exact inputs, and el10
builds are rare. devtools' `link-soak-el10-builds.timer` supplies them.
`host-setup-buildcache.sh` installs it with `~/bin/link-soak-el10-builds.sh`
(source: devtools `scripts/linksoak/`). It runs at 03:00 through `build-limited`,
keeps its own sunbird worktree `/build/$USER/link-soak/el10-builds/sunbird`, and
fetches origin. For each first-parent commit of `origin/main` after `last-built`
(the last 7 days on the first run), oldest first, it links the commit's siblings
(its `scripts/siblings.sh` or `scripts/siblings`, else the host's `siblings`),
then runs `FBG_VCACHE_LINK=verify ./build.sh --clean` twice in el10. No commit
starts after 06:45 (`LINK_SOAK_BUILD_DEADLINE`), and the unit stops after 4
hours. A commit whose build fails is retried once the next night.

`/build/$USER/link-soak/el10-builds/runs.tsv` has one row per build: start time,
sha, build (1 or 2), exit status, elapsed seconds, the link-verification log the
build named, and that log's links, verified, mismatch and not-stored counts,
counted with the same patterns as the daily collector. A `-` means the build
named no log (it failed before linking, or vcache was off). The second build of
a commit should verify every link whose inputs carry no per-build stamp; the
first verifies only links the previous commit left unchanged. A mismatch also
gets its own `MISMATCH: <sha> build <n>: ... in <log>` line in
`link-soak-el10-builds.log` there, and the run exits non-zero.

    column -t -s $'\t' /build/$USER/link-soak/el10-builds/runs.tsv | tail

The builds write their logs into the el10 store's `link-verification/`, so the
next el10 snapshot's `workload_evidence` lists each of them, and its counter line
shows a new mismatch as `link mismatch N (+1)`, which rejects the window.

## Check the observation window

Pass every snapshot for the chosen window, in chronological order **within each
platform**. Platforms may be interleaved. `link-soak-daily.sh --check` passes the
current epoch's snapshots in directory-name order, which is chronological:

```sh
~/bin/build-limited ~/bin/link-soak-daily.sh --check
```

By hand, the same check is:

```sh
~/bin/build-limited python3 scripts/link_soak.py check \
  /build/$USER/link-soak/2026*-*/snapshot.json
```

The JSON report includes each interval's deltas and separate platform totals.
Exit 0 means `counter_window_complete`; exit 1 means the evidence is incomplete
or failed; exit 2 means a command, input file or collection failed. The thresholds
are fixed:

- Both platforms need a baseline and subsequent observations from separate stores.
- Each needs at least 14 elapsed days and 200 new `link verified` outcomes.
- Consecutive collection intervals must be ordered, with no gap above 36 hours.
- Any counter decrease, new `link mismatch` or new `link verify error` rejects the window.
- Epoch, physical host, store identities and effective configuration must remain
  unchanged within a window.
- A binary or tracer hash may change between consecutive snapshots only for a
  logged deployment. The later snapshot's `reset_deployment_log` must hold a
  `deployment:` line with the version, publish time, commit and exactly that
  snapshot's binary and tracer SHA256, published no later than the snapshot. The
  `link verified` and `link mismatch` counters must not decrease across it. Then
  the window continues, and the report lists the deployment under the platform's
  `deployments` with its version, publish time, commit and the snapshot that first
  ran it. Without such a line, or with a counter decrease, the identity change
  rejects the window.

Historical mismatch/error counts in the baseline remain visible; their unchanged
values are not new failures. A daemon restart alone does not reset persisted
counters. Workload revisions may change and are retained in the context. A
configuration change still requires a new window. Do not drop failed intermediate
observations to make a window pass. Keep previous windows and failure evidence
alongside the new one.

An epoch marks a counter reset or a store change, not a release. After either,
write a new label to `epoch`, record the reason in `reset-deployment.log`, and the
next run collects a fresh baseline. Do not splice it onto an older window. Any new
mismatch or verification error still rejects the window: investigate it, retain
command, roots, keys, outputs and trace logs, and link the repair before starting
the required clean observation period again.

Counters have no persistent reset generation. A reset followed by enough activity
to exceed the previous snapshot is invisible to endpoint comparison. The epoch
label and reset/deployment log are therefore required, but their truth still needs
operator review. The collector timestamps current observations; it does not
backfill missing days or authenticate manually edited JSON.

The collector and checker neither change `vcache_link=verify` nor authorize
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

The timer's first collection on 2026-10-19 continued that epoch,
`vcache-1.6-observation-20261018`. The 2026-10-18 snapshots were copied unchanged
into `/build/$USER/link-soak/20261018-{host,el10}`. Store and stats identities and
the effective configuration are unchanged. The binary changed from 1.6 to vcache
1.12, published 2026-10-19 (commit 7fe1269), and its logged deployment line lets
the window continue. The checker accepts the series start as incomplete: 31 hours
elapsed and no new verified links on either platform. Every link the workload
logged since 2026-10-18 says `link: unsupported dependency file; not storing`, so
no candidate is stored to verify; the window cannot reach 200 verifications until
that changes.

| Platform | Snapshot | Link verified | Link mismatch | Link verify error |
|---|---|---:|---:|---:|
| Host | `20261019-host` | 1,048 | 11 | 0 |
| EL10 | `20261019-el10` | 1,121 | 0 | 0 |

## Outcome of the 2026-10 soak

The owner ended the soak on 2026-10-27, after 8.8 of the 14 days, on the
checker's report over the 20 snapshots `20261018` to `20261027`: both platforms
were far past the 200-verification threshold with no mismatch, no verification
error, no counter reset and the two logged deployments (1.12 and 1.13) tolerated.
The nightly el10 builds, started 2026-10-23, supplied most of el10's count.

| Platform | Link verified since the epoch | Link mismatch | Link verify error |
|---|---:|---:|---:|
| Host | +556 | +0 | +0 |
| EL10 | +1,256 | +0 | +0 |

The decision and the counters are in `reset-deployment.log` and on vcache-trk.3.
Both timers were disabled with `host-setup-buildcache.sh --link-soak off`.
`FBG_VCACHE_LINK` stays `off` by default; ordinary link hits remain opt-in per
build with `FBG_VCACHE_LINK=on`.

The same evening, a controlled ordinary-hit comparison on sunbird 8f9e01b: four
clean builds per platform (`off`, `off`, `on`, `on`), host and el10. Sunbird's
post-link `buildinfo patch` rewrites `.note.buildinfo` and `.note.buildsig` in
every output every build, so whole files never match; with those two sections
zeroed, all 11 link outputs were byte-identical across the four builds on both
platforms, including the 3 that the `on` builds served from the cache. A host
`run_tests.sh --audit` over a second `on` build with tests, in which 87 of 191
links were cache hits (85 test executables), passed all 510 ids three times.
The details are on vcache-trk.3.
