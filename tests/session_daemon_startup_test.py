#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Unto Labs
# SPDX-License-Identifier: Apache-2.0
import os
from pathlib import Path
import re
import shlex
import signal
import subprocess
import sys
import tempfile


def run(binary, work):
    source = work / "startup_fault.c"
    source.write_text(r"""
#define _GNU_SOURCE
#include <dlfcn.h>
#include <errno.h>
#include <limits.h>
#include <poll.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/file.h>
#include <sys/socket.h>
#include <time.h>
#include <unistd.h>

static pid_t parent_pid;
static int ready_read = -1, ready_write = -1, interrupted;
static const char *fault;
__attribute__((constructor)) static void initialize(void) {
  parent_pid = getpid();
  fault = getenv("VCACHE_TEST_STARTUP_FAULT");
}
static int injecting(const char *name) { return fault && strcmp(fault, name) == 0; }

int fsync(int descriptor) {
  int (*next)(int) = dlsym(RTLD_NEXT, "fsync");
  if (getpid() != parent_pid && injecting("pid-fsync-stall")) {
    char link[64], path[PATH_MAX];
    snprintf(link, sizeof(link), "/proc/self/fd/%d", descriptor);
    ssize_t length = readlink(link, path, sizeof(path) - 1);
    if (length >= 0) {
      path[length] = '\0';
      if (strstr(path, "/cache/daemon/.vcache-tmp-")) {
        fputs("stalled PID fsync\n", stderr);
        for (;;) pause();
      }
    }
  }
  return next(descriptor);
}

int pipe(int descriptors[2]) {
  int (*next)(int *) = dlsym(RTLD_NEXT, "pipe");
  if (getpid() == parent_pid && injecting("pipe")) { errno = EMFILE; return -1; }
  int result = next(descriptors);
  if (getpid() == parent_pid && result == 0 && ready_read < 0) {
    ready_read = descriptors[0];
    ready_write = descriptors[1];
  }
  return result;
}
pid_t fork(void) {
  pid_t (*next)(void) = dlsym(RTLD_NEXT, "fork");
  if (injecting("fork")) { errno = EAGAIN; return -1; }
  pid_t child = next();
  if (child == 0 && injecting("exit")) _exit(42);
  if (child == 0 && injecting("timeout")) for (;;) pause();
  return child;
}
int poll(struct pollfd *descriptors, nfds_t count, int timeout) {
  int (*next)(struct pollfd *, nfds_t, int) = dlsym(RTLD_NEXT, "poll");
  if (getpid() == parent_pid && count == 1 && descriptors[0].fd == ready_read) {
    if (injecting("poll-interrupted") && !interrupted++) { errno = EINTR; return -1; }
    if (injecting("deadline")) {
      struct timespec delay = {0, 10000000};
      nanosleep(&delay, NULL);
      errno = EINTR;
      return -1;
    }
    if (injecting("poll")) { errno = EINVAL; return -1; }
  }
  return next(descriptors, count, timeout);
}
ssize_t read(int descriptor, void *buffer, size_t size) {
  ssize_t (*next)(int, void *, size_t) = dlsym(RTLD_NEXT, "read");
  if (getpid() == parent_pid && descriptor == ready_read) {
    if (injecting("read-interrupted") && !interrupted++) { errno = EINTR; return -1; }
    if (injecting("read")) { errno = EIO; return -1; }
  }
  return next(descriptor, buffer, size);
}
int bind(int descriptor, const struct sockaddr *address, socklen_t size) {
  int (*next)(int, const struct sockaddr *, socklen_t) = dlsym(RTLD_NEXT, "bind");
  if (getpid() != parent_pid && injecting("bind")) { errno = EADDRINUSE; return -1; }
  return next(descriptor, address, size);
}
int flock(int descriptor, int operation) {
  int (*next)(int, int) = dlsym(RTLD_NEXT, "flock");
  if (getpid() != parent_pid && injecting("lock")) { errno = EWOULDBLOCK; return -1; }
  return next(descriptor, operation);
}
ssize_t write(int descriptor, const void *buffer, size_t size) {
  ssize_t (*next)(int, const void *, size_t) = dlsym(RTLD_NEXT, "write");
  if (getpid() != parent_pid && descriptor == ready_write) {
    if (injecting("unknown")) return next(descriptor, "?", 1);
    if (injecting("oversized")) {
      char message[8192];
      memset(message, 'x', sizeof(message));
      message[0] = 'E';
      return next(descriptor, message, sizeof(message));
    }
  }
  return next(descriptor, buffer, size);
}
char *mkdtemp(char *path) {
  char *(*next)(char *) = dlsym(RTLD_NEXT, "mkdtemp");
  if (injecting("directory")) { errno = EACCES; return NULL; }
  return next(path);
}
""")
    shim = work / "startup_fault.so"
    compiler = shlex.split(os.environ.get("CC", "cc"))
    subprocess.run([*compiler, "-std=c11", "-shared", "-fPIC", str(source),
                    "-ldl", "-o", str(shim)], check=True)
    environment = {key: value for key, value in os.environ.items()
                   if not key.startswith("VCACHE_") and key != "LD_PRELOAD"}
    environment.update(LD_PRELOAD=str(shim), TMPDIR=str(work))
    cases = [
        ("healthy", None),
        ("pid-fsync-stall", None),
        ("poll-interrupted", None),
        ("read-interrupted", None),
        ("directory", "temporary directory:"),
        ("pipe", "pipe:"),
        ("fork", "fork:"),
        ("poll", "poll failed:"),
        ("read", "read failed:"),
        ("exit", "EOF before readiness"),
        ("bind", "server error: bind "),
        ("lock", "daemon lock already held"),
        ("unknown", "unexpected readiness code=63"),
        ("oversized", "[truncated]"),
        ("timeout", "timeout waiting for readiness"),
        ("deadline", "timeout waiting for readiness"),
    ]
    failures = 0
    for name, error in cases:
        env = environment.copy()
        env["VCACHE_TEST_STARTUP_FAULT"] = name
        process = subprocess.Popen([str(binary), "--session-daemon-startup-probe"],
                                   env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                   universal_newlines=True, start_new_session=True)
        try:
            output, _ = process.communicate(timeout=10)
            passed = process.returncode == (1 if error else 0)
            if error:
                passed &= "session daemon startup failed:" in output and error in output
                if name != "directory":
                    retained = re.search(r"; retained=([^\n]+)", output)
                    passed &= bool(retained)
                    if retained:
                        diagnostic = Path(retained[1]) / "startup.txt"
                        passed &= diagnostic.exists() and error in diagnostic.read_text()
                        if name == "exit" and diagnostic.exists():
                            passed &= "exit=42" in diagnostic.read_text()
                if name == "exit":
                    passed &= "exit=42" in output
                if name in ("timeout", "deadline"):
                    elapsed = re.search(r"elapsed_ms=(\d+)", output)
                    passed &= bool(elapsed) and 1900 <= int(elapsed[1]) < 7000
                if name == "oversized":
                    passed &= len(output) < 6000
            else:
                passed &= "PASS" in output and "FAIL" not in output
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            process.communicate()
            output = "startup probe exceeded ten seconds\n"
            passed = False
        try:
            os.killpg(process.pid, 0)
        except ProcessLookupError:
            pass
        else:
            os.killpg(process.pid, signal.SIGKILL)
            output += "startup probe left a child process running\n"
            passed = False
        (work / (name + ".log")).write_text(output)
        print(("PASS " if passed else "FAIL ") + "session startup " + name, flush=True)
        failures += not passed
    return bool(failures)


if __name__ == "__main__":
    if not sys.platform.startswith("linux"):
        print("SKIP session startup fault injection requires Linux LD_PRELOAD")
        sys.exit(0)
    binary = Path(sys.argv[1]).resolve()
    if len(sys.argv) > 2:
        work = Path(sys.argv[2]).resolve()
        work.mkdir()
        sys.exit(run(binary, work))
    with tempfile.TemporaryDirectory(prefix="startup-") as directory:
        sys.exit(run(binary, Path(directory)))
