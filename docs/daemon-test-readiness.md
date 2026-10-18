# Session-daemon test readiness

The failures recorded during the lazy-scratch change initially did not recur in six
instrumented unit-suite runs or 1,000 isolated daemon starts. Those runs do not
establish the cause of the historical failures. The later disk-flush investigation
below identifies a confirmed startup stall without attributing every older timeout
to that mechanism.

The investigation did establish two defects in the test helper. Its single
`poll`/`read` attempt rejected an interrupted system call as a startup failure,
even when the daemon was ready. It also discarded the daemon's error reply and
removed the failed fixture, leaving no useful explanation. Some fixture setup
errors returned without recording a failed check at all.

`SessionDaemon` now waits against one monotonic two-second deadline. Interrupted
system calls resume within that deadline; there is no daemon restart or test
retry. Setup errors, poll/read errors, EOF, lock conflicts, unexpected replies,
server errors and timeouts produce distinct diagnostics. Error replies are
bounded to 4 KiB. A failed startup retains its directory, daemon logs and
`startup.txt`, including elapsed time, test section and child status. Cleanup
still stops and reaps the child; healthy fixtures are removed normally.

This changes test infrastructure only. The shipped daemon, cache keys and
installed binaries are unchanged. A future sporadic failure must be diagnosed
from the retained evidence; the interruption fix is not proof that it caused
the earlier failures.

## Verification

`tests/session_daemon_startup_test.py` runs a focused unit-binary entry point with
Linux `LD_PRELOAD` fault injection. It uses actual pipes and forked daemons,
checks nonzero exits for real failures, and rejects leftover child processes.

| Cases | Before | After |
|---|---|---|
| Healthy startup control | Pass | Pass |
| Interrupted poll or read | Fail | Pass |
| Directory, pipe or fork setup failure | Fail | Pass |
| Permanent poll or read error | Fail | Pass |
| Child exit, bind error or occupied daemon lock | Fail | Pass |
| Unknown or oversized readiness reply | Fail | Pass |
| Stalled child or repeated interruptions through the deadline | Fail | Pass |

The pre-fix executable comes from `2c5901f`, with only the focused probe entry
point added. All fourteen regression cases fail against it. All fifteen cases
pass on the host and with dc-build's GCC 11 and Python 3.6. The injection suite
explicitly skips non-Linux systems; macOS and arm64 were not executed.
The full host gate passed on its first run after the fix: 1,470 unit, 1,320
integration, 93 link-verification, 15 harness, seven scratch and fifteen startup
checks (`full-gate.log`).

Evidence is retained under
`/build/richard/codex-1000/daemon-ready-20261016/`: `startup-probe.log`,
`diagnostic-unit.log`, `diagnostic-unit-1.log` through `diagnostic-unit-5.log`,
`regression-before-final.log`, `regression-after.log`, `full-gate.log` and
`dc-regression.log`.
Detailed fault-case logs and retained fixtures are in
`/build/richard/codex-1000/dr-before-final-20261016/` and
`/build/richard/codex-1000/dr-after-20261016/`.

The normal `make test` gate includes the fault suite. For a single ordinary
startup, run `bin/vcache_test --session-daemon-startup-probe` through
`~/bin/build-limited`. Set `TMPDIR` under `/build/$USER/codex-1000/` first; compiler
suites also need ccache/sccache directories removed from `PATH`. The retained
`run.py` and `dc_verify.py` record the exact limited-execution commands used here.

## Readiness notification writes

The production daemon previously made one write to its readiness pipe and ignored
the result. An interrupted write could lose the `R` (ready), `L` (lock held) or `E`
(startup error) reply. The parent then saw EOF, even when the daemon was already
listening. A short write could also truncate the error diagnostic.

`SignalReady` now retries `EINTR` and advances through the remaining bytes after
each positive write. Zero-byte writes and permanent errors end the attempt and
close the pipe, so they cannot cause an infinite retry loop. A caller that has
already closed its reader still does not kill the daemon. The readiness format,
cache keys and startup deadlines are unchanged.

`tests/daemon_ready_write_test.py` uses real production daemons, real pipes and a
Linux preload shim restricted to the readiness descriptor. Five cases fail on
`10df83d`: interrupted ready, lock-held and error replies; a short error reply;
and a short error prefix followed by interruption. All five pass with the fix.
Healthy startup, lock contention, full error output, zero progress, permanent
write errors and a closed reader are controls. A separate mutation removing the
stop-on-zero/error behavior makes those two bounded-completion checks fail.
Every foreground daemon is stopped and reaped by the harness.

This established a notification defect without attributing the historical
two-second startup timeouts. The retained failures required the later investigation
below. The normal gate includes both the earlier fixture tests and this production
write suite. Evidence is under
`/build/richard/codex-1000/daemon-ready-write-20261018/`.

The runtime fix was merged as `4be8f9f` (implementation `cdb9bac`) and published
as Anvil 1.8 for all five targets. Installed host, EL10 and dc-build binary/tracer
pairs match those artifacts, and each passes all eleven readiness checks in its
own environment. The dc-build run uses Python 3.6; it exposed a harness cleanup
error from calling `communicate()` twice on closed output. The harness now collects
the child's output once, including after timeout cleanup, with assertions unchanged.
The full host gate passed 1,564 unit, 1,342 integration, 93 link-verification and
93 publisher checks plus the ancillary suites. Linux aarch64 and macOS were not
executed for this fix.

The final merge gate also exposed a timing assumption in the existing digest-table
test: its input could age past the two-second unsettled window during preceding
filesystem checks. That assertion now uses the existing injected clock at the
fixture's ctime, preserving the intended unsettled state regardless of test duration.
The original failure is retained in `gate-final.log` in the evidence directory.

## Lock acquisition errors

The daemon previously treated every failed nonblocking `flock` as contention.
An interrupted call therefore prevented startup even when no other daemon owned
the cache. Lock-service failures such as `ENOLCK` and I/O errors were also reported
as an existing daemon, hiding the actual failure from the caller.

`AcquireLock` retries `EINTR`, reports only `EWOULDBLOCK`/`EAGAIN` as contention,
and returns other errors with the lock path and original error description.
Failed descriptors are closed; successful startup and genuine contention retain
their existing behavior. Cache keys, readiness format and timeouts are unchanged.

The readiness suite injects faults only into the actual daemon lock descriptor.
Four cases fail on `dccde85`: two interruptions before acquiring a free lock, two
interruptions before discovering a held lock, `ENOLCK`, and `EIO`. The held-lock
case requires evidence that the real lock operation was retried, so an early
false contention reply cannot pass. The existing eleven readiness checks remain
controls. Evidence is retained under
`/build/richard/codex-1000/daemon-lock-errors-20261018/`.

This established a lock error-handling defect without attributing the
historical startup timeouts.

The disk-backed gate hit an existing lease/Put timing failure. A diagnostic unit
run reproduced a Put timeout: `unit-trace.2443138` records a 528 ms blob `fsync`,
followed by `EPIPE` after the test client's 500 ms deadline. Both logs are retained
as `gate.log` and `diagnostic-unit.log`; neither run had a daemon startup failure.
The full gate therefore also runs with task-local tmpfs scratch in a private mount
namespace, retaining the ordinary UID and dropping all namespace capabilities
before testing. No assertion or deadline is changed. This isolates the lock fix
from the known disk-latency limitation; it does not resolve that limitation.

`after.log` passes all fifteen readiness cases on disk-backed scratch.
`gate-tmpfs.log` passes the full host gate: 1,564 unit, 1,342 integration,
93 link-verification and 93 publisher checks plus the ancillary suites.
The fault shim requires Linux; no macOS or Linux aarch64 execution is claimed.

## PID-file flush investigation

The startup-timeout investigation remained relevant after the readiness-write and
lock-error fixes. Two retained failures from the shared digest-table gate had a
complete temporary PID file but no published `daemon/pid`. Those artifacts locate
the delay before PID publication, but alone do not distinguish disk waiting from
scheduling delays. Another retained failure had progressed as far as listening.

A normal disk-backed gate at `60203a3`, traced without injected delays or added
load, observed a real daemon spending **2.025114 seconds in the PID-file `fsync`**.
The next syscall renamed that temporary file to `daemon/pid`; the server then
started accepting requests. That flush alone exceeds the fixture's two-second
startup deadline. A separate unit-fixture PID flush took 1.598923 seconds in the
same run. The gate passed: the observed two-second stall occurred in an integration
daemon whose caller waits longer, not in a newly failing unit fixture.

`AcquireLock` now publishes the PID atomically without requesting durability.
The held file lock, not the PID file, establishes ownership. A PID is obsolete
after a crash, so persisting it before the server can listen adds no correctness
guarantee. Writes still check close errors and rename atomically. Durable cache
entries, manifests and upload journals are unchanged, as are cache keys and
startup deadlines.

Both startup harnesses now stall only an `fsync` of the daemon's temporary PID
file. On `60203a3`, the unit fixture fails at 2,002 ms and the production daemon
never reports readiness. Existing controls pass. The production regression also
checks that the new PID replaces stale contents and disappears on normal shutdown.
These tests reproduce the observed blocking mechanism; they do not claim that
every historical timeout had the same cause. The separate 500 ms cache-Put test
limitation concerns durable blob storage and is not fixed by this change.

Evidence is retained under `/build/richard/codex-1000/rpf-review-20261018/`:
`baseline-gate.log`, `baseline-trace.1008595`, `pid-flush-observations.json`,
`before-unit.log`, `before-production.log`, and their per-case logs.
`gate-fixed.log` passes the full ordinary disk-backed gate on its first run after
the change: 1,564 unit and 1,342 integration checks, both sixteen-case startup
suites, link verification, publisher checks and the remaining ancillary suites.
No tmpfs substitution, gate retry or wider deadline was used for this validation.
