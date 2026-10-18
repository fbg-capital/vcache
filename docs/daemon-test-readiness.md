# Session-daemon test readiness

The failures recorded during the lazy-scratch change did not recur in six
instrumented unit-suite runs or 1,000 isolated daemon starts. Those runs do not
establish the cause of the historical failures.

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

This is a confirmed notification defect, not an attribution of the historical
two-second startup timeouts. Those retained failures and their follow-up remain
open. The normal gate includes both the earlier fixture tests and this production
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
