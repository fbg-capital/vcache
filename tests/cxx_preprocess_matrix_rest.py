#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Unto Labs
# SPDX-License-Identifier: Apache-2.0
"""E7, E9 and E10 for baseline-file.

E7 creates one header at a path from that TU's -fabsent-deps list, chosen
because the same relative name was included from a later directory. E9
regenerates the wiregen layout header with one added field. E10 preprocesses
the same revision at a second worktree path and compares root-mapped digests.

Not part of make test. Redirect stdout to a log.
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import cxx_preprocess_corpus as corpus
import cxx_preprocess_eval as ev

GENERATED = (
    "WiregenEmbeddedLayoutRegistration.inc",
    "WiregenLayoutAsserts.inc",
    "WiregenLayoutAsserts.cc",
    "WiregenEmbeddedLayoutCheck.cc",
    "WiregenProtocolMaxMessageBytes.wiregen.h",
)


def choose_tus(indexed):
    """One TU per fan-out class, plus one non-PCH TU."""
    chosen = []
    used = set()

    def take(name, pred):
        for rel, (entry, headers) in indexed.items():
            if rel in used:
                continue
            if pred(entry, headers):
                used.add(rel)
                chosen.append((name, rel))
                return
        for rel, (entry, headers) in indexed.items():
            if pred(entry, headers):
                chosen.append((name, rel))
                return
        print(f"no TU for {name}", flush=True)

    for header in corpus.MATRIX_HEADERS:
        if header.endswith("imacros.h"):
            take(header, lambda entry, headers: True)
        elif header.endswith("NlohmannJsonExternTemplates.h"):
            take(header, lambda entry, headers: ev.is_pch_consumer(entry["command"]))
        else:
            take(header, lambda entry, headers, header=header: header in headers)
    take("non-pch", lambda entry, headers: not ev.is_pch_consumer(entry["command"]))
    return chosen


def included_paths(text):
    found = []
    seen = set()
    for line in text.splitlines():
        if not line.startswith("# ") or '"' not in line:
            continue
        path = line.split('"', 2)[1]
        if path in seen or path.startswith("<"):
            continue
        seen.add(path)
        found.append(path)
    return found


def pick_shadow(absent, included, source_root, build_prefix):
    """An absent path under the source root that shadows an included header."""
    root = Path(source_root).resolve()
    build = Path(build_prefix).resolve()
    real_files = []
    for inc in included:
        path = Path(inc)
        if path.is_file():
            real_files.append(path)
    cands = []
    for raw in absent:
        path = Path(raw)
        if path.suffix not in {".h", ".hpp", ".hh"}:
            continue
        try:
            path.resolve().relative_to(root)
        except ValueError:
            continue
        if path.exists() or not path.parent.is_dir():
            continue
        try:
            path.resolve().relative_to(build)
            continue
        except ValueError:
            pass
        parts = path.parts
        best = None
        best_n = 0
        for inc in real_files:
            ip = inc.parts
            n = 0
            while n < len(parts) and n < len(ip) and parts[-1 - n] == ip[-1 - n]:
                n += 1
            if n > best_n:
                best_n = n
                best = inc
        if best is None or best_n < 1:
            continue
        size = best.stat().st_size
        if size > 200_000:
            continue
        at_root = path.parent == root
        cands.append((0 if at_root else 1, size, str(path), str(best)))
    cands.sort()
    if not cands:
        return None
    return cands[0][2], cands[0][3]


def add_wiregen_field(ir_dir):
    """Append axb_e9 to a type that occurs in only one protocol IR file."""
    counts = {}
    for path in ir_dir.glob("*.wiregen.json"):
        data = json.loads(path.read_text())
        for ty in data.get("types") or []:
            counts[ty["name"]] = counts.get(ty["name"], 0) + 1
    for path in sorted(ir_dir.glob("*.wiregen.json")):
        data = json.loads(path.read_text())
        for ty in data.get("types") or []:
            if counts.get(ty["name"]) != 1:
                continue
            fields = ty.get("fields") or []
            for field in fields:
                extras = field.get("extras")
                if not isinstance(extras, dict) or extras.get("accessibility") != "public":
                    continue
                if field.get("name") in {None, "_base"}:
                    continue
                clone = json.loads(json.dumps(field))
                clone["name"] = "axb_e9"
                fields.append(clone)
                ty["fields"] = fields
                path.write_text(json.dumps(data))
                return ty["name"], path.name
    raise ev.CorpusAnomaly("no unique wiregen type accepted an added field")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--compile-commands", required=True)
    parser.add_argument("--deps", required=True)
    parser.add_argument("--source-prefix", required=True)
    parser.add_argument("--build-prefix", required=True)
    parser.add_argument("--source-root", required=True)
    parser.add_argument("--platform", required=True)
    parser.add_argument("--sunbird-revision", required=True)
    parser.add_argument("--vcache-revision", required=True)
    parser.add_argument("--results", required=True)
    parser.add_argument("--second-root", default="")
    parser.add_argument("--scratch", required=True)
    args = parser.parse_args()
    guard_paths = (
        Path("/build/richard/claude-tmp/forgrok/vcache-axb/guard-passed.json"),
        Path("/build/richard/test-scratch/vcache-axb/guard-passed.json"),
    )
    if not any(path.exists() for path in guard_paths):
        print("guard-passed.json missing; refuse corpus", file=sys.stderr)
        return 2

    commands = corpus.load_commands(args.compile_commands)
    deps = corpus.parse_deps(args.deps, args.build_prefix, args.source_prefix)
    indexed = {}
    for entry in commands:
        rel = corpus.tu_id(entry, args.source_prefix)
        headers = deps.get(corpus.object_key(entry, args.build_prefix), set())
        indexed[rel] = (entry, headers)
    print(json.dumps({"platform": args.platform, "commands": len(commands), "tus": len(indexed)}), flush=True)
    roots = [
        (args.source_prefix.rstrip("/"), "/code/sunbird_strategy"),
        (args.build_prefix.rstrip("/"), "/code/sunbird_strategy/build"),
    ]
    existing = ev.load_resume_keys(args.results)
    scratch = Path(args.scratch)
    scratch.mkdir(parents=True, exist_ok=True)
    anomaly_path = Path(args.results).parent / "anomaly.txt"
    # The el10 container writes results under the mounted scratch. The host
    # copy of anomaly.txt is the one beside the main results file when the
    # results path is already the task scratch; otherwise append there too.
    task_anomaly = Path("/build/richard/claude-tmp/forgrok/vcache-axb/anomaly.txt")

    def note_anomaly(text):
        print(f"I2 anomaly {text}", flush=True)
        for path in {anomaly_path, task_anomaly}:
            if not path.parent.is_dir():
                continue
            with path.open("a") as out:
                out.write(text + "\n")

    def record(edit, tu, before, after, absent_before, absent_after, elapsed, reason):
        row = {
            "candidate": "baseline-file",
            "edit": edit,
            "tu": tu,
            "platform": args.platform,
            "sunbird_revision": args.sunbird_revision,
            "vcache_revision": args.vcache_revision,
        }
        if ev.should_skip_row(existing, row):
            return
        reasons = ev.disqualify_reasons(
            before, after, before, after, absent_before, absent_after, absent_before, absent_after)
        row.update({
            "compiler_before": before,
            "compiler_after": after,
            "candidate_before": before,
            "candidate_after": after,
            "compiler_changed": before != after,
            "candidate_changed": before != after,
            "fell_back": False,
            "fallback_why": "",
            "disqualified": bool(reasons),
            "reasons": reasons,
            "false_invalidation": ev.false_invalidation(before, after, before, after),
            "elapsed_s": round(elapsed, 4),
            "absent_before": absent_before,
            "absent_after": absent_after,
            "reason": reason,
        })
        ev.append_jsonl(args.results, row)
        existing.add(ev.resume_key(row))

    def digest_with_absent(entry):
        absent_file = scratch / f"absent-{args.platform}.txt"
        code, text, err, elapsed = corpus.run_pp(
            corpus.preprocess_command(entry), entry.get("directory"),
            ["-fabsent-deps=" + str(absent_file)])
        if code != 0 or not text:
            return None, [], err[:300], elapsed
        listed = []
        if absent_file.exists():
            listed = [line.strip() for line in absent_file.read_text(errors="replace").splitlines() if line.strip()]
        return ev.compiler_digest(text, roots), listed, text, elapsed

    # E7
    for name, rel in choose_tus(indexed):
        tu = rel + "#" + name
        if ev.should_skip_row(existing, {
            "candidate": "baseline-file", "edit": "E7", "tu": tu,
            "platform": args.platform, "sunbird_revision": args.sunbird_revision,
            "vcache_revision": args.vcache_revision,
        }):
            print(f"E7 {name} skipped", flush=True)
            continue
        entry = indexed[rel][0]
        before, absent, text, elapsed = digest_with_absent(entry)
        if before is None:
            note_anomaly(f"E7 {rel} {name}: compiler -E failed: {text}")
            return 3
        picked = pick_shadow(absent, included_paths(text), args.source_root, args.build_prefix)
        if picked is None:
            note_anomaly(f"E7 {rel} {name}: no absent path shadows an included header")
            return 3
        shadow, real = picked
        shadow_path = Path(shadow)
        print(f"E7 {name} tu={rel} shadow={shadow}", flush=True)
        try:
            body = Path(real).read_bytes()
            if not body.endswith(b"\n"):
                body += b"\n"
            shadow_path.write_bytes(body + b"/* axb E7 */\n")
            after, absent_after, text_after, elapsed_after = digest_with_absent(entry)
            if after is None:
                note_anomaly(f"E7 {rel} {name}: compiler -E failed after creating {shadow}: {text_after}")
                return 3
            try:
                ev.check_compiler_expectation("E7", before, after)
            except ev.CorpusAnomaly as exc:
                note_anomaly(f"E7 {rel} {name}: {exc}")
                return 3
            record("E7", tu, before, after, [shadow], [], elapsed + elapsed_after,
                   f"shadow {shadow} of {real}")
        finally:
            if shadow_path.exists():
                shadow_path.unlink()
        restored, _absent_r, _text_r, _elapsed_r = digest_with_absent(entry)
        if restored != before:
            note_anomaly(f"E7 {rel} {name}: digest did not return after removing {shadow}")
            return 3

    # E9
    layout_rel = None
    for rel, (entry, _headers) in indexed.items():
        if rel.endswith("protocol/WiregenLayoutAsserts.cc"):
            layout_rel = rel
            break
    if layout_rel is None:
        note_anomaly("E9: WiregenLayoutAsserts.cc is not in the compile commands")
        return 3
    tu = layout_rel + "#E9"
    if not ev.should_skip_row(existing, {
        "candidate": "baseline-file", "edit": "E9", "tu": tu,
        "platform": args.platform, "sunbird_revision": args.sunbird_revision,
        "vcache_revision": args.vcache_revision,
    }):
        entry = indexed[layout_rel][0]
        before, _absent, _text, elapsed = digest_with_absent(entry)
        if before is None:
            note_anomaly(f"E9 {layout_rel}: compiler -E failed: {_text}")
            return 3
        protocol = Path(args.build_prefix) / "protocol"
        ir = scratch / f"e9-ir-{args.platform}"
        ir.mkdir(exist_ok=True)
        for path in protocol.glob("*.wiregen.json"):
            shutil.copy(path, ir / path.name)
        sunbird_scripts = Path(args.source_root) / "scripts"
        sys.path.insert(0, str(sunbird_scripts))
        import wiregen_layout
        type_name, ir_file = add_wiregen_field(ir)
        plans = wiregen_layout.load_plans(ir, Path(args.source_root) / "protocol" / "wire")
        backups = {name: (protocol / name).read_bytes() for name in GENERATED}
        print(f"E9 regenerate {type_name} from {ir_file} tu={layout_rel}", flush=True)
        try:
            wiregen_layout.write_outputs(plans, protocol)
            after, _a2, text_after, elapsed_after = digest_with_absent(entry)
            if after is None:
                note_anomaly(f"E9 {layout_rel}: compiler -E failed after regeneration: {text_after}")
                return 3
            try:
                ev.check_compiler_expectation("E9", before, after)
            except ev.CorpusAnomaly as exc:
                note_anomaly(f"E9 {layout_rel}: {exc}")
                return 3
            record("E9", tu, before, after, [], [], elapsed + elapsed_after,
                   f"regenerated WiregenLayoutAsserts.inc with field axb_e9 on {type_name}")
        finally:
            for name, body in backups.items():
                (protocol / name).write_bytes(body)
        restored, _ar, _tr, _er = digest_with_absent(entry)
        if restored != before:
            note_anomaly("E9 digest did not return after restoring generated files")
            return 3

    # E10
    if not args.second_root:
        print("E10 skipped: no second root", flush=True)
        print("matrix rest done", flush=True)
        return 0
    second = args.second_root.rstrip("/")
    build_name = Path(args.build_prefix).name
    link = Path(second) / build_name
    created_link = False
    if not link.exists():
        link.symlink_to(args.build_prefix)
        created_link = True
    second_roots = [
        (second, "/code/sunbird_strategy"),
        (str(link), "/code/sunbird_strategy/build"),
    ]

    def digest_at(entry, use_roots):
        code, text, err, elapsed = corpus.run_pp(corpus.preprocess_command(entry), entry.get("directory"))
        if code != 0 or not text:
            return None, err[:300], elapsed
        return ev.compiler_digest(text, use_roots), "", elapsed

    try:
        non_pch = [rel for rel, (entry, _h) in indexed.items() if not ev.is_pch_consumer(entry["command"])]
        equal = 0
        for rel in non_pch:
            tu = rel + "#E10"
            if ev.should_skip_row(existing, {
                "candidate": "baseline-file", "edit": "E10", "tu": tu,
                "platform": args.platform, "sunbird_revision": args.sunbird_revision,
                "vcache_revision": args.vcache_revision,
            }):
                continue
            entry = indexed[rel][0]
            left, err, elapsed = digest_at(entry, roots)
            prefix = args.source_prefix.rstrip("/")
            rewritten = {
                "command": entry["command"].replace(prefix, second),
                "directory": entry.get("directory", "").replace(prefix, second),
                "file": entry["file"].replace(prefix, second),
            }
            # Keep the second tree's raw text so an embedded checkout path
            # (__FILE__) can be told apart from a linemarker the roots missed.
            code_b, raw_b, err_b, elapsed_b = corpus.run_pp(
                corpus.preprocess_command(rewritten), rewritten.get("directory"))
            if left is None or code_b != 0 or not raw_b:
                note_anomaly(f"E10 {rel}: preprocess failed left={err!r} right={err_b[:300]!r}")
                return 3
            right = ev.compiler_digest(raw_b, second_roots)
            swapped = ev.compiler_digest(raw_b.replace(second, prefix), roots)
            if left == right:
                reason = "second worktree, roots mapped"
                equal += 1
            elif left == swapped:
                reason = "embedded checkout path; linemarkers match after root mapping"
                note_anomaly(
                    f"E10 {rel}: normalised digest differs only by an embedded checkout path")
            else:
                reason = "digest differs after root mapping"
                note_anomaly(f"E10 {rel}: E10 compiler digest changed; expected unchanged")
            record("E10", tu, left, right, [], [], elapsed + elapsed_b, reason)
        print(f"E10 non-pch equal {equal} of {len(non_pch)}", flush=True)
        pch_rel = next((rel for rel, (entry, _h) in indexed.items()
                        if ev.is_pch_consumer(entry["command"])), None)
        if pch_rel is not None:
            entry = indexed[pch_rel][0]
            left, err, elapsed = digest_at(entry, roots)
            rewritten = {
                "command": entry["command"].replace(args.source_prefix.rstrip("/"), second),
                "directory": entry.get("directory", "").replace(args.source_prefix.rstrip("/"), second),
                "file": entry["file"].replace(args.source_prefix.rstrip("/"), second),
            }
            right, err_b, elapsed_b = digest_at(rewritten, second_roots)
            tu = pch_rel + "#E10-pch"
            if left and right and left != right:
                note_anomaly(
                    f"E10 {pch_rel}: PCH consumer digest differs after root mapping; "
                    "the PCH was built in the first tree and embeds that tree's paths. "
                    "axb-b has no independent build.")
                record("E10", tu, left, right, [], [], elapsed + elapsed_b,
                       "pch embeds the producing tree path; not a source edit")
            elif left and right:
                record("E10", tu, left, right, [], [], elapsed + elapsed_b, "second worktree, roots mapped")
                print(f"E10 pch equal {pch_rel}", flush=True)
            else:
                note_anomaly(f"E10 pch {pch_rel}: preprocess failed")
                return 3
    finally:
        if created_link and link.is_symlink():
            link.unlink()
    print("matrix rest done", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
