#!/usr/bin/env python3
"""Real process-tree and fault-injection checks for mapped dependency records."""
import os
from pathlib import Path
import shlex
import resource
import struct
import subprocess
import sys
import tempfile
import time


FIXTURE = r'''
#define _GNU_SOURCE
#include <dirent.h>
#include <dlfcn.h>
#include <fcntl.h>
#include <pthread.h>
#include <sched.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/resource.h>
#include <sys/stat.h>
#include <sys/wait.h>
#include <unistd.h>
static const char *root, *self;
static void touch_path(const char *name, int flags) {
  char path[4096];
  snprintf(path, sizeof path, "%s/%s", root, name);
  int fd = open(path, flags, 0600);
  if (fd < 0) _exit(71);
  close(fd);
}
static void *worker(void *argument) {
  long index = (long)argument;
  char name[128], path[4096];
  struct stat info;
  for (int repeat = 0; repeat < 256; ++repeat) {
    snprintf(name, sizeof name, "sub/../read-%ld\\name", index);
    touch_path(name, O_RDONLY);
    snprintf(path, sizeof path, "%s/absent-%ld-%d", root, index, repeat);
    if (stat(path, &info) == 0) _exit(72);
    snprintf(name, sizeof name, "out-%ld-%d", index, repeat);
    touch_path(name, O_WRONLY | O_CREAT);
  }
  DIR *directory = opendir(root);
  if (!directory) _exit(73);
  closedir(directory);
  pid_t child = fork();
  if (child < 0) _exit(74);
  if (child == 0) {
    snprintf(name, sizeof name, "fork-%ld", index);
    touch_path(name, O_RDONLY);
    snprintf(name, sizeof name, "%ld", index);
    execl(self, self, root, "exec", name, (char *)0);
    _exit(75);
  }
  int status;
  if (waitpid(child, &status, 0) != child || status != 0) _exit(76);
  snprintf(name, sizeof name, "parent-%ld", index);
  touch_path(name, O_RDONLY);
  return 0;
}
int main(int argc, char **argv) {
  if (argc < 3) return 77;
  root = argv[1]; self = argv[0];
  if (strcmp(argv[2], "chroot") == 0) return chroot(root) == 0 ? 0 : 82;
  if (strcmp(argv[2], "pidns") == 0) {
    touch_path("read-0\\name", O_RDONLY);
    if (unshare(CLONE_NEWPID) != 0) return 83;
    pid_t child = fork();
    if (child < 0) return 84;
    if (child == 0) {
      touch_path("fork-0", O_RDONLY);
      _exit(0);
    }
    int status;
    if (waitpid(child, &status, 0) != child || status != 0) return 85;
    touch_path("parent-0", O_RDONLY);
    return 0;
  }
  if (strcmp(argv[2], "exec") == 0) {
    char name[128];
    snprintf(name, sizeof name, "exec-%s", argv[3]);
    touch_path(name, O_RDONLY);
    return 0;
  }
  if (strcmp(argv[2], "limit") == 0) {
    struct rlimit limit = {1, 1};
    if (setrlimit(RLIMIT_FSIZE, &limit) != 0) return 78;
    struct stat info;
    char path[4096];
    for (int index = 0; index < 4096; ++index) {
      snprintf(path, sizeof path, "%s/limited-%d", root, index);
      stat(path, &info);
    }
    return 0;
  }
  char library[4096];
  snprintf(library, sizeof library, "%s/libinput.so", root);
  if (!dlopen(library, RTLD_NOW)) return 79;
  for (int wave = 0; wave < 2; ++wave) {
    pthread_t threads[4];
    for (long index = 0; index < 4; ++index)
      if (pthread_create(&threads[index], 0, worker, (void *)(index + wave * 4))) return 80;
    for (int index = 0; index < 4; ++index) pthread_join(threads[index], 0);
  }
  unsetenv("VCACHE_TRACE_LOG");
  unsetenv("VCACHE_TRACE_FORMAT");
  struct stat info;
  char missing[4096];
  snprintf(missing, sizeof missing, "%s/environment-cleared", root);
  stat(missing, &info);
  return 0;
}
'''

FAULTS = r'''
#define _GNU_SOURCE
#include <errno.h>
#include <pthread.h>
#include <signal.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <sys/resource.h>
#include <sys/syscall.h>
#include <sys/wait.h>
#include <unistd.h>
static void *record_during_init(void *unused) {
  struct stat info;
  stat("/vcache-concurrent-init-missing", &info);
  return 0;
}
int vcache_trace_test_phase(const char *phase) {
  const char *fault = getenv("VCACHE_TEST_TRACE_FAULT");
  if (!fault) return 0;
  if (strcmp(phase, "marker") == 0 && getenv("VCACHE_TEST_MARKER_FAIL")) {
    errno = EACCES;
    return 1;
  }
  if (strcmp(fault, "interrupted") == 0 && strcmp(phase, "allocate") == 0) {
    static int interrupted;
    if (!__atomic_exchange_n(&interrupted, 1, __ATOMIC_RELAXED)) {
      errno = EINTR;
      return 1;
    }
    return 0;
  }
  if (strcmp(fault, "reentrant") == 0 && strcmp(phase, "writing") == 0) {
    struct stat info;
    stat("/vcache-reentrant-missing", &info);
    return 0;
  }
  if (strcmp(fault, "fork-reentrant") == 0 && strcmp(phase, "writing") == 0) {
    static int forked;
    if (__atomic_exchange_n(&forked, 1, __ATOMIC_RELAXED)) return 0;
    pid_t child = fork();
    if (child < 0) _exit(86);
    if (child == 0) {
      const struct rlimit no_core = {0, 0};
      setrlimit(RLIMIT_CORE, &no_core);
      struct stat info;
      stat("/vcache-child-reentrant-missing", &info);
      return 0;
    }
    int status;
    if (waitpid(child, &status, 0) != child || status != 0) _exit(87);
    return 0;
  }
  if (strcmp(fault, "concurrent-init") == 0 && strcmp(phase, "header-map") == 0) {
    pthread_t thread;
    if (pthread_create(&thread, 0, record_during_init, 0) != 0) _exit(81);
    pthread_join(thread, 0);
    return 0;
  }
  if (strcmp(fault, "quota") == 0 && strcmp(phase, "allocate") == 0) {
    errno = EDQUOT;
    return 1;
  }
  if (strcmp(fault, phase) != 0) return 0;
  if (getenv("VCACHE_TEST_AFTER_FIRST")) {
    static int calls;
    if (__atomic_fetch_add(&calls, 1, __ATOMIC_RELAXED) == 0) return 0;
  }
  if (getenv("VCACHE_TEST_CLEAR_ENV")) {
    unsetenv("VCACHE_TRACE_LOG");
    unsetenv("VCACHE_TRACE_FORMAT");
  }
  if (getenv("VCACHE_TEST_STOP")) {
    syscall(SYS_kill, syscall(SYS_getpid), SIGSTOP);
  }
  errno = strcmp(phase, "log-open") == 0 ? EMFILE : ENOSPC;
  return 1;
}
'''


def run(binary, reader, tracer, work):
    repo = Path(__file__).resolve().parent.parent
    env = {key: value for key, value in os.environ.items()
           if not key.startswith(("VCACHE_", "CCACHE_")) and key != "LD_PRELOAD"}
    compiler = shlex.split(os.environ.get("CC", "gcc"))

    def command(args, current=None, expected=0):
        result = subprocess.run([str(arg) for arg in args], cwd=work, env=current or env,
                                capture_output=True, text=True, timeout=45)
        if expected is not None and result.returncode != expected:
            raise AssertionError((args, result.returncode, result.stdout, result.stderr))
        return result

    def check(condition, name):
        print(("PASS " if condition else "FAIL ") + name, flush=True)
        if not condition:
            raise AssertionError(name)

    def prepare(name):
        path = work / (name + ".trace")
        mode = command([reader, "--prepare-trace", path]).stdout.strip()
        check(mode == "mmap-v1", name + ": parent selects mapped format")
        return path

    def read(path, expected=0):
        return command([reader, "--read-trace", path], expected=expected)

    def traced(path, library=tracer, extra=None, args=None, expected=0):
        current = dict(env, VCACHE_TRACE_LOG=str(path), VCACHE_TRACE_FORMAT="mmap-v1",
                       LD_PRELOAD=str(library))
        current.update(extra or {})
        return command(args or ["/bin/true"], current, expected)

    (work / "fixture.c").write_text(FIXTURE)
    (work / "faults.c").write_text(FAULTS)
    (work / "library.c").write_text("int trace_fixture_value = 1;\n")
    fixture = work / "fixture"
    command(compiler + ["-shared", "-fPIC", "library.c", "-o", work / "libinput.so"])
    command(compiler + ["-std=c11", "-O1", "-pthread", "fixture.c", "-Wl,--no-as-needed",
                        "-L" + str(work), "-linput", "-Wl,-rpath," + str(work), "-ldl",
                        "-o", fixture])
    fault_tracer = work / "fault-tracer.so"
    command(compiler + ["-std=c11", "-O2", "-fPIC", "-shared", "-pthread", "-Wl,-z,defs",
                       "-DVCACHE_TRACE_TEST_HOOK", "-I" + str(repo / "src/trace"),
                       os.environ.get("VCACHE_TEST_TRACE_SOURCE", repo / "src/trace/fstrace.c"),
                       "faults.c", "-ldl", "-o", fault_tracer])
    (work / "sub").mkdir()
    for index in range(8):
        for name in ("read-" + str(index) + "\\name", "fork-" + str(index),
                     "exec-" + str(index), "parent-" + str(index)):
            (work / name).write_text("input\n")

    mapped = prepare("threads")
    traced(mapped, args=[fixture, work, "threads"])
    result = read(mapped, expected=None)
    check(result.returncode == 0, "mapped thread/fork/exec records are complete")
    observed = set(result.stdout.splitlines())
    expected = {"M\t" + str(work / "environment-cleared"), "P\t" + str(fixture),
                "L\t" + str(work / "libinput.so")}
    for index in range(8):
        expected.add("R\t" + str(work) + "/sub/../read-" + str(index) + "\\name")
        expected.update("R\t" + str(work / (prefix + str(index)))
                        for prefix in ("fork-", "exec-", "parent-"))
        for repeat in range(256):
            expected.add("M\t" + str(work / ("absent-%d-%d" % (index, repeat))))
            expected.add("W\t" + str(work / ("out-%d-%d" % (index, repeat))))
    (work / "expected-records.txt").write_text("\n".join(sorted(expected)) + "\n")
    (work / "actual-records.txt").write_text("\n".join(sorted(observed)) + "\n")
    check(expected <= observed, "all unique reads, absent paths, writes, tools and loads survive")
    data = mapped.read_bytes()
    chunks = struct.unpack_from("=I", data, 32)[0]
    check(chunks > 17 and len(data) == 4096 + chunks * 65536,
          "thread reuse and growth reserve disjoint complete chunks")
    check(b"D\t" + str(work).encode() + b"\n" in data, "directory records remain in the transport")
    legacy = work / "legacy.trace"
    traced(legacy, extra={"VCACHE_TRACE_FORMAT": "text"}, args=[fixture, work, "threads"])
    legacy_records = set(read(legacy).stdout.splitlines())
    check(observed == legacy_records, "mapped and legacy process trees retain identical record sets")
    if command(["unshare", "-r", "-p", "-f", "/bin/true"], expected=None).returncode == 0:
        namespaced = prepare("pid-namespace")
        command(["unshare", "-r", "-p", "-f", "env", "LD_PRELOAD=" + str(tracer),
                 "VCACHE_TRACE_LOG=" + str(namespaced), "VCACHE_TRACE_FORMAT=mmap-v1",
                 fixture, work, "pidns"])
        namespace_records = set(read(namespaced).stdout.splitlines())
        check({"R\t" + str(work / name) for name in ("fork-0", "parent-0")} <= namespace_records,
              "equal numeric PIDs across namespaces never share a fork chunk")
    else:
        print("SKIP PID namespace fork: user namespaces unavailable", flush=True)

    for phase in ("reserved", "allocated", "writing", "record-start", "payload-half",
                  "payload", "record-commit"):
        path = prepare("killed-" + phase)
        current = dict(env, VCACHE_TRACE_LOG=str(path), VCACHE_TRACE_FORMAT="mmap-v1",
                       LD_PRELOAD=str(fault_tracer), VCACHE_TEST_TRACE_FAULT=phase,
                       VCACHE_TEST_STOP="1", VCACHE_TEST_MARKER_FAIL="1")
        if phase not in ("reserved", "allocated"):
            current["VCACHE_TEST_AFTER_FIRST"] = "1"
        child = subprocess.Popen(["/bin/true"], env=current)
        try:
            deadline = time.monotonic() + 5
            stopped = False
            while time.monotonic() < deadline:
                pid, status = os.waitpid(child.pid, os.WNOHANG | os.WUNTRACED)
                if pid:
                    stopped = os.WIFSTOPPED(status)
                    break
                time.sleep(0.005)
            check(stopped, phase + ": writer reaches the publication barrier")
        finally:
            if child.poll() is None:
                child.kill()
            child.wait()
        check(read(path, expected=None).returncode != 0,
              phase + ": killed writer refuses its missing dependency")

    for phase in ("header-map", "allocate", "quota", "chunk-map", "reentrant", "fork-reentrant",
                  "concurrent-init", "payload"):
        path = prepare("failed-" + phase)
        traced(path)
        failure = traced(path, fault_tracer, {"VCACHE_TEST_TRACE_FAULT": phase,
                                             "VCACHE_TEST_MARKER_FAIL": "1",
                                             "VCACHE_TEST_CLEAR_ENV": "1"}, expected=None)
        check(failure.returncode == 0, phase + ": recording failures preserve compiler exit")
        check(not Path(str(path) + ".incomplete").exists(), phase + ": marker creation failed")
        check(struct.unpack_from("=I", path.read_bytes(), 36)[0] == 1,
              phase + ": shared header preserves the lost-record notification")
        check(read(path, expected=None).returncode != 0, phase + ": no partial trace is accepted")
    unopened = prepare("failed-log-open")
    traced(unopened)
    traced(unopened, fault_tracer, {"VCACHE_TEST_TRACE_FAULT": "log-open",
                                   "VCACHE_TEST_MARKER_FAIL": "1", "VCACHE_TEST_CLEAR_ENV": "1"})
    check(unopened.stat().st_mode & 0o400 == 0 and read(unopened, expected=None).returncode != 0,
          "descriptor exhaustion without a marker cannot reuse another writer's trace")
    interrupted = prepare("interrupted")
    traced(interrupted, fault_tracer, {"VCACHE_TEST_TRACE_FAULT": "interrupted"})
    check(read(interrupted).returncode == 0, "interrupted allocation retries without losing records")
    limited = prepare("limited")
    traced(limited, args=[fixture, work, "limit"])
    check(read(limited, expected=None).returncode != 0,
          "a finite hard size limit declines tracing without delivering SIGXFSZ")
    if command(["unshare", "-r", "/bin/true"], expected=None).returncode == 0:
        jailed = prepare("chroot")
        command(["unshare", "-r", "env", "LD_PRELOAD=" + str(tracer),
                 "VCACHE_TRACE_LOG=" + str(jailed), "VCACHE_TRACE_FORMAT=mmap-v1",
                 fixture, work, "chroot"])
        check(struct.unpack_from("=I", jailed.read_bytes(), 36)[0] == 1,
              "successful chroot invalidates the shared header outside the new root")
    else:
        print("SKIP successful chroot: user namespaces unavailable", flush=True)

    for count in (4096, 0xffffffff):
        path = prepare("capacity-" + str(count))
        with path.open("r+b") as stream:
            stream.seek(32)
            stream.write(struct.pack("=I", count))
        traced(path)
        check(read(path, expected=None).returncode != 0, "exhausted/overflowing cursor refuses reuse")
    incompatible = prepare("no-opt-in")
    traced(incompatible, extra={"VCACHE_TRACE_FORMAT": "text"})
    check(incompatible.stat().st_size == 4096 and read(incompatible, expected=None).returncode != 0,
          "new tracer never appends legacy text to a mapped file without opt-in")

    (work / "setup-failure.c").write_text("#include <errno.h>\n#include <sys/types.h>\n"
        "int posix_fallocate(int fd, off_t start, off_t size) { return EOPNOTSUPP; }\n")
    setup_shim = work / "setup-failure.so"
    command(compiler + ["-shared", "-fPIC", "setup-failure.c", "-o", setup_shim])
    fallback = work / "setup-failure.trace"
    setup = command([reader, "--prepare-trace", fallback], dict(env, LD_PRELOAD=str(setup_shim)))
    check(setup.stdout.strip() == "text" and not fallback.exists(),
          "failed parent allocation selects text before publishing the trace")
    traced(fallback, extra={"VCACHE_TRACE_FORMAT": "text"})
    check(read(fallback).returncode == 0, "whole-invocation text fallback records a complete trace")
    (work / "page-size.c").write_text('#define _GNU_SOURCE\n#include <unistd.h>\n#include <dlfcn.h>\n'
        'long sysconf(int name) { if (name == _SC_PAGESIZE) return 65536; '
        'long (*next)(int) = dlsym(RTLD_NEXT, "sysconf"); return next(name); }\n')
    page_shim = work / "page-size.so"
    command(compiler + ["-shared", "-fPIC", "page-size.c", "-ldl", "-o", page_shim])
    page_fallback = work / "page-size.trace"
    setup = command([reader, "--prepare-trace", page_fallback], dict(env, LD_PRELOAD=str(page_shim)))
    check(setup.stdout.strip() == "text" and not page_fallback.exists(),
          "incompatible mapping alignment selects text before publishing the trace")

    def limit_setup():
        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
        resource.setrlimit(resource.RLIMIT_FSIZE, (1, 1))

    limited_setup = work / "limited-setup.trace"
    setup = subprocess.run([str(reader), "--prepare-trace", str(limited_setup)], env=env,
                           capture_output=True, text=True, preexec_fn=limit_setup, timeout=10)
    check(setup.returncode == 0 and setup.stdout.strip() == "text" and not limited_setup.exists(),
          "parent size-limit failure selects legacy without delivering SIGXFSZ")
    limited_attachment = prepare("limited-header-attachment")
    traced(limited_attachment)
    attachment_env = dict(env, VCACHE_TRACE_LOG=str(limited_attachment),
                          VCACHE_TRACE_FORMAT="mmap-v1", LD_PRELOAD=str(fault_tracer),
                          VCACHE_TEST_TRACE_FAULT="header-map", VCACHE_TEST_MARKER_FAIL="1")
    attachment = subprocess.run(["/bin/true"], env=attachment_env, preexec_fn=limit_setup,
                                capture_output=True, timeout=10)
    check(attachment.returncode == 0 and read(limited_attachment, expected=None).returncode != 0,
          "failed header attachment under a hard limit cannot reuse another writer's trace")

    cache_env = dict(env, VCACHE_DIR=str(work / "cache"), VCACHE_DAEMON="off",
                     VCACHE_CXX_MANIFEST="1", VCACHE_CXX_MANIFEST_TRACE="1",
                     VCACHE_LINK_CACHE="1", VCACHE_LINK_VERIFY="0")
    (work / "input.c").write_text('#if __has_include("optional.h")\n#include "optional.h"\n'
                                  '#else\n#define VALUE 1\n#endif\nint main(void) { return VALUE; }\n')

    def cached(name, args, extra=None):
        log = work / (name + ".log")
        current = dict(cache_env, VCACHE_LOG=str(log))
        current.update(extra or {})
        result = command([binary] + args, current)
        return log.read_text(), result

    compile_args = compiler + ["-O1", "-MD", "-MF", "input.d", "-c", "input.c", "-o", "input.o"]
    first, _ = cached("compile-record", compile_args)
    warm, _ = cached("compile-hit", compile_args)
    check("format=mmap-v1" in first and "preprocessor skipped" in warm,
          "compile manifest records mapped dependencies then hits")
    original = (work / "input.o").read_bytes()
    (work / "optional.h").write_text("#define VALUE 2\n")
    changed, _ = cached("compile-absent-created", compile_args)
    check("preprocessor skipped" not in changed and (work / "input.o").read_bytes() != original,
          "newly present include invalidates the mapped compile manifest")
    link_args = compiler + ["input.o", "-o", "app"]
    link, _ = cached("link-record", link_args)
    hit, _ = cached("link-hit", link_args)
    check("format=mmap-v1" in link and "link hit" in hit,
          "link consumer records mapped inputs then hits")
    command([work / "app"], expected=2)
    (work / "optional.h").write_text("#define VALUE 3\n")
    cached("compile-header-edit", compile_args)
    missed, _ = cached("link-object-edit", link_args)
    check("link hit" not in missed, "changed object invalidates the mapped link manifest")
    command([work / "app"], expected=3)
    for directory in ("early", "late"):
        (work / directory).mkdir()
    (work / "library-input.c").write_text("int choice(void) { return 4; }\n")
    command(compiler + ["-c", "library-input.c", "-o", "choice.o"])
    command(["ar", "rcs", "late/libchoice.a", "choice.o"])
    (work / "library-main.c").write_text("int choice(void); int main(void) { return choice(); }\n")
    command(compiler + ["-c", "library-main.c", "-o", "library-main.o"])
    library_args = compiler + ["library-main.o", "-Learly", "-Llate", "-lchoice", "-o", "chosen"]
    cached("link-library-record", library_args)
    library_hit, _ = cached("link-library-hit", library_args)
    check("link hit" in library_hit, "link library search records then hits")
    command([work / "chosen"], expected=4)
    (work / "library-input.c").write_text("int choice(void) { return 5; }\n")
    command(compiler + ["-c", "library-input.c", "-o", "choice.o"])
    command(["ar", "rcs", "early/libchoice.a", "choice.o"])
    library_miss, _ = cached("link-library-created", library_args)
    check("link hit" not in library_miss, "newly present library invalidates the mapped link manifest")
    command([work / "chosen"], expected=5)
    for consumer, args in (("compile", compile_args), ("link", link_args)):
        extra = {"VCACHE_DIR": str(work / (consumer + "-broken-cache")),
                 "VCACHE_TRACER": str(fault_tracer), "VCACHE_TEST_TRACE_FAULT": "payload",
                 "VCACHE_TEST_MARKER_FAIL": "1"}
        cached(consumer + "-broken-record", args, extra)
        failed, _ = cached(consumer + "-broken-repeat", args, extra)
        check("preprocessor skipped" not in failed and "link hit" not in failed,
              consumer + ": failed publication without marker cannot authorize a hit")
    print("mapped trace evidence:", work, flush=True)


if __name__ == "__main__":
    if sys.platform != "linux":
        print("SKIP mapped traces require Linux")
        sys.exit(0)
    binary = Path(sys.argv[1]).resolve()
    reader = Path(sys.argv[2]).resolve()
    tracer = Path(sys.argv[3]).resolve() if len(sys.argv) > 3 else binary.parent / "vcache-fstrace.so"
    work = Path(tempfile.mkdtemp(prefix="vcache-mapped-trace-"))
    print("mapped trace evidence:", work, flush=True)
    probe = subprocess.run([str(reader), "--prepare-trace", str(work / "support.trace")],
                           capture_output=True, text=True, check=True)
    if probe.stdout.strip() != "mmap-v1":
        print("SKIP mapped transport is unavailable on this filesystem/platform")
        sys.exit(0)
    run(binary, reader, tracer, work)
