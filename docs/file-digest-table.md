# Shared file digest memo table

`HashFileMemoized` still returns a content digest. Its memo identity is the complete
device, inode, size, nanosecond mtime and nanosecond ctime tuple. Both timestamps
must be at least two seconds old, including on a memo hit. A fresh hash is recorded
only if a second stat matches the identity read before hashing. Manifest validation,
absent-path probes and result keys are unchanged.

## Storage and publication

`filehash/table-v1` has a 4,096-byte header page followed by 16,384 aligned
192-byte slots: 3,149,824 bytes total. The first 32 header bytes contain the
eight-byte `VCMEMO\r\n` magic, 32-bit version 1, byte-order tag `0x01020304`, header
size, slot size, slot count and a zero reserved word. Integer fields use host byte
order; mismatched byte order or layout is rejected. The remaining header page is
zero padding. The storage version is independent of cache-key versions.

Each slot has a 32-bit atomic state, a reserved word, the 40-byte identity, the
64-byte lowercase hex BLAKE3 content digest, a 64-byte hex BLAKE3 checksum of the
identity bytes followed by the digest, and alignment padding. The first eight hex
digits of the existing identity memo key choose a bucket; lookup and insertion
probe at most 32 adjacent slots, wrapping at the fixed capacity.

A writer claims an empty slot (state 0) using a lock-free, process-shared atomic
compare/exchange to writing (state 1). It writes identity, digest and checksum,
then publishes committed (state 2) with release ordering. Readers acquire the
state, require the complete identity, and verify digest spelling and checksum.
Payloads never change after publication. There is no seqlock payload race, slot
reuse, resize, eviction lock or process-local mutex inherited by a fork. A crashed
writer consumes one slot; it cannot publish a partial entry. Competing fills may
create duplicate entries, which cost capacity but return the same digest.

The creator uses exclusive creation, allocates the full file with `posix_fallocate`,
then writes the header. Other processes accept only the exact size and header.
A racing reader or a process encountering interrupted setup uses the legacy memo
path. No process repairs, truncates or rewrites an existing table. A failed setup
can leave an unusable file until normal cache trimming or clearing removes it.

## Lifetime and fallback

Each thread retains at most one mapping, replacing it when the absolute memo
directory changes. The backing pages are shared across processes and threads;
only touched pages contribute to each process's resident set. The file descriptor
closes after mapping. Forked children may use the inherited mapping because all
publication synchronization resides in shared memory.

The mapped path is enabled only on Linux ext-family, XFS, Btrfs and tmpfs storage
with lock-free 32-bit atomics and successful allocation/mapping. Other filesystems
(including network filesystems and overlayfs), platforms, permission errors,
unknown versions, truncated tables, exhausted probe windows and failed mappings
use existing individual memo files or hash the input again. Existing memo files
can populate a table and remain readable by old binaries. No production store
cleanup is needed for upgrade or rollback.

Checksum failure is a miss. The checksum detects torn/corrupt persisted payloads;
it does not authenticate storage against an adversary who can rewrite both payload
and checksum. Memo storage has the same trust boundary as the existing cache.
There is no fsync per entry: losing entries after a machine crash costs rehashes.
Preallocation avoids ordinary sparse-file faults, but does not make failing disks
or an external process truncating a live mapped inode safe. Cache maintenance
must remove the directory entry rather than truncate the file; the implementation
never truncates it. This is a local coherent-filesystem optimization, not support
for arbitrary external mutation of a live mapping.

Normal `--trim` removes old table names using the existing 30-day memo policy.
Live mappings continue to reference their original inode until their owning
thread exits or switches stores. New callers create another table. `--clear`
has the same mapping lifetime; retained digests remain safe because the complete
file identity and settling rules still apply. A full table falls back until it is
trimmed; memory and lookup work remain bounded.

## Key compatibility

No semantic cache namespace changes. The complete identity, content digest and
before/after recording checks are preserved. The settling check also applies to
reads, so a future-clock test entry or a clock rollback cannot bypass it. This
does not change result keys or dependency discovery. Table representation is
versioned separately, and legacy memo paths remain unchanged.

## Measurements and validation

The control was revision `76103ca2d5a64be3822f95c1e226e9a978a3eefb`. Replays used
sunbird revision `097a9f79` with 522 host and 520 EL10 compilation commands, private
cache stores and fresh output materialization. Every measured replay was a manifest
hit with zero misses; object and depfile hashes matched the control. Initial cache
population and compiler runs were excluded. EL10's archived fixture needed ten PCHs
rebuilt for its current headers and one missing gtest include directory restored;
both implementations used that same refreshed fixture.

Twenty interleaved samples per input gave these medians. System CPU is the client
process's CPU time; syscall counts come from separate traced runs.

| Platform/input | Wall ms, before → after | System CPU ms, before → after | Memo syscalls, before → after |
|---|---:|---:|---:|
| Host small | 5.010 → 5.062 | 3.078 → 3.240 | 720 → 8 |
| Host median | 6.818 → 6.465 | 4.007 → 3.755 | 1,685 → 8 |
| Host PCH | 7.706 → 7.137 | 4.979 → 4.466 | 2,725 → 8 |
| EL10 small | 11.064 → 9.818 | 5.233 → 5.030 | 720 → 8 |
| EL10 median | 20.515 → 17.604 | 9.472 → 7.473 | 1,690 → 8 |
| EL10 PCH | 20.367 → 15.879 | 10.275 → 7.287 | 2,715 → 13 |

The last case retained one legacy memo read. Three aggregate replays at the build
wrapper's CPU cap reduced median summed client system CPU from 3.309 to 2.763
seconds on host and 4.387 to 3.173 seconds on EL10. Aggregate wall times were noisy:
host control 0.270–1.471 seconds versus table 0.279–1.484; EL10 control 4.006–4.292
versus table 2.509–7.398. These runs establish fewer syscalls and lower system CPU,
not a guaranteed whole-build wall-time improvement. Host's smallest input was
slightly slower. Individual wall-time p95 improved for the PCH cases (host
7.998 → 7.681 ms; EL10 22.283 → 17.459 ms), but not every input.

Each table allocates approximately 3 MiB on disk and shares those mapped pages
between clients. Separate process-memory samples showed roughly 50 additional
minor faults per invocation, no major faults, and peak RSS of 10.0–11.1 MiB on host
and 6.0–14.1 MiB on EL10. Control ranges were 6.0–8.2 MiB and 4.1–11.1 MiB,
respectively. These are single diagnostic samples with allocator variation, not
precise attribution of every resident page. A 65,536-slot prototype and prefaulting
were rejected because their mapping cost weakened short-process performance.

Focused tests cover incomplete publication, damaged payloads and checksums,
identity and timestamp changes, malformed/truncated headers, capacity exhaustion,
concurrent processes and threads, and removal of a live table's directory entry.
The baseline fails the table-creation assertion. Mutations that bypass publication,
checksum, identity, header, size or settling validation each fail the corresponding
assertion. The final pre-integration host gate passed 1,500 unit, 1,321 integration,
93 link-verification and 93 Anvil checks, plus the ancillary script suites.

Earlier gate attempts exposed existing daemon startup/durable-I/O timing failures.
A diagnostic trace measured a blob fsync taking 1.086 seconds against a 500 ms
Put deadline; it did not establish the cause of the separate startup failures.
Those failures remain tracked separately; this change does not alter daemon
deadlines. macOS and other fallback platforms were not runtime-tested here.

Raw logs, counter guards, output hashes, traces, memory samples and replay scripts
are retained under `/build/richard/codex-1000/vcache-83w/`, especially
`host-measurements-final/`, `el10-measurements-final/`, `summary.json`,
`mutations-final.log` and `gate-final.log`. These warm-hit measurements precede
the subsequent tracer fixes merged from `origin/fbg`; warm manifest hits do not
run the compiler or tracer. The combined revision is checked separately before
publication.
