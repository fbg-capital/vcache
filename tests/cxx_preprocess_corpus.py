#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Unto Labs
# SPDX-License-Identifier: Apache-2.0
"""Edit-matrix corpus. Appends one results.jsonl row at a time.

Not part of make test. Run under the task scratch; redirect stdout to a log.
"""

from __future__ import annotations

import argparse
import json
import os
import shlex
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import cxx_preprocess_eval as ev

MATRIX_HEADERS = (
    "protocol/types/SymbolId.h",
    "protocol/types/Price.h",
    "sunbird/ensemble/EnsembleSnapshot.h",
    "core/siren/SirenAlert.h",
    "core/transport/EpollDispatcher.h",
    "algo/ordex/OrderRejectPolicy.h",
    "protocol/dev/imacros.h",
    "cmake/pch/NlohmannJsonExternTemplates.h",
)
HEADER_EDITS = ("E1", "E2", "E3", "E4")


def load_commands(path):
    rows = []
    for entry in json.loads(Path(path).read_text()):
        cmd = entry.get("command", "")
        if " -c " not in f" {cmd} ":
            continue
        if ev.is_pch_producer(cmd):
            continue
        rows.append(entry)
    return rows


def parse_deps(path, build_dir=None, source_root=None):
    """Resolved object path -> repo-relative headers.

    Ninja prints dependencies relative to the physical build directory. A
    symlinked build dir climbs out with several `../` segments; resolving them
    is what makes the header name match the source tree.
    """
    build = Path(build_dir).resolve() if build_dir else None
    source = Path(source_root).resolve() if source_root else None
    out = {}
    current = None
    for line in Path(path).read_text(errors="replace").splitlines():
        if ": #deps" in line and not line.startswith(" "):
            name = line.split(": #deps", 1)[0]
            if build is not None:
                current = str((build / name).resolve())
            else:
                current = name
            out.setdefault(current, set())
            continue
        if current is None or not line.startswith("    "):
            continue
        raw = line.strip()
        rel = None
        if build is not None and source is not None:
            candidate = Path(raw) if raw.startswith("/") else build / raw
            try:
                resolved = candidate.resolve()
                rel = str(resolved.relative_to(source))
            except (OSError, ValueError):
                rel = None
        if rel is None and raw.startswith("../"):
            rel = raw[3:]
        if rel:
            out[current].add(rel)
    return out


def tu_id(entry, source_prefix):
    file = entry["file"]
    if source_prefix and file.startswith(source_prefix):
        return file[len(source_prefix):].lstrip("/")
    return file


def object_key(entry, build_prefix):
    output = entry.get("output", "")
    if output:
        try:
            return str(Path(output).resolve())
        except OSError:
            pass
    if build_prefix and output.startswith(build_prefix):
        return output[len(build_prefix):].lstrip("/")
    return output


def preprocess_command(entry):
    args = shlex.split(entry["command"])
    out = []
    skip = False
    for arg in args:
        if skip:
            skip = False
            continue
        if arg in {"-c", "-o", "-MF", "-MT", "-MQ"}:
            if arg != "-c":
                skip = True
            continue
        if arg.startswith("-o") and arg != "-o":
            continue
        out.append(arg)
    # Drop a trailing output that -o consumed. -E reads the source, which is
    # the last path-like argument the compile command already carries.
    if "-E" not in out:
        out.append("-E")
    return out


def run_pp(cmd, cwd, extra_args=None):
    full = list(cmd)
    if extra_args:
        # Place candidate flags immediately before -E.
        if "-E" in full:
            at = full.index("-E")
            full[at:at] = list(extra_args)
        else:
            full.extend(extra_args)
    env = ev._compiler_env()
    started = time.perf_counter()
    proc = subprocess.run(full, cwd=cwd or None, capture_output=True, env=env)
    elapsed = time.perf_counter() - started
    text = proc.stdout.decode(errors="replace")
    err = proc.stderr.decode(errors="replace")
    return proc.returncode, text, err, elapsed


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--compile-commands", required=True)
    parser.add_argument("--deps", required=True)
    parser.add_argument("--source-prefix", required=True)
    parser.add_argument("--build-prefix", required=True)
    parser.add_argument("--platform", required=True)
    parser.add_argument("--sunbird-revision", required=True)
    parser.add_argument("--vcache-revision", required=True)
    parser.add_argument("--results", required=True)
    parser.add_argument("--source-root", required=True)
    parser.add_argument("--limit", type=int, default=0)
    args = parser.parse_args()
    if not Path(args.results).parent.joinpath("guard-passed.json").exists() and \
            not Path("/build/richard/claude-tmp/forgrok/vcache-axb/guard-passed.json").exists():
        # The bench writes guard-passed.json in the scratch root.
        scratch_guard = Path(args.results).parents[0] / "guard-passed.json"
        if not scratch_guard.exists() and not Path(args.results).parent.joinpath("..", "guard-passed.json").resolve().exists():
            pass
    guard = Path("/build/richard/claude-tmp/forgrok/vcache-axb/guard-passed.json")
    if not guard.exists():
        print("guard-passed.json missing; refuse corpus", file=sys.stderr)
        return 2

    commands = load_commands(args.compile_commands)
    if args.limit:
        commands = commands[: args.limit]
    deps = parse_deps(args.deps, args.build_prefix, args.source_prefix)
    by_tu = []
    fanout = {header: [] for header in MATRIX_HEADERS}
    for entry in commands:
        rel = tu_id(entry, args.source_prefix)
        key = object_key(entry, args.build_prefix)
        headers = deps.get(key, set())
        by_tu.append((rel, entry, headers))
        for header in MATRIX_HEADERS:
            if header in headers:
                fanout[header].append(rel)
    summary = {
        "platform": args.platform,
        "commands": len(commands),
        "dep_objects": len(deps),
        "fanout": {header: len(names) for header, names in fanout.items()},
    }
    print(json.dumps(summary), flush=True)
    roots = [(args.source_prefix.rstrip("/"), "/code/sunbird_strategy"),
             (args.build_prefix.rstrip("/"), "/code/sunbird_strategy/build")]
    existing = ev.load_resume_keys(args.results)
    source_root = Path(args.source_root)

    def row_base(candidate, edit, tu):
        return {
            "candidate": candidate,
            "edit": edit,
            "tu": tu,
            "platform": args.platform,
            "sunbird_revision": args.sunbird_revision,
            "vcache_revision": args.vcache_revision,
        }

    def record(candidate, edit, tu, compiler_before, compiler_after, candidate_before,
               candidate_after, absent_before, absent_after, fell_back, why, elapsed,
               reason=""):
        base = row_base(candidate, edit, tu)
        if ev.should_skip_row(existing, base):
            return
        reasons = ev.disqualify_reasons(
            compiler_before, compiler_after, candidate_before, candidate_after,
            absent_before, absent_after, absent_before, absent_after)
        # baseline-file records absent the same way the compiler does.
        if candidate == "unsound-no-absent":
            reasons = ev.disqualify_reasons(
                compiler_before, compiler_after, candidate_before, candidate_after,
                absent_before, absent_after, [], [])
        item = dict(base)
        item.update({
            "compiler_before": compiler_before,
            "compiler_after": compiler_after,
            "candidate_before": candidate_before,
            "candidate_after": candidate_after,
            "compiler_changed": compiler_before != compiler_after,
            "candidate_changed": candidate_before != candidate_after,
            "fell_back": fell_back,
            "fallback_why": why,
            "disqualified": bool(reasons),
            "reasons": reasons,
            "false_invalidation": ev.false_invalidation(
                compiler_before, compiler_after, candidate_before, candidate_after),
            "elapsed_s": round(elapsed, 4),
            "absent_before": absent_before,
            "absent_after": absent_after,
            "reason": reason,
        })
        ev.append_jsonl(args.results, item)
        existing.add(ev.resume_key(item))
        if reasons and candidate not in {"unsound-main-file-only", "unsound-no-absent", "unsound-constant"}:
            ev.save_counterexample(Path(args.results).parent, candidate, edit, tu, item)

    indexed = {rel: (entry, headers) for rel, entry, headers in by_tu}

    def digest_tu(entry, extra=None):
        code, text, err, elapsed = run_pp(preprocess_command(entry), entry.get("directory"), extra)
        if code != 0:
            return None, err[:300], elapsed
        if not ev.empty_output_is_digest(code, text):
            return None, "empty output", elapsed
        return ev.compiler_digest(text, roots), "", elapsed

    # Baseline, no edit. Candidate baseline-file matches the compiler.
    # A resumed run reuses digests already stored for this revision.
    print("baseline", len(indexed), flush=True)
    baseline = {}
    if Path(args.results).exists():
        for line in Path(args.results).read_text().splitlines():
            if not line.strip():
                continue
            stored = json.loads(line)
            if (stored.get("candidate") == "baseline-file" and stored.get("edit") == "baseline"
                    and stored.get("platform") == args.platform
                    and stored.get("vcache_revision") == args.vcache_revision
                    and stored.get("compiler_before")):
                baseline[stored["tu"]] = stored["compiler_before"]
    for rel, (entry, _headers) in indexed.items():
        if rel in baseline:
            continue
        digest, why, elapsed = digest_tu(entry)
        if digest is None:
            record("baseline-file", "baseline", rel, "", "", "", "", [], [], True, why, elapsed)
            continue
        baseline[rel] = digest
        record("baseline-file", "baseline", rel, digest, digest, digest, digest, [], [], False, "", elapsed)
        main = ev.unsound_main_file_digest(Path(entry["file"]).read_text(errors="replace"))
        record("unsound-main-file-only", "baseline", rel, digest, digest, main, main, [], [], False, "", elapsed)

    def apply_and_measure(edit, header_rel, e8_phase=None, tu_suffix=""):
        path = source_root / header_rel
        if not path.is_file():
            print(f"missing header {header_rel}", flush=True)
            return
        original = path.read_text()
        try:
            if edit == "E11":
                path.write_text(ev.e11_variant(original, 1))
            elif edit == "E6":
                path.write_text(ev.apply_edit(original, "E6"))
            elif edit == "E8":
                path.write_text(ev.apply_edit(original, "E8", e8_phase=e8_phase))
            else:
                path.write_text(ev.apply_edit(original, edit))
        except ev.CorpusAnomaly as exc:
            print(f"anomaly {edit} {header_rel}: {exc}", flush=True)
            path.write_text(original)
            return
        if header_rel.endswith("protocol/dev/imacros.h"):
            affected = list(indexed)
        elif header_rel.endswith("NlohmannJsonExternTemplates.h"):
            affected = [rel for rel, (entry, _headers) in indexed.items()
                        if ev.is_pch_consumer(entry["command"])]
        else:
            affected = [rel for rel, (_entry, headers) in indexed.items() if header_rel in headers]
        pending = []
        for rel in affected:
            tu_name = rel + tu_suffix
            edits = ("E6", "E6 no-op") if edit == "E6" else (edit,)
            if any(ev.should_skip_row(existing, row_base("baseline-file", name, tu_name))
                   for name in edits):
                continue
            pending.append(rel)
        if not pending:
            print(f"{edit} {header_rel} skipped", flush=True)
            path.write_text(original)
            return
        print(f"{edit} {header_rel} tus={len(pending)}", flush=True)
        noop = 0
        changed = 0
        for rel in pending:
            entry, _headers = indexed[rel]
            before = baseline.get(rel)
            if before is None:
                continue
            digest, why, elapsed = digest_tu(entry)
            if digest is None:
                record("baseline-file", edit, rel + tu_suffix, before, "", "", "", [], [], True, why, elapsed)
                continue
            try:
                ev.check_compiler_expectation(edit, before, digest, e8_phase=e8_phase)
            except ev.CorpusAnomaly as exc:
                anomaly = Path(args.results).parent / "anomaly.txt"
                with anomaly.open("a") as out:
                    out.write(f"{edit} {rel} {header_rel}: {exc}\n")
                print(f"I2 anomaly {exc} tu={rel}", flush=True)
                path.write_text(original)
                raise SystemExit(3)
            main_before = ev.unsound_main_file_digest(Path(entry["file"]).read_text(errors="replace"))
            tu_name = rel + tu_suffix
            row_edit = edit
            reason = ""
            if edit == "E6" and digest == before:
                row_edit = "E6 no-op"
                reason = "both headers already included"
                noop += 1
            elif edit == "E6":
                changed += 1
            record("baseline-file", row_edit, tu_name, before, digest, before, digest,
                   [], [], False, "", elapsed, reason)
            record("unsound-main-file-only", row_edit, tu_name, before, digest, main_before, main_before,
                   [], [], False, "", elapsed, reason)
            const = ev.unsound_constant_digest(digest)
            record("unsound-constant", row_edit, tu_name, before, digest, ev.CONSTANT_DIGEST, const,
                   [], [], False, "", elapsed, reason)
            if digest != before and const == ev.CONSTANT_DIGEST:
                ev.save_counterexample(Path(args.results).parent, "unsound-constant", edit, tu_name, {
                    "compiler_before": before, "compiler_after": digest, "candidate": ev.CONSTANT_DIGEST,
                })
            if edit == "E4" and digest != before:
                ev.save_counterexample(Path(args.results).parent, "unsound-main-file-only", "E4", tu_name, {
                    "compiler_before": before, "compiler_after": digest,
                    "candidate_before": main_before, "candidate_after": main_before,
                })
        path.write_text(original)
        if edit == "E6":
            line = (f"\nE6 no-op rows ({args.platform}): {noop}; "
                    f"E6 changed rows: {changed}. "
                    f"No-op reason: both headers already included.\n")
            print(line.strip(), flush=True)
            with (Path(args.results).parent / "timings.md").open("a") as out:
                out.write(line)
        # I3: one sentinel TU must match its pre-matrix baseline after revert.
        if affected:
            rel = affected[0]
            entry, _headers = indexed[rel]
            digest, _why, _elapsed = digest_tu(entry)
            if digest is not None and rel in baseline:
                ev.check_edit_baseline(baseline[rel], digest)

    for header in MATRIX_HEADERS:
        for edit in HEADER_EDITS:
            # The header is part of the row key so E1 on SymbolId.h does not
            # resume-skip E1 on Price.h for a TU that includes both.
            apply_and_measure(edit, header, tu_suffix="#" + header)
    apply_and_measure("E8", "cmake/pch/NlohmannJsonExternTemplates.h", e8_phase="E1", tu_suffix="#E8E1")
    apply_and_measure("E8", "cmake/pch/NlohmannJsonExternTemplates.h", e8_phase="E4", tu_suffix="#E8E4")
    apply_and_measure("E5", "protocol/types/SymbolId.h")
    apply_and_measure("E6", "protocol/types/SymbolId.h")
    for index in range(1, 10):
        # Nine successive comment variants. Each must leave the compiler digest unchanged.
        path = source_root / "protocol/types/SymbolId.h"
        original = path.read_text()
        path.write_text(ev.e11_variant(original, index))
        affected = [rel for rel, (_e, headers) in indexed.items()
                    if "protocol/types/SymbolId.h" in headers]
        print(f"E11 v{index} tus={len(affected)}", flush=True)
        for rel in affected[:1] if args.limit else affected:
            entry, _headers = indexed[rel]
            before = baseline.get(rel)
            if before is None:
                continue
            digest, why, elapsed = digest_tu(entry)
            if digest is None:
                continue
            ev.check_compiler_expectation("E11", before, digest)
            record("baseline-file", "E11", f"{rel}#v{index}", before, digest, before, digest, [], [], False, "", elapsed)
        path.write_text(original)
    print("corpus done", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
