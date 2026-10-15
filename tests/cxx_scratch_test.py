#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Unto Labs
# SPDX-License-Identifier: Apache-2.0
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import tempfile


def run(binary, work):
    compiler = shlex.split(os.environ.get("CXX", "c++"))
    compiler[0] = shutil.which(compiler[0]) or compiler[0]
    shim_source = work / "scratch_probe.c"
    shim_source.write_text(r"""
#define _GNU_SOURCE
#include <dlfcn.h>
#include <errno.h>
#include <fcntl.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

char *mkdtemp(char *path) {
  char *(*real_mkdtemp)(char *) = dlsym(RTLD_NEXT, "mkdtemp");
  const char *name = strrchr(path, '/');
  const char *log = getenv("VCACHE_TEST_SCRATCH_LOG");
  if (name && strncmp(name + 1, "vcache-", 7) == 0 && log) {
    int fd = open(log, O_WRONLY | O_CREAT | O_APPEND, 0600);
    if (fd >= 0) {
      (void)write(fd, path, strlen(path));
      (void)write(fd, "\n", 1);
      close(fd);
    }
    if (getenv("VCACHE_TEST_DENY_SCRATCH")) {
      errno = EACCES;
      return NULL;
    }
  }
  return real_mkdtemp(path);
}
""")
    shim = work / "scratch_probe.so"
    subprocess.run([*compiler, "-x", "c", "-std=c11", "-shared", "-fPIC",
                    str(shim_source), "-ldl", "-o", str(shim)], check=True)
    environment = {key: value for key, value in os.environ.items()
                   if not key.startswith(("VCACHE_", "CCACHE_")) and key != "LD_PRELOAD"}
    temp = work / "tmp"
    temp.mkdir()
    environment.update(VCACHE_DIR=str(work / "cache"), VCACHE_DAEMON="off",
                       VCACHE_CXX_MANIFEST="1", VCACHE_CXX_MANIFEST_TRACE="0",
                       VCACHE_CXX_MANIFEST_ABSENT_DEPS="0", TMPDIR=str(temp),
                       LD_PRELOAD=str(shim), VCACHE_ROOTS=str(work) + "=scratch-test")
    header = work / "value.h"
    header.write_text("#define VALUE 1\n")
    (work / "source.cc").write_text('#include "value.h"\nint value() { return VALUE; }\n')
    failures = 0

    def check(condition, name):
        nonlocal failures
        print(("PASS " if condition else "FAIL ") + name, flush=True)
        failures += not condition

    def compile_case(name, deny=False, deps=True, cache=None):
        log = work / (name + ".log")
        allocations = work / (name + ".allocations")
        env = environment.copy()
        env.update(VCACHE_LOG=str(log), VCACHE_TEST_SCRATCH_LOG=str(allocations))
        if cache:
            env["VCACHE_DIR"] = str(cache)
        if deny:
            env["VCACHE_TEST_DENY_SCRATCH"] = "1"
        arguments = [*compiler, "-std=c++20", "-O2", "-c", "source.cc", "-o", "source.o"]
        if deps:
            arguments += ["-MD", "-MF", "source.d"]
        for output in (work / "source.o", work / "source.d"):
            if output.exists():
                output.unlink()
        result = subprocess.run([str(binary), *arguments], cwd=work, env=env,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                universal_newlines=True)
        (work / (name + ".stderr")).write_text(result.stderr)
        calls = allocations.read_text().splitlines() if allocations.exists() else []
        return result.returncode, log.read_text() if log.exists() else "", calls

    def clean_scratch():
        return not list(work.glob("cache*/tmp/vcache-*")) and not list(temp.glob("vcache-*"))

    status, log, calls = compile_case("cold")
    check(status == 0 and len(calls) == 1 and "preprocess:" in log and clean_scratch(),
          "a cold miss allocates scratch before preprocessing and cleans it up")
    golden_object = (work / "source.o").read_bytes()
    golden_depfile = (work / "source.d").read_bytes()
    for name, deny in (("warm", False), ("warm-denied", True)):
        status, log, calls = compile_case(name, deny=deny)
        check(status == 0 and not calls and "preprocessor skipped" in log
              and "preprocess:" not in log and clean_scratch()
              and (work / "source.o").read_bytes() == golden_object
              and (work / "source.d").read_bytes() == golden_depfile,
              name + ": a manifest hit restores identical outputs without allocating scratch")

    header.write_text("#define VALUE 2\n")
    status, log, calls = compile_case("stale")
    check(status == 0 and len(calls) == 1 and "rejected:" in log
          and "preprocess:" in log and clean_scratch()
          and (work / "source.o").read_bytes() != golden_object,
          "a stale manifest preprocesses with scratch and restores the changed result")

    cache = work / "cache-no-deps"
    status, _, _ = compile_case("no-deps", deps=False, cache=cache)
    check(status == 0, "an object-only result is seeded")
    status, log, calls = compile_case("unusable", cache=cache)
    check(status == 0 and len(calls) == 1 and "has no dependency file" in log
          and "preprocess:" in log and clean_scratch(),
          "an unusable manifest result allocates scratch before recompiling")

    header.write_text("#define VALUE 3\n")
    status, log, calls = compile_case("miss-denied", deny=True)
    check(status == 0 and len(calls) == 2 and "no temp dir" in log and clean_scratch(),
          "a miss with both scratch locations denied passes through to the compiler")
    return failures


if __name__ == "__main__":
    if not sys.platform.startswith("linux"):
        print("SKIP C/C++ scratch allocation probe requires Linux LD_PRELOAD")
        sys.exit(0)
    binary = Path(sys.argv[1]).resolve()
    if len(sys.argv) > 2:
        work = Path(sys.argv[2]).resolve()
        work.mkdir()
        sys.exit(bool(run(binary, work)))
    with tempfile.TemporaryDirectory(prefix="vcache-cxx-scratch-") as directory:
        sys.exit(bool(run(binary, Path(directory))))
