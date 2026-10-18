#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Unto Labs
# SPDX-License-Identifier: Apache-2.0
"""Host corpus for clang-rewrite-includes.

The candidate is a derived-key measurement. Its digest is the normalised
-E -frewrite-includes text. The compiler digest on the same row is plain -E.
A benign edit that changes the rewrite digest is one this mode fails to ignore.

Not part of make test. Redirect stdout to a log.
"""

from __future__ import annotations

import json
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import cxx_preprocess_corpus as corpus
import cxx_preprocess_eval as ev
import cxx_preprocess_matrix_rest as rest

PREFIX = "/build/richard/claude-tmp/worktrees/sunbird/axb-a"
DIGEST = "/build/richard/claude-tmp/worktrees/vcache/axb/bin/cxx_pp_digest"
RESULTS = "/build/richard/claude-tmp/forgrok/vcache-axb/results.jsonl"
ROOTS = PREFIX + "=/code/sunbird_strategy," + PREFIX + "/build=/code/sunbird_strategy/build"
DEPS = "/build/richard/claude-tmp/forgrok/vcache-axb/logs/host-ninja-deps.txt"
SUNBIRD = "c76c0222f"


def digest_bytes(data):
    proc = subprocess.run([DIGEST, "--roots", ROOTS, "--stdin"], input=data, capture_output=True)
    if proc.returncode != 0:
        return None
    return proc.stdout.decode().strip()


def one(entry, extra):
    cmd = corpus.preprocess_command(entry)
    if extra and "-E" in cmd:
        at = cmd.index("-E")
        cmd = cmd[:at] + extra + cmd[at:]
    proc = subprocess.run(cmd, cwd=entry.get("directory") or None, env=ev._compiler_env(), capture_output=True)
    if proc.returncode != 0 or not proc.stdout:
        return None
    digest = digest_bytes(proc.stdout)
    if digest is None:
        return None
    return digest, len(proc.stdout)


def main():
    rev = subprocess.run(
        ["git", "-C", str(Path(__file__).resolve().parents[1]), "rev-parse", "--short", "HEAD"],
        capture_output=True, text=True, check=True).stdout.strip()
    print(json.dumps({"vcache_revision": rev}), flush=True)
    commands = corpus.load_commands(PREFIX + "/build/compile_commands.json")
    deps = corpus.parse_deps(DEPS, PREFIX + "/build", PREFIX)
    indexed = {}
    for entry in commands:
        rel = corpus.tu_id(entry, PREFIX)
        headers = deps.get(corpus.object_key(entry, PREFIX + "/build"), set())
        indexed[rel] = (entry, headers)
    symbol = [rel for rel, (_e, headers) in indexed.items() if "protocol/types/SymbolId.h" in headers]
    pch = [rel for rel, (entry, _h) in indexed.items() if ev.is_pch_consumer(entry["command"])]
    print(json.dumps({"symbol": len(symbol), "pch": len(pch)}), flush=True)
    existing = ev.load_resume_keys(RESULTS)
    source = Path(PREFIX)
    norm_roots = [(PREFIX, "/code/sunbird_strategy"), (PREFIX + "/build", "/code/sunbird_strategy/build")]

    def record(edit, tu, compiler_before, compiler_after, candidate_before, candidate_after, nbytes, reason):
        row = {
            "candidate": "clang-rewrite-includes",
            "edit": edit,
            "tu": tu,
            "platform": "host",
            "sunbird_revision": SUNBIRD,
            "vcache_revision": rev,
        }
        if ev.should_skip_row(existing, row):
            return
        reasons = []
        if compiler_before != compiler_after and candidate_before == candidate_after:
            reasons.append("compiler digest changed and candidate digest did not")
        row.update({
            "compiler_before": compiler_before,
            "compiler_after": compiler_after,
            "candidate_before": candidate_before,
            "candidate_after": candidate_after,
            "compiler_changed": compiler_before != compiler_after,
            "candidate_changed": candidate_before != candidate_after,
            "fails_to_ignore": compiler_before == compiler_after and candidate_before != candidate_after,
            "fell_back": candidate_after is None,
            "fallback_why": "" if candidate_after else "rewrite -E failed",
            "disqualified": bool(reasons),
            "reasons": reasons,
            "false_invalidation": compiler_before == compiler_after and candidate_before != candidate_after,
            "bytes": nbytes,
            "reason": reason,
        })
        ev.append_jsonl(RESULTS, row)
        existing.add(ev.resume_key(row))

    bases = {}

    def fill(tus, tag):
        need = [rel for rel in tus if rel not in bases]
        print("base", tag, len(need), flush=True)

        def work(rel):
            entry = indexed[rel][0]
            return rel, one(entry, None), one(entry, ["-frewrite-includes"])

        with ThreadPoolExecutor(max_workers=8) as pool:
            for rel, plain, rewrite in pool.map(work, need):
                bases[rel] = (plain, rewrite)

    def measure(edit, header, tus, mutate, label=None):
        label = label or edit
        path = source / header
        original = path.read_bytes()
        print(label, len(tus), flush=True)
        path.write_bytes(mutate(original))
        try:
            def work(rel):
                entry = indexed[rel][0]
                return rel, one(entry, None), one(entry, ["-frewrite-includes"])

            with ThreadPoolExecutor(max_workers=8) as pool:
                done = list(pool.map(work, tus))
        finally:
            path.write_bytes(original)
        changed = ignored = fell = 0
        for rel, plain, rewrite in done:
            base_plain, base_rewrite = bases.get(rel, (None, None))
            if not plain or not base_plain or not rewrite or not base_rewrite:
                fell += 1
                record(edit, rel + "#" + label, "", "", None, None, 0, "rewrite -E failed")
                continue
            record(edit, rel + "#" + label, base_plain[0], plain[0], base_rewrite[0], rewrite[0],
                   rewrite[1], "")
            if base_plain[0] == plain[0] and base_rewrite[0] != rewrite[0]:
                ignored += 1
            if base_plain[0] != plain[0] and base_rewrite[0] != rewrite[0]:
                changed += 1
        print(json.dumps({
            "edit": label, "tus": len(done), "fails_to_ignore": ignored,
            "both_changed": changed, "fell_back": fell,
        }), flush=True)

    fill(symbol, "symbol")
    measure("E1", "protocol/types/SymbolId.h", symbol, lambda b: b + b"// axb E1\n")
    measure("E3", "protocol/types/SymbolId.h", symbol, lambda b: b + b"#define AXB_UNUSED_E3 1\n")
    measure("E4", "protocol/types/SymbolId.h", symbol,
            lambda b: b + b"#define AXB_E4 1\nextern int axb_e4_used[AXB_E4];\n")
    measure("E5", "protocol/types/SymbolId.h", symbol, lambda b: b + (
        b"#if __has_builtin(__builtin_expect)\nextern int axb_e5_builtin;\n"
        b"#else\nextern int axb_e5_nobuiltin;\n#endif\n"))
    for index in range(1, 10):
        measure("E11", "protocol/types/SymbolId.h", symbol,
                lambda b, index=index: b + f"// axb E11 v{index}\n".encode(),
                label="E11v" + str(index))
    fill(pch, "pch")
    pch_header = "cmake/pch/NlohmannJsonExternTemplates.h"
    measure("E8", pch_header, pch, lambda b: b + b"// axb E1\n", label="E8E1")
    measure("E8", pch_header, pch,
            lambda b: b + b"#define AXB_E4 1\nextern int axb_e4_used[AXB_E4];\n",
            label="E8E4")

    # E7: one TU per fan-out class. The shadow is removed before the next TU.
    scratch = Path("/build/richard/claude-tmp/forgrok/vcache-axb/rest-scratch")
    scratch.mkdir(parents=True, exist_ok=True)
    for name, rel in rest.choose_tus(indexed):
        entry = indexed[rel][0]
        absent_file = scratch / "rewrite-absent.txt"
        code, text, _err, _elapsed = corpus.run_pp(
            corpus.preprocess_command(entry), entry.get("directory"),
            ["-fabsent-deps=" + str(absent_file)])
        if code != 0 or not text or not absent_file.exists():
            print(json.dumps({"edit": "E7", "class": name, "tu": rel, "fell_back": True}), flush=True)
            continue
        absent = [line.strip() for line in absent_file.read_text(errors="replace").splitlines() if line.strip()]
        picked = rest.pick_shadow(absent, rest.included_paths(text), PREFIX, PREFIX + "/build")
        if picked is None:
            print(json.dumps({"edit": "E7", "class": name, "tu": rel, "shadow": None}), flush=True)
            continue
        shadow, real = picked
        shadow_path = Path(shadow)
        if rel not in bases:
            bases[rel] = (one(entry, None), one(entry, ["-frewrite-includes"]))
        base_plain, base_rewrite = bases[rel]
        try:
            body = Path(real).read_bytes()
            if not body.endswith(b"\n"):
                body += b"\n"
            shadow_path.write_bytes(body + b"/* axb E7 */\n")
            plain = one(entry, None)
            rewrite = one(entry, ["-frewrite-includes"])
        finally:
            if shadow_path.exists():
                shadow_path.unlink()
        if not plain or not base_plain or not rewrite or not base_rewrite:
            record("E7", rel + "#" + name, "", "", None, None, 0, "rewrite -E failed")
            print(json.dumps({"edit": "E7", "class": name, "fell_back": True}), flush=True)
            continue
        # Plain -E must change. An unchanged plain digest is the E7 anomaly.
        if plain[0] == base_plain[0]:
            raise ev.CorpusAnomaly(f"E7 {rel} {name}: compiler digest unchanged; expected changed")
        record("E7", rel + "#" + name, base_plain[0], plain[0], base_rewrite[0], rewrite[0],
               rewrite[1], f"shadow {shadow}")
        print(json.dumps({
            "edit": "E7", "class": name, "tu": rel,
            "rewrite_changed": base_rewrite[0] != rewrite[0],
            "fails_to_ignore": False,
        }), flush=True)
    # The header must be back to its bytes. One sentinel preprocess is enough.
    sentinel = indexed[symbol[0]][0]
    again = one(sentinel, None)
    if again and bases[symbol[0]][0] and again[0] != bases[symbol[0]][0][0]:
        raise ev.MatrixNotReverted("per-edit baseline digest differs from the pre-matrix baseline")
    print("rewrite done", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
