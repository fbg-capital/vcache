#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Unto Labs
# SPDX-License-Identifier: Apache-2.0
"""Incremental cases control, a, b, c, d. Not part of make test.

Compile-only replay: the invocation log records kind=compile and zero links.
Three rounds, interleaved. Uninstrumented. Load average is recorded first.
"""

from __future__ import annotations

import argparse
import json
import os
import shlex
import subprocess
import time
from pathlib import Path

import cxx_preprocess_eval as ev


def show_stats(vcache, env):
    proc = subprocess.run([vcache, "--show-stats"], env=env, capture_output=True, text=True)
    counts = {}
    for line in proc.stdout.splitlines():
        if not line.strip():
            continue
        # "manifest hits       12"
        parts = line.split()
        if len(parts) >= 2 and parts[-1].isdigit():
            name = " ".join(parts[:-1]).strip()
            counts[name] = int(parts[-1])
    return counts


def run_compiles(vcache, commands, env, invocation_log):
    """Replay compile commands. Writes kind=compile lines. Returns wall seconds."""
    log = Path(env["VCACHE_LOG"])
    before = log.stat().st_size if log.exists() else 0
    started = time.perf_counter()
    with invocation_log.open("a") as inv:
        for entry in commands:
            argv = shlex.split(entry["command"])
            proc = subprocess.run([vcache, *argv], cwd=entry.get("directory") or None,
                                  env=env, capture_output=True)
            inv.write("kind=compile exit=%s file=%s\n" % (proc.returncode, entry.get("file", "")))
            if proc.returncode != 0:
                inv.write("stderr=%s\n" % proc.stderr.decode(errors="replace")[:200].replace("\n", " "))
    wall = time.perf_counter() - started
    text = log.read_text(errors="replace")[before:] if log.exists() else ""
    return {
        "wall_s": round(wall, 3),
        "preprocess": text.count("] preprocess:"),
        "compile": text.count("] compile:"),
        "links": ev.link_count(ev.replay_commands(
            [{"kind": "compile"} for _ in commands], compile_only=True)),
        "invocations": len(commands),
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--vcache", required=True)
    parser.add_argument("--compile-commands", required=True)
    parser.add_argument("--deps", required=True)
    parser.add_argument("--source-root", required=True)
    parser.add_argument("--build-prefix", required=True)
    parser.add_argument("--source-prefix", required=True)
    parser.add_argument("--platform", required=True)
    parser.add_argument("--store", required=True)
    parser.add_argument("--out", required=True)
    args = parser.parse_args()
    ev.assert_store_allowed(args.store)
    import cxx_preprocess_corpus as corpus
    commands = corpus.load_commands(args.compile_commands)
    deps = corpus.parse_deps(args.deps, args.build_prefix, args.source_prefix)
    by_rel = {}
    for entry in commands:
        rel = corpus.tu_id(entry, args.source_prefix)
        headers = deps.get(corpus.object_key(entry, args.build_prefix), set())
        by_rel[rel] = (entry, headers, ev.is_pch_consumer(entry["command"]))

    def dependents(header, non_pch_only=False):
        out = []
        for rel, (entry, headers, pch) in by_rel.items():
            if header not in headers:
                continue
            if non_pch_only and pch:
                continue
            if non_pch_only is False and False:
                pass
            out.append(entry)
        if non_pch_only:
            # Drop the header if any PCH consumer includes it.
            if any(header in headers and pch for _rel, (_e, headers, pch) in by_rel.items()):
                return []
        return out

    symbol = [entry for rel, (entry, headers, _pch) in by_rel.items()
              if "protocol/types/SymbolId.h" in headers]
    non_pch_header = None
    non_pch_set = []
    counts = {}
    for _rel, (_entry, headers, pch) in by_rel.items():
        for header in headers:
            if not header.endswith(".h"):
                continue
            counts.setdefault(header, [0, 0])
            counts[header][1 if pch else 0] += 1
    best = None
    for header, (non, pch) in counts.items():
        if pch == 0 and non >= 1 and (best is None or non > best[0]):
            best = (non, header)
    if best:
        non_pch_header = best[1]
        non_pch_set = [entry for _rel, (entry, headers, _p) in by_rel.items()
                       if non_pch_header in headers]
    ensemble = [entry for _r, (entry, headers, _p) in by_rel.items()
                if "sunbird/ensemble/EnsembleSnapshot.h" in headers]
    epoll = [entry for _r, (entry, headers, _p) in by_rel.items()
             if "core/transport/EpollDispatcher.h" in headers]
    print(json.dumps({
        "symbol": len(symbol),
        "ensemble": len(ensemble),
        "epoll": len(epoll),
        "non_pch_header": non_pch_header,
        "non_pch": len(non_pch_set),
    }), flush=True)

    store = Path(args.store)
    store.mkdir(parents=True, exist_ok=True)
    log_path = store / "vcache.log"
    env = ev._compiler_env()
    env.update({
        "VCACHE_DIR": str(store),
        "VCACHE_DAEMON": "auto",
        "VCACHE_CXX_MANIFEST": "1",
        "VCACHE_ROOTS": "%s=/code/sunbird_strategy" % args.source_prefix.rstrip("/"),
        "VCACHE_LOG": str(log_path),
        "VCACHE_CACHE_SIZE": "20G",
    })
    source = Path(args.source_root)
    invocation = Path(args.out).with_suffix(".invocations.log")
    rounds = []

    def one(name, commands_to_run, edit=None, header=None):
        load = ev.load_average_1m()
        stats_before = show_stats(args.vcache, env)
        path = source / header if header else None
        original = path.read_text() if path else None
        if edit and path:
            path.write_text(ev.apply_edit(original, edit))
        try:
            measured = run_compiles(args.vcache, commands_to_run, env, invocation)
        finally:
            if path and original is not None:
                path.write_text(original)
        stats_after = show_stats(args.vcache, env)
        row = {
            "case": name,
            "platform": args.platform,
            "load_1m": load,
            "tus": len(commands_to_run),
            "instrumented": False,
            **measured,
            "manifest_hits_delta": stats_after.get("manifest hits", 0) - stats_before.get("manifest hits", 0),
            "pp_hits_recorded_delta": stats_after.get("pp hits recorded", 0) - stats_before.get("pp hits recorded", 0),
            "preprocess_failed_delta": stats_after.get("preprocess failed", 0) - stats_before.get("preprocess failed", 0),
        }
        ev.accept_timing_row(row)
        rounds.append(row)
        print(json.dumps(row), flush=True)
        return row

    # Seed the private store. This is a real compile of the case TUs.
    seed_set = symbol or list(entry for entry, _h, _p in by_rel.values())
    print("seed", len(seed_set), flush=True)
    one("seed", seed_set)
    # Three interleaved rounds of control and (a) on the seeded set, then (c),
    # then (b) and (d) when those sets exist.
    plan = [("control", seed_set, None, None),
            ("a", symbol, "E1", "protocol/types/SymbolId.h"),
            ("c", symbol, "E4", "protocol/types/SymbolId.h")]
    if non_pch_set and non_pch_header:
        plan.append(("b", non_pch_set, "E1", non_pch_header))
    if ensemble:
        plan.append(("d-ensemble-a", ensemble, "E1", "sunbird/ensemble/EnsembleSnapshot.h"))
        plan.append(("d-ensemble-c", ensemble, "E4", "sunbird/ensemble/EnsembleSnapshot.h"))
    if epoll:
        plan.append(("d-epoll-a", epoll, "E1", "core/transport/EpollDispatcher.h"))
        plan.append(("d-epoll-c", epoll, "E4", "core/transport/EpollDispatcher.h"))
    for _round in range(3):
        for name, cmds, edit, header in plan:
            if name == "control":
                one("control", cmds)
            elif cmds:
                one(name, cmds, edit, header)
    Path(args.out).write_text("".join(json.dumps(row) + "\n" for row in rounds))
    print("cases done", flush=True)


if __name__ == "__main__":
    raise SystemExit(main())
