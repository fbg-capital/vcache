#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Unto Labs
# SPDX-License-Identifier: Apache-2.0
"""Host corpus rows for baseline-stream and clang-minws.

baseline-stream hashes the same -E bytes through cxx_pp_digest --stdin.
clang-minws is a second -E with -fminimize-whitespace. Rows append to results.jsonl.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import cxx_preprocess_corpus as corpus
import cxx_preprocess_eval as ev

PREFIX = "/build/richard/claude-tmp/worktrees/sunbird/axb-a"
DIGEST = "/build/richard/claude-tmp/worktrees/vcache/axb/bin/cxx_pp_digest"
RESULTS = "/build/richard/claude-tmp/forgrok/vcache-axb/results.jsonl"
ROOTS = PREFIX + "=/code/sunbird_strategy," + PREFIX + "/build=/code/sunbird_strategy/build"
REV = "31bb671"


def digest_bytes(data, stdin):
    if stdin:
        proc = subprocess.run([DIGEST, "--roots", ROOTS, "--stdin"], input=data, capture_output=True)
    else:
        tmp = tempfile.NamedTemporaryFile(prefix="axb-pp-", delete=False)
        try:
            tmp.write(data)
            tmp.close()
            proc = subprocess.run([DIGEST, "--roots", ROOTS, tmp.name], capture_output=True)
        finally:
            os.unlink(tmp.name)
    if proc.returncode != 0:
        return None
    return proc.stdout.decode().strip()


def one_tu(entry, extra):
    cmd = corpus.preprocess_command(entry)
    if extra and "-E" in cmd:
        cmd = cmd[:cmd.index("-E")] + extra + cmd[cmd.index("-E"):]
    env = ev._compiler_env()
    proc = subprocess.run(cmd, cwd=entry.get("directory") or None, env=env, capture_output=True)
    if proc.returncode != 0 or not proc.stdout:
        return None
    file_d = digest_bytes(proc.stdout, False)
    stream_d = digest_bytes(proc.stdout, True) if not extra else None
    return file_d, stream_d, len(proc.stdout)


def main():
    commands = corpus.load_commands(PREFIX + "/build/compile_commands.json")
    deps = corpus.parse_deps(
        "/build/richard/claude-tmp/forgrok/vcache-axb/logs/host-ninja-deps.txt",
        PREFIX + "/build", PREFIX)
    indexed = {}
    for entry in commands:
        rel = corpus.tu_id(entry, PREFIX)
        headers = deps.get(corpus.object_key(entry, PREFIX + "/build"), set())
        indexed[rel] = (entry, headers)
    symbol = [rel for rel, (_e, h) in indexed.items() if "protocol/types/SymbolId.h" in h]
    pch = [rel for rel, (e, _h) in indexed.items() if ev.is_pch_consumer(e["command"])]
    print(json.dumps({"symbol": len(symbol), "pch": len(pch)}), flush=True)
    existing = ev.load_resume_keys(RESULTS)
    source = Path(PREFIX)

    def record(candidate, edit, tu, compiler_d, candidate_d, base_compiler, base_candidate, nbytes):
        row = {
            "candidate": candidate, "edit": edit, "tu": tu, "platform": "host",
            "sunbird_revision": "c76c0222f", "vcache_revision": REV,
        }
        if ev.should_skip_row(existing, row):
            return
        reasons = ev.disqualify_reasons(
            base_compiler, compiler_d, base_candidate, candidate_d, [], [], [], [])
        row.update({
            "compiler_before": base_compiler, "compiler_after": compiler_d,
            "candidate_before": base_candidate, "candidate_after": candidate_d,
            "compiler_changed": base_compiler != compiler_d,
            "candidate_changed": base_candidate != candidate_d,
            "stream_equals_file": candidate == "baseline-stream" and candidate_d == compiler_d,
            "fell_back": candidate_d is None, "fallback_why": "" if candidate_d else "",
            "disqualified": bool(reasons), "reasons": reasons,
            "false_invalidation": ev.false_invalidation(base_compiler, compiler_d, base_candidate, candidate_d),
            "bytes": nbytes, "reason": "",
        })
        ev.append_jsonl(RESULTS, row)
        existing.add(ev.resume_key(row))

    def measure(edit, header, tus, mutate):
        path = source / header
        original = path.read_bytes()
        pending = [tu for tu in tus if not ev.should_skip_row(existing, {
            "candidate": "clang-minws", "edit": edit, "tu": tu + "#" + header,
            "platform": "host", "sunbird_revision": "c76c0222f", "vcache_revision": REV,
        })]
        if not pending:
            print(edit, header, "skipped", flush=True)
            return
        print(edit, header, "tus", len(pending), flush=True)
        path.write_bytes(mutate(original))
        try:
            def work(rel):
                entry = indexed[rel][0]
                plain = one_tu(entry, None)
                minws = one_tu(entry, ["-fminimize-whitespace"])
                return rel, plain, minws
            with ThreadPoolExecutor(max_workers=8) as pool:
                for rel, plain, minws in pool.map(work, pending):
                    tu = rel + "#" + header
                    if plain is None or plain[0] is None:
                        record("baseline-stream", edit, tu, "", None, "", "", 0)
                        continue
                    file_d, stream_d, nbytes = plain
                    # Baseline for I1 is this same edit's plain digest versus the
                    # no-edit plain digest stored on the row's before fields.
                    record("baseline-file", edit, tu, file_d, file_d, file_d, file_d, nbytes)
                    record("baseline-stream", edit, tu, file_d, stream_d, file_d, stream_d, nbytes)
                    if minws and minws[0]:
                        record("clang-minws", edit, tu, file_d, minws[0], file_d, minws[0], minws[2])
                    else:
                        record("clang-minws", edit, tu, file_d, None, file_d, None, 0)
        finally:
            path.write_bytes(original)

    # The I1 comparison needs a no-edit digest. Capture it once per TU, then
    # each edit compares against that pair.
    bases = {}

    def fill_base(tus, tag):
        need = [tu for tu in tus if tu not in bases]
        print("base", tag, len(need), flush=True)
        def work(rel):
            return rel, one_tu(indexed[rel][0], None), one_tu(indexed[rel][0], ["-fminimize-whitespace"])
        with ThreadPoolExecutor(max_workers=8) as pool:
            for rel, plain, minws in pool.map(work, need):
                bases[rel] = (plain, minws)

    def measure_vs_base(edit, header, tus, mutate, label=None):
        label = label or edit
        path = source / header
        original = path.read_bytes()
        print(label, len(tus), flush=True)
        path.write_bytes(mutate(original))
        try:
            def work(rel):
                return rel, one_tu(indexed[rel][0], None), one_tu(indexed[rel][0], ["-fminimize-whitespace"])
            with ThreadPoolExecutor(max_workers=8) as pool:
                done = list(pool.map(work, tus))
        finally:
            path.write_bytes(original)
        for rel, plain, minws in done:
            tu = rel + "#" + label
            base_plain, base_min = bases.get(rel, (None, None))
            if not plain or not plain[0] or not base_plain or not base_plain[0]:
                continue
            file_d, stream_d, nbytes = plain
            bfile, bstream = base_plain[0], base_plain[1]
            record("baseline-stream", edit, tu, file_d, stream_d, bfile, bstream, nbytes)
            if minws and minws[0] and base_min and base_min[0]:
                record("clang-minws", edit, tu, file_d, minws[0], bfile, base_min[0], minws[2])
        equal = sum(1 for _r, plain, _m in done if plain and plain[0] and plain[1] and plain[0] == plain[1])
        total = sum(1 for _r, plain, _m in done if plain and plain[0] and plain[1])
        differ = 0
        for _r, plain, minws in done:
            if plain and minws and plain[0] and minws[0] and plain[0] != minws[0]:
                differ += 1
        print(json.dumps({"edit": label, "stream_eq": equal, "stream_n": total, "minws_differs": differ}), flush=True)

    fill_base(symbol, "symbol")
    # baseline row
    for rel in symbol:
        plain, minws = bases[rel]
        if not plain or not plain[0]:
            continue
        record("baseline-stream", "baseline", rel + "#baseline", plain[0], plain[0], plain[1] or "", plain[1] or "", plain[2])
    text = (source / "protocol/types/SymbolId.h").read_bytes()
    measure_vs_base("E1", "protocol/types/SymbolId.h", symbol, lambda b: b + b"// axb E1\n")
    measure_vs_base("E3", "protocol/types/SymbolId.h", symbol, lambda b: b + b"#define AXB_UNUSED_E3 1\n")
    measure_vs_base("E4", "protocol/types/SymbolId.h", symbol,
                    lambda b: b + b"#define AXB_E4 1\nextern int axb_e4_used[AXB_E4];\n")
    measure_vs_base("E5", "protocol/types/SymbolId.h", symbol, lambda b: b + (
        b"#if __has_builtin(__builtin_expect)\nextern int axb_e5_builtin;\n"
        b"#else\nextern int axb_e5_nobuiltin;\n#endif\n"))
    for index in range(1, 10):
        measure_vs_base("E11", "protocol/types/SymbolId.h", symbol,
                        lambda b, index=index: b + f"// axb E11 v{index}\n".encode(),
                        label="E11v" + str(index))
    fill_base(pch, "pch")
    pch_header = "cmake/pch/NlohmannJsonExternTemplates.h"
    measure_vs_base("E8", pch_header, pch, lambda b: b + b"// axb E1\n", label="E8E1")
    measure_vs_base("E8", pch_header, pch,
                    lambda b: b + b"#define AXB_E4 1\nextern int axb_e4_used[AXB_E4];\n",
                    label="E8E4")
    print("prototypes done", flush=True)


if __name__ == "__main__":
    raise SystemExit(main())
