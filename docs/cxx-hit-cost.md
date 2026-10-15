# C/C++ warm-hit cost — 2026-10-15

The compile-only replay supports optimizing digest memo access first. For the median
host translation unit, vcache takes 7.47 ms versus ccache's 4.27 ms and makes 2,986
syscalls versus 907. Memo-file access accounts for 1,685 vcache calls. EL10 confirms
that ranking. Create a shared digest table (`vcache-83w`) and, separately, defer
unused scratch creation (`vcache-zs1`). These are follow-up implementations; this
measurement task (`vcache-tsw`) changes no shipped binary or cache policy.

The shared machine produced substantial scheduling outliers. These measurements
establish remaining CPU work and candidate ranking, not a reliable whole-build
speedup. Links, cold compilation and stale-header preprocessing are outside the
steady-hit comparison.

## Workload and controls

- Workload: sunbird `097a9f79db9e6a88b68d1575827a109241257172`, two fresh attached
  worktrees, Release, deployed targets, Clang 23, `SUNBIRD_TU_TIME=OFF`, Python build
  skipped. Explicitly replayed compile-database argument arrays, adding Ninja's
  `-MD -MF` depfile arguments equally for both tools. PCH prerequisites were built
  separately. No Ninja no-op or link is counted.
- Host: 522 object commands, excluding 10 PCH producers; EL10: 520, excluding 10.
  These differ from the historical 525-TU workload. The complete argument lists
  and unique output IDs are in each results directory's `commands.json`.
- Installed vcache and tracer: `eb9c25c51d409d656eab2627cf98d30a673a2067`.
  Repository HEAD when measured: `b7665a6` (documentation only above that build).
  Host vcache is 7,607,336 bytes, dynamically linked to libstdc++, libm, libgcc and
  libc; EL10 is 21,642,472 bytes, linked to libm and libc. Both retain symbols.
  Exact binary/tracer SHA-256 values are in `fingerprint.json`.
- Host ccache 4.12.3; EL10 ccache 4.11.3. Both Clang builds identify as 23.1.4
  `absent-deps-1`, with LLVM revisions `bb46d66418b9` (host) and `d657d8d8c0fd`
  (EL10). Compare tools within a platform, not absolute host versus EL10 times.
- Private 20 GiB stores; `VCACHE_CXX_MANIFEST=1`, `VCACHE_DAEMON=auto`, running
  private daemon verified for the final samples; idle timeout zero, single-flight
  and admission off, link cache/verification off. No production counters reset.
  Ccache uses the source tree's `cmake/ccache.conf` and base `/build/richard` on
  host, `/code` in EL10. Saved `*-config.txt` files contain all effective settings.
- Host source/build roots map to `/code/sunbird_strategy` and
  `/code/sunbird_strategy/build`. EL10 mounts both physical trees at the same
  `/code/sunbird_strategy`, with build output at `build-el10`.
- Warm page cache, fixed outputs overwritten by explicit cache invocations;
  no page-cache eviction. Primary timings have `VCACHE_LOG` disabled; separate
  diagnostic batches prove all manifest-hit decisions. Every measured command
  exits zero, and exact counter deltas prove 522/520 hits with zero misses.
- Every build/test/benchmark used `~/bin/build-limited`, with wrapper-provided
  parallelism. The captured limits were 64 CPUs, 88 GiB memory, no swap, and
  8,192 tasks, shared across the slice. Do not copy these limits into reruns.
  Hardware: Threadripper 9980X, 64 cores/128 threads, Fedora 44 kernel
  `7.2.9-200.fc44.x86_64`; `/build` is local NVMe XFS with `noatime`.
- No task setup builds overlapped the final timing runs. Other machine work was
  uncontrolled: load averages at fingerprint capture were 22.6/29.8/24.8;
  before final EL10 timing, 61.4/45.4/34.5. Aggregate wall includes Python replay
  scheduling; child CPU comes from `wait4` and excludes the Python parent.
  Daemon CPU is measured separately by PID/start-time and private store, at
  10 ms `/proc` tick resolution.

## Cross-worktree reuse versus steady hits

The first B pass from A's store is separate from the repeated warm B passes:

| Platform/tool | Invocations | Manifest/direct hits | Other result hits | Misses | Wall / child user / sys (s) |
|---|---:|---:|---:|---:|---|
| Host vcache | 522 | 32 | 464 | 26 | 14.893 / 103.780 / 27.721 |
| Host ccache | 522 | 0 | 0 | 522 | 18.415 / 613.759 / 37.223 |
| EL10 vcache | 520 | 520 | 0 | 0 | 4.616 / 1.368 / 7.017 |
| EL10 ccache | 520 | 520 | 0 | 0 | 3.789 / 1.516 / 3.628 |

The host generated `cmake_pch.hxx` contains an absolute checkout path. Its changed
contents reject a manifest state even when the subsequent preprocessed result
hits; `cross-b-vcache/0.vcache.log` records that example. The remaining host
misses and ccache's path sensitivity were not individually attributed. Do not
interpret the first host B pass as the cost of a manifest hit. After B is seeded,
all repeated host invocations hit. Canonical EL10 avoids that path-layout change.
The first EL10 B pass used direct disk fallback because restarting the container
stopped its daemon. It is retained as a control, not mixed into the final
running-daemon timings below.

## Three interleaved compile-only runs

Seconds; each cell lists rounds 0, 1, 2. Tool order was V/C, C/V, V/C. Every round
has exactly the workload's object count in manifest/direct hits. Daemon user/sys
are additional CPU, not included in child user/sys.

| Platform/tool | Wall | Child user | Child sys | Daemon user / sys |
|---|---|---|---|---|
| host vcache | 0.819 / 0.419 / 1.802 | 1.246 / 1.208 / 1.072 | 4.638 / 4.381 / 4.403 | 0.25 / 0.81; 0.26 / 0.44; 0.22 / 0.42 |
| host ccache | 0.355 / 0.465 / 0.181 | 1.342 / 1.357 / 1.028 | 3.843 / 4.364 / 1.965 | — |
| el10 vcache | 10.855 / 8.629 / 4.732 | 1.006 / 1.072 / 1.314 | 4.911 / 5.278 / 5.795 | 0.12 / 0.29; 0.12 / 0.31; 0.12 / 0.31 |
| el10 ccache | 4.615 / 3.159 / 3.644 | 1.513 / 1.609 / 1.868 | 4.000 / 3.960 / 4.440 | — |

Median child system CPU is 4.40 s versus 3.84 s on host, plus 0.44 s daemon system
CPU; EL10 is 5.28 s versus 4.00 s, plus 0.31 s daemon system CPU. Median wall is
0.819 versus 0.355 s on host and 8.629 versus 3.644 s on EL10, with wide ranges.
The older 3–10 s whole-build CPU gap cannot be attributed wholesale to hits:
that workload included links and used a different target set and launch method.

## Twenty interleaved repetitions per translation unit

Selection is by unique depfile input count: lowest non-PCH, median non-PCH and
median PCH-consuming command. The PCH cases differ between platforms because
of their target/input distributions.

| Case | Source relative to sunbird | Host / EL10 inputs |
|---|---|---:|
| Small | `wiregen/WiregenDescriptor.cc` | 142 / 141 |
| Median | `sunbird/third_party/treelite_4_6_1_local/model_loader/detail/xgboost_json/delegated_handler.cc` | 335 / 335 |
| Host PCH | `sunbird/live/WarmupFeatureCalc.cc` | 541 / — |
| EL10 PCH | `sunbird/ensemble/testSymbolRules.cc` | — / 545 |

All times below are milliseconds per invocation, from the individual child
record, not the one-item thread-pool batch duration. p95 is the 19th ordered
sample of 20; no p99 claim. User and sys medians are computed separately and
need not sum to the median CPU total.

| Platform/case/tool | Wall median | p95 | Min–max | User median | Sys median | Syscalls |
|---|---:|---:|---:|---:|---:|---:|
| host/small/vcache | 5.597 | 6.927 | 4.572–106.361 | 1.371 | 3.176 | 1309 |
| host/small/ccache | 3.323 | 6.553 | 2.926–95.077 | 1.263 | 1.808 | 506 |
| host/median/vcache | 7.469 | 208.876 | 6.518–593.250 | 1.196 | 4.962 | 2986 |
| host/median/ccache | 4.266 | 19.918 | 3.709–91.676 | 1.860 | 2.107 | 907 |
| host/pch/vcache | 8.127 | 9.480 | 7.416–10.956 | 1.531 | 5.784 | 4735 |
| host/pch/ccache | 4.976 | 5.740 | 4.169–6.536 | 2.095 | 2.142 | 1343 |
| el10/small/vcache | 10.898 | 12.163 | 9.198–12.500 | 0.758 | 5.481 | 1453 |
| el10/small/ccache | 6.615 | 7.732 | 4.383–8.886 | 0.391 | 3.502 | 558 |
| el10/median/vcache | 19.913 | 72.477 | 11.527–104.771 | 1.796 | 8.677 | 3226 |
| el10/median/ccache | 10.571 | 49.569 | 9.060–121.444 | 1.268 | 4.659 | 996 |
| el10/pch/vcache | 19.545 | 21.678 | 14.675–21.711 | 1.886 | 9.897 | 3535 |
| el10/pch/ccache | 17.564 | 76.228 | 14.026–96.635 | 3.502 | 7.602 | 1436 |

The median host non-PCH overhead is **3.20 ms**, exceeding the bead's 2 ms
comparison threshold. EL10's median non-PCH overhead is 9.34 ms; its PCH case is
within 2 ms (1.98 ms), despite extra system CPU. These are medians under the
recorded shared-host load, not latency guarantees.

Across each set of 20 vcache repetitions, daemon user/sys totals were host small
0/0 ms, median 0/0 ms, PCH 0/10 ms; EL10 small 0/0 ms, median 0/10 ms, PCH
10/20 ms. Zeros mean below tick resolution in the sampled windows, not free
service. Aggregate daemon CPU above is the stronger measure.

## Syscall attribution

Traces are separate from timing. Counts cover the cache process from `execve`
through `exit_group`; the attached EL10 trace excludes 69 Python child setup
calls before exec and the waiting parent. Split unfinished/resumed records are
recombined. No compiler process is executed on these hits.

| Platform/case/tool | Metadata | File I/O | Network | Memory | Locks | Process/other | Total |
|---|---:|---:|---:|---:|---:|---:|---:|
| host/small/vcache | 598 | 645 | 16 | 32 | 6 | 12 | 1309 |
| host/small/ccache | 335 | 82 | 0 | 59 | 1 | 29 | 506 |
| host/median/vcache | 1500 | 1418 | 16 | 34 | 6 | 12 | 2986 |
| host/median/ccache | 738 | 82 | 0 | 57 | 1 | 29 | 907 |
| host/pch/vcache | 2419 | 2249 | 16 | 33 | 6 | 12 | 4735 |
| host/pch/ccache | 1172 | 82 | 0 | 59 | 1 | 29 | 1343 |
| el10/small/vcache | 747 | 654 | 16 | 23 | 5 | 8 | 1453 |
| el10/small/ccache | 362 | 107 | 0 | 61 | 1 | 27 | 558 |
| el10/median/vcache | 1743 | 1431 | 16 | 23 | 5 | 8 | 3226 |
| el10/median/ccache | 795 | 107 | 0 | 66 | 1 | 27 | 996 |
| el10/pch/vcache | 1203 | 2279 | 16 | 24 | 5 | 8 | 3535 |
| el10/pch/ccache | 1233 | 107 | 0 | 68 | 1 | 27 | 1436 |

The class table groups `stat`/`readlink` as metadata, open/read/write/close and
filesystem mutation as file I/O, socket setup/send as network, allocation as
memory, and `flock`/`futex` as locks. Socket `read`/`close` are file-I/O syscalls in
that table; the path/phase table below correctly attributes them to daemon IPC.

Median vcache hit, with each syscall assigned exactly once:

| Phase/path bucket | Host calls | EL10 calls | Host traced elapsed (ms) | EL10 traced elapsed (ms) |
|---|---:|---:|---:|---:|
| Startup/configuration/root resolution | 85 | 85 | 0.570 | 3.332 |
| Compiler identity memo | 13 | 12 | 0.079 | 0.103 |
| System include-directory memo | 0 | 0 | 0.000 | 0.000 |
| Manifest key/source/include setup | 17 | 23 | 0.069 | 0.235 |
| Digest memo files | 1685 | 1690 | 3.833 | 19.389 |
| Manifest input identity validation | 336 | 337 | 1.106 | 13.523 |
| Absent-path probes | 769 | 1003 | 1.728 | 30.769 |
| Daemon IPC: manifest and result | 29 | 29 | 0.389 | 3.053 |
| Object/depfile materialization | 14 | 14 | 0.161 | 0.729 |
| Scratch/owner setup and cleanup | 26 | 21 | 0.203 | 0.398 |
| Counters | 10 | 10 | 0.134 | 0.515 |
| Other/unassigned | 2 | 2 | 0.007 | 2.849 |

Totals reconcile to 2,986 host and 3,226 EL10 calls. The two unassigned calls are
one `brk` during result receipt and process exit. Include-directory memo calls
are zero in these warm paths. The compiler identity uses its memo, not a compiler
version subprocess. The manifest and result blobs arrive over daemon sockets,
so there is no client blob-file read to count separately. There are two
connections, each with a configuration hello and GET: four request/reply
exchanges, with no compile-session, lease or admission request. Daemon work is
outside the client trace and is accounted for by its separate CPU measurements.

A memo hit performs open, fstat, read, EOF read, close: 337 memo reads on host
and 338 on EL10, including source-key work. These account for 56% and 52% of
all calls respectively. Absent probes remain necessary for correctness. A warm
hit creates a scratch directory and owner lock, then cleans them up. It updates
two counters in one locked write; it does not call `RecordDecision` or emit a
production log with logging disabled. Output writes have no fsync.

**Traced elapsed time is not system CPU or a saving estimate.** Ptrace and shared
host scheduling strongly inflate it, especially in EL10. It is retained only to
make the attribution reproducible; optimization projections use uninstrumented
CPU, call counts and sampling evidence.

## User-side sampling

Each profile samples 200 confirmed hits separately from timing; counters prove
401 hits per case/tool across strace (1), perf stat (200) and perf record (200).
Host perf/strace are 7.2.9/7.2; `perf_event_paranoid=2`, with user cycles and
instructions available. No kernel setting changed. EL10 has neither executable,
so host tools attach to the verified Python replay PID and inherit its children.
Reports filter to the cache process and use copied EL10 binaries/libraries.
`perf stat` includes the replay parent; its totals are not presented as client
CPU. Context-switch events are user-restricted and report zero, so they do not
establish an absence of scheduling delays.

Top resolved median-vcache symbols, percent of process-filtered sampled weight:

| Symbol | Host | EL10 |
|---|---:|---:|
| Loader `do_lookup_x` | 11.39% | 2.04% |
| Loader `_dl_lookup_symbol_x` | 11.11% | below the leading entries |
| Loader relocation (`_dl_relocate_object_no_relro`) | 4.86% | below the leading entries |
| `HashFileMemoizedAt` | 4.12% | 4.36% |
| BLAKE3 hash-many | 2.79% | 4.92% |
| libc memory copy | 3.38% | 4.03% |
| tcmalloc `CentralFreeList::RemoveRange` | 2.07% | 3.58% |
| Boost.Spirit depfile parsing sequence | 2.49% | 1.63% |

Loader symbols total 32.06% on host and 9.50% on EL10; EL10's client already
statically incorporates the C++ runtime. Host call stacks also show tcmalloc
initialization under startup. Unresolved sampled addresses remain 19.32%/24.75%
for the median profiles; no cost is assigned to those symbols. Sampling thus
supports candidate ranking, not exact whole-phase CPU accounting. Ccache's host
median profile is led by Zstd decompression (21.41%) and loader work; EL10 PCH
vcache is led by BLAKE3 (26.86%) and copying (12.87%), so memo work is not the
only cost across all objects.

## Ranked follow-ups and projections

These are screening estimates for the **host median hit**, not measured gains.
All ranges admit zero until a correct prototype demonstrates improvement. The
memo estimate is supported by 56% of calls, 4.96 ms measured system CPU and the
2.86 ms system-CPU gap to ccache. Startup uses its sampled loader share against
1.20 ms median user CPU. Scratch/counter estimates use their small call counts
with extra allowance for filesystem mutation. No estimate converts strace
elapsed time into saved CPU.

| Rank / lever | Potential saved user / sys per hit | CPU over 525 hits | Decision |
|---|---|---|---|
| 1. Shared mapped digest table | 0–0.2 / 0–3 ms; target 1–3 ms sys | 0–0.105 / 0–1.575 s | Stage `vcache-83w`; preserve full identity, settled-write and corruption safeguards |
| 1 alternative. Identity plus digest in manifest | 0–0.2 / 0–3 ms | Same envelope | Competes with the table; same-tree scope, format growth and invalidation proof needed; do not add savings |
| 2. Cheaper startup/lazy initialization | 0–0.5 / 0–0.2 ms | 0–0.263 / 0–0.105 s | Defer implementation; smaller EL10 loader share weakens a blanket static-link recommendation |
| 3. Lazy scratch allocation | 0–0.03 / 0–0.10 ms | 0–0.016 / 0–0.053 s | Stage `vcache-zs1`, small and independent; 26 host calls, not just mkdir/rmdir |
| 4. Daemon-side validation with resident digests | 0–1.2 / 0–3 ms client CPU | 0–0.630 / 0–1.575 s client CPU | Defer: overlaps memo designs, moves validation CPU to daemon; net saving may be zero or negative |
| 5. Batch counters and decisions | 0 / 0 from combining those operations | 0 | Already one hit-counter write, no decision write; a different concurrent-counter design needs contention evidence |

A bounded shared table is preferred over expanding every manifest with local
stat identities because it can share memoized digests across commands and
worktrees without duplicating identity data per manifest. This is a design
choice to validate, not a content-correctness proof. File replacement, restored
mtime, unsettled writes, torn publication and concurrent eviction must be tested.

For H unchanged-manifest hits, multiply a candidate's per-hit CPU range by H;
use 522/520 for this workload, 525 only for comparison with the historical one.
At 100 hits the memo target is 0.1–0.3 s system CPU, not necessarily wall time.
A leaf edit with one compile and zero hits saves zero hit-validation CPU.
A shared-header edit that invalidates manifests invokes preprocessing; those
commands are not counted as unchanged hits and belong to `vcache-axb`.
Parallel wall savings depend on critical-path work, contention and scheduling;
none can be inferred by dividing or summing these serial-hit estimates.

## Evidence, rerun and rejected controls

All scripts, configs, command lists, return codes, deltas, logs, perf data and
analysis are retained on `zeus.sg.fbg` at:

`/build/richard/codex-1000/cxx-hit-attribution-20261015/`

- `fingerprint.json`, `el10-daemon-environment.json`, `*-config.txt`: environment.
- `cxx_hit_benchmark.py`, `check_harness.py`, `run.py`, `el10.py`: guarded replay
  and limited execution; `host-final.json` / `el10-final.json`: measured configs.
- `host-results/idle-warm-*`, `host-results/repeat-*`: final host timings.
- `el10-results/daemon-warm-*`, `el10-results/daemon-repeat-*`: final EL10 timings.
- `diagnostics-vcache` / `daemon-diagnostics-vcache`: separate decision logs.
- Host `profile-*`; EL10 `host2-profile-*`, `el10-profile-control2`: valid profiles,
  exact host PID, per-profile child PID lists and 1/200/200 hit-counter evidence.
- `analysis.json`, `analyze.py`: all samples, syscall classes and reconciled buckets.
  `*.perf.data.flat` files contain the full resolved symbol rankings.
- `replay.py` and `REPRODUCE.md`: fresh-result rerun against the retained prepared
  worktrees; use a new label and do not overwrite earlier artifacts.

The accounting fixture deliberately omits an invocation, duplicates a target,
changes a hit counter, adds a miss, fails a command, resets/removes counters,
and inserts invalid compile-database entries. Every mutation is rejected by its
named guard; quoted paths parse successfully. `harness-guards.log` records this.
No regression assertion was added to the repository solely to record measurements.

Rejected/control data is retained: early host warm batches overlapped setup;
EL10 `warm-*`/`repeat-*` ran with direct disk fallback; the first `host-profile-*`
attached to the waiting container entrypoint rather than its replay child and
has no useful cache samples. None enters the final comparisons above. The
corrected container profiles assert the actual replay PID and child hit counts.

The repository gate initially encountered a daemon-readiness failure, then a
compiler-barrier failure on the next run, with 1,470 unit checks passing on that
second run. Those unchanged synchronization checks and both logs are retained;
the subsequent full gate passed 1,470 unit, 1,320 integration, 93 link-verification
and 15 harness checks (`full-gate-final.log`). No test code was changed.


## Lazy scratch implementation follow-up

The follow-up `vcache-zs1` moves the existing C/C++ scratch allocation and its
cleanup guard past the early manifest-hit return, immediately before preprocessing.
A hit now restores its outputs even if both scratch locations reject allocation.
Cold misses, stale manifests and unusable cached results still allocate before
preprocessing; a miss with no scratch still passes through to the real compiler.

No cache-key/version change is needed: compiler identity, roots, manifest state
selection, dependency validation and result materialization are unchanged. The
recording timestamp remains before source reads; allocating later does not weaken
changed-during-run rejection. The guard retains the same lifetime on the miss path.
Manifest ordering/tie-breaking and output bytes are preserved; floating-point and
application RNG behavior are not involved. The change removes only unused scratch
work. Its opportunity score was impact 1 × confidence 5 / effort 1 = 5; this is a
small independent cleanup, not the leading digest-table optimization.

`tests/cxx_scratch_test.py` probes and selectively denies vcache's `mkdtemp` calls
on Linux. Both warm-hit assertions fail against the `7eca5a7` baseline and pass
after the change, including byte-identical objects and dependency files. The cold,
stale, unusable-result and denied-miss controls pass before and after. The full
host gate passes 1,470 unit, 1,320 integration, 93 link-verification, 15 harness
checks, plus the seven scratch checks. The new test also passes with the EL10
release toolchain. Its allocation probe is explicitly skipped off Linux; no macOS
or arm64 execution is claimed.

The retained sunbird workload was replayed against baseline and changed binaries
in interleaved before/after, after/before, before/after order. All 522 host and
520 EL10 commands hit in every aggregate run, with identical SHA-256 values for
every object and depfile. Both clients used the same private running baseline
daemon and warm store. Setup builds and tests finished before timing. Per-case
runs contain twenty repetitions of each binary, with separate strace samples.

Milliseconds per hit; wall reports median / p95 / range, user/sys separate medians:

| Platform/case | Before wall | After wall | Before user / sys | After user / sys | Syscalls before → after |
|---|---|---|---|---|---|
| host/small | 5.408 / 6.157 / 4.586–6.161 | 5.359 / 6.695 / 3.932–430.867 | 1.010 / 3.646 | 1.015 / 3.887 | 1309 → 1288 |
| host/median | 6.886 / 7.458 / 6.328–7.850 | 6.787 / 7.681 / 5.599–7.844 | 1.873 / 4.225 | 1.942 / 4.192 | 2985 → 2964 |
| host/pch | 8.212 / 175.205 / 6.828–240.460 | 8.335 / 190.431 / 7.146–228.449 | 2.236 / 4.802 | 2.009 / 5.218 | 4735 → 4715 |
| el10/small | 10.655 / 85.817 / 7.116–100.716 | 10.728 / 70.808 / 9.646–122.631 | 1.039 / 5.271 | 0.916 / 5.531 | 1453 → 1432 |
| el10/median | 19.864 / 70.490 / 12.121–109.072 | 19.246 / 21.280 / 11.230–39.611 | 0.940 / 9.052 | 1.196 / 8.883 | 3225 → 3204 |
| el10/pch | 20.405 / 69.272 / 15.739–79.323 | 20.195 / 77.230 / 15.024–100.059 | 1.577 / 10.284 | 2.000 / 9.804 | 3534 → 3513 |

All six changed-binary traces have zero scratch-path accesses, down from fourteen
path-bearing calls each, including directory creation and owner locking/cleanup.
Total call counts fall by 20–21; source/header validation remains in place. The
older 26-call attribution included setup operations that can also occur elsewhere
in a process, so it is not an exact prediction of the net reduction.

Aggregate seconds, rounds 0 / 1 / 2; daemon CPU is additional to child CPU:

| Platform/version | Wall | Child user | Child sys | Daemon user / sys |
|---|---|---|---|---|
| host/before | 0.812 / 1.439 / 0.372 | 0.864 / 1.125 / 1.142 | 2.999 / 5.164 / 6.272 | 0.15 / 0.30; 0.28 / 0.46; 0.29 / 0.57 |
| host/after | 3.028 / 1.517 / 0.498 | 1.042 / 1.094 / 1.102 | 4.168 / 4.478 / 5.605 | 0.21 / 0.74; 0.24 / 0.46; 0.30 / 0.52 |
| el10/before | 15.084 / 4.518 / 7.850 | 0.921 / 0.971 / 1.112 | 5.300 / 4.942 / 5.172 | 0.11 / 0.43; 0.12 / 0.19; 0.10 / 0.23 |
| el10/after | 4.095 / 8.528 / 13.257 | 0.951 / 1.004 / 1.393 | 4.913 / 4.825 / 10.082 | 0.10 / 0.30; 0.11 / 0.22; 0.17 / 0.51 |

The latency results are mixed, with substantial shared-host scheduling variance.
They do not establish a measurable speedup. The verified gain is removal of unused
filesystem operations and scratch availability from the hit path. Aggregate and
per-case daemon samples, load averages, binary hashes, output hashes and raw logs
are retained; syscall timings were not substituted for CPU measurements.

Evidence: `/build/richard/codex-1000/lazy-scratch-20261015/`.
`regression-before-portable.log` contains the two intended failures with the final
Python 3.6-compatible harness, which removes its outputs before each compile.
`regression-after.log`, `build-el10.log` and `full-gate-portable.log` contain passing checks.
`compare.py`, `compare_el10.py`, `el10_workload.py` and `run.py` retain the exact
limited-execution recipe. `host-measurements/` and `el10-measurements/` hold command
results, counter deltas, golden output hashes, PID-scoped traces and environment
snapshots; `summary.json` contains all timing ranges and separate daemon CPU.
Artifact names are exclusive; use fresh result/control directories for a rerun.

