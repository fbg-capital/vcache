#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Unto Labs
# SPDX-License-Identifier: Apache-2.0
"""Refuse misleading concurrent-build evidence before reporting success."""

import copy
import unittest

from link_verify_worktrees import phase_errors


class LinkVerifyEvidenceTest(unittest.TestCase):
    def setUp(self):
        self.report = {
            'before': {'link verified': 5, 'link mismatch': 0, 'link verify error': 0,
                       'cache miss': 5},
            'after': {'link verified': 7, 'link mismatch': 0, 'link verify error': 0,
                      'cache miss': 7},
            'outputs': ['bin/app', 'bin/app.map'],
            'require_verification': True, 'require_each_tree': True,
            'builds': [{'tree': name, 'exit_code': 0, 'link_hits': 0, 'start_monotonic': start,
                        'end_monotonic': end,
                        'artifacts': {'bin/app': {'sha256': 'binary', 'executable': True},
                                      'bin/app.map': {'sha256': 'map', 'executable': False}},
                        'verification': [{'outcome': 'verified',
                                          'outputs': ['bin/app', 'bin/app.map']}]}
                       for name, start, end in [('a', 1, 3), ('b', 2, 4)]],
        }

    def assert_rejected(self, message):
        self.assertIn(message, phase_errors(self.report))

    def test_complete_evidence(self):
        self.assertEqual(phase_errors(self.report), [])

    def test_noop_builds(self):
        self.report['after'] = copy.deepcopy(self.report['before'])
        for build in self.report['builds']:
            build['verification'] = []
        self.assert_rejected('zero successful verifications')

    def test_counter_reset(self):
        self.report['after']['link verified'] = 1
        self.assert_rejected('counter reset: link verified')

    def test_missing_counter(self):
        del self.report['after']['link verified']
        self.assert_rejected('missing counter: link verified')

    def test_mismatch(self):
        self.report['after']['link mismatch'] = 1
        self.assert_rejected('nonzero link mismatch')

    def test_verification_error(self):
        self.report['after']['link verify error'] = 1
        self.assert_rejected('nonzero link verify error')

    def test_failed_build(self):
        self.report['builds'][0]['exit_code'] = 37
        self.assert_rejected('build failed: a')

    def test_served_cached_link(self):
        self.report['builds'][0]['link_hits'] = 1
        self.assert_rejected('served cached link output: a')

    def test_unverified_side_output(self):
        self.report['builds'][0]['verification'][0]['outputs'] = ['bin/app']
        self.assert_rejected('output has no successful verification: a/bin/app.map')

    def test_missing_output(self):
        del self.report['builds'][0]['artifacts']['bin/app.map']
        self.assert_rejected('missing output: a/bin/app.map')

    def test_different_bytes_or_permissions(self):
        for field, value in [('sha256', 'different'), ('executable', False)]:
            with self.subTest(field=field):
                self.report['builds'][0]['artifacts']['bin/app'][field] = value
                self.assert_rejected('cross-tree output differs: bin/app')

    def test_sequential_builds(self):
        self.report['builds'][1]['start_monotonic'] = 3
        self.assert_rejected('build processes did not overlap')

    def test_counter_log_disagreement(self):
        self.report['after']['link verified'] = 8
        self.assert_rejected('verification logs and counter delta disagree')

    def test_uncounted_real_link(self):
        self.report['after']['cache miss'] = 6
        self.assert_rejected('verification outcomes exceed real link misses')

    def test_log_failure_despite_clean_counters(self):
        self.report['builds'][0]['verification'].append({'outcome': 'error', 'outputs': []})
        self.assert_rejected('verification log reports failure: a')


if __name__ == '__main__':
    unittest.main()
