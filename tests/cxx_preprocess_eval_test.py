#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Unto Labs
# SPDX-License-Identifier: Apache-2.0
"""Checker tests. Not part of `make test`."""

import tempfile
import unittest
from pathlib import Path

import cxx_preprocess_eval as ev


class JudgeTest(unittest.TestCase):
    def test_main_file_only_passes_e1(self):
        reasons = ev.disqualify_reasons("same", "same", "main", "main", [], [], [], [])
        self.assertEqual(reasons, [])

    def test_main_file_only_fails_e4(self):
        reasons = ev.disqualify_reasons("before", "after", "main", "main", [], [], [], [])
        self.assertIn("compiler digest changed and candidate digest did not", reasons)

    def test_unsound_no_absent_fails_e7(self):
        reasons = ev.disqualify_reasons(
            "before", "after", "before", "after",
            ["/absent/real.h"], [], [], [])
        self.assertIn("compiler absent set changed and candidate absent set did not", reasons,
                      "unsound-no-absent is disqualified on E7")

    def test_unsound_constant_fails_first_semantic_edit(self):
        before = ev.unsound_constant_digest("compiler-before")
        after = ev.unsound_constant_digest("compiler-after")
        self.assertEqual(before, ev.CONSTANT_DIGEST, "unsound-constant digest is not the compiler digest")
        self.assertEqual(after, ev.CONSTANT_DIGEST, "unsound-constant digest is not the compiler digest")
        reasons = ev.disqualify_reasons("compiler-before", "compiler-after", before, after,
                                        [], [], [], [])
        self.assertIn("compiler digest changed and candidate digest did not", reasons)

    def test_e10_normalised_compiler_digest_is_unchanged(self):
        roots = [("/worktree-a/proj", "/canon"), ("/worktree-b/proj", "/canon")]
        left = '# 1 "/worktree-a/proj/a.cc"\nint x;\n'
        right = '# 1 "/worktree-b/proj/a.cc"\nint x;\n'
        self.assertEqual(ev.compiler_digest(left, roots), ev.compiler_digest(right, roots),
                         "E10 normalised compiler digest is unchanged")
        ev.check_compiler_expectation("E10", ev.compiler_digest(left, roots),
                                      ev.compiler_digest(right, roots))

    def test_instrumented_run_cannot_be_a_timing_row(self):
        with self.assertRaises(ev.InstrumentedTiming) as raised:
            ev.accept_timing_row({"instrumented": True, "wall_s": 1})
        self.assertIn("instrumented run cannot be a timing row", str(raised.exception))

    def test_compile_only_replay_invokes_zero_linkers(self):
        commands = [{"kind": "compile"}, {"kind": "link"}, {"kind": "compile"}]
        replayed = ev.replay_commands(commands, compile_only=True)
        self.assertEqual(ev.link_count(replayed), 0, "compile-only replay invokes zero linkers")
        self.assertEqual(ev.compile_count(replayed), 2)

    def test_resume_key_includes_sunbird_and_vcache_revisions(self):
        old = {"candidate": "baseline-file", "edit": "E1", "tu": "a.cc", "platform": "host",
               "sunbird_revision": "aaa", "vcache_revision": "111"}
        new = dict(old, sunbird_revision="bbb")
        self.assertNotIn(ev.resume_key(new), {ev.resume_key(old)},
                         "resume key includes sunbird and vcache revisions")

    def test_per_edit_baseline_must_match_pre_matrix(self):
        with self.assertRaises(ev.MatrixNotReverted) as raised:
            ev.check_edit_baseline("baseline", "still-e4")
        self.assertIn("per-edit baseline digest differs from the pre-matrix baseline",
                      str(raised.exception))

    def test_e1_e3_e10_unchanged_and_e4_changed(self):
        ev.check_compiler_expectation("E1", "a", "a")
        ev.check_compiler_expectation("E3", "a", "a")
        ev.check_compiler_expectation("E4", "a", "b")
        with self.assertRaises(ev.CorpusAnomaly):
            ev.check_compiler_expectation("E4", "a", "a")
        with self.assertRaises(ev.CorpusAnomaly):
            ev.check_compiler_expectation("E1", "a", "b")

    def test_e6_unchanged_is_not_an_anomaly(self):
        changed = ev.check_compiler_expectation("E6", "same", "same")
        self.assertFalse(changed)
        changed = ev.check_compiler_expectation("E6", "before", "after")
        self.assertTrue(changed)

    def test_e8_phases(self):
        ev.check_compiler_expectation("E8", "a", "a", e8_phase="E1")
        ev.check_compiler_expectation("E8", "a", "b", e8_phase="E4")

    def test_empty_output_is_not_a_digest(self):
        self.assertFalse(ev.empty_output_is_digest(0, ""))
        self.assertTrue(ev.empty_output_is_digest(0, "int x;\n"))

    def test_three_crashes_mark_unusable(self):
        seen = set()
        self.assertFalse(ev.note_crash(seen, "a.cc"))
        self.assertFalse(ev.note_crash(seen, "b.cc"))
        self.assertTrue(ev.note_crash(seen, "c.cc"))

    def test_pch_producer_is_excluded_and_consumer_is_not(self):
        self.assertTrue(ev.is_pch_producer("clang++ -emit-pch -x c++-header h.hxx"))
        self.assertFalse(ev.is_pch_producer("clang++ -c a.cc"))
        self.assertTrue(ev.is_pch_consumer("clang++ -Xclang -include-pch -Xclang p.pch -c a.cc"))

    def test_production_store_is_refused(self):
        with self.assertRaises(ev.ProductionStore):
            ev.assert_store_allowed("/build/richard/cache/vcache/host", user="richard")
        allowed = ev.assert_store_allowed(
            "/build/richard/claude-tmp/forgrok/vcache-axb/store", user="richard")
        self.assertTrue(allowed.endswith("store"))

    def test_false_invalidation_is_candidate_only_change(self):
        self.assertTrue(ev.false_invalidation("a", "a", "x", "y"))
        self.assertFalse(ev.false_invalidation("a", "b", "x", "y"))

    def test_real_compiler_guard(self):
        with tempfile.TemporaryDirectory(prefix="axb-guard-") as tmp:
            rows = ev.run_guard(tmp)
            by_id = {(row["candidate"], row["edit"]): row for row in rows}
            self.assertFalse(by_id[("unsound-main-file-only", "E1")]["disqualified"])
            self.assertTrue(by_id[("unsound-main-file-only", "E4")]["disqualified"])
            self.assertTrue(by_id[("unsound-no-absent", "E7")]["disqualified"])
            self.assertTrue(by_id[("unsound-constant", "E4")]["disqualified"])
            saved = Path(tmp) / "counterexamples" / "unsound-main-file-only" / "E4" / "t.cc" / "record.json"
            self.assertTrue(saved.is_file(), "counterexample saved under unsound-main-file-only/E4")


if __name__ == "__main__":
    unittest.main()
