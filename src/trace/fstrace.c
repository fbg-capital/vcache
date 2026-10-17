/* SPDX-FileCopyrightText: 2026 Unto Labs
 * SPDX-License-Identifier: Apache-2.0
 *
 * LD_PRELOAD file-access tracer for link steps, and for the C/C++ compiles a
 * manifest state is recorded from.
 *
 * A link has no -E equivalent, so the only way to learn its true input set is
 * to watch it. A compile has a depfile, which names what it read but not what
 * the preprocessor looked for and did not find. This records both halves of
 * the dependency question:
 *
 *   R  a path that was opened or stat'd successfully -- its CONTENT is an input
 *   M  a path probed and not found (ENOENT)         -- its ABSENCE is an input
 *   P  an executable that ran                       -- part of the toolchain
 *   L  a shared object a process that ran loaded    -- part of the toolchain
 *   W  a path opened for writing only               -- an output; a C/C++
 *                                                       manifest drops it, the
 *                                                       link cache keeps it as
 *                                                       an input all the same
 *   D  a directory that was listed
 *
 * The second is the half a "record what was read" manifest cannot express, and
 * it is what makes a later hit unsound: -lfoo resolves to /usr/lib/libfoo.so
 * only because /opt/lib/libfoo.so was absent, so a manifest that records only
 * what it read will happily hit after someone installs the latter.
 *
 * Processes announce themselves from a constructor rather than by interposing
 * exec*. glibc's execvp calls execve internally without crossing the PLT, so
 * exec interposition sees only some of the tree: measured on one LTO link, it
 * caught 3 of the 11 distinct executables, while the constructor caught all 11.
 *
 * Writes go out through raw syscalls so tracing never re-enters our own hooks.
 */
#define _GNU_SOURCE
#include <dlfcn.h>
#include <dirent.h>
#include <errno.h>
#include <fcntl.h>
#include <limits.h>
#include <link.h>
#include <pthread.h>
#include <signal.h>
#include <stdarg.h>
#include <stdio.h>
#include <stdint.h>
#include <stdlib.h>
#include <string.h>
#include <sys/auxv.h>
#include <sys/mman.h>
#include <sys/resource.h>
#include <sys/stat.h>
#include <sys/syscall.h>
#include <time.h>
#include <unistd.h>

#include "format.h"

static int log_fd = -1;

/* Records must fit in one publication. A path is bounded by PATH_MAX, but the tracer
   also sees strings that merely occupy a path argument, so the bound is
   enforced rather than assumed. */
#define VCACHE_RECORD_MAX VCACHE_TRACE_RECORD_MAX

static int log_init_state;
static int log_init_lost_record;
enum trace_mode { VCACHE_TRACE_TEXT, VCACHE_TRACE_MAPPED, VCACHE_TRACE_REFUSED };
static enum trace_mode trace_mode;
static struct vcache_trace_header *trace_header;

/* Initial-exec TLS avoids a loader allocation from inside an interposed call.
   The library is loaded by LD_PRELOAD, before application threads exist. */
static __thread struct {
  volatile sig_atomic_t pid;
  volatile sig_atomic_t emitting;
  volatile sig_atomic_t fork_pending;
  char *chunk;
  uint32_t offset;
} trace_thread __attribute__((tls_model("initial-exec")));

#ifdef VCACHE_TRACE_TEST_HOOK
extern int vcache_trace_test_phase(const char *phase);
#define TRACE_PHASE(phase) vcache_trace_test_phase(phase)
#else
#define TRACE_PHASE(phase) 0
#endif

/* VCACHE_TRACE_LOG, or NULL when tracing is off. A relative name counts as
   unset: the log and its marker would land in whatever directory each process
   of the tree is in when it first records. Read once, by the constructor's P
   record, and kept: a process that later unsets or clears its environment
   must still be able to mark its trace incomplete. */
static char log_name[PATH_MAX];
static int log_name_state;  /* 0 unread, 1 in log_name, 2 tracing off */

static const char *log_path(void) {
  if (log_name_state == 0) {
    const char *p = getenv("VCACHE_TRACE_LOG");
    size_t n = p != NULL ? strlen(p) : 0;
    if (p != NULL && p[0] == '/' && n < sizeof log_name) {
      memcpy(log_name, p, n + 1);
      log_name_state = 1;
    } else {
      log_name_state = 2;
    }
  }
  return log_name_state == 1 ? log_name : NULL;
}

static void mark_incomplete(void);

static int ensure_log(void) {
  int state = __atomic_load_n(&log_init_state, __ATOMIC_ACQUIRE);
  if (state == -1) return log_fd >= 0 && trace_mode != VCACHE_TRACE_REFUSED;
  const int pid = (int)syscall(SYS_getpid);
  if ((state != 0 && state == pid) ||
      !__atomic_compare_exchange_n(&log_init_state, &state, pid, 0,
                                    __ATOMIC_ACQ_REL, __ATOMIC_ACQUIRE)) {
    __atomic_store_n(&log_init_lost_record, 1, __ATOMIC_RELEASE);
    if (__atomic_load_n(&log_init_state, __ATOMIC_ACQUIRE) == -1) mark_incomplete();
    return 0;
  }
  const char *p = log_path();
  if (p == NULL) {
    log_fd = -2;
    __atomic_store_n(&log_init_state, -1, __ATOMIC_RELEASE);
    return 0;
  }
  const char *format = getenv("VCACHE_TRACE_FORMAT");
  const int requested = format != NULL && strcmp(format, "mmap-v1") == 0;
  trace_mode = requested ? VCACHE_TRACE_REFUSED : VCACHE_TRACE_TEXT;
  log_fd = TRACE_PHASE("log-open") ? -1 :
      (int)syscall(SYS_openat, AT_FDCWD, p, O_RDWR | O_CREAT | O_CLOEXEC, 0600);
  struct vcache_trace_header header;
  long got = -1;
  if (log_fd >= 0) {
    do { got = syscall(SYS_read, log_fd, &header, sizeof header); }
    while (got < 0 && errno == EINTR);
  }
  if (got >= 8 && memcmp(header.magic, vcache_trace_header_v1.magic, 8) == 0) {
    trace_mode = VCACHE_TRACE_REFUSED;
#if defined(__LP64__) && defined(SYS_mmap) && defined(SYS_fallocate)
    struct stat info;
    if (got == sizeof header && __atomic_always_lock_free(4, 0) &&
        memcmp(&header, &vcache_trace_header_v1, 32) == 0 &&
        memcmp(header.padding, vcache_trace_header_v1.padding, sizeof header.padding) == 0 &&
        syscall(SYS_fstat, log_fd, &info) == 0 && info.st_size >= VCACHE_TRACE_HEADER_BYTES) {
      void *mapped = TRACE_PHASE("header-map") ? MAP_FAILED :
          (void *)syscall(SYS_mmap, NULL, VCACHE_TRACE_HEADER_BYTES,
                          PROT_READ | PROT_WRITE, MAP_SHARED, log_fd, 0);
      if (mapped != MAP_FAILED) {
        trace_header = mapped;
        if (requested) trace_mode = VCACHE_TRACE_MAPPED;
      }
    }
#endif
  } else if (!requested && log_fd >= 0) {
    if (syscall(SYS_fcntl, log_fd, F_SETFL, O_APPEND) != 0) log_fd = -2;
  }
  __atomic_store_n(&log_init_state, -1, __ATOMIC_RELEASE);
  if (log_fd < 0 || trace_mode == VCACHE_TRACE_REFUSED ||
      __atomic_load_n(&log_init_lost_record, __ATOMIC_ACQUIRE)) mark_incomplete();
  return log_fd >= 0 && trace_mode != VCACHE_TRACE_REFUSED;
}

/* What one write of the record returned, past EINTR: `n` when it all landed. */
static long write_once(const char *buf, size_t n) {
  for (;;) {
    long r = syscall(SYS_write, log_fd, buf, n);
    if (r < 0 && errno == EINTR) continue;
    return r;
  }
}

static int write_whole(const char *buf, size_t n) {
  return write_once(buf, n) == (long)n;
}

/* Set once any record finds a finite soft RLIMIT_FSIZE; from then on every
   record of the process goes through the lock below. A record that read an
   infinite limit while another thread had lifted it would otherwise write
   after that thread put the program's limit back. */
static int fsize_limit_seen;

/* The pid holding the window (lift, write, restore), or 0. The limit is
   process-wide: a second thread lifting inside the window would read the
   lifted value as the program's and restore that. A fork() child puts the
   program's limit back from an atfork handler (see fsize_before_fork). A child
   that a raw clone made inside a window finds its parent's pid at its first
   record and takes the lock over; in a new PID namespace its pid can equal the
   holder's, and it waits out the bound instead. A CLONE_VM child that is not a
   thread can take it over too and break the exclusion: a vfork child making
   hooked calls before exec, which POSIX leaves undefined and glibc's
   posix_spawn child never does. */
static int fsize_lock;

/* The open window, for a child that takes it over: the program's limit, and
   the lifted one the child's copy of the limits still holds if it was made
   after the lift. Accessed atomically, so the compiler keeps stores that only
   another process (the child) reads. A child that posix_spawn or a raw clone
   made inside the window keeps the lifted limit if it execs before it records,
   or if its memory was copied after the window closed: the kernel copies the
   limits first. */
static int fsize_window_open;
static struct rlimit64 fsize_window_program_limit;
static struct rlimit64 fsize_window_lifted;

/* A window lasts a few syscalls. A wait this long is a holder that cannot run,
   such as one starved by a real-time spinner on its CPU, so the record is
   written without lifting instead of hanging the program. */
#define VCACHE_FSIZE_LOCK_WAIT_NS 1000000000LL

/* Kept free under a finite limit: other processes append between the size
   read and the write, and a write that crosses the limit is cut short. */
#define VCACHE_FSIZE_MARGIN (16 * VCACHE_RECORD_MAX)

/* The kernel's sigset, large enough for every Linux ABI's _NSIG. */
struct kernel_sigset {
  uint64_t bits[2];
};
#define VCACHE_KERNEL_SIGSET_BYTES (_NSIG / 8)

static int prlimit_fsize(const struct rlimit64 *next, struct rlimit64 *now) {
  return (int)syscall(SYS_prlimit64, 0, RLIMIT_FSIZE, next, now);
}

static void store_rlimit(struct rlimit64 *to, const struct rlimit64 *from) {
  __atomic_store_n(&to->rlim_cur, from->rlim_cur, __ATOMIC_RELAXED);
  __atomic_store_n(&to->rlim_max, from->rlim_max, __ATOMIC_RELAXED);
}

static struct rlimit64 load_rlimit(const struct rlimit64 *from) {
  struct rlimit64 value = {__atomic_load_n(&from->rlim_cur, __ATOMIC_RELAXED),
                           __atomic_load_n(&from->rlim_max, __ATOMIC_RELAXED)};
  return value;
}

static void fsize_take_over_window(void) {
  if (!__atomic_load_n(&fsize_window_open, __ATOMIC_ACQUIRE)) return;
  __atomic_store_n(&fsize_window_open, 0, __ATOMIC_RELAXED);
  const struct rlimit64 lifted = load_rlimit(&fsize_window_lifted);
  const struct rlimit64 program_limit = load_rlimit(&fsize_window_program_limit);
  struct rlimit64 now;
  if (prlimit_fsize(NULL, &now) == 0 && now.rlim_cur == lifted.rlim_cur &&
      now.rlim_max == lifted.rlim_max) {
    prlimit_fsize(&program_limit, NULL);
  }
}

static long long monotonic_ns(void) {
  struct timespec now = {0, 0};
  syscall(SYS_clock_gettime, CLOCK_MONOTONIC, &now);
  return (long long)now.tv_sec * 1000000000LL + now.tv_nsec;
}

/* 1 when the lock was taken, 0 when the wait ran out. */
static int fsize_lock_acquire(int self_pid) {
  long long deadline_ns = -1;
  for (unsigned yields = 0;; ++yields) {
    int holder = 0;
    if (__atomic_compare_exchange_n(&fsize_lock, &holder, self_pid, 0, __ATOMIC_ACQUIRE,
                                    __ATOMIC_RELAXED)) {
      return 1;
    }
    if (holder != self_pid &&
        __atomic_compare_exchange_n(&fsize_lock, &holder, self_pid, 0, __ATOMIC_ACQUIRE,
                                    __ATOMIC_RELAXED)) {
      fsize_take_over_window();
      return 1;
    }
    if (yields % 64 == 0) {
      long long now_ns = monotonic_ns();
      if (deadline_ns < 0) {
        deadline_ns = now_ns + VCACHE_FSIZE_LOCK_WAIT_NS;
      } else if (now_ns >= deadline_ns) {
        return 0;
      }
    }
    syscall(SYS_sched_yield);
  }
}

static void fsize_lock_release(int self_pid) {
  int holder = self_pid;
  __atomic_compare_exchange_n(&fsize_lock, &holder, 0, 0, __ATOMIC_RELEASE, __ATOMIC_RELAXED);
}

/* fork() copies the limits and the memory at different moments while other
   threads run, so a child made during a window can hold the lifted limit with
   a memory that already says the window closed; other threads' stores can
   even be missing from it, so no window count or flag can confirm the lift.
   The forking thread's own stores are always there. So fork's prepare step
   reads the program's limit under the lock, when no window has it lifted, and
   the child puts that limit back if its own copy is the lifted form of it. A
   program thread that raises its soft limit to the hard one during the fork
   leaves the child the limit from before, which is one outcome of that race.
   The lock is held only for the read: held across fork it would invert with
   glibc's atfork lock, which a thread holds with signals open while its
   handler records. The reading is a seqlock written under the lock, so a child
   copied mid-write by a concurrent fork falls back to the window it finds. A
   prepare whose wait ran out leaves the previous reading. */
static unsigned fsize_fork_reading_seq;
static int fsize_fork_program_limit_known;
static struct rlimit64 fsize_fork_program_limit;

static void fsize_before_fork(void) {
  if (!__atomic_load_n(&fsize_limit_seen, __ATOMIC_SEQ_CST)) return;
  struct kernel_sigset all_signals, program_mask;
  memset(&all_signals, 0xff, sizeof all_signals);
  memset(&program_mask, 0, sizeof program_mask);
  const int has_blocked_signals =
      syscall(SYS_rt_sigprocmask, SIG_SETMASK, &all_signals, &program_mask,
              VCACHE_KERNEL_SIGSET_BYTES) == 0;
  const int self_pid = (int)syscall(SYS_getpid);
  if (fsize_lock_acquire(self_pid)) {
    struct rlimit64 program_limit;
    const int is_read = prlimit_fsize(NULL, &program_limit) == 0;
    __atomic_fetch_add(&fsize_fork_reading_seq, 1, __ATOMIC_SEQ_CST);
    if (is_read) store_rlimit(&fsize_fork_program_limit, &program_limit);
    __atomic_store_n(&fsize_fork_program_limit_known, is_read, __ATOMIC_RELAXED);
    __atomic_fetch_add(&fsize_fork_reading_seq, 1, __ATOMIC_SEQ_CST);
    fsize_lock_release(self_pid);
  }
  if (has_blocked_signals) {
    syscall(SYS_rt_sigprocmask, SIG_SETMASK, &program_mask, NULL, VCACHE_KERNEL_SIGSET_BYTES);
  }
}

/* 1 when the child's copy of memory holds a whole reading from a prepare. */
static int fsize_load_fork_reading(struct rlimit64 *program_limit) {
  const unsigned seq = __atomic_load_n(&fsize_fork_reading_seq, __ATOMIC_SEQ_CST);
  if ((seq & 1) != 0 || !__atomic_load_n(&fsize_fork_program_limit_known, __ATOMIC_RELAXED)) {
    return 0;
  }
  *program_limit = load_rlimit(&fsize_fork_program_limit);
  return __atomic_load_n(&fsize_fork_reading_seq, __ATOMIC_SEQ_CST) == seq;
}

static void trace_after_fork_in_child(void) {
  const int saved = errno;
  if (trace_thread.emitting) {
    /* A handler may return into an interrupted emitter with live pointers
       into its inherited chunk. Keep that mapping, but refuse this trace. */
    trace_thread.pid = (int)syscall(SYS_getpid);
    mark_incomplete();
  } else {
    /* A child in a new PID namespace can have the parent's numeric PID. */
    trace_thread.pid = 0;
  }
  trace_thread.fork_pending = 0;
  errno = saved;
}

static void after_fork_in_child(void) {
  trace_after_fork_in_child();
  struct rlimit64 program_limit, now;
  if (fsize_load_fork_reading(&program_limit)) {
    if (program_limit.rlim_cur < program_limit.rlim_max && prlimit_fsize(NULL, &now) == 0 &&
        now.rlim_cur == program_limit.rlim_max && now.rlim_max == program_limit.rlim_max) {
      prlimit_fsize(&program_limit, NULL);
    }
  } else if (__atomic_load_n(&fsize_lock, __ATOMIC_ACQUIRE) != 0) {
    fsize_take_over_window();
  }
  __atomic_store_n(&fsize_window_open, 0, __ATOMIC_RELAXED);
  __atomic_store_n(&fsize_lock, 0, __ATOMIC_RELEASE);
}

/* Room under `limit` for `n` more bytes and the margin; an unknown size counts
   as room, since a failed write is still caught. */
static int has_room(size_t n, rlim64_t limit) {
  if (limit == RLIM64_INFINITY) return 1;
  long log_size = syscall(SYS_lseek, log_fd, 0, SEEK_END);
  return log_size < 0 || (uint64_t)log_size + n + VCACHE_FSIZE_MARGIN <= limit;
}

/* write_whole for a caller that has blocked every signal, saving the program's
   mask in `program_mask` (NULL when blocking failed). A SIGXFSZ that this
   write's EFBIG raised is then pending on this thread, and is taken back
   unless the program's own mask blocks it too, when it may be the program's. */
static int write_whole_unsignalled(const char *buf, size_t n,
                                   const struct kernel_sigset *program_mask) {
  long r = write_once(buf, n);
  if (r == (long)n) return 1;
  const uint64_t xfsz_bit = 1ULL << (SIGXFSZ - 1);
  if (r < 0 && errno == EFBIG && program_mask != NULL &&
      (program_mask->bits[0] & xfsz_bit) == 0) {
    const struct kernel_sigset xfsz_only = {{xfsz_bit, 0}};
    const struct timespec no_wait = {0, 0};
    syscall(SYS_rt_sigtimedwait, &xfsz_only, NULL, &no_wait, VCACHE_KERNEL_SIGSET_BYTES);
  }
  return 0;
}

/* A program may lower its soft RLIMIT_FSIZE to inject write failures, and the
   log is usually past it already. The kernel would then fail every append
   with EFBIG and raise SIGXFSZ, killing a program that left it at the default,
   or cut a record that straddles the limit short. So the soft limit is lifted
   to the hard one before the write rather than after a failure, and the
   program's value is put back after it. Its own writes in other threads see
   the lifted limit for that window. Every signal is blocked across it, so no
   handler can run, longjmp out or park the thread while it holds the lock.
   A record with no room under the limit in force is dropped unwritten.
   1 when the whole record landed. */
static int append_record(const char *buf, size_t n) {
  struct rlimit64 program_limit;
  if (!__atomic_load_n(&fsize_limit_seen, __ATOMIC_SEQ_CST)) {
    if (prlimit_fsize(NULL, &program_limit) != 0) return write_whole(buf, n);
    if (program_limit.rlim_cur == RLIM64_INFINITY &&
        !__atomic_load_n(&fsize_limit_seen, __ATOMIC_SEQ_CST)) {
      return write_whole(buf, n);
    }
    __atomic_store_n(&fsize_limit_seen, 1, __ATOMIC_SEQ_CST);
  }

  struct kernel_sigset all_signals, program_mask;
  memset(&all_signals, 0xff, sizeof all_signals);
  memset(&program_mask, 0, sizeof program_mask);
  const int has_blocked_signals =
      syscall(SYS_rt_sigprocmask, SIG_SETMASK, &all_signals, &program_mask,
              VCACHE_KERNEL_SIGSET_BYTES) == 0;
  const struct kernel_sigset *blocked_from = has_blocked_signals ? &program_mask : NULL;
  int self_pid = (int)syscall(SYS_getpid);
  int owns_lock = fsize_lock_acquire(self_pid);
  int written = 0;
  if (prlimit_fsize(NULL, &program_limit) != 0) {
    written = write_whole_unsignalled(buf, n, blocked_from);
  } else if (!owns_lock || program_limit.rlim_cur == RLIM64_INFINITY ||
             program_limit.rlim_cur >= program_limit.rlim_max) {
    if (has_room(n, program_limit.rlim_cur)) {
      written = write_whole_unsignalled(buf, n, blocked_from);
    }
  } else {
    const struct rlimit64 lifted = {program_limit.rlim_max, program_limit.rlim_max};
    store_rlimit(&fsize_window_program_limit, &program_limit);
    store_rlimit(&fsize_window_lifted, &lifted);
    __atomic_store_n(&fsize_window_open, 1, __ATOMIC_RELEASE);
    if (prlimit_fsize(&lifted, &program_limit) == 0) {
      store_rlimit(&fsize_window_program_limit, &program_limit);
      if (has_room(n, lifted.rlim_cur)) written = write_whole_unsignalled(buf, n, blocked_from);
      struct rlimit64 during;
      if (prlimit_fsize(&program_limit, &during) == 0 &&
          (during.rlim_cur != lifted.rlim_cur || during.rlim_max != lifted.rlim_max)) {
        /* The program set its own limit inside the window; that one stands. */
        prlimit_fsize(&during, NULL);
      }
    } else if (has_room(n, program_limit.rlim_cur)) {
      written = write_whole_unsignalled(buf, n, blocked_from);
    }
    __atomic_store_n(&fsize_window_open, 0, __ATOMIC_RELEASE);
  }
  if (owns_lock) fsize_lock_release(self_pid);
  if (has_blocked_signals) {
    syscall(SYS_rt_sigprocmask, SIG_SETMASK, &program_mask, NULL, VCACHE_KERNEL_SIGSET_BYTES);
  }
  return written;
}

/* A "!" line, which readers refuse the whole log on. It starts with a newline
   of its own: a torn record has none, and "R\t/x/ab!" would read as a record. */
static void write_incomplete_sentinel(void) {
  if (!ensure_log() || trace_mode != VCACHE_TRACE_TEXT) return;
  append_record("\n!\n", 3);
}

/* A lost record is not a lost log line: it is an input missing from the set
   that decides whether a later hit is sound. So any failure to record is made
   loud by creating "<log>.incomplete", which the caller checks before storing
   an entry. A marker whose name does not fit, or that its directory refuses,
   would leave the log looking whole -- possibly with other processes' P
   records in it -- so the shared flag or text sentinel is its backstop. */
static void mark_incomplete(void) {
  if (__atomic_load_n(&log_init_state, __ATOMIC_ACQUIRE) != -1) {
    __atomic_store_n(&log_init_lost_record, 1, __ATOMIC_RELEASE);
    if (!ensure_log()) return;
  }
  if (trace_header != NULL) {
    __atomic_store_n(&trace_header->incomplete, 1, __ATOMIC_RELEASE);
  } else if (trace_mode != VCACHE_TRACE_TEXT && log_fd >= 0) {
    const uint32_t incomplete = 1;
    /* The header was allocated by the parent, so this does not need another
       block even when extent allocation or header mapping failed. */
    struct kernel_sigset blocked = {{1ULL << (SIGXFSZ - 1), 0}}, previous = {{0, 0}};
    const int masked = syscall(SYS_rt_sigprocmask, SIG_BLOCK, &blocked, &previous,
                                VCACHE_KERNEL_SIGSET_BYTES) == 0;
    long result;
    do { result = syscall(SYS_pwrite64, log_fd, &incomplete, sizeof incomplete, 36); }
    while (result < 0 && errno == EINTR);
    const int write_error = result < 0 ? errno : 0;
    /* A child can exec under a hard size limit below the flag's offset.
       Revoking read permission survives both that limit and failed mmap. */
    if (result != sizeof incomplete) syscall(SYS_fchmod, log_fd, 0);
    if (masked) {
      if (write_error == EFBIG && !(previous.bits[0] & blocked.bits[0])) {
        const struct timespec no_wait = {0, 0};
        syscall(SYS_rt_sigtimedwait, &blocked, NULL, &no_wait, VCACHE_KERNEL_SIGSET_BYTES);
      }
      syscall(SYS_rt_sigprocmask, SIG_SETMASK, &previous, NULL, VCACHE_KERNEL_SIGSET_BYTES);
    }
  }
  const char *log = log_path();
  if (log == NULL) return;
  if (trace_mode != VCACHE_TRACE_TEXT && log_fd < 0) {
    syscall(SYS_fchmodat, AT_FDCWD, log, 0);
  }
  static const char suffix[] = ".incomplete";
  char marker[PATH_MAX];
  size_t n = strlen(log);
  if (n + sizeof suffix <= sizeof marker) {
    memcpy(marker, log, n);
    memcpy(marker + n, suffix, sizeof suffix);
    int fd = TRACE_PHASE("marker") ? -1 :
        (int)syscall(SYS_openat, AT_FDCWD, marker, O_WRONLY | O_CREAT | O_CLOEXEC, 0600);
    if (fd >= 0) { syscall(SYS_close, fd); return; }
  }
  if (trace_mode == VCACHE_TRACE_TEXT) write_incomplete_sentinel();
}

static int allocate_trace_chunk(uint64_t offset) {
#if defined(__LP64__) && defined(SYS_fallocate)
  struct kernel_sigset blocked = {{1ULL << (SIGXFSZ - 1), 0}}, previous = {{0, 0}};
  if (syscall(SYS_rt_sigprocmask, SIG_BLOCK, &blocked, &previous,
              VCACHE_KERNEL_SIGSET_BYTES) != 0) return 0;
  long result;
  do {
    result = TRACE_PHASE("allocate") ? -1 :
        syscall(SYS_fallocate, log_fd, 0, offset, (uint64_t)VCACHE_TRACE_CHUNK_BYTES);
  } while (result < 0 && errno == EINTR);
  if (result < 0 && errno == EFBIG && !(previous.bits[0] & blocked.bits[0])) {
    const struct timespec no_wait = {0, 0};
    syscall(SYS_rt_sigtimedwait, &blocked, NULL, &no_wait, VCACHE_KERNEL_SIGSET_BYTES);
  }
  syscall(SYS_rt_sigprocmask, SIG_SETMASK, &previous, NULL, VCACHE_KERNEL_SIGSET_BYTES);
  return result == 0;
#else
  return 0;
#endif
}

static int append_mapped_record(const char *buf, uint32_t length) {
#if defined(__LP64__) && defined(SYS_mmap)
  if (__atomic_load_n(&trace_header->incomplete, __ATOMIC_ACQUIRE)) return 0;
  const uint32_t bytes = (4 + length + 7) & ~7U;
  if (trace_thread.chunk == NULL || trace_thread.offset > VCACHE_TRACE_CHUNK_BYTES - bytes) {
    uint32_t chunk = __atomic_load_n(&trace_header->reserved_chunks, __ATOMIC_ACQUIRE);
    do {
      if (chunk >= VCACHE_TRACE_MAX_CHUNKS) return 0;
    } while (!__atomic_compare_exchange_n(&trace_header->reserved_chunks, &chunk, chunk + 1,
                                          0, __ATOMIC_ACQ_REL, __ATOMIC_ACQUIRE));
    if (TRACE_PHASE("reserved")) return 0;
    const uint64_t offset = VCACHE_TRACE_HEADER_BYTES + (uint64_t)chunk * VCACHE_TRACE_CHUNK_BYTES;
    if (!allocate_trace_chunk(offset)) return 0;
    if (TRACE_PHASE("allocated")) return 0;
    void *mapped = TRACE_PHASE("chunk-map") ? MAP_FAILED :
        (void *)syscall(SYS_mmap, NULL, VCACHE_TRACE_CHUNK_BYTES,
                        PROT_READ | PROT_WRITE, MAP_SHARED, log_fd, offset);
    if (mapped == MAP_FAILED) return 0;
    if (trace_thread.chunk != NULL) {
      syscall(SYS_munmap, trace_thread.chunk, VCACHE_TRACE_CHUNK_BYTES);
    }
    trace_thread.chunk = mapped;
    trace_thread.offset = VCACHE_TRACE_CHUNK_HEADER_BYTES;
  }
  uint32_t *state = (uint32_t *)trace_thread.chunk;
  __atomic_store_n(state, VCACHE_TRACE_CHUNK_WRITING, __ATOMIC_SEQ_CST);
  if (TRACE_PHASE("writing")) return 0;
  uint32_t *record = (uint32_t *)(trace_thread.chunk + trace_thread.offset);
  __atomic_store_n(record, length | VCACHE_TRACE_RECORD_WRITING, __ATOMIC_SEQ_CST);
  if (TRACE_PHASE("record-start")) return 0;
#ifdef VCACHE_TRACE_TEST_HOOK
  memcpy((char *)record + 4, buf, length / 2);
  if (TRACE_PHASE("payload-half")) return 0;
  memcpy((char *)record + 4 + length / 2, buf + length / 2, length - length / 2);
#else
  memcpy((char *)record + 4, buf, length);
#endif
  if (TRACE_PHASE("payload")) return 0;
  __atomic_store_n(record, length, __ATOMIC_RELEASE);
  if (TRACE_PHASE("record-commit")) return 0;
  trace_thread.offset += bytes;
  __atomic_store_n(state, VCACHE_TRACE_CHUNK_COMMITTED, __ATOMIC_RELEASE);
  return 1;
#else
  return 0;
#endif
}

/* The working directory, written to `out` with a trailing '/', and its length;
   0 when the kernel cannot name it as a path: removed, or outside this
   process's root, which it spells "(unreachable)/...". */
static size_t cwd_prefix(char *out, size_t size) {
  long n = syscall(SYS_getcwd, out, size - 1);
  if (n <= 1 || out[0] != '/') return 0;
  size_t used = (size_t)n - 1;
  if (out[used - 1] != '/') out[used++] = '/';
  return used;
}

/* glibc declares the path of statx, fstatat and friends nonnull, though the
   kernel takes NULL with AT_EMPTY_PATH and Rust's File::metadata passes it.
   A compiler drops a NULL test on a parameter so declared (el10's glibc 2.39
   headers, gcc 16); hiding where the pointer came from keeps every test below. */
static const char *maybe_null(const char *path) {
  __asm__("" : "+r"(path));
  return path;
}

static void emit_record(char kind, const char *path) {
  path = maybe_null(path);
  if (path == NULL || *path == '\0') return;
  if (!ensure_log()) return;
  if (trace_header != NULL && __atomic_load_n(&trace_header->incomplete, __ATOMIC_ACQUIRE)) return;

  char buf[VCACHE_RECORD_MAX];
  size_t n = 0;
  buf[n++] = kind;
  buf[n++] = '\t';
  const size_t path_begin = n;
  if (*path != '/') {
    /* A relative path names a file in the directory this process is in now,
       which no reader can know: the whole tree appends to one log, and any
       process may have changed directory since it started. glibc changes it
       off the PLT too (fts, ftw, posix_spawn), so the directory is asked for
       on every record rather than remembered. */
    size_t used = cwd_prefix(buf + n, sizeof buf - n);
    if (used == 0) {
      mark_incomplete();
      return;
    }
    n += used;
  }
  const char *q = path;
  for (; *q != '\0' && n < sizeof buf - 1; ++q) buf[n++] = *q;
  if (memchr(buf + path_begin, '\n', n - path_begin) != NULL ||
      memchr(buf + path_begin, '\t', n - path_begin) != NULL) {
    /* The line protocol cannot represent these bytes. Substitution would
       name a different file and make later validation unsound. */
    mark_incomplete();
    return;
  }
  if (*q != '\0') {
    /* Truncating would record a path that is not the one that was touched,
       and validating it later would check the wrong file. Drop the record and
       say so instead. */
    mark_incomplete();
    return;
  }
  buf[n++] = '\n';

  /* In text mode, one record is one write. The process tree appends to one regular
     file, where O_APPEND makes each write land whole and contiguous; a second
     write for the rest of a short one can land after another process's record
     and tear both. So a short write is a lost record, and is made loud. */
  if (!(trace_mode == VCACHE_TRACE_MAPPED ? append_mapped_record(buf, (uint32_t)n)
                                        : append_record(buf, n))) {
    mark_incomplete();
  }
}

static void emit(char kind, const char *path) {
  if (__atomic_load_n(&log_init_state, __ATOMIC_ACQUIRE) == -1 &&
      trace_mode == VCACHE_TRACE_TEXT) {
    emit_record(kind, path);
    return;
  }
  if (trace_thread.fork_pending) {
    mark_incomplete();
    return;
  }
  const int pid = (int)syscall(SYS_getpid);
  if (trace_thread.pid != pid) {
    struct kernel_sigset all_signals, previous;
    memset(&all_signals, 0xff, sizeof all_signals);
    if (syscall(SYS_rt_sigprocmask, SIG_SETMASK, &all_signals, &previous,
                VCACHE_KERNEL_SIGSET_BYTES) != 0) {
      mark_incomplete();
      return;
    }
    const int interrupted = trace_thread.emitting;
    if (interrupted) {
      trace_thread.pid = pid;
      mark_incomplete();
    } else if (trace_thread.pid != pid) {
      char *inherited = trace_thread.chunk;
      trace_thread.chunk = NULL;
      trace_thread.offset = 0;
      trace_thread.pid = pid;
      if (inherited != NULL) syscall(SYS_munmap, inherited, VCACHE_TRACE_CHUNK_BYTES);
    }
    syscall(SYS_rt_sigprocmask, SIG_SETMASK, &previous, NULL, VCACHE_KERNEL_SIGSET_BYTES);
    if (interrupted) return;
  }
  if (trace_thread.emitting) {
    mark_incomplete();
    return;
  }
  trace_thread.emitting = 1;
  emit_record(kind, path);
  trace_thread.emitting = 0;
}

/* Only ENOENT counts as a negative input. A permission error is a different
   question, and treating it as "absent" would invalidate entries for reasons
   that have nothing to do with the link. */
static void emit_result(const char *path, int failed, int saved_errno) {
  if (!failed) { emit('R', path); return; }
  if (saved_errno == ENOENT) emit('M', path);
}

/* An open that cannot read says nothing about the file's content as an input.
   Read-write opens stay R: the process may read what it finds. */
static void emit_open_result(const char *path, int failed, int saved_errno,
                             int written_only) {
  if (!failed && written_only) { emit('W', path); return; }
  emit_result(path, failed, saved_errno);
}

static int fopen_mode_written_only(const char *mode) {
  return mode != NULL && (mode[0] == 'w' || mode[0] == 'a') && strchr(mode, '+') == NULL;
}

/* The directory `dirfd` names, written to `out` with a trailing '/', and its
   length; 0 when it cannot be named as a path: a descriptor that is not a
   directory in this namespace (a socket, an anonymous inode), or a directory
   since removed, which the kernel spells "<path> (deleted)" and which no
   reader could find again. */
static size_t dirfd_prefix(int dirfd, char *out, size_t size) {
  char proc_path[64];
  int pn = snprintf(proc_path, sizeof proc_path, "/proc/self/fd/%d", dirfd);
  if (pn <= 0 || (size_t)pn >= sizeof proc_path) return 0;
  long n = syscall(SYS_readlinkat, AT_FDCWD, proc_path, out, size - 1);
  if (n <= 0 || (size_t)n >= size - 1 || out[0] != '/') return 0;
  static const char removed[] = " (deleted)";
  size_t used = (size_t)n;
  if (used >= sizeof removed - 1 &&
      memcmp(out + used - (sizeof removed - 1), removed, sizeof removed - 1) == 0) {
    return 0;
  }
  if (out[used - 1] != '/') out[used++] = '/';
  return used;
}

/* `path` appended to the `used` bytes already in `out`; 0 when it does not fit. */
static int append_path(char *out, size_t used, size_t size, const char *path) {
  size_t rest = strlen(path);
  if (used + rest >= size) return 0;
  memcpy(out + used, path, rest + 1);
  return 1;
}

/* The record path of a *at call. A relative path names a file in the
   directory `dirfd` named when the call was made, so that directory is read
   before the call: afterwards another thread may have closed and reused the
   descriptor, or renamed the directory. Treating the path as cwd-relative
   instead records a different file and can omit the real input. */
struct at_path {
  char buf[VCACHE_RECORD_MAX];
  size_t dir_used;
  int dir_known;
};

static void at_path_begin(struct at_path *at, int dirfd, const char *path) {
  path = maybe_null(path);
  int saved = errno;
  at->dir_used = 0;
  at->dir_known = path != NULL && *path != '\0';
  if (at->dir_known && *path != '/' && dirfd != AT_FDCWD) {
    at->dir_used = dirfd_prefix(dirfd, at->buf, sizeof at->buf);
    at->dir_known = at->dir_used > 0;
  }
  errno = saved;
}

static void emit_at_result(struct at_path *at, const char *path, int failed,
                           int saved_errno, int written_only) {
  path = maybe_null(path);
  /* An empty path names no file: without AT_EMPTY_PATH it is ENOENT by
     definition, with it the descriptor's file was recorded at its open. */
  if (path == NULL || *path == '\0') return;
  if (failed && saved_errno != ENOENT) return;
  if (!at->dir_known || !append_path(at->buf, at->dir_used, sizeof at->buf, path)) {
    mark_incomplete();
    return;
  }
  emit_open_result(at->buf, failed, saved_errno, written_only);
}

/* A stat of the descriptor itself, as Rust's File::metadata makes with
   statx(fd, "", AT_EMPTY_PATH). The trace follows names, not descriptors: the
   file was recorded when the descriptor was opened, as it is for a read(). */
static int stats_descriptor(const char *path, int flags) {
  path = maybe_null(path);
  return (path == NULL || *path == '\0') && (flags & AT_EMPTY_PATH) != 0;
}

#define NEXT(sym)                                     \
  ({                                                  \
    static __typeof__(&sym) next_##sym;                   \
    if (next_##sym == NULL)                           \
      next_##sym = (__typeof__(&sym))dlsym(RTLD_NEXT, #sym); \
    next_##sym;                                       \
  })

typedef pid_t (*fork_function)(void);

static fork_function resolve_fork(void) {
  return NEXT(fork);
}

pid_t fork(void) {
  const int entry_errno = errno;
  fork_function real_fork = resolve_fork();
  if (real_fork == NULL) { errno = ENOSYS; return -1; }
  const sig_atomic_t previous = trace_thread.fork_pending;
  trace_thread.fork_pending = 1;
  errno = entry_errno;
  const pid_t child = real_fork();
  const int saved = errno;
  /* Reset ownership even when registered handlers were not run. */
  if (child == 0) trace_after_fork_in_child();
  trace_thread.fork_pending = previous;
  errno = saved;
  return child;
}

/* Our own load address, so we can leave ourselves out of the input set.
   Compared by base rather than by name: dladdr and dl_iterate_phdr can spell
   the same object differently, and a name comparison would quietly stop
   matching. */
static ElfW(Addr) self_base(void) {
  static ElfW(Addr) base;
  static int resolved;
  if (!resolved) {
    resolved = 1;
    Dl_info info;
    if (dladdr((void *)(uintptr_t)&self_base, &info) != 0) {
      base = (ElfW(Addr))(uintptr_t)info.dli_fbase;
    }
  }
  return base;
}

/* The kernel's vDSO, which ld.so lists under a bare name but is no file. */
static int is_vdso(const struct dl_phdr_info *info) {
  uintptr_t ehdr = (uintptr_t)getauxval(AT_SYSINFO_EHDR);
  if (ehdr == 0) return 0;
  return (uintptr_t)info->dlpi_phdr == ehdr + ((const ElfW(Ehdr) *)ehdr)->e_phoff;
}

static int announce_object(struct dl_phdr_info *info, size_t size, void *data) {
  (void)size;
  (void)data;
  /* The main program, announced as P. */
  if (info->dlpi_name == NULL || info->dlpi_name[0] == '\0') return 0;

  /* The tracer observes the link; it does not take part in it. Recording
     ourselves would make every rebuild of vcache invalidate every link entry,
     for a file that cannot affect the output. */
  if (info->dlpi_addr == self_base()) return 0;
  if (is_vdso(info)) return 0;

  /* Dependencies are loaded before interposition is active. Include every
     other already-loaded DSO: for the common linkers the implementation is a
     shared library -- ld.bfd's logic lives in libbfd, lld's in libLLVM -- and
     libz/libzstd emit output bytes directly for compressed debug sections.
     Its own kind, so a reader that keys the toolchain another way can tell
     these apart from the files the process opened. */
  if (info->dlpi_name[0] == '/') {
    emit('L', info->dlpi_name);
    return 0;
  }
  /* Found through a relative LD_LIBRARY_PATH or DT_RUNPATH entry, the name is
     the one ld.so opened, relative to the directory the process started in. */
  char resolved[VCACHE_RECORD_MAX];
  size_t used = cwd_prefix(resolved, sizeof resolved);
  if (used == 0 || !append_path(resolved, used, sizeof resolved, info->dlpi_name)) {
    mark_incomplete();
    return 0;
  }
  emit('L', resolved);
  return 0;
}

__attribute__((constructor)) static void announce_self(void) {
  (void)resolve_fork();
  pthread_atfork(fsize_before_fork, NULL, after_fork_in_child);
  char exe[4096];
  long n = syscall(SYS_readlinkat, AT_FDCWD, "/proc/self/exe", exe, sizeof exe - 1);
  if (n > 0) { exe[n] = '\0'; emit('P', exe); }
  dl_iterate_phdr(announce_object, NULL);
}

int open(const char *path, int flags, ...) {
  mode_t m = 0;
  if (flags & O_CREAT) { va_list a; va_start(a, flags); m = (mode_t)va_arg(a, int); va_end(a); }
  int r = NEXT(open)(path, flags, m);
  int saved = errno;
  emit_open_result(path, r < 0, saved, (flags & O_ACCMODE) == O_WRONLY);
  errno = saved;
  return r;
}

int open64(const char *path, int flags, ...) {
  mode_t m = 0;
  if (flags & O_CREAT) { va_list a; va_start(a, flags); m = (mode_t)va_arg(a, int); va_end(a); }
  int r = NEXT(open64)(path, flags, m);
  int saved = errno;
  emit_open_result(path, r < 0, saved, (flags & O_ACCMODE) == O_WRONLY);
  errno = saved;
  return r;
}

int openat(int dirfd, const char *path, int flags, ...) {
  mode_t m = 0;
  if (flags & O_CREAT) { va_list a; va_start(a, flags); m = (mode_t)va_arg(a, int); va_end(a); }
  struct at_path at;
  at_path_begin(&at, dirfd, path);
  int r = NEXT(openat)(dirfd, path, flags, m);
  int saved = errno;
  emit_at_result(&at, path, r < 0, saved, (flags & O_ACCMODE) == O_WRONLY);
  errno = saved;
  return r;
}

int openat64(int dirfd, const char *path, int flags, ...) {
  mode_t m = 0;
  if (flags & O_CREAT) { va_list a; va_start(a, flags); m = (mode_t)va_arg(a, int); va_end(a); }
  struct at_path at;
  at_path_begin(&at, dirfd, path);
  int r = NEXT(openat64)(dirfd, path, flags, m);
  int saved = errno;
  emit_at_result(&at, path, r < 0, saved, (flags & O_ACCMODE) == O_WRONLY);
  errno = saved;
  return r;
}

/* What open and openat become under _FORTIFY_SOURCE with a mode-less call. */
int __open_2(const char *path, int flags);
int __open64_2(const char *path, int flags);
int __openat_2(int dirfd, const char *path, int flags);
int __openat64_2(int dirfd, const char *path, int flags);

int __open_2(const char *path, int flags) {
  int r = NEXT(__open_2)(path, flags);
  int saved = errno;
  emit_open_result(path, r < 0, saved, (flags & O_ACCMODE) == O_WRONLY);
  errno = saved;
  return r;
}

int __open64_2(const char *path, int flags) {
  int r = NEXT(__open64_2)(path, flags);
  int saved = errno;
  emit_open_result(path, r < 0, saved, (flags & O_ACCMODE) == O_WRONLY);
  errno = saved;
  return r;
}

int __openat_2(int dirfd, const char *path, int flags) {
  struct at_path at;
  at_path_begin(&at, dirfd, path);
  int r = NEXT(__openat_2)(dirfd, path, flags);
  int saved = errno;
  emit_at_result(&at, path, r < 0, saved, (flags & O_ACCMODE) == O_WRONLY);
  errno = saved;
  return r;
}

int __openat64_2(int dirfd, const char *path, int flags) {
  struct at_path at;
  at_path_begin(&at, dirfd, path);
  int r = NEXT(__openat64_2)(dirfd, path, flags);
  int saved = errno;
  emit_at_result(&at, path, r < 0, saved, (flags & O_ACCMODE) == O_WRONLY);
  errno = saved;
  return r;
}

FILE *fopen(const char *path, const char *mode) {
  FILE *r = NEXT(fopen)(path, mode);
  int saved = errno;
  emit_open_result(path, r == NULL, saved, fopen_mode_written_only(mode));
  errno = saved;
  return r;
}

FILE *fopen64(const char *path, const char *mode) {
  FILE *r = NEXT(fopen64)(path, mode);
  int saved = errno;
  emit_open_result(path, r == NULL, saved, fopen_mode_written_only(mode));
  errno = saved;
  return r;
}

int stat(const char *path, struct stat *st) {
  int r = NEXT(stat)(path, st);
  int saved = errno;
  emit_result(path, r != 0, saved);
  errno = saved;
  return r;
}

int lstat(const char *path, struct stat *st) {
  int r = NEXT(lstat)(path, st);
  int saved = errno;
  emit_result(path, r != 0, saved);
  errno = saved;
  return r;
}

int access(const char *path, int mode) {
  int r = NEXT(access)(path, mode);
  int saved = errno;
  emit_result(path, r != 0, saved);
  errno = saved;
  return r;
}

/* gnulib and coreutils probe with these; eaccess is a separate symbol. */
int euidaccess(const char *path, int mode) {
  int r = NEXT(euidaccess)(path, mode);
  int saved = errno;
  emit_result(path, r != 0, saved);
  errno = saved;
  return r;
}

int eaccess(const char *path, int mode) {
  int r = NEXT(eaccess)(path, mode);
  int saved = errno;
  emit_result(path, r != 0, saved);
  errno = saved;
  return r;
}

int faccessat(int dirfd, const char *path, int mode, int flags) {
  struct at_path at;
  at_path_begin(&at, dirfd, path);
  int r = NEXT(faccessat)(dirfd, path, mode, flags);
  int saved = errno;
  emit_at_result(&at, path, r != 0, saved, 0);
  errno = saved;
  return r;
}

int fstatat(int dirfd, const char *path, struct stat *st, int flags) {
  if (stats_descriptor(path, flags)) return NEXT(fstatat)(dirfd, path, st, flags);
  struct at_path at;
  at_path_begin(&at, dirfd, path);
  int r = NEXT(fstatat)(dirfd, path, st, flags);
  int saved = errno;
  emit_at_result(&at, path, r != 0, saved, 0);
  errno = saved;
  return r;
}

DIR *opendir(const char *path) {
  DIR *r = NEXT(opendir)(path);
  int saved = errno;
  if (r != NULL) emit('D', path); else emit_result(path, 1, saved);
  errno = saved;
  return r;
}

ssize_t readlink(const char *path, char *buf, size_t n) {
  ssize_t r = NEXT(readlink)(path, buf, n);
  int saved = errno;
  emit_result(path, r < 0, saved);
  errno = saved;
  return r;
}

int stat64(const char *path, struct stat64 *st) {
  int r = NEXT(stat64)(path, st);
  int saved = errno;
  emit_result(path, r != 0, saved);
  errno = saved;
  return r;
}

int lstat64(const char *path, struct stat64 *st) {
  int r = NEXT(lstat64)(path, st);
  int saved = errno;
  emit_result(path, r != 0, saved);
  errno = saved;
  return r;
}

int fstatat64(int dirfd, const char *path, struct stat64 *st, int flags) {
  if (stats_descriptor(path, flags)) return NEXT(fstatat64)(dirfd, path, st, flags);
  struct at_path at;
  at_path_begin(&at, dirfd, path);
  int r = NEXT(fstatat64)(dirfd, path, st, flags);
  int saved = errno;
  emit_at_result(&at, path, r != 0, saved, 0);
  errno = saved;
  return r;
}

/* statx is what newer libstdc++ and LLVM reach for. Forward-declared, so the
   hook builds against a glibc whose headers predate it (2.28) too. */
struct statx;
int statx(int dirfd, const char *path, int flags, unsigned int mask, struct statx *st) {
  if (stats_descriptor(path, flags)) return NEXT(statx)(dirfd, path, flags, mask, st);
  struct at_path at;
  at_path_begin(&at, dirfd, path);
  int r = NEXT(statx)(dirfd, path, flags, mask, st);
  int saved = errno;
  emit_at_result(&at, path, r != 0, saved, 0);
  errno = saved;
  return r;
}

/* Binaries built against glibc before 2.33 call these for stat, lstat and
   fstatat: their headers redirected those names here, with a struct layout
   version first. */
int __xstat(int ver, const char *path, struct stat *st) {
  (void)ver;
  int r = NEXT(__xstat) != NULL ? NEXT(__xstat)(ver, path, st) : NEXT(stat)(path, st);
  int saved = errno;
  emit_result(path, r != 0, saved);
  errno = saved;
  return r;
}

int __xstat64(int ver, const char *path, struct stat64 *st) {
  (void)ver;
  int r = NEXT(__xstat64) != NULL ? NEXT(__xstat64)(ver, path, st) : NEXT(stat64)(path, st);
  int saved = errno;
  emit_result(path, r != 0, saved);
  errno = saved;
  return r;
}

int __lxstat(int ver, const char *path, struct stat *st) {
  (void)ver;
  int r = NEXT(__lxstat) != NULL ? NEXT(__lxstat)(ver, path, st) : NEXT(lstat)(path, st);
  int saved = errno;
  emit_result(path, r != 0, saved);
  errno = saved;
  return r;
}

int __lxstat64(int ver, const char *path, struct stat64 *st) {
  (void)ver;
  int r = NEXT(__lxstat64) != NULL ? NEXT(__lxstat64)(ver, path, st) : NEXT(lstat64)(path, st);
  int saved = errno;
  emit_result(path, r != 0, saved);
  errno = saved;
  return r;
}

int __fxstatat(int ver, int dirfd, const char *path, struct stat *st, int flags) {
  if (NEXT(__fxstatat) == NULL) return fstatat(dirfd, path, st, flags);
  if (stats_descriptor(path, flags)) return NEXT(__fxstatat)(ver, dirfd, path, st, flags);
  struct at_path at;
  at_path_begin(&at, dirfd, path);
  int r = NEXT(__fxstatat)(ver, dirfd, path, st, flags);
  int saved = errno;
  emit_at_result(&at, path, r != 0, saved, 0);
  errno = saved;
  return r;
}

int __fxstatat64(int ver, int dirfd, const char *path, struct stat64 *st, int flags) {
  if (NEXT(__fxstatat64) == NULL) return fstatat64(dirfd, path, st, flags);
  if (stats_descriptor(path, flags)) return NEXT(__fxstatat64)(ver, dirfd, path, st, flags);
  struct at_path at;
  at_path_begin(&at, dirfd, path);
  int r = NEXT(__fxstatat64)(ver, dirfd, path, st, flags);
  int saved = errno;
  emit_at_result(&at, path, r != 0, saved, 0);
  errno = saved;
  return r;
}

ssize_t readlinkat(int dirfd, const char *path, char *buf, size_t n) {
  struct at_path at;
  at_path_begin(&at, dirfd, path);
  ssize_t r = NEXT(readlinkat)(dirfd, path, buf, n);
  int saved = errno;
  emit_at_result(&at, path, r < 0, saved, 0);
  errno = saved;
  return r;
}

DIR *fdopendir(int fd) {
  DIR *r = NEXT(fdopendir)(fd);
  int saved = errno;
  if (r != NULL) {
    char dir[VCACHE_RECORD_MAX];
    size_t used = dirfd_prefix(fd, dir, sizeof dir);
    if (used == 0) {
      mark_incomplete();
    } else {
      if (used > 1) --used;  /* the separator dirfd_prefix adds, unless this is "/" */
      dir[used] = '\0';
      emit('D', dir);
    }
  }
  errno = saved;
  return r;
}

/* After a change of root every absolute record names a file inside the new
   root, which a reader would check outside it, and the log's own name now
   resolves inside it too. So the log's directory is held open across the
   call, and a successful one leaves the marker in the directory the reader
   looks in. A failed call changes nothing and leaves none. */
int chroot(const char *path) {
  ensure_log();
  const char *log = log_path();
  if (log == NULL) return NEXT(chroot)(path);
  char dir[PATH_MAX];
  const char *base = strrchr(log, '/') + 1;
  size_t dir_len = (size_t)(base - log);
  int dir_fd = -1;
  if (dir_len < sizeof dir) {
    memcpy(dir, log, dir_len);
    dir[dir_len] = '\0';
    dir_fd = (int)syscall(SYS_openat, AT_FDCWD, dir, O_PATH | O_DIRECTORY | O_CLOEXEC);
  }
  int r = NEXT(chroot)(path);
  int saved = errno;
  if (r == 0) {
    if (trace_mode != VCACHE_TRACE_TEXT) mark_incomplete();
    static const char suffix[] = ".incomplete";
    char marker[PATH_MAX];
    size_t n = strlen(base);
    int fd = -1;
    if (dir_fd >= 0 && n + sizeof suffix <= sizeof marker) {
      memcpy(marker, base, n);
      memcpy(marker + n, suffix, sizeof suffix);
      fd = (int)syscall(SYS_openat, dir_fd, marker, O_WRONLY | O_CREAT | O_CLOEXEC, 0600);
    }
    if (fd >= 0) syscall(SYS_close, fd); else write_incomplete_sentinel();
  }
  if (dir_fd >= 0) syscall(SYS_close, dir_fd);
  errno = saved;
  return r;
}

/* glibc has had a wrapper since 2.43; before it, a call is a raw syscall.
   Defined whatever headers the tracer is built against: a tracer built on an
   older glibc is still loaded into programs linked against a newer one, which
   would otherwise open files through it unseen. struct open_how is declared
   only from 2.43, so it is forward-declared and its first member, the u64
   open flags (the kernel's ABI), is read directly. */
struct open_how;
int openat2(int dirfd, const char *path, const struct open_how *how, size_t size);
int openat2(int dirfd, const char *path, const struct open_how *how, size_t size) {
  if (NEXT(openat2) == NULL) {
    errno = ENOSYS;
    return -1;
  }
  struct at_path at;
  at_path_begin(&at, dirfd, path);
  int r = NEXT(openat2)(dirfd, path, how, size);
  int saved = errno;
  uint64_t flags = 0;
  if (r >= 0 && how != NULL && size >= sizeof flags) memcpy(&flags, how, sizeof flags);
  emit_at_result(&at, path, r < 0, saved, r >= 0 && (flags & O_ACCMODE) == O_WRONLY);
  errno = saved;
  return r;
}
