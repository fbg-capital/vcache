# Cold C/C++ manifest recording: 2026-10-16 measurements

A supported cold compile now runs once under the tracer and derives its result
key from the recorded inputs. It does not run `-E` first. Stale manifest states
and matching states with unusable results retain the preprocessor lookup and
preprocessor-hit state recording. Existing key families and schemas are unchanged.

This proves the removal of an invocation for eligible compiles. These Sunbird
measurements do **not** establish an overall performance improvement or meet the
cold-build target of being within 10% of ccache.

## Setup

- Sunbird `eb947e0f6363749e4191911eb8b2fed225f9c986`, two attached worktrees;
  `ninja all all_tests`, 523 object files, Clang 23.1.4 with absent-deps-1.
- Original vcache: `0d8ed735c0f9ef099ee0b42fc1a166024286c129` from `dist/el10`.
  Candidate: that source plus this change, built natively with GCC 16.2.0,
  the same compiler recorded in the original binary. Candidate SHA256:
  `42cdd7c41873f87bdeb16271c63eb69ca84e3e4f7b34543c06a4392bab6ccab7`.
- EL10 image `5875f8cac6534e973c4eb443c80d30ae9dfa003d57729bf067c54f047ac86ec8`;
  live devtools `8b7f960dd3c0`, siren `3aad09014cb5`.
- Every command ran through `build-limited`. Sunbird's normal memory calculation
  selected nine jobs in its 32 GiB container. No limits were increased.
- Three interleaved rounds (10–12), separate fresh stores per tool and round.
  Vcache used a private daemon, tracing, single-flight and a 40 GiB store;
  ccache used Sunbird's normal depend-mode configuration. Link caching was off.
- “Cold” removes all 523 objects and uses an empty compiler cache. Existing PCHs
  and generated inputs remain prepared. This is an object-cache fill, not a
  from-scratch configure/PCH build. “Cross-tree” restores objects in the other
  checkout using that store. Both phases include relinking.
- The no-op sample must execute no compiler or linker. The one-file sample
  changes `TcpOrUdsSocket.cc` in the first tree, causing one real cache miss and
  dependent relinks. The second tree's reuse of that edit is retained separately
  and is not used in the one-file table.

## Results

Seconds, median (minimum–maximum), three observations per cell. User CPU includes
Ninja's children; the separately running daemon is reported below.

| Phase | Candidate wall | Original wall | ccache wall |
|---|---:|---:|---:|
| cold | 85.125 (71.564–90.126) | 92.196 (79.906–99.548) | 51.413 (51.161–90.617) |
| cross-tree | 15.649 (10.728–15.713) | 13.253 (13.053–14.170) | 13.526 (9.291–19.996) |
| noop | 0.110 (0.104–0.129) | 0.110 (0.105–0.123) | 0.105 (0.102–0.181) |
| one-file | 5.220 (4.566–7.054) | 7.104 (5.948–11.875) | 4.864 (4.365–7.221) |

| Phase | Candidate user CPU | Original user CPU | ccache user CPU |
|---|---:|---:|---:|
| cold | 517.117 (460.877–523.209) | 467.205 (455.184–498.508) | 377.039 (374.744–389.082) |
| cross-tree | 8.814 (8.632–9.671) | 9.091 (8.854–9.164) | 9.180 (8.834–12.798) |
| noop | 0.051 (0.048–0.060) | 0.054 (0.040–0.054) | 0.051 (0.035–0.070) |
| one-file | 4.144 (3.925–4.403) | 4.006 (3.995–4.081) | 4.466 (3.839–5.166) |

The candidate's cold medians are 65.6% above ccache in wall time and 37.2% above
it in user CPU. Against original vcache, wall time is 7.7% lower while user CPU
is 10.7% higher. The differing wall/CPU direction and broad wall spread prevent
a claim of an overall speedup. Daemon CPU is small: 1.2–1.4 seconds per cold
fill and 0.30–0.34 seconds per cross-tree run.

491 of 523 objects explicitly consume a PCH. Their route is preserved: PCH
producers need preprocessing for portable identity, and consumers can list
headers in depfiles that the compile does not read. That prevents proving the
complete trace coverage required by this optimization. Only 32 object commands
are candidates for skipping preprocessing. Traced compilation, input validation,
hashing and storage remain, and the other 491 commands retain preprocessing.
These remaining costs explain why removing an invocation from eligible commands
cannot eliminate the whole gap to ccache; this measurement does not separately
attribute the observed increase in cold CPU.

Warm cross-tree wall medians rose 18.1%, despite all 523 requests hitting the
manifest and user/system CPU remaining similar (candidate 8.814/13.779 seconds;
original 9.091/13.790). To separate cache restoration from relinking, an additional
three interleaved pairs restored only the 523 object targets from warm stores:

| Warm object restoration | Candidate | Original |
|---|---:|---:|
| Wall median (range) | 4.694 (4.514–4.703) | 4.632 (4.511–4.699) |
| User CPU median | 5.408 | 5.335 |
| System CPU median | 7.461 | 7.600 |

Every isolated run had 523 manifest hits and zero misses. The 1.3% wall difference
and overlapping ranges do not reproduce the whole-build increase. Relinking and
shared-host scheduling are therefore the likely source of that spread; the
isolated result provides no evidence of a substantial warm lookup regression.
The real no-op is unchanged, and the first one-file edit retains one miss with
similar CPU use. No incremental speedup is claimed from the lower wall median.

## Correctness and retained evidence

All six cold/cross-tree comparisons contain 523 objects, with no missing or
byte-different objects between original and candidate. Each cold fill records
523 misses; each cross-tree run records 523 manifest hits, zero misses and zero
uncacheable requests. Dedicated real-Sunbird logs show no `preprocess:` command
for an ordinary cold compile, preprocessing for a PCH consumer, and a manifest
hit without preprocessing on both warm calls.

The dedicated GCC/Clang regression matrix passes 50 checks, covering cold and
stale routing, first versus seeded benign edits, cross-tree bytes/depfiles,
single-flight, incomplete/truncated/uncovered/changed traces, missing results,
empty/corrupt/erroring manifests, volatile inputs, `.incbin`, and real compiler
failure diagnostics/counters. Cold-path assertions fail on `0d8ed73`.
Existing preprocessor-hit fixtures explicitly seed their preprocessed entries;
their recording assertions remain intact. The full host gate passes 1470 unit,
1321 integration and 93 link verification checks plus auxiliary suites.

Evidence is retained on the measurement host under
`/build/richard/codex-1000/cold-recording-20261016/`:

- `paired.py`, `measure.py`, `launcher.py`, `analyze.py`: exact benchmark drivers;
  `measurements/{candidate,baseline,ccache}-{10,11,12}/`: configurations, raw
  timings, before/after counters, build logs, revisions and object digests.
- `summary.json`, `object-comparison.json`, `binary-hashes.txt`: aggregate results
  and byte/revision evidence. Compiler calls are reconciled with `.ninja_log`,
  because Ninja suppresses some progress messages during console tasks.
- `warm-objects.py`, `warm-objects/results.json`: the isolated warm comparison.
- `diagnostics/`: actual Sunbird command lines and cold/warm `VCACHE_LOG` files.
- `before-complete.log`, `baseline-integration.log`, `after-final.log`,
  `gate-final.log`, `el10-regressions.log`: fails-first and candidate validation.

Earlier controls are retained but excluded: a CMake launcher override bypassed
the cache, a purported no-op rebuilt generated inputs, and a progress-line
counter undercounted compiler calls. The final rounds reject those conditions.
The machine was shared throughout; three observations are not a statistical
confidence interval. These results do not justify changing Sunbird's default
C/C++ cache or broadening the optimization to PCH/untraced recording.

## Installed verification

The merged `fbg` gate also passed: 1470 unit, 1321 integration and 93 link
verification checks plus the auxiliary suites. All three `dist/` variants were
rebuilt from the merged code. Host and EL10 each passed the 50-check GCC/Clang
matrix; dc-build passed 25 checks with GCC 11.2.1. Its Python 3.6 driver only
substituted the older subprocess argument spelling. EL10 cargo 1.97.1 loaded
successfully under the installed tracer.

The dc-build image also contains Clang 3.4.2, which rejects `-ffile-prefix-map`
with both the original and new binaries. That compiler is not validated for this
feature; its existing compatibility failure is retained in the evidence.
`installed-binaries.json` records installed revisions and SHA256 values, including
that any revision difference from merge `d393550` changes only tracker metadata.
`gate-merged.log`, `installed-host.log`, `installed-el10.log`,
`installed-dc-gcc.log` and `dc-clang-previous.log` retain the checks.
