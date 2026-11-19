# Patching gcc and clang for `cxx_manifest_absent_deps`

`cxx_manifest_absent_deps` lets vcache record a C/C++ manifest state from the
compiler's own list of the paths it looked for and did not find. Stock gcc and
clang cannot write that list. This page says what the patch adds, how to build a
compiler with it, and how to check that vcache uses it.

## When the list matters

A manifest state must record two things: the files the compile read, with their
digests, and the paths the compile probed and did not find. The second is what
catches a header created later earlier on the include path, which would shadow
the one the state recorded. A depfile names only the first.

vcache gets the absent paths from a trace of the compile through
`vcache-fstrace.so`. Where there is no trace, the state comes from the depfile,
and a shadowing header created after recording can serve a stale object. No
trace means any of these:

- no tracer beside the binary,
- a host other than Linux,
- `LD_PRELOAD` already set,
- a compiler process that does not load the tracer,
- `cxx_manifest_trace = false`.

A patched compiler fills that gap: vcache passes `-fabsent-deps=<scratch file>`
and records the absent paths from the list. With a working tracer the flag is
not passed. On `src/daemon/server.cc` the list was not measurably cheaper than
the trace, and the trace also sees the driver's reads.

The setting is safe to leave on with stock compilers. vcache asks for the list
only when the compiler's `-v` banner ends in `absent-deps-1` or `absent-deps-2`.
A stock compiler has no such token and is never asked.

| | fbg (this fork) | upstream |
| --- | --- | --- |
| `cxx_manifest_absent_deps` default | `true` | `false` |
| `cxx_manifest` default | `false` (devtools `vcache-env.sh` exports `VCACHE_CXX_MANIFEST=1`) | `false` |

Upstream needs `VCACHE_CXX_MANIFEST_ABSENT_DEPS=1`, or the setting in the config
file, in addition to `cxx_manifest`.

## What the patches change

The patches are in devtools:

| Compiler | Patch | Applies to | Banner token |
| --- | --- | --- | --- |
| gcc | `3rdparty/gcc_absent_deps.patch` | tag `releases/gcc-16.2.0` | `absent-deps-1` |
| clang | `3rdparty/clang_absent_deps.patch` | tag `llvmorg-23.1.4` | `absent-deps-2` |

Object files are byte-identical to a stock build. The patches change three
things.

1. **`-fabsent-deps=FILE`** writes every path the compile looked for and did
   not find, one per line, sorted in byte order. Each path is spelled as the
   compiler formed it, so a relative path is relative to the compile's working
   directory. `-` writes to stdout. The list includes:
   - each search directory tried before the one that supplied a header,
   - each `__has_include` that found nothing,
   - each `.gch` or `.pch` probed beside a header,
   - each `-I` or built-in include directory that does not exist.

   clang's list (version 2) also includes the driver's own probes: its `.cfg`
   config files, the GCC-installation candidates, the distro files, and the PCH
   probes for `-include`. The driver passes these to cc1 as `-fabsent-entry=`
   arguments.
2. **The `-MD` / `-M` output lists more of what the compile depends on:**
   - a header that `__has_include` found, even if nothing includes it (gcc),
   - every precompiled header read (gcc and clang),
   - gcc also lists a `.gch` it validated and rejected, since validating it
     read the file.
3. **`--version` and `-v` end with the token** (gcc: `gcc version 16.2.0 (GCC) absent-deps-1`;
   clang: `clang version 23.1.4 (...) absent-deps-2`). vcache memoises the `-v`
   banner and reads the token from it. It never probes for the feature by
   running the flag.

Because the depfile differs, the token stays in vcache's compiler
fingerprint. A patched compiler and a stock one of the same version do not
share cache entries.

The flag also exists in the option tables: gcc `c.opt` and `cppopts.texi`, clang
`Options.td`. A stock compiler rejects it. vcache declines a caller's own
`-fabsent-deps=` as a second output file, so only vcache passes it.

## Building with devtools

On a host with the devtools toolchain, the build scripts apply the patches.
Run them through `build-limited`:

```bash
cd ~/fbg/devtools/3rdparty
~/bin/build-limited ./build.sh gcc     # build-gcc.sh: clone, apply, bootstrap, install
~/bin/build-limited ./build.sh clang   # build-clang.sh: the pinned major only
```

- `local-patch.sh` records the applied patch in the clone's git dir
  (`devtools-local.patch`). A re-run with the same tag and patch leaves the
  sources and their mtimes alone. A version bump or a changed patch reverse-
  applies the old one first, and a failed hunk stops the build.
- Both scripts fail the build if the installed driver's `--version` lacks the
  token.
- Only the pinned clang major (`CLANG_VER` in `versions.sh`) is patched. The
  previous major (`CLANG_PREV_VER`, now clang 22) builds stock and gets no list.
- `/opt/infra` only carries the patch after the toolchain is rebuilt and
  released. Check the installed compilers with
  `/opt/infra/bin/gcc-16 --version | head -1` and
  `/opt/infra/bin/clang-23 --version | head -1`.

## Building by hand

To patch a compiler outside devtools, start from the same release tag:

```bash
# gcc
git clone --depth 1 -b releases/gcc-16.2.0 https://github.com/gcc-mirror/gcc.git
cd gcc
git apply --check ~/fbg/devtools/3rdparty/gcc_absent_deps.patch
git apply ~/fbg/devtools/3rdparty/gcc_absent_deps.patch
./contrib/download_prerequisites
mkdir build && cd build
../configure --prefix=/opt/gcc-absent --enable-languages=c,c++ --disable-multilib
~/bin/build-limited make && make install

# clang
git clone --depth 1 -b llvmorg-23.1.4 https://github.com/llvm/llvm-project.git
cd llvm-project
git apply --check ~/fbg/devtools/3rdparty/clang_absent_deps.patch
git apply ~/fbg/devtools/3rdparty/clang_absent_deps.patch
cmake -G Ninja -S llvm -B build -DLLVM_ENABLE_PROJECTS=clang \
  -DCMAKE_BUILD_TYPE=Release -DCMAKE_INSTALL_PREFIX=/opt/clang-absent
~/bin/build-limited ninja -C build install
```

The patch touches libcpp headers that most of gcc includes, so a re-apply
rebuilds nearly every bootstrap stage. Apply it before the first build.

### Porting to another version

The patches only touch code that changes slowly, but they are tested only on
the tags above. Where `git apply --check` fails, apply with `git apply --3way`
and resolve each hunk by hand. Then check the result with the tests the patches
add:

- gcc:
  - `gcc/testsuite/c-c++-common/cpp/absent-deps-1.c` (the list and a missing
    `-I` directory),
  - `has-include-dep-1.c` (a `__has_include` hit in `-M` output).
- clang: `clang/test/Frontend/absent-deps.c`, which covers the list, the PCH in
  the depfile, `-save-temps`, the driver's config and GCC-installation probes,
  and the banner. Run it with `build/bin/llvm-lit -v clang/test/Frontend/absent-deps.c`.

If a port changes what the list means, change the token. Examples are new kinds
of probe, or a different spelling of relative paths. vcache reads only
versions 1 and 2. It never asks a compiler reporting `absent-deps-3` or later,
and logs the version instead. A new version needs a vcache change too:
`IsReadableAbsentDepsVersion` in `src/core/compile.cc`, plus a cache-key
version bump if the recorded states differ.

## Checking it works

1. The banner:

   ```bash
   gcc-16 --version | head -1      # ... 16.2.0 absent-deps-1
   clang-23 --version | head -1    # ... absent-deps-2
   ```

2. The list. In a scratch directory, a missing `-I` directory and a
   `__has_include` miss must both appear:

   ```bash
   printf '#if __has_include(<nope.h>)\n#endif\n' > t.c
   gcc-16 -I missing-dir -c t.c -o t.o -fabsent-deps=t.lst
   grep -E 'missing-dir|nope.h' t.lst
   ```

3. vcache records from the list. Turn tracing off so the list is the only
   source of absent paths:

   ```bash
   VCACHE_CXX_MANIFEST=1 VCACHE_CXX_MANIFEST_TRACE=0 VCACHE_LOG=/dev/stderr \
     vcache gcc-16 -c t.c -o t.o 2>&1 | grep 'absent list'
   ```

   The log says `tracing is off; recording from the compiler's absent list`,
   then `cxx manifest: recorded from the compiler's absent list: N files, M
   absent paths`. If it says `recording from the depfile` instead, the next
   line says why:
   - the banner has no token,
   - `cxx_manifest_absent_deps` is off,
   - the compile uses clang's `-working-directory`.

   `docs/configuration.md` ("cxx_manifest") has the full list of log lines and
   declines.

## Limits

- gcc writes nothing for an input that is already preprocessed, and the second
  step of `-save-temps` does not overwrite the first step's list.
- gcc's list leaves out:
  - framework lookups, `-remap` map files and C++ module interfaces,
  - the driver's own probes (its `specs` file, for example). The trace sees
    those; with the list they are left to the compiler fingerprint.
- gcc refuses `-MF` and `-fabsent-deps=` naming the same file.
- clang writes one list per cc1 job. Only the preprocessing step gets the flag.
- vcache does not ask for a list under clang's `-working-directory`, because
  the relative paths in the list would be relative to that directory. Such a
  compile records no state.
- `-fmodules` is not covered. vcache declines it anyway.
- If a path the compiler listed as absent exists when vcache records the
  state, that is a compiler fault, and vcache records no state (`not recording:
  the compiler listed <path> as absent but it exists`).
