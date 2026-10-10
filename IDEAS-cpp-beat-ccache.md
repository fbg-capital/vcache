# Ideas: make vcache's C++ path beat ccache

Status: proposal, not planned. Written 2026-10-10 against `fbg` at `075bc68`, from one
measured comparison (section 2). The owner decides what to build.

## 1. Summary

On a ~430-TU C++ project that uses a precompiled header, ccache beats vcache in the case
that matters most: a worktree building what another worktree already built. ccache takes
5.5-6.3 s and vcache 19.5 s. vcache wins only when several worktrees build the same commit
from cold at the same time (49.6 s against 61 s, through single-flight). That gain cannot be
had alongside ccache today, because single-flight is part of vcache's own lookup.

Three causes, in order of size:

1. **vcache cannot cache 164 of the 434 compiles.** It keys a PCH consumer by re-expanding
   the header's source text with the consumer's own flags (`-E`). In this project the PCH is
   built once and reused by targets whose include paths do not reach every header it
   includes, so `-E` fails and the compile runs uncached. ccache keys the same consumers on
   a digest of the PCH's inputs and hits all 434.
2. **A vcache hit costs ~80 ms against ccache's ~9 ms**, because every lookup runs the
   preprocessor. ccache's direct mode hashes the source and a manifest of its includes
   instead.
3. **The PCH builds themselves are not cached** (`-x c++-header`), so every cold tree
   rebuilds them before any consumer can start.

Four changes, each measurable on its own (section 4). With 4.1-4.3, vcache should match
ccache in the warm case and keep its single-flight win in the concurrent cold case.

## 2. Measurements

Setup: the project's default container build (`ninja all all_tests`: 434 compiles, 222
links) at `-j26`, every build in one cgroup slice capped at 30 CPUs and 64 GiB, clang 22.
Each configuration started from an empty, separate cache. vcache was reached through a
compiler masquerade with ccache off. Every worktree sees the source at the same container
path, so path differences between worktrees play no part.

| Case | ccache | vcache, daemon off | vcache, single-flight |
| --- | --- | --- | --- |
| 1. Cold, one worktree | 44.9 s (433 misses) | 53.1 s | 44.5 s (262 misses, 164 uncached) |
| 2. Warm: cache filled by another worktree, clean build | **5.5 / 6.3 s** (434 hits) | 17.8 s (262 hits, 164 recompiled) | 19.5 / 19.6 s |
| 3. Cold, two worktrees at once | 61.0 / 61.8 s (828 misses in all) | - | **49.6 / 49.6 s** (262 compiles shared) |
| 4a. No-op rebuild, warm tree | 0.46 s | - | 1.47 s |
| 4b. One-file edit (1 compile + 216 relinks) | 5.2 s | - | 6.2 s |

- Whole-build CPU in case 2: 36 s with ccache, 278 s with vcache.
- Per-lookup cost, one non-PCH source timed alone: ccache hit ~9 ms, vcache hit ~80 ms, plain
  compile ~480 ms. For a PCH consumer neither tool beat the plain compile (~130 ms) in that
  isolated test.
- Correctness: no wrong hits; tests built by vcache pass. 377 of 400 compared objects have
  identical `.text`. The other 23 differ only in `__FILE__` string lengths: ccache's
  `base_dir` makes paths relative, while vcache keeps the absolute canonical path, as an
  uncached build does.
- Uncacheable in vcache: the 164 PCH consumers (preprocess failed), the PCH builds, and the
  222 links ("multiple inputs"; the masquerade routes links through vcache).

## 3. What stays as it is

- The key stays content-based and path-independent through roots; nothing here weakens it.
- Single-flight, admission and the jobserver are unchanged; they apply to whatever becomes
  cacheable.
- Rust is untouched.

## 4. Proposals

### 4.1 Key PCH consumers on the PCH's input digest (largest gain)

**What.** For a compile that uses a PCH (`-include-pch`, or `-include x.h` with `x.h.gch` or
`x.h.pch` beside it), key on: a digest of the PCH's inputs, plus the consumer's
preprocessed text *without* the PCH header expanded. Stop re-expanding the header
with the consumer's flags.

**How.**
- Where the digest comes from, in order:
  - a `<pch>.sum` file next to the PCH, if the build writes one (this project writes one,
    for ccache's `pch_external_checksum`);
  - otherwise vcache's own record of the PCH build. Proposal 4.2 records each PCH
    output's key beside it, as `<pch>.vcache-key`.
  - If neither exists, fall back to today's re-expansion.
- Preprocess the consumer with the PCH include removed, so `-E` no longer needs the PCH's
  include paths.
- Bump the C/C++ key version.

**Risk.**
- The digest has to cover everything that changes the PCH: its header text, the headers
  that header includes, and the flags it was built with.
- A `.sum` the build writes is trusted, as ccache trusts it. Document that, and refuse a
  `.sum` older than its PCH.
- A stale `.gch` is already documented as a hole; this proposal does not widen it.

**Gain.** The 164 uncached compiles become cacheable: case 2 should fall from ~19 s to about
ccache's time, plus the per-hit gap that 4.3 closes.

**Tests.**
- Two worktrees whose consumers lack the PCH header's include path hit each other.
- Editing a header that the PCH includes changes the consumer's key.
- A PCH built with an extra `-D` changes the key.
- A missing or older `.sum` falls back to re-expansion.

### 4.2 Cache the PCH builds

**What.** Accept `-x c++-header` / `-x c-header` compiles whose output is `.pch`, `.gch` or
`.pcm`, keyed like any compile from the preprocessed header.

**How.**
- Store the PCH bytes as the entry, and on restore write them with the build's own
  `-fno-pch-timestamp` expectation (clang); refuse when timestamps are on.
- Record `<pch>.vcache-key` next to the output for 4.1.

**Risk.** PCH files are large (tens of MB): use the reflink restore path
(`IDEA-reflink-compile-outputs.md`). PCHs are compiler-version specific, which the
compiler identity in the key already covers.

**Gain.** A cold tree stops rebuilding PCHs before its consumers can start: a shorter
critical path in cases 1 and 3.

**Tests.**
- A PCH built in worktree A is restored in worktree B and accepted by clang.
- A PCH built with timestamps on is not cached.

### 4.3 Direct mode for C and C++ lookups

**What.**
- Before preprocessing, hash the source, the canonical flags and a manifest of the
  include files recorded by an earlier miss. On a manifest match whose files all
  re-hash equal, use the stored key and skip `-E`.
- `docs/design.md` already plans this ("the manifest path would replace step 4"). The
  dep-scan manifest (`vcache-depmanifest-2`) and the Rust dep-info manifest are existing
  models.

**How.**
- On a miss, record the include set from the compile's own `-MD` output (or from
  linemarkers of the `-E` pass) as canonical paths with content hashes.
- On lookup, verify every file by hash, like the Rust manifest. Merge concurrent manifest
  writes the way the dep-scan manifest already does.

**Risk.** Includes that resolve differently because of a new file earlier on the include
path. ccache handles this by also recording include-path directory state, or by refusing
when a new file could shadow. Copy ccache's rules and its tests.

**Gain.** A hit drops from ~80 ms toward ccache's ~9 ms. In case 2 that is the remaining
gap. In 4a and 4b it also covers the per-file cost of a no-op rebuild that ninja does
not skip.

**Tests.**
- A manifest hit skips the preprocessor (log line or counter).
- Editing an included header misses.
- Adding a shadowing header earlier on the include path misses.
- Two writers merge their manifests.

### 4.4 Document the launcher form for CMake; keep links out

**What.**
- Recommend `CMAKE_C_COMPILER_LAUNCHER` / `CMAKE_CXX_COMPILER_LAUNCHER=vcache` for CMake
  builds rather than the masquerade.
- The masquerade routes every link through vcache, where it is refused as "multiple
  inputs" at a small cost each. The launcher only wraps compiles.

**How.** Docs and an integration test that a CMake build through the launcher sends no
link to vcache. No code, unless link caching (`LINK-CACHE-PLAN.md`) later wants them.

**Gain.** Small: removes 222 refused lookups per build, and the confusion in the stats.

### 4.5 Later: single-flight in front of another cache

Once 4.3 exists, the direct-mode key is cheap enough to serve as a lease key without
preprocessing. A "lease-only" mode could take a lease on that key, then run the next
wrapper (ccache) as the compiler, and release the lease when it finishes. Concurrent
worktrees would then wait on one ccache miss instead of each missing. Only worth doing if
4.1-4.3 are not, since with them vcache can replace ccache outright.

## 5. Order and how to judge it

Build in the order 4.1, then 4.2, then 4.3, re-running cases 1-4 of section 2 after each
step with the same setup. 4.4 can go any time.

vcache is worth switching to when all of the following hold:
- case 2 is within 20% of ccache;
- cases 4a and 4b are within 0.5 s of ccache;
- case 3 keeps its single-flight win;
- there are no wrong hits, and the vcache-built tests pass.
