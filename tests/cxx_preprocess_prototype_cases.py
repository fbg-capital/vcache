#!/usr/bin/env python3
"""Host case (a) and (c) for baseline-stream and clang-minws. Three interleaved rounds."""
import json
import os
import shlex
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, "/build/richard/claude-tmp/worktrees/vcache/axb/tests")
import cxx_preprocess_corpus as corpus
import cxx_preprocess_eval as ev

PREFIX = "/build/richard/claude-tmp/worktrees/sunbird/axb-a"
VCACHE = PREFIX and "/build/richard/claude-tmp/worktrees/vcache/axb/bin/vcache"
HEADER = "protocol/types/SymbolId.h"


def selected():
    deps = corpus.parse_deps(
        "/build/richard/claude-tmp/forgrok/vcache-axb/logs/host-ninja-deps.txt",
        PREFIX + "/build", PREFIX)
    cmds = json.loads(Path(PREFIX + "/build/compile_commands.json").read_text())
    out = []
    for entry in cmds:
        if " -c " not in entry["command"] or ev.is_pch_producer(entry["command"]):
            continue
        headers = deps.get(corpus.object_key(entry, PREFIX + "/build"), set())
        if HEADER in headers:
            out.append(entry)
    return out


def show_stats(env):
    proc = subprocess.run([VCACHE, "--show-stats"], env=env, capture_output=True, text=True)
    counts = {}
    for line in proc.stdout.splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[-1].isdigit():
            counts[" ".join(parts[:-1]).strip()] = int(parts[-1])
    return counts


def run_set(commands, env):
    log = Path(env["VCACHE_LOG"])
    before = log.stat().st_size if log.exists() else 0
    started = time.perf_counter()
    links = 0
    for entry in commands:
        argv = shlex.split(entry["command"])
        subprocess.run([VCACHE, *argv], cwd=entry["directory"], env=env, capture_output=True)
    text = log.read_text(errors="replace")[before:] if log.exists() else ""
    return {
        "wall_s": round(time.perf_counter() - started, 3),
        "preprocess": text.count("] preprocess:"),
        "compile": text.count("] compile:"),
        "links": links,
        "tus": len(commands),
    }


def main():
    commands = selected()
    print("tus", len(commands), flush=True)
    path = Path(PREFIX) / HEADER
    original = path.read_bytes()
    modes = ("baseline-stream", "clang-minws")
    stores = {}
    for mode in modes:
        store = Path("/build/richard/claude-tmp/forgrok/vcache-axb/stores") / ("host-" + mode)
        store.mkdir(parents=True, exist_ok=True)
        env = ev._compiler_env()
        env.update({
            "VCACHE_DIR": str(store),
            "VCACHE_DAEMON": "auto",
            "VCACHE_CXX_MANIFEST": "1",
            "VCACHE_ROOTS": PREFIX + "=/code/sunbird_strategy",
            "VCACHE_LOG": str(store / "vcache.log"),
            "VCACHE_CACHE_SIZE": "20G",
            "VCACHE_CXX_PREPROCESS_CANDIDATE": mode,
        })
        stores[mode] = env
        print("seed", mode, flush=True)
        row = run_set(commands, env)
        row.update(case="seed", candidate=mode, load_1m=ev.load_average_1m(), instrumented=False)
        print(json.dumps(row), flush=True)
    plan = (("a", b"// axb E1\n"), ("c", b"#define AXB_E4 1\nextern int axb_e4_used[AXB_E4];\n"))
    try:
        for round_i in range(3):
            for name, suffix in plan:
                for mode in modes:
                    load = ev.load_average_1m()
                    before = show_stats(stores[mode])
                    path.write_bytes(original + suffix)
                    measured = run_set(commands, stores[mode])
                    path.write_bytes(original)
                    after = show_stats(stores[mode])
                    row = dict(measured)
                    row.update({
                        "case": name, "round": round_i, "candidate": mode,
                        "load_1m": load, "instrumented": False,
                        "manifest_hits_delta": after.get("manifest hits", 0) - before.get("manifest hits", 0),
                        "pp_hits_recorded_delta": after.get("pp hits recorded", 0) - before.get("pp hits recorded", 0),
                    })
                    ev.accept_timing_row(row)
                    print(json.dumps(row), flush=True)
    finally:
        path.write_bytes(original)
    print("prototype cases done", flush=True)


if __name__ == "__main__":
    raise SystemExit(main())
