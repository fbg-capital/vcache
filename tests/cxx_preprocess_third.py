#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Unto Labs
# SPDX-License-Identifier: Apache-2.0
"""Host fallback count for simplecpp, pcpp, tcc and warp.

Each tool is given the compile command's -I/-isystem/-include/-D flags and
the compiler's system include directories. A non-zero exit, empty stdout, or
an error on stderr is a fallback. The sound fallback is the compiler -E; these
rows only count that the tool did not produce a digest.

Not part of make test. Redirect stdout to a log.
"""

from __future__ import annotations

import json
import shlex
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import cxx_preprocess_corpus as corpus
import cxx_preprocess_eval as ev

PREFIX = "/build/richard/claude-tmp/worktrees/sunbird/axb-a"
RESULTS = "/build/richard/claude-tmp/forgrok/vcache-axb/results.jsonl"
DEPS = "/build/richard/claude-tmp/forgrok/vcache-axb/logs/host-ninja-deps.txt"
SCRATCH = Path("/build/richard/claude-tmp/forgrok/vcache-axb")
SUNBIRD = "c76c0222f"
TOOLS = {
    "simplecpp": str(SCRATCH / "bin/simplecpp"),
    "pcpp": str(SCRATCH / "venv/bin/pcpp"),
    "tcc": str(SCRATCH / "src-tinycc/tcc"),
    "warp": str(SCRATCH / "bin/warp"),
}
SYSINC = [
    "/opt/infra.fedora44/include/c++/16.2.0",
    "/opt/infra.fedora44/include/c++/16.2.0/x86_64-pc-linux-gnu",
    "/opt/infra.fedora44/include/c++/16.2.0/backward",
    "/opt/infra.fedora44/lib/clang/23/include",
    "/usr/local/include",
    "/usr/include",
]


def split_flags(entry):
    args = shlex.split(entry["command"])
    inc = []
    includes = []
    defines = []
    i = 0
    while i < len(args):
        arg = args[i]
        if arg in {"-I", "-isystem"} and i + 1 < len(args):
            inc.append(args[i + 1])
            i += 2
            continue
        if arg == "-include" and i + 1 < len(args) and not args[i + 1].startswith("-"):
            includes.append(args[i + 1])
            i += 2
            continue
        # Clang passes the PCH header as `-Xclang -include -Xclang PATH`.
        if (arg == "-Xclang" and i + 3 < len(args) and args[i + 1] == "-include"
                and args[i + 2] == "-Xclang" and not args[i + 3].startswith("-")):
            includes.append(args[i + 3])
            i += 4
            continue
        # `-include/path` is a glued -include. `-include-pch` is a different flag.
        if arg.startswith("-include/"):
            includes.append(arg[len("-include"):])
        elif arg.startswith("-isystem") and arg != "-isystem":
            inc.append(arg[len("-isystem"):])
        elif arg.startswith("-I") and arg != "-I":
            inc.append(arg[2:])
        elif arg.startswith("-D") and arg != "-D":
            defines.append(arg[2:])
        i += 1
    return inc, includes, defines


def command_for(tool, entry):
    inc, includes, defines = split_flags(entry)
    dirs = inc + SYSINC
    binary = TOOLS[tool]
    if tool == "simplecpp":
        cmd = [binary, "-std=c++20"]
        cmd += ["-I" + path for path in dirs]
        cmd += ["-D" + item for item in defines]
        cmd += ["-include=" + path for path in includes]
        cmd.append(entry["file"])
        return cmd
    if tool == "pcpp":
        cmd = [binary]
        for path in dirs:
            cmd += ["-I", path]
        for item in defines:
            cmd += ["-D", item]
        cmd.append(entry["file"])
        return cmd
    if tool == "tcc":
        cmd = [binary, "-E"]
        for path in dirs:
            cmd += ["-I", path]
        for item in defines:
            cmd += ["-D", item]
        cmd.append(entry["file"])
        return cmd
    cmd = [binary, "--stdout"]
    for path in dirs:
        cmd += ["-I", path]
    for item in defines:
        cmd += ["-D", item]
    cmd.append(entry["file"])
    return cmd


def run_tool(tool, entry):
    started = time.perf_counter()
    try:
        proc = subprocess.run(
            command_for(tool, entry), cwd=entry.get("directory") or None,
            env=ev._compiler_env(), capture_output=True, timeout=30)
    except subprocess.TimeoutExpired:
        return False, "timed out after 30s", time.perf_counter() - started, 0
    elapsed = time.perf_counter() - started
    err = proc.stderr.decode(errors="replace").strip().splitlines()
    why = err[0][:240] if err else ""
    ok = proc.returncode == 0 and bool(proc.stdout) and not err
    if not ok and not why:
        why = "empty output" if not proc.stdout else f"exit {proc.returncode}"
    return ok, why, elapsed, len(proc.stdout)


def main():
    rev = subprocess.run(
        ["git", "-C", str(Path(__file__).resolve().parents[1]), "rev-parse", "--short", "HEAD"],
        capture_output=True, text=True, check=True).stdout.strip()
    commands = corpus.load_commands(PREFIX + "/build/compile_commands.json")
    deps = corpus.parse_deps(DEPS, PREFIX + "/build", PREFIX)
    indexed = {}
    for entry in commands:
        rel = corpus.tu_id(entry, PREFIX)
        headers = deps.get(corpus.object_key(entry, PREFIX + "/build"), set())
        indexed[rel] = (entry, headers)
    symbol = [rel for rel, (_e, headers) in indexed.items() if "protocol/types/SymbolId.h" in headers]
    pch = next(rel for rel, (entry, _h) in indexed.items()
               if ev.is_pch_consumer(entry["command"]) and rel.endswith("EmbeddedWiregenDescriptors.cc"))
    print(json.dumps({"vcache_revision": rev, "symbol": len(symbol), "pch": pch}), flush=True)
    existing = ev.load_resume_keys(RESULTS)

    def record(tool, edit, tu, ok, why, elapsed, nbytes):
        row = {
            "candidate": tool, "edit": edit, "tu": tu, "platform": "host",
            "sunbird_revision": SUNBIRD, "vcache_revision": rev,
        }
        if ev.should_skip_row(existing, row):
            return
        row.update({
            "compiler_before": "", "compiler_after": "",
            "candidate_before": "", "candidate_after": "" if not ok else "produced",
            "compiler_changed": False, "candidate_changed": False,
            "fell_back": not ok, "fallback_why": "" if ok else why,
            "disqualified": False, "reasons": [],
            "false_invalidation": False, "elapsed_s": round(elapsed, 4),
            "bytes": nbytes, "reason": "" if ok else "fallback",
        })
        ev.append_jsonl(RESULTS, row)
        existing.add(ev.resume_key(row))

    only = set(sys.argv[1:])
    for tool in TOOLS:
        if only and tool not in only:
            continue
        if not Path(TOOLS[tool]).exists():
            print(json.dumps({"tool": tool, "missing": True}), flush=True)
            continue
        fell = 0
        reasons = {}

        def work(rel, tool=tool):
            return rel, run_tool(tool, indexed[rel][0])

        with ThreadPoolExecutor(max_workers=4) as pool:
            for rel, (ok, why, elapsed, nbytes) in pool.map(work, symbol):
                suffix = "#matrix2" if tool == "simplecpp" else "#SymbolId"
                record(tool, "baseline", rel + suffix, ok, why, elapsed, nbytes)
                if not ok:
                    fell += 1
                    reasons[why[:80]] = reasons.get(why[:80], 0) + 1
        ok, why, elapsed, nbytes = run_tool(tool, indexed[pch][0])
        pch_tu = pch + ("#pch-matrix2" if tool == "simplecpp" else "#pch")
        record(tool, "baseline", pch_tu, ok, why, elapsed, nbytes)
        if not ok:
            fell += 1
        print(json.dumps({
            "tool": tool, "symbol": len(symbol), "fallback_symbol_plus_pch": fell,
            "pch_ok": ok, "top_why": sorted(reasons.items(), key=lambda item: -item[1])[:3],
        }), flush=True)
    print("third done", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
