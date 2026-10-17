# AGENTS.md — working in the vcache fork

This document contains instructions and context for AI coding agents working on this project.

---
## RULE 1 – ABSOLUTE: no deleting, discarding or overwriting without approval

You may NOT delete, discard or overwrite code or data unless I give the **exact
command and explicit approval in the same message**. This covers:

- deleting files or directories: `rm`, `rm -rf`, `find -delete`, removing a worktree;
- discarding uncommitted changes: `git checkout -- <path>`, `git restore`,
  `git reset --hard`, `git clean -fd`;
- overwriting what you did not write: replacing a file wholesale, `>` onto an
  existing file, force-pushing, `git branch -f`.

EXCEPT: files you created in this session (tests, tmp files, scripts) and build output
(`build/`, `bin/`, `dist/`, `third-party/.dl/`, `third-party/gperftools/`).

EXCEPT: the repo's own scripts' scratch. `tests/integration_test.sh`,
`tests/linux_kernel_test.sh` and `make test` create and remove their `mktemp -d` work
directories, and the session scratch dir (`/tmp/claude-*`, `/tmp/codex-1000`,
`/tmp/sunbird-tests/`) is yours. Running such a script, or removing what it left under
those paths, needs no approval; editing a script to widen what it removes does.

EXCEPT: `git rm` / `git mv` of a file that the bead being implemented names for removal or
renaming in its description or acceptance criteria. The bead is the approval; quote its id in
the commit body. Files a bead does not name still need the exact command.

- You do not get to decide that something is "safe" to remove. If you think something
  should be removed, stop and ask. Approval comes **before** the command is proposed.
- If you are not 100% sure what a command will delete, do not propose or run it.
- Prefer safe tools: `git status`, `git diff`, copying to backups.
- After approval, restate the command verbatim, list what it will affect, and wait for
  confirmation. When it has run, record in your response: the exact user text that
  authorized it, the command, and when you ran it. If that audit trail is missing,
  act as if the operation never happened.
- One exception needs no approval: when you changed files by mistake and the
  destructive-command guard blocks `git checkout --`, first check that every listed file
  holds only your change. Then set them aside with
  `git stash push -m "<what you did by mistake>" -- <those exact files>`. Report the
  stash in your response. Never stash anything else in a shared worktree.

## NO HOME-DIRECTORY FILESYSTEM WALKS

Never run `find`, `fd`, or similar recursive walks rooted at `$HOME`, `~`,
`/home/<user>`, or other home-directory paths. Scope to the workspace or a known
project root (e.g. `/home/richard/fbg/<project>`). Prefer ripgrep / Glob /
targeted `ls`. Piping a home-root walk to `head` does not make it safe.

---
## What this is

A fork of [Unto-Labs/vcache](https://github.com/unto-Labs/vcache), a compiler cache
whose point is cross-directory hits: it maps each checkout's paths to canonical
names (`VCACHE_ROOTS`) so one worktree's compiles hit another's. We use it as
`RUSTC_WRAPPER` for the Rust workspaces of the sibling repos (sunbird, siren,
launchctl, compass, devtools' `build-run`); C and C++ stay on ccache. The fork
carries fixes and features upstream does not have yet (`git log upstream/main..fbg`),
chiefly the Rust manifest mode, per-reason stats, a path-valued env-dep
canonicalisation (`rust_path_env_vars`, used for `OUT_DIR`) and the dep-scan key fix.

Work is tracked as beads in `.beads/` (prefix `vcache-`); use `br ready` /
`bv --robot-next` to pick work, and read a bead's description + notes before
implementing it. Check for an existing bead before inventing new work.

## Remotes and branches

| Ref | Meaning |
|---|---|
| `upstream/main` | Unto-Labs. Read-only for us; PRs go there only when the owner decides (commit author identity is still open). The local `main` tracks it; never commit on it. |
| `origin/fbg` | **Our integration branch** (named `main` until 2026-10-09): upstream main plus every merged fork feature. Builds, binaries and beads come from here. |
| `origin/feat/*`, `origin/fix/*` | One feature branch per bead or upstream issue, branched from `origin/fbg`. |
| `origin/dev` | Head of upstream PR #25, the fix stack offered to Unto-Labs. Push to it only to update that PR, and merge what you push into `fbg`. |

The fork has no `main` branch, so `main` always means upstream's. A push to `origin main`
would recreate the old name; push `fbg`. A clone from before the rename switches with
`git branch -m main fbg && git fetch origin && git branch -u origin/fbg fbg && git remote set-head origin -a`
(`host-setup-buildcache.sh` does this for `~/fbg/vcache`), then mirrors upstream with
`git branch --track main upstream/main`. We never push to `upstream`; set
`git remote set-url --push upstream no-push://upstream-is-read-only` so a push fails.

Workflow for a feature:

```bash
git fetch --multiple origin upstream
git worktree add ../vcache.wt/<slug> -b feat/<slug> origin/fbg   # worktrees live in ~/fbg/vcache.wt/
# ... implement, make test ...
git push -u origin feat/<slug>
git checkout fbg && git merge --no-ff feat/<slug> && git push origin fbg:fbg
```

Merging a new upstream release: `git merge upstream/main` into `fbg` (conflicts so far
have been adjacent additions in `core/config.{h,cc}` and the test includes), run
`make test`, then deploy the binaries (below). Upstream issues we filed: #21, #23, #24 (the daemon scheduler proposal, epic `vcache-cug`); the
feature requests #12–#20 are ours too.

## Git

Agent shells have no TTY. Never open an editor; do not change `core.editor`.

- Commits: `git commit -m "$(cat <<'EOF' ... EOF)"`
- Rebase continue: `GIT_EDITOR=true git rebase --continue`

The beads post-checkout/merge/rewrite hooks are devtools shims
(`../devtools/scripts/install-git-hooks.py`), routed to `scripts/git-hooks/`. There is no
pre-commit or pre-push gate: run `make test` yourself before every push.

### Commit messages

- Match upstream's style: `type(scope): lowercase summary` (`feat(daemon):`,
  `fix(rust):`, `test:`, `docs:`, `build:`), a short subject, 0–5 body lines on the *why*.
  `Fails first:` lines do not count toward the five.
- Do not reference beads or other issue-tracker IDs in commit messages; upstream issue
  numbers (`#21`) are fine.
- A change that alters a cache key bumps the matching version constant
  (`kCacheKeyVersion`, `kManifestKeyVersion`, `kDepScanKeyVersion`, `vcache-rust-key-vN`,
  the manifest header) and says so in the body; an unbumped key change serves old entries
  for new inputs.
  Dependency-discovery and validation fixes also need this review: if an old entry
  can bypass the fix, bump the affected lookup namespace even when its format is
  unchanged. Verify an old warm entry misses after upgrade, then the new entry hits.
- A change that alters the shipped binary ends its body with `Deploy: vcache` so the
  reader knows `dist/` must be rebuilt.

## Facts go in the name; comments explain purpose, and only when it is very unobvious

- **Facts belong in the identifier.** What a value *is* — its unit, side, state,
  lifetime, what produced it — is encoded in the name, never in a comment.
- **Comments explain *purpose*, and only when it is very unobvious**: a non-obvious
  rationale, a known gotcha, an invariant, a deliberate deviation. Upstream's comments
  are full sentences explaining *why* (read `src/daemon/server.cc` for the register);
  match that, and write no comment when the code already says it.
- **Never narrate mechanics.** No `// loop over keys`, no line-number citations, no
  "Changed X to Y", no plan-doc or bead ids in comments.
- **Single-letter names** only for loop counters.

C++ style: C++20, 2-space indent, `namespace vcache::<dir>`, `snake_case` members with a
trailing underscore in classes, `kConstant`, `CamelCase` functions, `std::optional` for
fallible reads, no exceptions across the public surface. No formatter is checked in;
keep lines under 100 columns and follow the surrounding file.

## Tests must fail first

A test proves nothing until you have seen it fail. Every new or changed test that
guards a fix, a gate, a refusal, or a generated artifact must be shown to fail on
the pre-fix code:

1. Run it against the code without your change and see it fail on the assertion
   that names the defect. For a new gate with no pre-fix code, break what it
   guards and see it fail.
2. Keep other agents' work intact: export the pre-fix commit into scratch and build it
   there, sharing this checkout's fetched third-party tree:
   ```bash
   d=/tmp/sunbird-tests/vcache-base && mkdir -p "$d" && git archive <sha> | tar -x -C "$d" \
     && ln -sfn "$PWD/third-party" "$d/third-party" && cp tests/<test> "$d/tests/" \
     && (cd "$d" && make all && ./tests/integration_test.sh)
   ```
   Do not `git stash` in a shared worktree.
3. Record it in the commit body, one line per test:
   `Fails first: <test> — <pre-fix sha or mutation> — <failing assertion>`.
4. A test that passes both before and after the change is not a regression test.

Test-only refactors that do not change what is asserted are exempt; say so in the body.

- **Dependency states:** when changing what recording accepts, test the full sequence:
  record, hit, filesystem change, miss. Include file-to-directory transitions for
  absence checks, so recording and lookup agree on errors such as `ENOTDIR`.
- **Tracer completeness:** when changing trace hooks or transport, inject record loss,
  truncation and environment changes; verify incomplete traces cannot certify a hit.
  Check hook exports in a build against the oldest supported headers: newer host
  headers can hide missing hooks in shipped binaries.
- **Compiler output:** when parsing a new output form, establish its escaping rules
  with a real compiler fixture, including a path containing a literal backslash.

## Build / test

Prerequisites: a C++20 compiler, `make`, `curl` and `jq` (the Rust integration tests also
need `rustc`). `third-party/fetch.sh` fetches and builds gperftools once per checkout
(the committed Boost subset and toml++ need nothing).

```sh
export MAKEFLAGS=-j8                   # ALWAYS: see Gotchas
make                                   # bin/vcache + bin/vcache-fstrace.so
make test                              # unit (tests/unit_test.cc) + integration (tests/integration_test.sh)
./tests/integration_test.sh            # integration only; sections print PASS/FAIL per check
make kernel-test                       # Linux kernel recipe, by hand only
make clean / make distclean            # distclean also drops the fetched third-party trees
```

Baseline on `fbg` after the 1.3.0 merge: **499 unit / 379 integration checks, 0 failed**.
Any PR must keep both at zero failures.

- Unit tests: one `Section("...")` per area, `Check(cond, "what")` / `CheckEq(actual,
  expected, "what")`; scratch under `util::MakeTempDir`, removed by a guard.
- Integration tests: `section`, `check NAME ACTUAL EXPECTED`, `ok`/`bad`, `reset_cache`,
  `hits`/`misses`/`uncacheable` read `--show-stats`; set `VCACHE_LOG=<file>` to assert on
  decisions. Each section builds its own tree under `$WORK`.
- `VCACHE_LOG=<file>` logs every decision of a real build; `--show-stats` lists counts per
  reason (`src/core/reason.h`); `--daemon-status` is the daemon's view.
- **Portability:** for platform-sensitive test/build changes, use
  `.github/workflows/platform-check.yml` as the target matrix. Specify language
  standards, detect compiler/tool capabilities, and use the target's ABI values.
  Time-sensitive fixtures must meet every timestamp condition: backdating mtime
  does not age ctime. Report tested and untested targets separately.

### Installed binaries

The repos consume `~/fbg/vcache/dist/{host,el10,dc-build}/vcache`, not `bin/`. Bring
`~/fbg/vcache` to the merged `fbg` (`git -C ~/fbg/vcache pull --ff-only`). The next build
that sources devtools' `scripts/vcache-env.sh` then runs `scripts/vcache-update.sh` for its
OS: devtools `build-run`, sunbird `build.sh`/`run_tests.sh`, and `container.run.sh` for el10.
When that OS's `vcache.commit` is not HEAD, the script prints a line saying it is rebuilding
and how long the last rebuild took, then rebuilds and installs the binary. It does this only
on branch `fbg`, and for host only with a clean tree. It skips a commit that changes only
`.beads/` or `*.md`.

To rebuild every OS now, run `~/fbg/devtools/scripts/host-setup-buildcache.sh`. It builds host
here, and el10 and dc-build from a `git archive` of HEAD inside the images. Each old binary is
kept as `vcache.prev`. Add `--force` only to rebuild a binary already built from HEAD, or to
build uncommitted changes. Both paths build whatever `~/fbg/vcache` has checked out; neither
pulls. Stores live under `/build/<user>/cache/vcache/<os>`; `--show-stats` needs
`VCACHE_DIR` and the same `VCACHE_CACHE_SIZE` the builds use, or a running daemon refuses
the client.

After rebuilding, check each `vcache.commit` against the merged revision and run
the affected regressions against the installed binary/tracer pair in its own
environment. Resolve compiler executable names there before running the tests.

### Publishing to anvil

vcache is also the artifact `vcache` in the FBG artifact store, anvil (`anvil.toml`: major
1, one target per OS the consumers build on: `fedora44`, `el10`, `ubuntu24`, `amzn2023` and
`dc-build`). After every merge to `fbg`, run `scripts/anvil-publish.sh` from a checkout of
this repository on a fedora44 host (zeus, until a builder host does it). It publishes
origin's `fbg` tip: it builds every target from `git archive`, each through `build-limited`:

| Target | Built | glibc |
|---|---|---|
| `fedora44` | natively, `/opt/infra.fedora44`'s newest `gcc-N` under `env -i` (`PATH=/usr/bin:/bin` and only the variables the build needs) | 2.43 |
| `el10` | in `sun_container:el10`, `/opt/infra.el10` at `/opt/infra` | 2.39 |
| `ubuntu24` | in `localhost/fbg-dev:ubuntu2404`, `/opt/infra.ubuntu24` at `/opt/infra` | 2.39 |
| `amzn2023` | in `localhost/fbg-dev:al2023`, `/opt/infra.amzn2023` at `/opt/infra` | 2.34 |
| `dc-build` | in `localhost/dc:build` (CentOS 7), its devtoolset-11 gcc | 2.17 |

It refuses a commit whose `anvil.toml` lists other targets than these five, before any
build. It checks that each target was built on its own OS (the build's `/etc/os-release`,
read inside its image and named by anvil's tag rule; dc-build's is `centos7`, fedora44's
the host's), so an image swapped for another OS's is refused even where both ship one
glibc (el10 and ubuntu24); that its compiler linked against that OS's glibc (the table, a
toolchain check); that it links libstdc++ statically; and that it runs `vcache --version`
and loads its tracer where it runs: fedora44 on the host, every other target in its image.
It prints the build record (commit, image digests, compilers, the ld, as, cmake and glibc
each build saw, the OS and glibc each target was built on, the newest glibc symbol version
each file needs, file hashes), then sends all five targets in one `anvil publish`; once
anvil's D23 server is deployed it refuses a publish that misses one. Source paths are
mapped to `.`, so a merge that leaves the binaries unchanged is reported as already
published. A commit not on `origin/fbg`, or without `anvil.toml`, is refused.

The newest glibc symbol a target needs can be newer than its OS's glibc version, through
the distribution's backports: the amzn2023 vcache needs `_dl_find_object@GLIBC_2.35`
(static libgcc's unwinder), which Amazon Linux 2023 carries from glibc `2.34-216`; a host
needs that package or newer (mvresearch1 and compass1 run `2.34-231`). The record lists
each file's newest symbol; it is not a gate.
`--dry-run` prints the steps and runs none of them; `--commit <sha>` publishes an earlier
`fbg` commit. Raise `major` only when a change breaks how consumers call vcache.

It needs `anvil` on `PATH` with `~/.fbg/anvil.env` holding a token that may publish
`vcache` (on zeus the `anvil-tunnel` service reaches the server), `podman` with the four
images (host_setup builds the `fbg-dev` ones: `ansible/bin/build-image ubuntu2404|al2023`),
and the four toolchain trees (devtools `3rdparty/build-os.sh <tag>`). Exit 8 means the
server was unreachable and the publish waits in anvil's outbox; the next `anvil` command
sends it.

## Gotchas

- **`MAKEFLAGS=-j127` from the login shell OOM-kills the host**: the Boost.Spirit units
  at `-O3 -ggdb3 -flto=auto` take several GB each. `export MAKEFLAGS=-j8` first;
  `third-party/fetch.sh` inherits it.
- **`/usr/lib64/ccache` on `PATH` hangs `make test`**: ccache's masquerade symlinks loop
  with vcache's own compiler-wrapper probe. Strip it from `PATH` for test runs.
- A fresh worktree has no `third-party/gperftools`: run `third-party/fetch.sh` (or symlink
  the primary checkout's `third-party/`) before `make`.
- `pch_external_checksum`, `base_dir` and `sloppiness` are **ccache** settings seen in the
  sibling repos; vcache has no equivalents and declines `-fpch-preprocess`, `-fmodules`
  and `.incbin` on purpose (`docs/design.md`).
- The daemon is on by default: `exportVcacheEnv` in `~/fbg/devtools/scripts/vcache-env.sh`
  exports `VCACHE_DAEMON=auto` (unless already set) and `VCACHE_DAEMON_SINGLE_FLIGHT=1`, so
  concurrent checkouts compile each missed key once (`docs/daemon.md`, Results).
  `FBG_VCACHE_SINGLE_FLIGHT=0` leaves it off. A client whose cache settings differ from the
  running daemon's is refused silently (upstream #23).
- `docs/design.md` lags the code in places (it says linking is uncached; `RunLink`
  exists); treat the code as the baseline and fix the doc in the same change.
- The `br` destructive-command guard pattern-matches bead and commit text: avoid the
  literal tokens `docker rm` and `unlink` in messages and notes.

## Landing the Plane (Session Completion)

**When ending a work session**, complete ALL steps below. Work is NOT complete until changes are pushed.

1. **Check status**: `git status` and `git diff`.
2. **Run the gate**: `make test` on the branch you will push.
3. **Commit** (see "Commit messages"), including `.beads/issues.jsonl` when beads changed.
4. **Push** the feature branch; merge to `fbg` with `--no-ff` and push `fbg:fbg`.
5. **Deploy** a merged change that ends with `Deploy: vcache`: bring `~/fbg/vcache` to `fbg`, and the next build rebuilds
   `dist/` (see "Installed binaries").
6. **Publish** every merge to `fbg` with `scripts/anvil-publish.sh` (see "Publishing to anvil").
