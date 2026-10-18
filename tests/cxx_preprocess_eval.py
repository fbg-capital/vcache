#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Unto Labs
# SPDX-License-Identifier: Apache-2.0
"""Soundness checker for C/C++ preprocess candidates.

The compiler's normalised digest is the oracle. A candidate is disqualified
when that digest changes and the candidate's does not, or when the compiler's
absent-path set changes and the candidate's does not. Digests are opaque
strings: the checker never treats two algorithms as comparable to each other,
only each side to its own previous value.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path


CANDIDATE_IDS = (
    "baseline-file",
    "baseline-stream",
    "clang-minws",
    "clang-rewrite-includes",
    "gcc-directives-only",
    "warp",
    "simplecpp",
    "pcpp",
    "mcpp",
    "tcc",
    "unsound-main-file-only",
    "unsound-no-absent",
    "unsound-constant",
)

RESUME_FIELDS = (
    "candidate",
    "edit",
    "tu",
    "platform",
    "sunbird_revision",
    "vcache_revision",
)

# E2 may change because linemarkers move. E11 is unchanged by the matrix but is
# not in the I2 list; the matrix still requires it to stay unchanged.
UNCHANGED_EDITS = {"E1", "E3", "E10", "E11"}
CHANGED_EDITS = {"E4", "E5", "E7", "E9"}
CONSTANT_DIGEST = "constant"

# Mutation switches. The tests flip these, show the guard fail, and restore them.
IGNORE_ABSENT_DELTAS = False
COMPARE_RAW_COMPILER_TEXT = False
ACCEPT_INSTRUMENTED_TIMING = False
COMPILE_ONLY_KEEPS_LINKS = False
RESUME_OMITS_REVISION = False
SKIP_REVERT_CHECK = False


class CorpusAnomaly(RuntimeError):
    """The compiler digest disagreed with the edit's expected invariance."""


class MatrixNotReverted(RuntimeError):
    """An edit was still applied when the next baseline was taken."""


class InstrumentedTiming(ValueError):
    """An instrumented run was offered as a timing row."""


class ProductionStore(RuntimeError):
    """A path is under the production vcache store."""


def production_store_prefix(user=None):
    who = user or os.environ.get("USER") or "richard"
    return f"/build/{who}/cache/vcache"


def assert_store_allowed(path, user=None):
    text = os.path.abspath(path)
    prefix = production_store_prefix(user)
    if text == prefix or text.startswith(prefix + os.sep):
        raise ProductionStore(text)
    return text


def normalise_preprocessed(text, roots):
    """Rewrite linemarker paths. `roots` is a list of (local, canonical), longest local first."""
    ordered = sorted(roots, key=lambda item: len(item[0]), reverse=True)
    out = []
    for line in text.splitlines(keepends=True):
        newline = "\n" if line.endswith("\n") else ""
        body = line[:-1] if newline else line
        out.append(_normalise_line(body, ordered) + newline)
    return "".join(out)


def _normalise_line(line, roots):
    if not line.startswith("#"):
        return line
    i = 1
    while i < len(line) and line[i] in " \t":
        i += 1
    if i >= len(line) or not line[i].isdigit():
        return line
    while i < len(line) and line[i].isdigit():
        i += 1
    while i < len(line) and line[i] in " \t":
        i += 1
    if i >= len(line) or line[i] != '"':
        return line
    open_quote = i
    j = open_quote + 1
    while j < len(line):
        if line[j] == "\\":
            j += 2
            continue
        if line[j] == '"':
            break
        j += 1
    if j >= len(line):
        return line
    raw = _unescape(line[open_quote + 1:j])
    mapped = raw
    for local, canonical in roots:
        if raw == local or raw.startswith(local + "/"):
            mapped = canonical + raw[len(local):]
            break
    return line[: open_quote + 1] + _escape(mapped) + line[j:]


def _unescape(text):
    out = []
    i = 0
    while i < len(text):
        if text[i] == "\\" and i + 1 < len(text):
            out.append(text[i + 1])
            i += 2
        else:
            out.append(text[i])
            i += 1
    return "".join(out)


def _escape(text):
    return text.replace("\\", "\\\\").replace('"', '\\"')


def digest_text(text):
    return hashlib.sha256(text.encode()).hexdigest()


def compiler_digest(text, roots):
    body = text if COMPARE_RAW_COMPILER_TEXT else normalise_preprocessed(text, roots)
    return digest_text(body)


def unsound_main_file_digest(source_text):
    """Hash of the translation unit's own source only. Headers are ignored."""
    return digest_text(source_text)


def unsound_constant_digest(compiler_digest_value):
    del compiler_digest_value
    return CONSTANT_DIGEST


def absent_delta(before, after):
    return set(before) ^ set(after)


def disqualify_reasons(compiler_before, compiler_after, candidate_before, candidate_after,
                       compiler_absent_before, compiler_absent_after,
                       candidate_absent_before, candidate_absent_after):
    reasons = []
    if compiler_before != compiler_after and candidate_before == candidate_after:
        reasons.append("compiler digest changed and candidate digest did not")
    if not IGNORE_ABSENT_DELTAS:
        compiler_absent = absent_delta(compiler_absent_before, compiler_absent_after)
        candidate_absent = absent_delta(candidate_absent_before, candidate_absent_after)
        if compiler_absent and not candidate_absent:
            reasons.append("compiler absent set changed and candidate absent set did not")
    return reasons


def false_invalidation(compiler_before, compiler_after, candidate_before, candidate_after):
    return compiler_before == compiler_after and candidate_before != candidate_after


def check_compiler_expectation(edit, before, after, e8_phase=None):
    changed = before != after
    if edit == "E8":
        if e8_phase == "E1":
            expect = False
        elif e8_phase == "E4":
            expect = True
        else:
            raise CorpusAnomaly(f"E8 requires e8_phase E1 or E4, got {e8_phase!r}")
    elif edit in {"E2", "E6"}:
        # E2 may or may not move linemarkers. E6 changes the text only when a
        # swapped include still expands; a no-op is recorded, not an anomaly.
        return changed
    elif edit in UNCHANGED_EDITS:
        expect = False
    elif edit in CHANGED_EDITS:
        expect = True
    else:
        raise CorpusAnomaly(f"unknown edit {edit}")
    if changed != expect:
        direction = "changed" if changed else "unchanged"
        raise CorpusAnomaly(
            f"{edit} compiler digest {direction}; expected "
            f"{'changed' if expect else 'unchanged'}")
    return changed


def check_edit_baseline(pre_matrix, per_edit_baseline):
    if SKIP_REVERT_CHECK:
        return
    if pre_matrix != per_edit_baseline:
        raise MatrixNotReverted(
            "per-edit baseline digest differs from the pre-matrix baseline")


def accept_timing_row(row):
    if row.get("instrumented") and not ACCEPT_INSTRUMENTED_TIMING:
        raise InstrumentedTiming("instrumented run cannot be a timing row")
    if row.get("instrumented") and ACCEPT_INSTRUMENTED_TIMING:
        return row
    return row


def replay_commands(commands, compile_only):
    if compile_only and not COMPILE_ONLY_KEEPS_LINKS:
        return [cmd for cmd in commands if cmd.get("kind") != "link"]
    return list(commands)


def link_count(commands):
    return sum(1 for cmd in commands if cmd.get("kind") == "link")


def compile_count(commands):
    return sum(1 for cmd in commands if cmd.get("kind") == "compile")


def resume_key(row):
    fields = RESUME_FIELDS
    if RESUME_OMITS_REVISION:
        fields = tuple(field for field in RESUME_FIELDS
                       if field not in ("sunbird_revision", "vcache_revision"))
    return tuple(row[field] for field in fields)


def should_skip_row(existing, row):
    return resume_key(row) in existing


def load_resume_keys(path):
    keys = set()
    file = Path(path)
    if not file.exists():
        return keys
    for line in file.read_text().splitlines():
        if not line.strip():
            continue
        keys.add(resume_key(json.loads(line)))
    return keys


def append_jsonl(path, row):
    file = Path(path)
    file.parent.mkdir(parents=True, exist_ok=True)
    with file.open("a", encoding="utf-8") as out:
        out.write(json.dumps(row, sort_keys=True) + "\n")


def counterexample_dir(root, candidate, edit, tu):
    safe_tu = tu.replace("\\", "/")
    return Path(root) / "counterexamples" / candidate / edit / safe_tu


def save_counterexample(root, candidate, edit, tu, record):
    dest = counterexample_dir(root, candidate, edit, tu)
    dest.mkdir(parents=True, exist_ok=True)
    (dest / "record.json").write_text(json.dumps(record, indent=2, sort_keys=True) + "\n")
    return dest


def empty_output_is_digest(exit_code, output):
    """Exit 0 with empty output is a failure, never a digest."""
    if exit_code == 0 and output == "":
        return False
    return exit_code == 0


def note_crash(crash_tus, tu):
    crash_tus.add(tu)
    return len(crash_tus) >= 3


def is_pch_producer(command):
    return "-emit-pch" in command or "c++-header" in command


def is_pch_consumer(command):
    return "include-pch" in command


def is_link_command(command):
    lowered = command.lower()
    base = command.split()[0] if command.split() else ""
    name = os.path.basename(base)
    if name in {"ld", "ld.lld", "ld.gold", "lld", "gold", "mold"}:
        return True
    if " -shared" in f" {command}" or " -Wl," in command:
        return " -c " not in f" {command} "
    return False


def load_average_1m():
    return os.getloadavg()[0]


def apply_edit(text, edit, e8_phase=None):
    """Return edited header text. The caller saves the original and writes it back."""
    which = e8_phase if edit == "E8" else edit
    if which == "E1" or which == "E11":
        line = "// axb E1\n" if which == "E1" else "// axb E11\n"
        if not text.endswith("\n"):
            text += "\n"
        return text + line
    if which == "E2":
        if not text.endswith("\n"):
            text += "\n"
        return text + "\n   \n"
    if which == "E3":
        if not text.endswith("\n"):
            text += "\n"
        return text + "#define AXB_UNUSED_E3 1\n"
    if which == "E4":
        if not text.endswith("\n"):
            text += "\n"
        return text + "#define AXB_E4 1\nextern int axb_e4_used[AXB_E4];\n"
    if which == "E5":
        if not text.endswith("\n"):
            text += "\n"
        return text + (
            "#if __has_builtin(__builtin_expect)\n"
            "extern int axb_e5_builtin;\n"
            "#else\n"
            "extern int axb_e5_nobuiltin;\n"
            "#endif\n"
        )
    if which == "E6":
        lines = text.splitlines(keepends=True)
        includes = [i for i, line in enumerate(lines) if line.startswith("#include")]
        if len(includes) < 2:
            raise CorpusAnomaly("E6 needs two #include lines")
        a, b = includes[0], includes[1]
        lines[a], lines[b] = lines[b], lines[a]
        return "".join(lines)
    raise CorpusAnomaly(f"apply_edit does not implement {edit}")


def e11_variant(text, index):
    if not text.endswith("\n"):
        text += "\n"
    return text + f"// axb E11 v{index}\n"


def _compiler_env():
    env = dict(os.environ)
    parts = [item for item in env.get("PATH", "").split(":") if item != "/usr/lib64/ccache"]
    env["PATH"] = ":".join(parts)
    env.pop("LD_PRELOAD", None)
    return env


def preprocess(compiler, source, include_dirs):
    import subprocess
    cmd = [compiler]
    for inc in include_dirs:
        cmd.extend(["-I", inc])
    cmd.extend(["-E", source])
    result = subprocess.run(cmd, capture_output=True, env=_compiler_env())
    return result.returncode, result.stdout.decode(errors="replace"), result.stderr.decode(errors="replace")


def run_guard(root, compiler=None):
    """Real-compiler guard. Writes counterexamples and guard-passed.json.

    Returns a list of result dicts. Raises if a guard candidate fails to fail,
    or if E1 does not leave both digests unchanged.
    """
    import shutil
    import subprocess
    root = Path(root)
    if compiler is None:
        compiler = shutil.which("clang++-22") or shutil.which("clang++") or shutil.which("g++")
    if not compiler:
        raise RuntimeError("no C++ compiler for the preprocess guard")
    work = root / "guard-fixture"
    if work.exists():
        shutil.rmtree(work)
    (work / "real").mkdir(parents=True)
    (work / "shadow").mkdir()
    header = work / "h.h"
    source = work / "t.cc"
    real_header = work / "real" / "real.h"
    shadow_header = work / "shadow" / "real.h"
    header.write_text("#pragma once\nint axb_value(void);\n")
    source.write_text('#include "h.h"\nint axb_value(void) { return 0; }\n')
    real_header.write_text("int from_real;\n")
    roots = [(str(work), "/canon")]
    original = header.read_text()
    rows = []

    def pp(path, includes):
        code, out, err = preprocess(compiler, path, includes)
        if code != 0:
            raise RuntimeError(f"compiler -E failed: {err[:400]}")
        return compiler_digest(out, roots)

    base = pp(str(source), [str(work)])
    main_base = unsound_main_file_digest(source.read_text())
    header.write_text(apply_edit(original, "E1"))
    e1 = pp(str(source), [str(work)])
    main_e1 = unsound_main_file_digest(source.read_text())
    check_compiler_expectation("E1", base, e1)
    e1_reasons = disqualify_reasons(base, e1, main_base, main_e1, [], [], [], [])
    if e1_reasons:
        raise RuntimeError("unsound-main-file-only was disqualified on E1: " + "; ".join(e1_reasons))
    rows.append({"candidate": "unsound-main-file-only", "edit": "E1", "disqualified": False,
                 "compiler_changed": False, "candidate_changed": main_base != main_e1})
    header.write_text(original)
    check_edit_baseline(base, pp(str(source), [str(work)]))

    header.write_text(apply_edit(original, "E4"))
    e4 = pp(str(source), [str(work)])
    main_e4 = unsound_main_file_digest(source.read_text())
    check_compiler_expectation("E4", base, e4)
    e4_reasons = disqualify_reasons(base, e4, main_base, main_e4, [], [], [], [])
    if not e4_reasons:
        raise RuntimeError("unsound-main-file-only was not disqualified on E4")
    save_counterexample(root, "unsound-main-file-only", "E4", "t.cc", {
        "compiler_before": base, "compiler_after": e4,
        "candidate_before": main_base, "candidate_after": main_e4,
        "reasons": e4_reasons, "source": str(source), "header": header.read_text(),
        "command": f"{compiler} -E -I {work} {source}",
    })
    rows.append({"candidate": "unsound-main-file-only", "edit": "E4", "disqualified": True,
                 "reasons": e4_reasons})
    header.write_text(original)

    shadow = str(shadow_header)
    includes = [str(work / "shadow"), str(work / "real")]
    e7_source = work / "e7.cc"
    e7_source.write_text('#include "real.h"\n')
    before = pp(str(e7_source), includes)
    absent_before = [shadow]
    shadow_header.write_text("int from_shadow;\n")
    after = pp(str(e7_source), includes)
    absent_after = []
    check_compiler_expectation("E7", before, after)
    # The candidate still sees the preprocessed text, so its digest changes.
    # Absent recording is disabled, so its absent set stays empty.
    e7_reasons = disqualify_reasons(before, after, before, after, absent_before, absent_after, [], [])
    if not e7_reasons:
        raise RuntimeError("unsound-no-absent was not disqualified on E7")
    save_counterexample(root, "unsound-no-absent", "E7", "e7.cc", {
        "compiler_before": before, "compiler_after": after,
        "candidate_absent_before": [], "candidate_absent_after": [],
        "compiler_absent_before": absent_before, "compiler_absent_after": absent_after,
        "reasons": e7_reasons,
        "command": f"{compiler} -E -I {work / 'shadow'} -I {work / 'real'} {e7_source}",
    })
    rows.append({"candidate": "unsound-no-absent", "edit": "E7", "disqualified": True,
                 "reasons": e7_reasons})

    const_before = unsound_constant_digest(base)
    const_after = unsound_constant_digest(e4)
    const_reasons = disqualify_reasons(base, e4, const_before, const_after, [], [], [], [])
    if not const_reasons:
        raise RuntimeError("unsound-constant was not disqualified on the first semantic edit")
    save_counterexample(root, "unsound-constant", "E4", "t.cc", {
        "compiler_before": base, "compiler_after": e4,
        "candidate_before": const_before, "candidate_after": const_after,
        "reasons": const_reasons,
    })
    rows.append({"candidate": "unsound-constant", "edit": "E4", "disqualified": True,
                 "reasons": const_reasons})
    (root / "guard-passed.json").write_text(json.dumps({"compiler": compiler, "rows": rows}, indent=2) + "\n")
    return rows


def main(argv):
    if len(argv) >= 2 and argv[1] == "guard":
        root = argv[2] if len(argv) > 2 else "/build/richard/claude-tmp/forgrok/vcache-axb"
        rows = run_guard(root)
        for row in rows:
            print(f"{row['candidate']} {row['edit']} disqualified={row['disqualified']}")
        return 0
    print("usage: cxx_preprocess_eval.py guard [scratch]", file=__import__("sys").stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main(__import__("sys").argv))
