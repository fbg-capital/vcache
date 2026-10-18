#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Unto Labs
# SPDX-License-Identifier: Apache-2.0
"""Exercise the real daemon's readiness pipe under interrupted and short writes."""

import errno
import fcntl
import os
from pathlib import Path
import select
import shlex
import subprocess
import sys
import tempfile
import time


SHIM = r'''
#define _GNU_SOURCE
#include <dlfcn.h>
#include <errno.h>
#include <stdlib.h>
#include <string.h>
#include <sys/socket.h>
#include <unistd.h>

static int ready_fd = -1;
static unsigned int calls;
static const char *fault;

__attribute__((constructor)) static void initialize(void) {
  const char *descriptor = getenv("VCACHE_DAEMON_READY_FD");
  if (descriptor) ready_fd = atoi(descriptor);
  fault = getenv("VCACHE_TEST_READY_WRITE");
}

int bind(int fd, const struct sockaddr *address, socklen_t size) {
  int (*next)(int, const struct sockaddr *, socklen_t) = dlsym(RTLD_NEXT, "bind");
  if (fault && strncmp(fault, "error-", 6) == 0) {
    errno = EADDRINUSE;
    return -1;
  }
  return next(fd, address, size);
}

ssize_t write(int fd, const void *data, size_t size) {
  ssize_t (*next)(int, const void *, size_t) = dlsym(RTLD_NEXT, "write");
  if (fd == ready_fd && fault) {
    unsigned int call = calls++;
    if ((strstr(fault, "eintr") && call == 0) ||
        (strcmp(fault, "error-partial-interrupted") == 0 && call == 1)) {
      next(STDERR_FILENO, "injected EINTR\n", 15);
      errno = EINTR;
      return -1;
    }
    if (strcmp(fault, "zero") == 0) return 0;
    if (strcmp(fault, "failed") == 0) { errno = EIO; return -1; }
    if (strstr(fault, "short") || strcmp(fault, "error-partial-interrupted") == 0) {
      if (size > 2) size = 2;
      next(STDERR_FILENO, "injected short write\n", 21);
    }
  }
  return next(fd, data, size);
}
'''


def run(binary, work):
    source = work / 'ready_write.c'
    source.write_text(SHIM)
    shim = work / 'ready_write.so'
    compiler = shlex.split(os.environ.get('CC', 'cc'))
    subprocess.run(compiler + ['-std=c11', '-shared', '-fPIC', str(source), '-ldl',
                              '-o', str(shim)], check=True)
    config = work / 'config.toml'
    config.write_text('[vcache]\n')
    environment = {key: value for key, value in os.environ.items()
                   if not key.startswith('VCACHE_') and key != 'LD_PRELOAD'}
    environment.update(VCACHE_CONFIG=str(config), VCACHE_DAEMON='auto', LC_ALL='C',
                       VCACHE_CACHE_SIZE='1G', VCACHE_DAEMON_IDLE_TIMEOUT='0')
    socket_path = work / 's'
    cases = ['healthy', 'ready-eintr', 'lock', 'lock-eintr', 'error-control',
             'error-eintr', 'error-short', 'error-partial-interrupted',
             'zero', 'failed', 'closed-reader']
    failures = 0
    for name in cases:
        store = work / name
        state = store / 'daemon'
        state.mkdir(parents=True)
        with (state / 'lock').open('w') as lock:
            if name.startswith('lock'):
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            read_fd, write_fd = os.pipe()
            env = dict(environment, VCACHE_DIR=str(store), VCACHE_DAEMON_SOCKET=str(socket_path))
            child_env = dict(env, LD_PRELOAD=str(shim), VCACHE_DAEMON_READY_FD=str(write_fd),
                             VCACHE_TEST_READY_WRITE=name)
            if name == 'closed-reader':
                os.close(read_fd)
                read_fd = -1
            process = subprocess.Popen([str(binary), '--daemon-foreground'], env=child_env,
                                       pass_fds=(write_fd,), stdout=subprocess.PIPE,
                                       stderr=subprocess.STDOUT)
            os.close(write_fd)
            reply = b''
            ended = read_fd < 0
            output = b''
            output_collected = False
            status_output = b''
            passed = False
            try:
                deadline = time.monotonic() + 5
                while not ended and time.monotonic() < deadline:
                    if select.select([read_fd], [], [], max(0, deadline - time.monotonic()))[0]:
                        data = os.read(read_fd, 4096)
                        reply += data
                        ended = not data
                expected_exit = 0
                expected = b'R'
                if name.startswith('error-'):
                    expected_exit = 1
                    expected = ('Ebind ' + str(socket_path) + ': ' +
                                os.strerror(errno.EADDRINUSE)).encode()
                elif name.startswith('lock'):
                    expected_exit = 3
                    expected = b'L'
                elif name in ('zero', 'failed', 'closed-reader'):
                    expected = b''
                if expected_exit == 0:
                    while True:
                        status = subprocess.run([str(binary), '--daemon-status'], env=env,
                                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                                timeout=5)
                        if (name != 'closed-reader' or status.returncode == 0 or
                                time.monotonic() >= deadline or process.poll() is not None):
                            break
                        time.sleep(0.01)
                    status_output = status.stdout
                    passed = status.returncode == 0
                    process.terminate()
                else:
                    passed = True
                output, _ = process.communicate(timeout=5)
                output_collected = True
                passed &= ended and reply == expected and process.returncode == expected_exit
                if 'eintr' in name or name == 'error-partial-interrupted':
                    passed &= b'injected EINTR\n' in output
                if 'short' in name or name == 'error-partial-interrupted':
                    passed &= b'injected short write\n' in output
            except subprocess.TimeoutExpired:
                output += b'readiness write did not terminate\n'
                passed = False
            finally:
                if read_fd >= 0:
                    os.close(read_fd)
                if process.poll() is None:
                    process.kill()
                if not output_collected:
                    remainder, _ = process.communicate()
                    output += remainder
            with (work / (name + '.log')).open('xb') as log:
                log.write(('reply={!r}\nexit={}\n'.format(reply, process.returncode)).encode())
                log.write(output + status_output)
            print(('PASS ' if passed else 'FAIL ') + 'daemon readiness ' + name, flush=True)
            failures += not passed
    return bool(failures)


if __name__ == '__main__':
    if not sys.platform.startswith('linux'):
        print('SKIP daemon readiness write injection requires Linux LD_PRELOAD')
        sys.exit(0)
    binary = Path(sys.argv[1]).resolve()
    if len(sys.argv) > 2:
        work = Path(sys.argv[2]).resolve()
        work.mkdir()
        sys.exit(run(binary, work))
    with tempfile.TemporaryDirectory(prefix='ready-write-') as directory:
        sys.exit(run(binary, Path(directory)))
