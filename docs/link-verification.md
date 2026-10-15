# Concurrent worktree link verification

`tests/link_verify_worktrees.py` checks link verification against real project
builds in two fresh detached worktrees. Run it manually: it builds a whole
project and retains its stores, worktrees and outputs. It is not part of the
ordinary test suite; its evidence-validator unit tests are.

The harness first builds tree A, then builds tree B against A's cache. This
serial step proves cross-tree candidate reuse. It then forces concurrent warm
relinks in both trees (three rounds by default), followed by concurrent relinks
against a separate empty store. Single-flight and memory admission are enabled.
Each tree maps to the same canonical root, but has its own output directory and
log. Verification remains enabled throughout; ordinary link hits are rejected.

## Running a real project

Pass a setup command, a full build command, a command that forces relinking, and
every output you want verified. The commands run in each worktree with `VCACHE`
pointing to the selected binary. Wire that binary into the project's compiler
or link launcher; setting verification environment variables alone does not
make a build use vcache. The relink command must actually invoke the linker.
The harness never cleans or removes existing worktrees or caches.

For example, from the vcache checkout, use its full C++ build with a link map:

```bash
repo=$PWD
build='make -j8 TOP=. LTO= OPT="-O1 -g" CXX="bash $LINK_VERIFY_COMPILER g++" CC="bash $LINK_VERIFY_COMPILER gcc" LDFLAGS="-pthread -Wl,--gc-sections -Wl,-Map,bin/vcache.map" all'
~/bin/build-limited python3 tests/link_verify_worktrees.py \
  --repo "$repo" --ref origin/fbg --vcache "$repo/dist/host/vcache" \
  --setup "ln -s '$repo/third-party/gperftools' third-party/gperftools" \
  --build "$build" \
  --relink "touch build/release/src/main.o && $build" \
  --output bin/vcache --output bin/vcache.map
```

This recipe requires the checkout's fetched gperftools build. It uses relative
output paths and disables LTO. The optional `LINK_VERIFY_COMPILER` helper runs
`-c` compiles directly with explicit path mapping, then routes link commands
through vcache. This isolates link verification from compile-cache behavior.
Without explicit mapping for all compiles, debug paths can make
objects differ across trees, giving legitimate link misses and no verification
coverage. This recipe does not establish LTO or sunbird coverage.

The script enters `~/bin/build-limited` automatically if it is not already in
`buildlimit.slice`. It removes ccache/sccache directories from the build PATH,
uses a private config and stores, and puts scratch under
`/build/$USER/codex-1000/link-verify-worktrees-*`. Worktrees live under
`/build/$USER/claude-tmp/worktrees/<repo>/<run>/{a,b}`. All worktrees are created
from `--ref` (default `origin/main`) and must resolve to the same revision.
Checkout hooks are disabled for worktree creation so tracker hooks cannot write
through an experimental tree before its setup runs.
For sunbird, attach each worktree to a beads session in `--setup` before any
command that uses `br` or commits. Follow the target repository's instructions.

Use `--describe-tools` to record the actual compiler/linker versions or image
identity when using a different toolchain. The default records `c++ --version`
and `ld --version`. `--rounds` changes the number of warm concurrent rounds;
`--cache-size` sets the private store size (default `10G`).

## Reading the evidence

The printed evidence directory contains:

- `run.json`, a copy of the harness, and `toolchain.txt`: source revision,
  binary/tracer SHA-256, commands, worktree paths, resource group and tool versions.
- Each phase's `stats-before.txt`, `stats-after.txt` and `report.json`: counter
  snapshots, outcomes, process overlap, output digests and executable status.
- Per-tree `config.txt`, `build.log`, `vcache.log`, `time.txt` and `outputs/`:
  effective settings, diagnostics, link keys/digests, wall/user/system seconds,
  and exact fresh outputs.
- `store-before/`: a copy of the candidate store before each measured phase.
  This preserves the old sidecars even when verification replaces a mismatch.
- `complete.json`: written only when every phase passes. It explicitly says
  the soak is incomplete.

Warm phases require a successful verification of every declared output in
every participating tree. Cold concurrency requires at least one verification,
because the first link must fill the empty store. Both concurrent builds must
overlap and produce identical declared bytes and executable status. Source
changes that produce legitimate misses are useful workload coverage, but cannot
replace these positive reuse checks.

The harness stops with a nonzero exit on failed builds, missing outputs,
mismatches, verification errors, served link hits, zero required verifications,
counter/log disagreement, decreasing counters, or missing process overlap.
Only decreases between snapshots can identify a counter reset; this short run
is not a continuous reset monitor. A mismatch may reveal linker nondeterminism;
retain and investigate it rather than normalizing away different output bytes.

Successful controlled runs are preparatory evidence. They do not establish the
14-day host/el10 sunbird soak, hundreds of verifications per store, input-change
coverage, audit controls, or the owner's rollout decision. Defaults remain off.
