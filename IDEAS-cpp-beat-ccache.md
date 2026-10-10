# Ideas: make vcache's C++ path beat ccache

Status: proposal, not planned. Version 2, written 2026-10-10 against `fbg` at `1c208a3`.
Version 1 (`5d36096`) rested on one comparison; this version re-checks its causes against
the compilers, ccache and the code, adds three measurements (sections 2.2-2.4), records the
small wins that landed (section 3) and reorders the larger work (section 5). The owner
decides what to build.

## 1. Summary

On sunbird's el10 build (434 compiles, 222 links, clang 22, a precompiled header), a
worktree building what another worktree already built takes 5.5-6.3 s with ccache and
19.5 s with vcache. Version 1 named three causes. Two of them stand, with a different
explanation; the third was a misreading of ccache.

1. **vcache cannot preprocess 164 of the 434 compiles.** The PCH consumers lack one
   `-isystem` directory that the PCH producers have (`nlohmann/json.hpp` lives there), so
   `-E` fails. That is a sunbird build defect, not a cache property: with the directory
   added to the consumers, vcache's warm build falls to 10-12 s (section 2.2). ccache is
   unaffected only because sunbird runs it in `depend_mode`, which never preprocesses.
2. **A vcache hit costs 80-100 ms against ccache's 7-12 ms.** vcache runs the preprocessor
   on every lookup; ccache in depend mode hashes the source and a manifest of includes.
   For a small PCH consumer a vcache hit (98 ms) costs almost as much as compiling it
   (120 ms).
3. **The 8 PCH builds are not cached** and sit on the critical path: 1.7-2.1 s each under
   vcache against 0.25-0.6 s as ccache hits, so vcache's last PCH finishes at 6.8 s where
   ccache's finishes at 3.1 s. This is about 3.7 s of the remaining 5 s gap.

Version 1's proposal 4.1 (key a PCH consumer on the PCH's input digest plus the consumer's
text preprocessed *without* the PCH) is unsound and is withdrawn: the PCH's macros decide
which `#ifdef` branches the consumer's text expands to, so two sources that differ only
inside such branches would share a key. Clang offers no way to preprocess with a PCH
loaded (section 2.5). The route to the same result is a manifest mode (section 5.2).

## 2. Measurements

All el10 runs: `ninja all all_tests` at `-j26` in a 30-CPU, 64 GiB cgroup slice, each
configuration from an empty, separate cache; vcache reached through a compiler masquerade
with ccache off, so links also passed through it. Every worktree sees the source at the
same container path.

### 2.1 Version 1 comparison (fbg `075bc68`, vcache 1.3.0 fork)

| Case | ccache | vcache, daemon off | vcache, single-flight |
| --- | --- | --- | --- |
| 1. Cold, one worktree | 44.9 s (433 misses) | 53.1 s | 44.5 s (262 misses, 164 uncached) |
| 2. Warm: cache filled by another worktree, clean build | **5.5 / 6.3 s** (434 hits) | 17.8 s (262 hits, 164 recompiled) | 19.5 / 19.6 s |
| 3. Cold, two worktrees at once | 61.0 / 61.8 s (828 misses) | - | **49.6 / 49.6 s** (262 shared) |
| 4a. No-op rebuild, warm tree | 0.46 s | - | 1.47 s |
| 4b. One-file edit (1 compile + 216 relinks) | 5.2 s | - | 6.2 s |

- Whole-build CPU in case 2: 36 s with ccache, 278 s with vcache.
- Correctness: no wrong hits; vcache-built tests pass; 377 of 400 compared objects have
  identical `.text`, the other 23 differ only in `__FILE__` string lengths (ccache's
  `base_dir` makes paths relative; vcache keeps the absolute canonical path, as an uncached
  build does).

### 2.2 Case 2 with the consumers' include path fixed (sunbird `d8fc4c3e5`, fbg `a841c30`)

`-DCMAKE_CXX_FLAGS=-isystem/opt/infra/3rdparty/release.clang22/include` on all four
worktrees, ccache and vcache alike. vcache then preprocesses every consumer: fill build
440 misses, 0 preprocess failures; uncacheable only the 222 links, the 8 PCH builds and 17
non-compile calls that reach the masquerade.

| Config | Wall, two runs | Children CPU user / sys | Stats |
| --- | --- | --- | --- |
| ccache | 7.8 / 5.3 s (repeats 6.5, 6.8) | 27.8 / 11.6 s | 448 direct hits |
| vcache, daemon off | 12.2 / 11.5 s (repeats 12.5, 11.6) | 80.4 / 21.5 s | 440 hits, 0 misses |
| vcache, single-flight | 10.1 / 10.3 s | 78.4 / 20.4 s | 440 hits, 0 misses |

From `.ninja_log` of the last warm run of each:

| Edges | vcache | ccache |
| --- | --- | --- |
| 8 PCH builds, each | 1.7-2.1 s | 0.25-0.59 s |
| last PCH ends at | 6.8 s | 3.1 s |
| 440 object edges, sum / median | 165.6 s / 369 ms | 49.8 s / 119 ms |
| other 777 edges, sum | ~84 s | ~84 s |

Per hit, timed alone in the container (5 runs, min):

| File | ccache hit | vcache hit | plain compile |
| --- | --- | --- | --- |
| PCH consumer, small (`VdsoClockStartupGate.cc`) | 12 ms | 98 ms | 118-144 ms |
| no PCH (`protocol/validate_pod.x.cc`) | 7 ms | 79 ms | 511-522 ms |

Case 4a is explained and is not a cache effect: a no-op runs no compiler. The time was
`buildinfo gather` running `git status` against a worktree index that the container's
read-only git mount cannot rewrite; one host-side `git status` settles it to 0.1 s for
both tools.

### 2.3 Where a vcache C++ hit spends its time (host, clang 22, `src/daemon/server.cc`, 3.4 MB preprocessed)

Before section 3's changes: 4 ms startup, 70 ms preprocessor child, 10 ms reading and
hashing the output (blake3 fed one line at a time), 2 ms blob read, 4 ms object write
(fsync and the rename it slows), under 1 ms stats. Total 89-92 ms. Only the 14 ms outside
the compiler is vcache's to cut without a design change.

### 2.4 Where a vcache Rust hit spends its time (host, sunbird `feedreplay` lib, warm store)

Total 52-71 ms traced, against a 1.5 s compile. Of it, hashing the 12 `--extern` files in
full on every lookup is 37-46 ms (251 MB; one 232 MB `.rmeta` is 33 ms alone), the fsync'd
restore of three outputs 6.5-21 ms, manifest verification of the 12 recorded sources under
1.5 ms, blob fetch 1-3 ms. Across the warm workspace build 350 rustc calls hash 3.5 GB of
externs. The whole warm Rust build: 4.0 s, 352 hits, 5 misses (build-script probes that
fail by design).

### 2.5 Facts checked against the compilers and ccache (gcc 16, clang 22, ccache 4.12)

- gcc `-E` on a PCH consumer whose include path misses a header fails. gcc
  `-E -fpch-preprocess` loads the `.gch`, applies its macros and emits one
  `#pragma GCC pch_preprocess` line in place of the header text.
- clang `-E` substitutes the header's text in every spelling (`-include` with a `.pch`
  beside it, `-include-pch`, `-Xclang -include-pch`); the PCH is never loaded for `-E`.
- ccache in its default mode fails on the same clang consumer ("Preprocessor gave exit
  status 1", uncacheable). With `depend_mode = true` and `-MD` it hits; without `-MD` it is
  uncacheable. sunbird's `cmake/ccache.conf` sets `depend_mode = true`,
  `pch_external_checksum = true` and `sloppiness = pch_defines,time_macros`.
- clang's `-MD` output for a PCH consumer lists the PCH's headers but not the `.pch`; gcc's
  lists neither the `.gch` nor its header. A manifest must add the PCH from the command line.
- The ccache manual: direct mode "can't check with 100% accuracy if the existence of a new
  header file should invalidate the result". It does not handle shadowing.
- A clang PCH built with `-fno-pch-timestamp` (sunbird sets it) restored into another tree
  with touched headers is accepted. gcc `.gch` bytes differ between identical builds.
- A `.gch` built once and used by a consumer compiles fine without the header's include
  paths; so does clang with `-include-pch` alone. CMake's form adds a textual `-include` of
  the header after `-include-pch`; sunbird's consumers compile with it, while a test header
  without an include guard was re-read and failed, so the guard state the PCH carries
  decides it.

## 3. Small wins landed on `fbg` (section 2.3 and 2.4 costs)

1. `cd00cc8` perf(core): the preprocessed text is hashed in 256 KiB batches with
   linemarkers found by a line-leading `#` search. Hashing 3.4 MB: 8-9 ms to 1.4-1.6 ms.
   Same bytes, same key.
2. `2b407ef` perf: restored build outputs (object, depfile, Rust outputs, dep-scan output)
   are written without fsync; cache entries and memos keep it. About 4 ms per C++ hit, 6 ms
   per Rust hit.
   Hit, min of 60 runs: clang++ 83 → 70 ms, g++ 97 → 86 ms.
3. `6777382` perf(rust): digests of `--extern` files memoised by device, inode, size,
   mtime and ctime under `<cache_dir>/filehash/`, swept by `--trim` after 30 days. The
   feedreplay hit: about 45 ms to about 9 ms of vcache time.
4. `403b27d` fix(core): a `-MD` compile that hits an entry stored without `-MD` now
   recompiles and stores the depfile; before, it wrote no depfile and counted as a hit.

## 4. What stays as it is

- The key stays content-based and path-independent through roots; nothing here weakens it.
- Single-flight, admission and the jobserver are unchanged; they apply to whatever becomes
  cacheable.
- The preprocessor-mode key stays the result key. A manifest mode is a fast path to the
  same entries, so a tree in one mode hits entries another tree stored in the other.

## 5. Proposals, in the order to build them

### 5.0 sunbird: give the PCH consumers the PCH's include path (no vcache code)

The 164 failing consumers lack `-isystem /opt/infra/3rdparty/release.clang22/include`,
which every PCH producer has. Adding it to the consumer targets halves vcache's warm time
(section 2.2) and changes nothing for ccache. It is also the correct build: a target that
consumes a PCH should see what the PCH saw. Owner's call in the sunbird repo.

### 5.1 Cache the PCH builds (was 4.2; now the largest gain)

**What.** Accept `-x c++-header` / `-x c-header` compiles whose output is `.pch` or `.gch`
(and clang's `-emit-pch`), keyed like any compile from the preprocessed header.

**How.**
- clang: cache only when `-fno-pch-timestamp` is on (sunbird sets it); otherwise the
  restored PCH would be rejected in a tree with different header mtimes. gcc: `.gch` bytes
  are not reproducible but need not be; store what was built. PCHs are compiler-version
  specific; the compiler identity in the key covers it.
- Store the PCH bytes as the entry and restore through the reflink path of
  `IDEA-reflink-compile-outputs.md` (tens of MB per PCH).
- Write `<pch>.vcache-key` beside the output: the PCH's own cache key (compiler, flags,
  preprocessed header text). 5.2 reads it where a build writes no `.sum`.
- The integration test at `tests/integration_test.sh:3545` asserts "clang PCH generation
  is declined" and flips.

**Gain.** About 3.7 s of the 5 s gap in case 2 (last PCH at 3.1 s instead of 6.8 s), and
a shorter critical path in cases 1 and 3.

**Tests.** A PCH built in worktree A is restored in B and accepted by clang; a PCH built
with timestamps on is declined; `.vcache-key` changes when a header the PCH includes
changes or when the PCH's `-D` set changes.

### 5.2 Manifest mode for C and C++ (was 4.3; ccache's depend mode, done soundly)

**What.** Before preprocessing, look up a manifest keyed on the source digest, the
canonical flags, the compiler identity, roots, and the digest of every keyed file (the
PCH among them). Each manifest state lists the files an earlier run read, with digests; a
state whose files all still match names the result key, and the preprocessor never runs.
This is the shape `RunDepScan` already has (`DepManifestEntry`, `kMaxManifestStates`,
merged concurrent writes), and the Rust dep-info manifest.

**How.**
- On a miss, record the include set from the real compile. Two sources, to be chosen by
  measurement: the compile's own `-MD` output (ccache's way; needs `-MD`, which CMake's
  Ninja generator always passes), or `vcache-fstrace.so`, which the link cache already uses
  and which also records paths probed and absent (`M` lines), making the manifest sound
  against a new header shadowing an old one. ccache cannot do that; it documents the hole.
- The result key is today's preprocessor key when `-E` succeeds, so entries are shared
  with preprocessor-mode lookups. When `-E` fails (the PCH consumers before 5.0), derive
  the result key from the manifest state's digests, as ccache's depend mode does.
- The PCH enters the manifest from the command line, since the depfiles do not name it:
  `<pch>.sum` if the build writes one (sunbird does, for `pch_external_checksum`; refuse a
  `.sum` older than its PCH), else `<pch>.vcache-key` from 5.1, else the PCH bytes through
  the file-identity memo of section 3.3. The PCH's own headers can be left out of the
  consumer's manifest when the PCH's depfile names them, since the `.sum` covers them.
- `__DATE__`, `__TIME__` and `__TIMESTAMP__` in a source or header make a manifest hit
  wrong (preprocessor mode is immune because the expansion carries the value). Scan the
  bytes being hashed and decline manifest mode for that TU when a token appears.
- Manifest verification re-hashes every recorded file on every lookup; for a PCH consumer
  that is hundreds of headers. Use the file-identity memo of section 3.3 for them.
- Bump `kManifestKeyVersion` or add a new version constant for the new manifest kind.

**Gain.** A hit drops from 80-100 ms toward ccache's 7-12 ms; in case 2 that is most of
the remaining gap after 5.1 (object edges 166 s against 50 s). It also makes the 164
consumers cacheable without 5.0, since no `-E` is needed on a hit.

**Tests.** A manifest hit skips the preprocessor (log line and a stats counter); editing an
included header misses; editing a header the PCH includes and rebuilding the PCH misses;
adding a shadowing header earlier on the include path misses (tracer variant); a source
with `__TIME__` never takes the manifest path; two writers merge their manifests; a tree
in preprocessor mode hits an entry a manifest-mode tree stored.

### 5.3 Document the launcher form for CMake (was 4.4; unchanged)

Recommend `CMAKE_<LANG>_COMPILER_LAUNCHER=vcache` over the masquerade; the launcher wraps
compiles only, so the 222 links never reach vcache. sunbird already uses a launcher for
its `.sum` writer. Docs and an integration check only.

### 5.4 gcc projects: `-fpch-preprocess` in preprocessor mode (optional)

For gcc, vcache could add `-fpch-preprocess` to its own `-E` command when a `.gch` is in
play, drop the pragma line and add the `.gch` digest (or `.sum`) to the key. That makes
gcc PCH consumers cacheable without their headers' include paths and cheaper to look up,
with no manifest. Clang has no equivalent, and sunbird builds with clang, so this only
matters for a gcc project.

### 5.5 Dropped

- 4.1 (re-expand the consumer without the PCH): unsound, see section 1.
- 4.5 (single-flight in front of ccache): only worth it if 5.1-5.2 are not built.

## 6. Order and how to judge it

5.0 first, because it is free and shows the ceiling. Then 5.1, then 5.2, re-running cases
1-4 of section 2 after each step with the section 2.2 setup. 5.3 any time.

vcache is worth switching to when all of the following hold:
- case 2 is within 20% of ccache (with the section 2.2 flag applied to both);
- cases 4a and 4b are within 0.5 s of ccache, measured after a host-side `git status`;
- case 3 keeps its single-flight win;
- there are no wrong hits, and the vcache-built tests pass.
