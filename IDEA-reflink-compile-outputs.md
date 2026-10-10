# Plan: clone large Rust compile outputs instead of rewriting them

Version 4. Status: proposal, not planned. Written 2026-10-09 against vcache 1.3.0
(`f76ea19`), revised 2026-10-10 against `fbg` at `33a1053` after checking the claims
against the code and measuring the store, then reviewed in three rounds by three
external models (section 10). The measurements come from one host's store
(`/build/$USER/cache/vcache/host`, XFS with `reflink=1`), which holds Rust entries
only: C and C++ on that host go through ccache.

## 1. Summary

A cached link output is restored as a reflink. A cached Rust compile output is not:
its bytes are read out of the cache entry, copied into memory a second time, written
to a new file and fsynced. On a miss the same thing happens to rustc's own output: it
is read out of the staging directory into memory and written again, with fsync. Most
output files are small and this costs little, but 9% of the files hold two thirds of
the bytes in the store, and those are the ones that pay.

Three steps of increasing size, measured one at a time:

1. Stop fsyncing restored outputs. The temp-file write and atomic rename stay. No
   format change. Ships and rolls back alone.
2. On a miss, move rustc's staged output into the output directory with `rename`
   instead of reading it into memory and writing it again. No format change. Ships and
   rolls back alone.
3. Store each Rust output file at or above a size threshold as a separate
   content-addressed file in the disk cache, written and read through `DiskStorage`,
   and restore it with `CloneFile`. Both Rust key versions bump so older binaries
   cannot reach the new entries. Step 3 builds on step 2's placement and cannot ship
   or stay without it.

## 2. How outputs are written today

| Case | Code | How the file is written |
|---|---|---|
| Rust hit | `RestoreOutputs` in `src/rust/rust_compile.cc` | bytes from the entry, `WriteFileAtomic` (fsync) |
| Rust miss | `CaptureOutputs` then `RestoreOutputs` | rustc writes to a scratch dir inside the cache (`util::MakeScratchDir`, since `d92cf92`; `$TMPDIR` if that fails); each file is read into memory and written to the real output dir (fsync) |
| C/C++ hit | `MaterializeHit` in `src/core/compile.cc` | bytes from the entry, `WriteFileAtomic` (fsync) |
| C/C++ miss | `RunCompile` in `src/core/compile.cc` | `LinkOrCopy` from a private temp dir: a hardlink, else a copy |
| Link hit | `MaterializeOutputs` in `src/core/link.cc` | `CloneFile` from `<shard>/<digest>.linkout`: reflink, else `copy_file_range`, else a streamed copy; no fsync |
| Link store | `RunLink` in `src/core/link.cc` | `CloneFile` from the output into `<shard>/<digest>.linkout`; the entry stores the path, digest and executable flag |

`WriteFileAtomic` fsyncs the file by default (`durable = true`); it never fsyncs the
directory, and it ignores the result of `close`. `CloneFile` deliberately never
hardlinks (see `src/util/fs.h`): a linker may truncate its output in place, and a
shared inode would corrupt the cached copy. A reflink is a separate inode, so it does
not have that problem. `CloneFile` creates the destination at `DefaultFileMode()`, does
not create parent directories, does not carry the source's execute bits, and names its
temp file `<to>.tmp.<pid>`, opened with `O_TRUNC` and no `O_EXCL`, so a collision
across PID namespaces truncates the other writer's temp. `MaterializeOutputs` adds execute bits afterwards, only where the umask
allows read. `RestoreOutputs` adds all three execute bits unconditionally, which is
wrong under a strict umask and should not be copied into new code.

The memory cost is higher than the entry size. `DeserializeBlob` (`src/storage/chain.cc`)
copies every section out of the entry string, so an in-process hit holds about twice
the entry size. With the daemon on, which `devtools/scripts/vcache-env.sh` now does by
default (`VCACHE_DAEMON=auto` with single flight; the AGENTS.md note saying it is off
is stale), the daemon reads the entry and copies it into the reply frame, and the
client copies the frame into the result and then deserialises it. The copies have
different lifetimes, so the peak is up to three copies rather than exactly three.

## 3. Why compile hits cannot use a reflink now

- **One opaque value per entry.** `storage::Storage` stores a single blob per key. That
  keeps the disk and S3 backends trivial and lets a remote hit be written back into the
  local layer unchanged (`CacheChain::Get` backfills when a slower local layer hits).
- **The output is inside the blob.** A Rust entry is a container of sections, with each
  output file's bytes at an arbitrary offset, plus a checksum over the body. `FICLONE`
  clones a whole file, and `FICLONERANGE` needs block-aligned offsets, so no output
  inside a blob can be cloned.

Link outputs were split out of the blob because of a measurement recorded in
`src/core/link.cc`: replaying a 2.2 GB shared library from a blob took 37 s against
51 s to link it again, and peaked at more memory than the linker.

## 4. Measurements

The store on 2026-10-10, 3.6 days old. Output files inside entries, measured by walking
the section headers:

| Band | Files | Bytes | Share of bytes | Mostly |
|---|---|---|---|---|
| All | 11,888 | 32.8 GiB | 100% | |
| 1 MiB or more | 4,589 | 32.1 GiB | 98% | executables, then rlib, rmeta |
| 8 MiB or more | 1,085 | 21.6 GiB | 66% | executables (787), rlib (188), rmeta (90) |
| 64 MiB or more | 42 | 9.0 GiB | 27% | |

No `.d` file is larger than 0.4 MiB. The largest entries are `asupersync` (eleven at
524 MiB, one at 685 MiB; each one rlib of about 300 MiB and one rmeta of about 220 MiB)
and the `launchd` binary at 419 MiB. The rlib and rmeta bytes differ between those
eleven entries, so content addressing would not deduplicate them: the gain is in moving
bytes, not in storing fewer.

Hit evidence, by entry: 2,388 of 8,437 entries have an mtime later than their birth
time, which only `DiskStorage::Get` sets; 253 of the 1,098 entries of 8 MiB or more.
Two caveats: `Get` refreshes the mtime before the caller validates the entry, so an
unusable hit counts, and a re-stored key gets a new inode, so a rebuilt crate counts as
never hit. The numbers are a floor on lookups, not a count of successful restores.

The sunbird debug `deps` directories under `/build/$USER` hold 34 to 75 GiB each, and
97% of those bytes are in files of 8 MiB or more. The 40 GiB store therefore cannot
keep one workspace's large outputs for long, which bounds the hit rate on exactly the
files that cost the most to restore. The store and every target directory are on the
same XFS filesystem, so a reflink between them is possible.

Restoring the 685 MiB entry on that filesystem, source in the page cache. The first
row is what `RestoreOutputs` does: read the file into memory, write a temp file, fsync,
rename. The reflink row is the clone alone; a step 3 hit also hashes the file (below).

| Method | Alone, 3 runs | 8 in parallel, wall time |
|---|---|---|
| read, write, fsync, rename | 0.70 s, 0.70 s, 1.50 s | 4.3 s |
| read, write, rename | 0.69 s, 0.49 s, 0.50 s | 2.0 s |
| reflink (`cp --reflink=always`) | 0.00 s each | 0.07 s |
| `sha256sum` of the file, warm, for scale | 0.36 s | not measured |

`HashFile` is BLAKE3 in 1 MiB reads. Its cost on 685 MiB was not measured and should
be, warm and cold, before step 3's gain is quoted: a step 3 hit is one hash plus one
clone, so the "0.00 s" row is not the hit.

Under parallel load, which is how the memory-limited build slice runs, the fsync is
half the cost and the copy is the other half. Step 1 takes the first half with a
one-line change per call site; steps 2 and 3 take the copy and the memory.

Not measured: how much of a real clean build's wall time goes to restoring. `VCACHE_LOG`
lines carry timestamps but there is no span around the restore itself, so add a
"restored N files, M bytes, T ms" log line first, then run a clean `cargo build` of a
large workspace with the log on, before and after each step.

Measured after step 1 (`docs/design.md`, "Restoring outputs"): a warm dev-profile build
of sunbird's root workspace restores 4,801 MiB in 341 hits. With the fsync it took
6.4 s wall and 37.2 s summed over the restore writes; without, 3.6 s wall, 1.2 s in
the writes and 5.7 s from lookup to placement.

## 5. Step 1: no fsync on restored outputs

`RestoreOutputs` and `MaterializeHit` pass `durable = false` to `WriteFileAtomic`. The
temp-file write and the rename stay, so a concurrent reader still never sees a partial
file. Only the flush before the rename goes. The entry written by `DiskStorage::Put`
keeps its fsync. In the same change `WriteFileAtomic` starts checking the result of
`close`: without an fsync, a delayed write error can surface only there, and today it
would be followed by the rename and a successful hit.

The fsync is there for cache entries, not outputs: `WriteFileAtomic` was written for
`DiskStorage::Put`, whose entries must survive a crash intact, and the restore paths
inherit its default. What step 1 gives up is crash durability of build artifacts. The
window: vcache exits 0, cargo records the unit's fingerprint as fresh, and the machine
crashes before the output's dirty pages reach disk. After reboot the output can be
empty or partial while cargo's fingerprint, which checks existence and mtimes rather
than contents, still counts it fresh. Recovery is `cargo clean -p` for that crate.

Today both a vcache hit and a vcache miss fsync, so this is a real change from current
vcache behaviour. It is the ordinary durability of compiler artifacts: rustc and cargo
do not fsync what they write, as far as the reviewers and the author could tell (rustc's
metadata writer renames without fsync on Linux; this repo cannot prove it), so a build
without vcache has the same window today. Step 2 opens the same window on misses by
renaming rustc's own, never-fsynced file into place.

Covers Rust hits, Rust misses (which rewrite rustc's output through the same function)
and C/C++ hits. Measure with the restore log line on a clean `cargo build` before and
after.

## 6. Step 2: place a miss by rename

On a miss, each staged output file other than the `.d` is moved into the output
directory with `rename`. On `EXDEV` (the scratch dir fell back to `$TMPDIR`, which is
tmpfs on the measured host, or the output directory is on another filesystem) the
fallback is `CloneFile` followed by the umask-aware execute-bit fix from
`MaterializeOutputs`. Try the rename and handle the error; do not infer same-filesystem
from the scratch dir's location. Create the parent directory first, as `WriteFileAtomic`
does today, and keep the `IsSafeOutputName` check over the whole set before placing
anything.

Two fixes to `util` land with this step because the fallback introduces a new
`CloneFile` call site: `CloneFile` takes a `mkstemp`-style exclusive temp name beside
its destination, and the capture loop uses `lstat` rather than `is_regular_file`, which
follows symlinks. Today a symlink in the stage dir is read through; renamed, it would
become a symlink in the output directory that dangles once the stage dir is removed.
Capture rejects symlinks and duplicate names, and a rejection places nothing and falls
through to passthrough as `kCaptureFailed` does today.

The `.d` file still goes through memory: `CaptureOutputs` canonicalises it and replaces
the stage directory with `/vcache-outdir`, and `RestoreOutputs` localises it and
substitutes the real output directory. Those two rewrites are the invariant; the staged
`.d` must never be renamed into place as is. The other files are no longer read into
memory on the way to the output directory; `CaptureOutputs` still reads them once to
build the entry, so the store path and its memory use are unchanged until step 3.

Placement happens before rustc's stderr is forwarded, for the reason recorded in
`RunRustCompile`: cargo starts dependants the moment it sees the artifact message, and
it opens the path it passed as `--out-dir`, not the stage path in the message. A
same-filesystem rename keeps rustc's own mode bits, where today's rewrite applies
`0666 & ~umask` plus execute; that is a visible change and the right one.

## 7. Step 3: sidecar files for large outputs

Modelled on `RunLink` and `MaterializeOutputs`, with the fixes the review found in
that model (section 10).

**Storage API.** Sidecars are written and read through `DiskStorage`, not by building
paths in the Rust code. `PutFile(digest, suffix, source_path)` clones into
`<shard>/<digest>.<suffix>` and then runs the same shard-growth check as `Put`, minus
the scratch sweep (below). If the file already exists it still clones and renames over
it, which is cheap and replaces a sidecar torn by a crash (sidecars are never fsynced),
but it skips the growth check, because the shard did not grow: the check subtracts the
object's own size from the shard total, so a rewrite of a sidecar at or above the
per-shard budget would otherwise trip a global trim every time. `GetFile(digest,
suffix)` returns the path and refreshes its mtime; every `utimensat` in the API passes
`AT_FDCWD` (today's `utimensat(0, ...)` in `Get` would resolve a relative cache dir
against stdin). A refresh that fails is logged and ignored, as on a physically
read-only store. A hit whose clone fails verification unlinks the sidecar, so a torn
file cannot be kept fresh by failed hits until the next store replaces it. Both
honour `read_only`, and `PutFile` distinguishes a missing source from a storage error
so the caller can feed `error_on_cache_media_failure`. Sidecars are not fsynced; a
crash can lose one, and that is a verified miss. Link's `LinkOutputPath` moves onto the
same API with suffix `linkout` in the same change or straight after.

The growth check in `DiskStorage::Put` calls `RemoveStaleScratchDirs` before
`TrimGlobal` whenever a shard crosses a multiple of its budget, and a sidecar above
160 MiB (the per-shard budget at 40 GiB) always crosses. Today that call comes after
the outputs are placed. In step 3 the sidecar is stored before placement, so `PutFile`
must not run the sweep: a compile over six hours would otherwise delete its own stage
dir between storing the sidecar and placing the output. The sweep stays in `Put`, which
still comes last.

**Store, in this order, after rustc exits and before anything is forwarded to cargo.**
1. Enumerate the staged files with `lstat`, record each name and executable bit, reject
   duplicate or unsafe names and symlinks.
2. Scan everything for the leaked local values of the path-valued env deps
   (`rust_path_env_vars`, which is what `FindEnvPathInOutput` checks) before anything
   is written to the store. Each file at or above the threshold is hashed and searched
   in one streaming pass with an overlap of the pattern length across chunks,
   binary-safe, failing closed on a short read. Inline files and the canonicalised stderr are searched in memory as today.
3. Clone each large file into the store with `PutFile` while it is still private.
4. Place every output as step 2 does, rewrite the `.d`, then forward stderr.
5. `Put` the entry, which names each large file by digest in a new section kind
   (name, executable flag, digest). The daemon completes the single-flight lease as soon
   as `Put` succeeds, so every sidecar must exist before `Put`, not merely before
   `ReleaseLease`.

A rejection at step 1 (unsafe or duplicate name, symlink) keeps today's behaviour:
`kCaptureFailed` and passthrough, which compiles again and publishes nothing from the
stage dir. A leak found at step 2, or a failed clone at step 3, means no entry is
stored for this compile, but the outputs are still placed and stderr is still
forwarded, so the build succeeds without caching. Today's leak check runs after
placement and returns early; moving it earlier means its early return must become
"skip the store", never "skip placement". A failed sidecar after another sidecar was already cloned leaves an orphan
that ages out. On a failed sidecar the existing early return from the compile session
releases the lease as failed and waiters compile.

Small outputs and the `.d` stay inline. The `.d` is excluded by name, never by size.

**Hit, in this order, before anything is forwarded to cargo.**
1. For each digest section, `GetFile` (which refreshes the sidecar's mtime first, so a
   trim that starts during the hash sees a fresh file), create the parent directory,
   and `CloneFile` to a private exclusive temp name in the output directory. A missing
   sidecar fails here.
2. Hash the clone, not the source. The reflink shares the source's extents at the
   moment of cloning, so this verifies exactly the bytes that will be published and
   closes the verify-then-clone window that `MaterializeOutputs` has today.
3. Only when every file has verified: rename each temp into place, apply the
   umask-aware execute-bit fix, write the inline files with `durable = false`, rewrite
   the `.d`, then forward stderr.

Any failure before the first rename means the hit is unusable: remove the temps,
forward nothing, `continue` to the next manifest state as the Rust loop does today,
and recompile if none serves. A failure after the first rename skips both the
remaining manifest states and the direct-key lookup that follows the loop: another
entry might not write every name this one already placed, so the compile runs and
overwrites the whole set. A partial file set is never reported as success.

The reader fails closed on its own format. A digest section whose sidecar cannot be
restored is a failed hit, and `ServeHit`'s `blob.files.empty()` guard changes to "no
output section of either kind": an entry whose outputs are all sidecars, with no
dep-info (`--emit=link` without `dep-info` is legal), has an empty inline list and must
still hit. The key bumps below keep old readers away from these entries; they do not
and cannot make an old reader fail closed.

**Why the hash stays.** The blob checksum covers the entry body. Once the large bytes
are outside it, the entry's checksum covers only the digest, and the sidecar's name and
mtime prove nothing about its contents. A wrong rlib is worse than a miss, as a wrong
binary is for link. The hash is a full read of the file, from the page cache when warm.

**Key versions.** Two constants bump together in `src/rust/rust_compile.cc`, in the
same commit as the new section reader: `kCacheKeyVersion` from `vcache-rust-key-v3` to
`v4` and `kManifestKeyVersion` from `vcache-rust-manifest-v1` to `v2`. The second is
what makes the first sufficient. The usual hit does not compute the entry key:
`RunRustCompile` loads the dep-info manifest under the manifest key and calls
`cache->Get(states[i].key)` on the key stored there, and `FindRustStateMismatch` checks
sources, externs and env, never the key's version. An old binary sharing a store with a
new one, or a rollback to `vcache.prev`, would follow a manifest state into a v4 entry.
`DeserializeBlob` skips an unknown section kind by design, the inline `.d` keeps
`blob.files` non-empty, `ServeHit` writes the inline files, forwards stderr and returns
success, and cargo pipelines dependants onto whatever rlib was already in the
directory. That is a silent wrong hit, the worst failure this system has, and it costs
one constant to prevent. A writer that bumped only the entry key would still store v4
keys in a v1 manifest and leave the hole open. The blob magic `VCACHE02` and
`kRustManifestHeader` stay: with both Rust namespaces isolated no old reader sees a new
entry, and a magic bump would invalidate every C/C++ and link entry for nothing. The
C/C++ `vcache-key-v2` is a different constant and is untouched.

The entry key also hashes whether sidecars are in use, so an inline entry and a sidecar
entry never share a key. Without that, a disk-only `recache` could overwrite an inline
entry that an S3-enabled daemon still has queued for upload, and the upload worker,
which re-reads the current disk value, would ship a sidecar entry to the remote.

**Threshold.** 8 MiB covers 66% of the bytes in the measured store with 1,085 files,
9% of the output files. 1 MiB would cover 98% with 4,589 files, and the trimmer stats
every file on every walk. Start at 8 MiB as one constant, labelled provisional.

**Where sidecars apply.** Only when `config.disk.enabled` is true and `config.s3.enabled`
is false. The test is on the config, not on the process's `CacheChain`: with the daemon
the chain holds only the remote client and S3 lives in the daemon's `HandlePut`, so a
chain-based test would store sidecar entries that the daemon uploads. With S3
configured, or without a disk layer, every file stays inline as today, so every entry
that can reach a remote layer is self-contained and no upload filter is needed. Link's
`kLinkWithoutDisk` is not the model here: it declines to cache at all without a disk
layer, and it does not keep link entries off S3 (`RunLink` puts through the ordinary
chain and the daemon uploads every stored entry). A sidecar entry that nonetheless
reached another host would fail hit step 1 and recompile, and because both
`DiskStorage::Get` and the daemon refresh the entry's mtime before the client validates
it, every failed attempt would make the dangling entry most recently used. Keeping
sidecars off S3 deployments avoids both. No sibling repo uses S3 today. `read_only`
creates no sidecar and still reads them. `recache` skips the hit, re-puts the sidecar
(a no-op mtime refresh when it exists) and overwrites the entry.

**Daemon mode.** No protocol change: the entry crosses the socket as an opaque value,
the client reaches the disk directory directly for sidecars through `DiskStorage` on
`config.disk.dir`, as the link path already does for `.linkout`. An old daemon left
running across a binary replacement stores and serves the new entries unchanged.

**Several processes.** Single flight leases the entry key; different keys can produce
the same digest, `recache` takes no lease, and the daemon can be off. None of that needs
a lock: a sidecar is published by renaming a finished, verified temp onto a
content-addressed name, so two writers of one digest publish identical bytes and the
last rename wins harmlessly. A published sidecar is never modified in place.

**Link.** `MaterializeOutputs` moves to `GetFile`, which gives `.linkout` the mtime
refresh before its hash. Whether link also adopts clone-then-verify is a separate
decision; this plan specifies it for the Rust hit only.

## 8. What step 3 gains

- A hit on a large output becomes a hash and a clone instead of a read, a copy, a
  write and an fsync. A miss stops reading its large outputs into memory at all.
- Peak memory on a hit no longer includes the large outputs. Today an in-process hit
  holds about twice the entry size, up to about 1.4 GiB for the largest entry, and a
  daemon hit up to three copies.
- Every target directory and the store share the physical blocks of an output restored
  from the cache. That shrinks real disk use on a host with many worktrees per repo. It
  does not raise the store's capacity: the trimmer budgets logical `st_size`, so the hit
  rate on large outputs stays bounded by the 40 GiB setting until that is raised.

## 9. Known limits of step 3

1. **Eviction is per file.** A sidecar is evicted by its own mtime, independently of its
   entry, and two entries can share one sidecar with no refcount. The mtime refresh is
   a hint, not a pin: `TrimGlobal` sorts a snapshot and does not re-stat before removing,
   so a sidecar touched during a running trim can still go. A sidecar larger than the
   trim target can be removed by the very `Put` that publishes its entry. The
   pending-upload pin list matches entry keys, never sidecar names. Every one of these
   is a failed verification and a recompile, never a wrong output. A missing sidecar
   costs one failed attempt per manifest state that names it plus the direct-key
   lookup before the recompile starts. The reverse case, an orphan sidecar whose entry
   was evicted, uses budget until it is among the oldest files over the limit.
2. **Size accounting.** The trimmer sums `st_size`. A cloned file counts in full in the
   store and in each target directory although the blocks are shared, so the budget
   stays conservative and `du` disagrees with real disk use. `st_blocks` would not help:
   XFS reports shared extents in full for every inode that references them. The shard
   growth check is also racy between concurrent writers, as it is for entries today: two
   stores that cross the same budget multiple at once can both skip the trim, and the
   next crossing catches it.
3. **Reflink is a property of the filesystem.** A store on ext4, or a target directory on
   another filesystem than the store, gets `copy_file_range`, which may or may not share
   extents, or a streamed 1 MiB-buffer copy if that fails. The bounded memory remains;
   the block sharing may not.
4. **Step 3 invalidates the Rust entries once.** Every store misses once after the key
   bumps, per distinct key per store. The measured store turns over in days. Steps 1
   and 2 change no format. Rollback to an old binary is safe for the same reason: it
   computes v3 keys and a v1 manifest key, misses, and recompiles; orphan sidecars live
   in the shards and age out under the normal trim. After a rollback the v4 entries are
   the newest inodes, so the trim evicts still-valid v3 entries before them.
5. **The scratch sweep is by age.** `RemoveStaleScratchDirs` removes scratch dirs whose
   top-level mtime is older than six hours, and a long rustc does not update that mtime.
   A compile over six hours can lose its stage dir under another process's trim. That
   exists today, and while rustc runs the stage file is the only copy in every variant.
   Step 3 adds a window after rustc exits: the stage file stays the only complete copy
   of a large output until the sidecar is stored, where today and in step 2 the bytes
   are also in memory by then. A liveness mark for scratch dirs is a separate fix.
6. **Not a whole-set atomic restore.** Per-file rename is atomic; the set is not. The hit
   order above guarantees cargo is told nothing until the whole set is in place, and a
   failed hit may leave temps or a partial set that the recompile overwrites.
7. **Input races are untouched.** Sources and externs are hashed before rustc runs, with
   no post-compile check; a source edited during the compile is keyed by its old digest.
   Sidecar digests prove the payload is intact, not that it belongs to the input state.

## 10. Review log

Reviewers, both rounds: `grok-4.7[context=500k,reasoning_effort=xhigh,fast=false]`
(cursor-agent), `gpt-6-astra` at `model_reasoning_effort=xhigh` (codex), `kimi-k3-max`
(cursor-agent). Every finding below was checked against the code before it changed the
document.

### Round 1, 2026-10-10, against version 1 and `fbg` at `33a1053`

**Consensus (all three), adopted.** The `kCacheKeyVersion` bump alone does not isolate
old binaries: manifest states carry entry keys under `kManifestKeyVersion`, and an old
reader restores a partial file set as a success; bump both (section 7, "Key versions").
"Not put to S3" was not link behaviour: `RunLink` puts through the chain and the daemon
uploads everything; replaced by "sidecars only without S3" (section 7, "Where sidecars
apply"). Sidecars must exist before `Put`, because `HandlePut` completes the lease.
Execute bits: `CloneFile` drops them and `RestoreOutputs` adds them unconditionally;
the new paths use `MaterializeOutputs`' umask-aware fix (sections 2, 6, 7). Refresh
sidecar and `.linkout` mtime on a hit, and say why it is load-bearing. `EXDEV` fallback
by trying the rename, not by inferring the filesystem. Streaming leak scan with chunk
overlap, binary-safe, fail closed, over the bytes that will be stored. `.d` excluded by
name. The hit must verify every sidecar before placing anything or forwarding stderr.
The threshold table counted entries, not files; remeasured per file (section 4). The
reflink row is not a hit; the hash cost belongs beside it. Hit evidence by mtime is a
floor, not a count. The six-hour scratch sweep and the pin list's blindness to sidecars
are recorded as limits.

**Astra only, adopted.** `DiskStorage::Put` estimates shard growth by subtracting the
entry's own size, so a hand-cloned sidecar never triggers a trim; sidecars go through a
`DiskStorage` file API that runs the same check (section 7, "Storage API"). `CloneFile`'s
temp name is not exclusive and can collide across PID namespaces; use a `mkstemp`-style
name. Verify the clone, not the source. Steps 2 and 3 need an explicit order because a
renamed staged file cannot be cloned afterwards. Step 1's "as durable as a miss already
is" was wrong for current vcache, which fsyncs misses too; rewritten. Symlinks and
duplicate names are rejected at capture. The input race between hashing and compiling
is recorded as a limit.

**Grok only, adopted.** The link manifest loop `break`s on a failed materialise where the
Rust loop `continue`s; the Rust behaviour is kept. Block sharing does not raise the
store's logical capacity; the gains section now says so. A sidecar larger than the trim
target can be removed by the `Put` that publishes its entry. `recache` takes no lease.
Step 3's "already on the cache filesystem" sentence dropped the `$TMPDIR` fallback.

**Kimi only, adopted.** `CaptureOutputs` stores a `.d` raw when it fails to parse, with
stage paths inside; listed under pre-existing issues below. Orphan sidecars and shared
digests with no refcount recorded as a limit. The restore log line before measuring.

**Rejected or deferred.** Bump the blob magic to `VCACHE03` as well (Grok): with both
Rust key namespaces isolated no old reader can reach a new entry, and a magic bump
invalidates every C/C++ and link entry in every store. Inline fallback when a sidecar
clone fails (Kimi, as an option): skip the store instead, as link does; simpler and the
compile still succeeds. A liveness mechanism for scratch dirs (Astra): pre-existing,
tracked separately. Fix `CaptureOutputs`' canonicalise-before-substitute order for a
cache inside a mapped root (Astra): pre-existing and not the case on any host here;
tracked separately. Measure the full hit path rather than `cp --reflink` (Astra): agreed
and recorded as unmeasured; the table is kept as a bound with that caveat.

### Round 2, 2026-10-10, against version 2

**Consensus (all three), adopted.** The `CloneFile` temp-name fix and the symlink
rejection belong to step 2, which introduces the new `CloneFile` call site (section 6).
A refusal or failed sidecar before placement must still place the outputs and forward
stderr; today's leak check returns early only because placement has already happened
(section 7, store). Step 3 cannot ship or stay without step 2 (section 1).

**Grok only, adopted.** `DiskStorage::Put` runs the scratch sweep before `TrimGlobal`
on every budget crossing, and a sidecar over 160 MiB always crosses, so a `PutFile`
before placement could delete the compile's own stage dir; `PutFile` runs no sweep
(section 7, "Storage API"). The key bumps keep old readers away but do not make the new
reader fail closed: a digest section whose sidecar is missing must fail the hit, and the
`blob.files.empty()` guard must admit an all-sidecar entry with no dep-info (section 7,
hit). "No S3" is tested on `config.s3.enabled`, not on the chain, because with the daemon
the chain has no S3 layer (section 7, "Where sidecars apply"). `copy_file_range` may
still share extents (section 9.3). A failure after the first rename recompiles rather
than trying the next manifest state (section 7, hit). The hit creates parent directories
before cloning, since `CloneFile` does not. `.linkout` refresh before its hash. A
dangling entry is kept fresh by the mtime refresh in `Get`, not by backfill. After a
rollback the trim evicts valid v3 entries before the v4 ones (section 9.4). The writer
must bump both constants in the same binary as the new reader.

**Astra only, adopted.** `WriteFileAtomic` ignores `close`, which is the only place a
delayed write error can surface without fsync; step 1 checks it (section 5). An inline
entry queued for upload could be overwritten under the same key by a disk-only
`recache` and the upload worker would ship the sidecar entry; the entry key now hashes
the sidecar mode (section 7, "Key versions"). `PutFile` of an existing sidecar would
trip a global trim every time; it refreshes the mtime instead (section 7, "Storage API").
`utimensat(0, ...)` resolves a relative cache dir against stdin; use `AT_FDCWD`. The file
API takes a suffix so link's `.linkout` lookup survives the migration. The old-reader
fails-first test cannot pass by constant bumps alone; reworded to run the old binary
against a store the new one populated. "At least three copies" softened to "up to three".
`PutFile` distinguishes a missing source from a media error; sidecars are not fsynced
and a lost one is a verified miss.

**Kimi only, adopted.** The leak scan must finish before any sidecar is published, or
a leak in an inline file refuses the entry after sidecars are already in the store
(section 7, store step 2). Step 3, not step 2, is where the stage file becomes the only
copy (section 9.5). Clone-then-verify is specified for Rust only; link is a separate
decision (section 7, "Link"). A missing sidecar costs one failed attempt per manifest
state plus the direct lookup (section 9.1).

**Rejected or deferred.** None in round 2.

### Round 3, 2026-10-10, against version 3, scoped to the round-2 changes

Grok ran as `grok-4.7-xhigh` (256k context): cursor-agent rejected the parameterized
id on every attempt this round. Astra and Kimi reported nothing beyond wording.

**Grok only, adopted.** A `PutFile` that only refreshed an existing file would never
replace a sidecar torn by a crash, and `GetFile`'s refresh before the hash would keep
it newest forever: `PutFile` now clones over an existing file, and a failed
verification unlinks the sidecar (section 7, "Storage API"). The false budget crossing
on rewrite applies only to objects at or above the per-shard budget; reworded. The
`AT_FDCWD` rule covers every refresh in the API. A failure after the first rename must
also skip the direct-key lookup after the manifest loop, since `try_entry_hit` can serve
another entry and return 0 (section 7, hit).

**Astra only, adopted.** The "refusal still places" rule is scoped to the leak check and
sidecar failures; capture-validation failures keep today's passthrough (section 7).

**Kimi only, adopted.** Wording: the leak scan looks for the path-valued env deps, not
any local path; the scratch-sweep window; `CloneFile`'s temp is `O_TRUNC`.

**Pre-existing issues the review found, outside this plan.** `RestoreOutputs` adds
execute for owner, group and other regardless of umask. `CaptureOutputs` stores an
unparseable `.d` raw and follows symlinks. `RemoveStaleScratchDirs` keys on directory
mtime, so a compile over six hours can lose its scratch, and `Put` runs it on every
budget crossing. `.linkout` files are never refreshed on a hit, and `MaterializeOutputs`
hashes the source and then clones it. `CloneFile`'s temp name is not exclusive.
`WriteFileAtomic` ignores `close`. `DiskStorage::Get` passes `0` as `utimensat`'s dirfd.
AGENTS.md says the daemon is off; the devtools env script turns it on by default. Each
is a candidate bead.

**Fails-first tests** (AGENTS.md requires each to fail on the pre-change binary or on a
named mutation). Step 1: a restored output is written without fsync (mutation: strace or
a `durable` spy). Step 2: the `.d` is localised after a renamed miss; `EXDEV` placement
from a tmpfs scratch dir; a symlink in the stage dir is refused; a binary is executable
after an `EXDEV` fallback under a strict umask. Step 3: the previous `dist` binary run
against a store the new one populated gets no hit and no wrong output (through the real
manifest lookup); a hit with one sidecar missing forwards no stderr and recompiles; an
entry whose outputs are all sidecars and has no dep-info hits; a leaked `OUT_DIR` path
inside a file above the threshold, straddling a chunk boundary, is refused and no
sidecar is stored; a refused compile still places its outputs; a sidecar clone into
another shard triggers the growth check and no scratch sweep; `read_only` writes no
sidecar; a sidecar's mtime is newer after a hit; the unknown-section skip in
`DeserializeBlob` still round-trips for kinds the reader does not know.

## 11. Not in scope

C and C++ compile outputs are usually small, and on the measured host they go through
ccache, not vcache. The same mechanism would cover them, but there is no data here
showing it is worth it.
