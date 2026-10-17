# Mapped dependency traces

The parent selects one format before launching a compiler or linker tree.
`VCACHE_TRACE_FORMAT=mmap-v1` opts into mapped transport; `text` or no opt-in
uses legacy append records. This is an internal parent/tracer protocol.

The mapped file starts with a 4,096-byte header page. Its 64-byte header holds
`VCTRACE\0`, version 1, the host byte-order tag `0x01020304`, header/chunk sizes,
the maximum chunk count, a reserved word, an aligned atomic 32-bit reservation
count, an aligned atomic incomplete flag, and six reserved words. Chunks are
65,536 bytes, with a 64-byte state header. At most 4,096 chunks are reserved:
the trace is bounded at 256 MiB plus its header. Layout and reserved fields
must match exactly. Only coherent local Linux filesystems with 64-bit syscall
arguments and lock-free 32-bit atomics select this format.
The supported filesystem types are ext, XFS, Btrfs and tmpfs; others select text.
Header and chunk offsets must also meet the kernel's page alignment; a larger
page size selects text before any writer starts.

An atomic reservation increments the count before allocation. Each writer
allocates its disjoint extent before mapping it; file growth never truncates
another writer's extent. A reservation beyond EOF, an uninitialized chunk, or
any allocation failure makes the trace incomplete. Each thread owns a chunk
and checks its PID before recording, discarding inherited ownership after fork.
The fork child handler also clears ownership so equal numeric PIDs in different
PID namespaces cannot share a chunk.
The public `fork` hook covers initialization paths that do not run registered
handlers. Signals cannot interrupt the thread's mapping reset; reentrant records
inside the fork transition make the trace incomplete without blocking user handlers.
If a handler forks while an emitter is active, the child keeps its inherited
mapping until the interrupted frame returns and marks the whole trace incomplete.
Full chunks remain immutable. Thread exit and exec need no flush. Mappings
left by exited threads are bounded by the invocation's chunk limit.

A chunk is uninitialized (0), writing (1), or committed (2). Before modifying
an existing chunk the writer publishes writing. A record has a 32-bit length
with its high bit set while writing, followed by the original newline-terminated
record, padded to eight-byte alignment. The payload is copied before a release
store publishes its length; a release store then commits the chunk. A writer
killed before the first byte, during the payload, or before either publication
leaves a reserved hole or an unfinished state. Readers run after completion,
require committed chunks, validate every record and boundary, and require zero
padding and a wholly zero unused tail. They never depend on inter-thread order.

After mapped mode is selected, failures set the preallocated header's incomplete
flag and try the existing sidecar marker. A mapped file never receives a legacy
record or sentinel. A failed header mapping uses a positional write to the
preallocated flag. Unsupported parent setup can choose legacy mode before any
writer starts. If both mapping and the positional write fail (for example, an
exec child has a hard file-size limit below the flag's offset), the tracer
revokes the log's read permission. Descriptor exhaustion uses the same backstop
by pathname, without opening another descriptor. The reader explicitly refuses that state,
including when running as root. Unexpected backing-store failure or external
truncation of a live mapping remains outside the supported coherent-filesystem
contract.

Old parents leave the opt-in absent, so new tracers write text. Old tracers used
with a new parent may append text to the binary header; the new reader refuses
that malformed file. A new tracer encountering a mapped header without the
opt-in marks it incomplete. The reader still accepts legacy text, including
legacy tracers that replace the entire file. Unknown or malformed mapped headers
are refused. Binary/tracer rollback therefore loses caching when incompatible,
rather than trusting a partial dependency set.

The R/M/P/L/W/D meanings, absolute path spellings and 4,096-byte record limit
are unchanged. No semantic cache-key version changes are needed for transport;
the private format carries its own version.

## Measurements (2026-10-18)

The transport eliminates per-record trace writes. These measurements do not
establish an overall build speedup. Chunk allocation and unused space still cost
CPU and storage; the system-CPU gap that motivated this work remains.

The baseline is the code at `3182237` (binary-changing predecessor `d460a2e`),
including the shared file-digest memo. Host comparisons rebuild that baseline
with the candidate's GCC 16.2.1 and unchanged objects. EL10 uses GCC 16.2.0
absent-deps-1 for both. Compiler commands, PCHs, source trees, outputs and cache
settings are held fixed. All work runs through `build-limited`, with no limit
overrides and no concurrent agent builds during the final timing rounds.

Three alternating pairs replay the Sunbird corpus used in the C/C++ attribution
work: 522 host / 520 EL10 objects, excluding PCH producers and links. Each cold
fill uses a new store and private daemon; each warm control uses that same store.
This is the object-fill portion of the migration workload, not a fresh configure
or a full linked build. The threaded link is measured separately below. Cold
runs contain 521–522 / 519–520 misses; an equivalent preprocessor result sometimes
produces one within-fill hit. Every warm run has exactly 522 / 520 manifest hits
and no misses or errors. All output objects match byte-for-byte.

Seconds, median of three. CPU is summed over clients and their compiler children.

| Platform / phase | Before wall / user / sys | Mapped wall / user / sys |
|---|---:|---:|
| host cold | 16.844 / 524.430 / 47.249 | 17.405 / 535.194 / 48.996 |
| host warm | 0.397 / 0.987 / 11.299 | 0.688 / 1.080 / 3.290 |
| el10 cold | 22.716 / 485.749 / 60.373 | 22.151 / 485.900 / 59.713 |
| el10 warm | 2.519 / 1.160 / 3.445 | 2.680 / 1.311 / 3.630 |

Warm wall times vary substantially (including host outliers above three seconds).
Warm client user CPU rises by about 0.09 s on host and 0.15 s on EL10 across the
whole corpus; warm paths do not emit dependency traces. These samples are not
evidence that overall cold or warm performance improved. Raw ranges, per-command
CPU/RSS, daemon CPU deltas, counters, commands and logs are retained with the run.

Five alternating direct-compiler samples per cell follow. The three TUs are the
existing small/median/PCH selections; the link uses 32 real objects containing
6,400 functions and `lld --threads=8`. Output bytes match. Dependency sets match
apart from compiler-generated random temporary output names, whose exact
differences are retained. RSS is the child-tree maximum reported by `wait4`.

| Platform / case | Before wall / user / sys (s) | Mapped wall / user / sys (s) | Before / mapped RSS (KiB) |
|---|---:|---:|---:|
| host small | 0.5956 / 0.5671 / 0.0249 | 0.6104 / 0.5757 / 0.0289 | 142944 / 142096 |
| host median | 1.2267 / 1.1569 / 0.0408 | 1.2149 / 1.1644 / 0.0428 | 186236 / 185704 |
| host pch | 0.5106 / 0.4747 / 0.0348 | 0.5258 / 0.4821 / 0.0408 | 202380 / 203400 |
| host lld | 0.0224 / 0.0161 / 0.0147 | 0.0257 / 0.0150 / 0.0196 | 69244 / 69104 |
| el10 small | 0.6551 / 0.6203 / 0.0268 | 0.6408 / 0.6050 / 0.0268 | 138212 / 140256 |
| el10 median | 1.4014 / 1.2833 / 0.0579 | 1.3359 / 1.2741 / 0.0489 | 179760 / 180496 |
| el10 pch | 1.8501 / 1.7425 / 0.0856 | 1.8357 / 1.7403 / 0.0904 | 343392 / 342116 |
| el10 lld | 0.0293 / 0.0133 / 0.0226 | 0.0309 / 0.0154 / 0.0218 | 67416 / 68576 |

Host `strace -f -yy` attributes these calls to the trace file itself. Parent
setup is outside the compiler profile: one header allocation and one positional
header write. Each traced process maps the header, and each chunk adds one
allocation and one mapping. Successful record publication has zero writes.

| Case | Text writes → mapped writes | Mapped allocations / mappings | Text apparent / allocated bytes | Mapped apparent / allocated bytes |
|---|---:|---:|---:|---:|
| small | 684 → 0 | 1 / 2 | 44981 / 45056 | 69632 / 69632 |
| median | 1599 → 0 | 2 / 3 | 115507 / 118784 | 135168 / 135168 |
| pch | 1632 → 0 | 3 / 4 | 126565 / 126976 | 200704 / 200704 |
| lld | 311 → 0 | 2 / 4 | 19236 / 20480 | 135168 / 135168 |

The small TU uses 69,632 bytes for 44,981 bytes of text; the threaded link uses
135,168 bytes for 19,236 bytes of text. This is the measured cost of 64 KiB
thread-owned chunks. The bounded protocol deliberately keeps this initial chunk
size rather than introducing another allocation policy without evidence.

## Verification evidence

`tests/trace_mapped_test.py` checks exact unique R/M/P/L/W sets, retained D records,
literal backslashes, symlink-sensitive path spelling, thread reuse, fork/exec,
environment clearing, reentrant/concurrent initialization, killed writers at
seven publication barriers, allocation/map failures, EINTR, ENOSPC, EDQUOT, hard
file-size limits, failed marker creation, cursor exhaustion and both consumers.
Newly created includes and earlier search-path libraries invalidate existing
manifest hits; edited inputs change the compiled and linked results.

The old implementation fails the mapped-reader and process-tree assertions.
Deliberate mutations fail the header, reservation, state, length, maximum-size,
padding, omitted-record/chunk, fork ownership, busy-state, shared failure flag
and successful-chroot assertions. Separate failing-first checks caught parent
SIGXFSZ delivery, failed header attachment under a hard limit, descriptor
exhaustion with no marker, incompatible mapping alignment, equal-PID namespace
forks, and constructor-time reentrant forks on both host and EL10. Old-parent/new-tracer
compiles still hit through text;
new-parent/old-tracer compiles return identical objects but refuse manifest reuse.

Raw evidence and scripts are in
`/build/richard/codex-1000/vcache-zfn/`: `reader-before-focused.log`,
`process-before.log`, `setup-limit-before.log`, `attachment-before.log`,
`mutation-*.log`, `compatibility/`, `host-matched-summary.json`,
`el10-matched-summary.json`, and both `*-matched-profile/` directories.
`measurements-final.log` includes the intentional alignment failure after all
measurements finished. Earlier pilot profiles used the installed host baseline
with a different compiler and are excluded from the tables above.
Timings precede the final legacy-mode bypass, fork-lifetime protections and
descriptor/page-alignment refusal guards. Those changes were checked by regressions and the full gate; the timing
tables are evidence for this transport design, not final-release speed claims.
The final alignment precheck selects the same mapped mode on these 4 KiB-page hosts.

## Delivery

Merged source: `180d20c1a5692a51f610f903ea76f93bfe4d3221` (implementation
`aad86e6`). Anvil **vcache 1.6** contains fedora44, el10, ubuntu24, amzn2023 and
dc-build. The host, EL10 and dc-build `dist/` binary/tracer pairs match those
published artifacts byte-for-byte, with the merged revision in each stamp.

The release gate passed 1,564 unit, 1,342 integration, 93 link-verification and
93 publisher checks, plus the ancillary suites. Final EL10 fault and process-tree
checks also passed. Installed-pair checks in each of host, EL10 and dc-build
verify compile/link record and hit, changed and newly present inputs, and every
expected threaded/fork/exec dependency. EL10 additionally passes Cargo 1.97.1
loading and a real threaded `lld` record/hit. Every target exports all 33 prior
tracer hooks plus `fork`, including the build against glibc 2.17 headers; no test
hook is exported.

All five x86-64 Linux targets pass startup and tracer loading in their own OS
environment. Linux aarch64 and macOS were not executed in this session.
Evidence: `gate-release.log`, `publish.log`, `installed.log` (host), and
`installed-clean-launch.log` (EL10, dc-build and export comparison), under the
scratch directory above.
Link-verification soak remains configured as `vcache_link=verify`.
