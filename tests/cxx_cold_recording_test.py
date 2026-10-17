#!/usr/bin/env python3
"""Real compiler checks for cold manifest recording and stale-state routing."""
import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import sys
import tempfile


def run(binary, work):
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(("VCACHE_", "CCACHE_")) and k != "LD_PRELOAD"}
    env.update(VCACHE_DIR=str(work / "cache"), VCACHE_DAEMON="off",
               VCACHE_CXX_MANIFEST="1", VCACHE_CXX_MANIFEST_TRACE="1",
               VCACHE_CXX_MANIFEST_ABSENT_DEPS="0")
    failures = 0

    def check(condition, name):
        nonlocal failures
        print(("PASS " if condition else "FAIL ") + name, flush=True)
        failures += not condition

    for compiler in ("gcc", "clang"):
        if not shutil.which(compiler):
            continue
        env["VCACHE_DIR"] = str(work / (compiler + "-cache"))
        trees = [work / (compiler + suffix) for suffix in ("-a", "-b")]
        for tree in trees:
            tree.mkdir()
            (tree / "h.h").write_text("/* first */\n#define VALUE 7\n")
            (tree / "t.c").write_text('#include "h.h"\nint value(void) { return VALUE; }\n')

        def compile_case(name, tree=trees[0], overrides=None, flags=(), driver=compiler):
            log = work / (compiler + "-" + name + ".log")
            current = dict(env, VCACHE_LOG=str(log), VCACHE_ROOTS=str(tree) + "=cold-test")
            current.update(overrides or {})
            result = subprocess.run([str(binary), driver, "-O2", "-g", "-MD", "-MF", "t.d",
                                     *flags, "-c", "t.c", "-o", "t.o"], cwd=tree, env=current,
                                    capture_output=True, text=True)
            (work / (compiler + "-" + name + ".stderr")).write_text(result.stderr)
            return result, log.read_text() if log.exists() else ""

        result, log = compile_case("cold")
        check(result.returncode == 0 and log.count("] compile: ") == 1
              and "] preprocess: " not in log and "derived key " in log,
              compiler + ": cold recording runs one compile and zero preprocess commands")
        golden = (trees[0] / "t.o").read_bytes()
        result, log = compile_case("cross-tree", trees[1])
        check(result.returncode == 0 and "preprocessor skipped" in log
              and (trees[1] / "t.o").read_bytes() == golden
              and "h.h" in (trees[1] / "t.d").read_text(),
              compiler + ": cross-tree manifest hit replays identical object and dependencies")
        (trees[0] / "h.h").write_text("/* second */\n#define VALUE 7\n")
        result, log = compile_case("first-edit")
        check(result.returncode == 0 and "] preprocess: " in log and "] compile: " in log,
              compiler + ": first benign edit seeds the previously absent preprocessed result")
        (trees[0] / "h.h").write_text("/* third */\n#define VALUE 7\n")
        result, log = compile_case("second-edit")
        check(result.returncode == 0 and "] preprocess: " in log and "] compile: " not in log
              and "recorded a state from the preprocessor run" in log,
              compiler + ": seeded benign edit records a preprocessor hit")
        result, log = compile_case("after-edit")
        check("preprocessor skipped" in log and "] preprocess: " not in log,
              compiler + ": unchanged lookup after benign edit uses its recorded state")
        (trees[0] / "h.h").write_text("#define VALUE 8\n")
        result, log = compile_case("semantic-edit")
        check(result.returncode == 0 and "] preprocess: " in log and "] compile: " in log
              and (trees[0] / "t.o").read_bytes() != golden,
              compiler + ": semantic header edit preprocesses and compiles changed bytes")

        for name, overrides in (("read-only", {"VCACHE_READONLY": "1"}),
                                ("recache", {"VCACHE_RECACHE": "1"}),
                                ("untraced", {"VCACHE_CXX_MANIFEST_TRACE": "0"}),
                                ("missing-tracer", {"VCACHE_TRACER": str(work / "missing.so")}),
                                ("manifest-off", {"VCACHE_CXX_MANIFEST": "0"})):
            overrides["VCACHE_DIR"] = str(work / (compiler + "-" + name + "-cache"))
            result, log = compile_case(name, overrides=overrides)
            check(result.returncode == 0 and "] preprocess: " in log,
                  compiler + ": " + name + " preserves preprocessing")
        result, log = compile_case("mmd", flags=("-MMD",), overrides={
            "VCACHE_DIR": str(work / (compiler + "-mmd-cache"))})
        check(result.returncode == 0 and "] preprocess: " in log,
              compiler + ": MMD preserves preprocessing")

        wrapper = work / (compiler + "-slow")
        wrapper.write_text('#!/bin/sh\ncase " $* " in *" -c "*) sleep 0.3;; esac\nexec '
                           + shlex.quote(shutil.which(compiler)) + ' "$@"\n')
        wrapper.chmod(0o755)
        for tree in trees:
            (tree / "h.h").write_text("#define VALUE 7\n")
        concurrent_env = dict(env, VCACHE_DIR=str(work / (compiler + "-flight")),
                              VCACHE_DAEMON="on", VCACHE_DAEMON_SINGLE_FLIGHT="1")
        subprocess.run([str(binary), "--start-daemon"], env=concurrent_env,
                       check=True, capture_output=True, timeout=30)
        try:
            with ThreadPoolExecutor(max_workers=2) as pool:
                futures = [pool.submit(compile_case, "concurrent-" + str(i), tree,
                                       concurrent_env, (), str(wrapper))
                           for i, tree in enumerate(trees)]
                results = [future.result(timeout=30) for future in futures]
            logs = "\n".join(log for _, log in results)
            check(all(result.returncode == 0 for result, _ in results)
                  and logs.count("] compile: ") == 1 and "] preprocess: " not in logs
                  and "cxx manifest hit" in logs
                  and (trees[0] / "t.o").read_bytes() == (trees[1] / "t.o").read_bytes(),
                  compiler + ": concurrent cold callers compile once and validate the leader state")
        finally:
            subprocess.run([str(binary), "--stop-daemon"], env=concurrent_env,
                           check=True, capture_output=True, timeout=30)

        for damage in ("incomplete", "truncated", "missing-input", "changed-input"):
            wrapper = work / (compiler + "-" + damage)
            action = {
                "incomplete": ': > "$VCACHE_TRACE_LOG.incomplete"',
                "truncated": 'printf "broken-record" > "$VCACHE_TRACE_LOG"',
                "missing-input": 'sed "\\|/h.h$|d" "$VCACHE_TRACE_LOG" > "$VCACHE_TRACE_LOG.edited"; '
                                 'cat "$VCACHE_TRACE_LOG.edited" > "$VCACHE_TRACE_LOG"',
                "changed-input": 'printf "/* changed during compile */\\n" >> h.h',
            }[damage]
            # Corrupt legacy text after all mapped writers have exited. Truncating
            # a live mapping would test SIGBUS, not the consumer's refusal guard.
            wrapper.write_text("#!/bin/sh\n"
                               'if [ -n "${LD_PRELOAD:-}" ]; then\n'
                               '  export VCACHE_TEST_PRELOAD="$LD_PRELOAD"\n'
                               '  unset LD_PRELOAD\n  exec "$0" "$@"\nfi\n'
                               'if [ -n "${VCACHE_TRACE_LOG:-}" ]; then\n'
                               '  : > "$VCACHE_TRACE_LOG"\nfi\n'
                               'VCACHE_TRACE_FORMAT=text LD_PRELOAD="${VCACHE_TEST_PRELOAD:-}" '
                               + shlex.quote(shutil.which(compiler)) + ' "$@"\n'
                               'status=$?\nif [ "$status" = 0 ] && [ -n "${VCACHE_TRACE_LOG:-}" ]; then\n'
                               + action + '\nfi\nexit "$status"\n')
            wrapper.chmod(0o755)
            overrides = {"VCACHE_DIR": str(work / (compiler + "-" + damage + "-cache"))}
            result, log = compile_case(damage, overrides=overrides, driver=str(wrapper))
            check(result.returncode == 0 and "] preprocess: " not in log
                  and "not recording:" in log and "derived key " not in log
                  and "cxx manifest: stored" not in log,
                  compiler + ": " + damage + " cold trace returns output without storing")

        for damage in ("empty", "corrupt", "media-error"):
            cache = work / (compiler + "-manifest-" + damage)
            overrides = {"VCACHE_DIR": str(cache)}
            result, log = compile_case("manifest-fill-" + damage, overrides=overrides)
            manifest = re.search(r"cxx manifest key ([0-9a-f]+)", log)[1]
            path = cache / manifest[:2] / manifest[2:]
            if damage == "media-error":
                path.unlink()
                path.mkdir()
                overrides["VCACHE_ERROR_ON_CACHE_MEDIA_FAILURE"] = "1"
            else:
                path.write_bytes(b"" if damage == "empty" else b"invalid manifest blob")
            result, log = compile_case("manifest-" + damage, overrides=overrides)
            check(result.returncode == (90 if damage == "media-error" else 0)
                  and "] preprocess: " not in log and "] compile: " in log
                  and (trees[0] / "t.o").is_file(),
                  compiler + ": " + damage + " manifest allows cold compile and preserves media status")

        (trees[0] / "h.h").write_text("/* __TIME__ */\n#define VALUE 7\n")
        result, log = compile_case("volatile", overrides={
            "VCACHE_DIR": str(work / (compiler + "-volatile"))})
        check(result.returncode == 0 and "] preprocess: " not in log
              and "uses __TIME__" in log and "derived key " not in log,
              compiler + ": volatile cold inputs return output without storing")
        (trees[0] / "h.h").write_text("#define VALUE 7\n")

        # A matched manifest with a missing result must still search the preprocessed cache.
        result, log = compile_case("missing-fill", overrides={
            "VCACHE_DIR": str(work / (compiler + "-missing-cache"))})
        key = re.search(r"derived key ([0-9a-f]+)", log)
        if key:
            matches = list((work / (compiler + "-missing-cache")).rglob("*" + key[1][2:]))
            # The storage layout is checked before changing this fixture's own files.
            check(len(matches) == 1, compiler + ": derived result is identifiable")
            for path in matches:
                path.unlink()
            result, log = compile_case("missing-result", overrides={
                "VCACHE_DIR": str(work / (compiler + "-missing-cache"))})
            check(result.returncode == 0 and "matched but its entry did not serve" in log
                  and "] preprocess: " in log,
                  compiler + ": missing result keeps preprocessor fallback")

        (trees[0] / "payload").write_bytes(b"\x00")
        (trees[0] / "t.c").write_text('__asm__(".incbin \\"payload\\"");\n')
        result, log = compile_case("incbin", overrides={
            "VCACHE_DIR": str(work / (compiler + "-incbin"))})
        check(result.returncode == 0 and "] preprocess: " not in log
              and "contains .incbin" in log and "derived key " not in log,
              compiler + ": cold incbin output is never stored")

        (trees[0] / "t.c").write_text('#error cold_recording_diagnostic\n')
        failure_env = dict(env, VCACHE_DIR=str(work / (compiler + "-failure-cache")))
        result, log = compile_case("failure", overrides=failure_env)
        stats = subprocess.check_output([str(binary), "--show-stats"], env=failure_env, text=True)
        check(result.returncode == 1 and "cold_recording_diagnostic" in result.stderr
              and "] preprocess: " not in log and "derived key " not in log
              and re.search(r"compile failed\s+1\b", stats)
              and re.search(r"preprocess failed\s+0\b", stats)
              and re.search(r"entries stored\s+0\b", stats),
              compiler + ": failed cold compile preserves diagnostics and truthful counters")
    return failures


if __name__ == "__main__":
    if sys.platform != "linux":
        print("SKIP cold recording needs the Linux tracer")
        sys.exit(0)
    binary = Path(sys.argv[1]).resolve()
    with tempfile.TemporaryDirectory(prefix="cold-recording-", dir=os.environ.get("TMPDIR")) as temp:
        sys.exit(bool(run(binary, Path(temp))))
