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
