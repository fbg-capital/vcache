#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Unto Labs
# SPDX-License-Identifier: Apache-2.0
"""Reject incomplete or discontinuous link-verification observation windows."""

import copy
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import link_soak


def snapshot(platform, day):
    started = datetime(2020, 1, 1, tzinfo=timezone.utc) + timedelta(days=day)
    store = '/cache/' + platform
    result = {
        'schema': 1, 'platform': platform, 'epoch': 'after-correction', 'hostname': 'builder',
        'started_utc': started.strftime('%Y-%m-%dT%H:%M:%S.%fZ'),
        'ended_utc': (started + timedelta(seconds=1)).strftime('%Y-%m-%dT%H:%M:%S.%fZ'),
        'store': store, 'store_identity': [[1, 2 if platform == 'host' else 3], [1, 4]],
        'binary_sha256': 'a' * 64, 'tracer_sha256': 'b' * 64,
        'config': ('disk cache:       enabled\n  dir:            ' + store +
                   '\n  max size:       42949672960 bytes\nlink cache:       on\n'
                   'link verify:      on\ndaemon:           auto\n'),
        'context': {name: 'recorded evidence' for name in link_soak.CONTEXT_FIELDS},
        'counters': {'link verified': 1000 + day * 20,
                     'link mismatch': 11, 'link verify error': 2},
    }
    set_counters(result)
    return result


def set_counters(record, **values):
    record['counters'].update(values)
    record['stats'] = ''.join('{}  {}\n'.format(name, count)
                              for name, count in record['counters'].items())


class LinkSoakTest(unittest.TestCase):
    def setUp(self):
        self.rows = [snapshot(platform, day) for day in range(16)
                     for platform in link_soak.PLATFORMS]

    def assert_rejected(self, message):
        report = link_soak.check_window(self.rows)
        self.assertFalse(report['counter_window_complete'], message)
        self.assertIn(message, report['errors'])

    def test_complete_counter_window_preserves_old_failures(self):
        report = link_soak.check_window(self.rows)
        self.assertTrue(report['counter_window_complete'], report['errors'])
        self.assertEqual(report['platforms']['host']['delta'],
                         {'link verified': 300, 'link mismatch': 0, 'link verify error': 0})
        self.assertEqual(len(report['platforms']['el10']['intervals']), 15)
        self.assertTrue(report['remaining_review'])

    def test_missing_platform(self):
        self.rows = self.rows[::2]
        self.assert_rejected('el10: need at least two snapshots')

    def test_missing_baseline(self):
        self.rows = self.rows[-2:]
        self.assert_rejected('host: need at least two snapshots')

    def test_single_snapshot_has_zero_elapsed_time(self):
        self.rows = self.rows[-2:]
        report = link_soak.check_window(self.rows)
        self.assertEqual(report['platforms']['host']['elapsed_seconds'], 0)

    def test_short_window(self):
        self.rows = self.rows[:28]
        self.assert_rejected('host: observation window shorter than 14 days')

    def test_zero_attempt_window(self):
        for row in self.rows:
            set_counters(row, **{'link verified': 1000})
        self.assert_rejected('host: zero successful verifications')

    def test_insufficient_verifications(self):
        for index, row in enumerate(self.rows):
            set_counters(row, **{'link verified': 1000 + index // 2})
        self.assert_rejected('host: fewer than 200 successful verifications')

    def test_each_platform_needs_activity(self):
        for row in self.rows[1::2]:
            set_counters(row, **{'link verified': 1000})
        self.assert_rejected('el10: zero successful verifications')

    def test_counter_reset_even_when_endpoint_recovers(self):
        for name in link_soak.COUNTERS:
            with self.subTest(counter=name):
                original = copy.deepcopy(self.rows[4])
                set_counters(self.rows[4], **{name: 0})
                self.assert_rejected('host: counter reset: ' + name)
                self.rows[4] = original

    def test_mismatch_and_error(self):
        for name in ('link mismatch', 'link verify error'):
            with self.subTest(counter=name):
                original = copy.deepcopy(self.rows[4])
                set_counters(self.rows[4], **{name: self.rows[4]['counters'][name] + 1})
                self.assert_rejected('host: nonzero ' + name)
                self.rows[4] = original

    def test_changed_epoch_store_binary_or_policy(self):
        for name, value in (
                ('epoch', 'reset'), ('hostname', 'replacement-builder'), ('store', '/new-store'),
                ('store_identity', [[2, 3], [2, 4]]), ('binary_sha256', 'c' * 64),
                ('tracer_sha256', 'd' * 64),
                ('config', self.rows[4]['config'].replace('42949672960', '123456'))):
            with self.subTest(field=name):
                original = self.rows[4][name]
                self.rows[4][name] = value
                self.assert_rejected('host: window identity changed: ' + name)
                self.rows[4][name] = original

    def test_daily_gap(self):
        del self.rows[4:6]
        self.assert_rejected('host: daily observation gap exceeds 36 hours')

    def test_reversed_order(self):
        self.rows.reverse()
        self.assert_rejected('host: snapshots are not strictly ordered')

    def test_duplicate_snapshot(self):
        self.rows.insert(0, self.rows[0])
        self.assert_rejected('host: snapshots are not strictly ordered')

    def test_shared_store(self):
        for row in self.rows[1::2]:
            row['store_identity'] = self.rows[0]['store_identity']
        self.assert_rejected('host and el10 must use separate stores')

    def test_disabled_verification(self):
        self.rows[0]['config'] = self.rows[0]['config'].replace('link verify:      on',
                                                              'link verify:      off')
        self.assert_rejected('invalid snapshot 1: link verify must be on')

    def test_missing_context(self):
        del self.rows[0]['context']['reset_deployment_log']
        self.assert_rejected('invalid snapshot 1: missing context: reset_deployment_log')

    def test_missing_counter(self):
        del self.rows[0]['counters']['link verified']
        self.assert_rejected('invalid snapshot 1: invalid counter type')

    def test_counter_disagrees_with_retained_text(self):
        self.rows[0]['counters']['link verified'] += 1
        self.assert_rejected('invalid snapshot 1: parsed counters disagree with retained stats')

    def test_boolean_counter(self):
        self.rows[0]['counters']['link verified'] = True
        self.assert_rejected('invalid snapshot 1: invalid counter type')

    def test_future_timestamp(self):
        self.rows[0]['started_utc'] = '2999-01-01T00:00:00.000000Z'
        self.rows[0]['ended_utc'] = '2999-01-01T00:00:01.000000Z'
        self.assert_rejected('invalid snapshot 1: snapshot timestamp is in the future')

    def test_collection_duration(self):
        self.rows[0]['ended_utc'] = '2020-01-01T00:01:01.000000Z'
        self.assert_rejected('invalid snapshot 1: invalid collection duration')

    def test_malformed_snapshot(self):
        self.rows.insert(0, [])
        self.assertFalse(link_soak.check_window(self.rows)['counter_window_complete'])

    def test_strict_counter_parser(self):
        text = self.rows[0]['stats']
        for malformed in (text + 'link verified  1\n', text.replace('1000', '-1'),
                          text.replace('1000', '1garbage'), text.replace('link verified', 'old')):
            with self.subTest(text=malformed):
                with self.assertRaises(ValueError):
                    link_soak.parse_stats(malformed)

    def test_cli_exit_status_and_no_overwrite(self):
        with tempfile.TemporaryDirectory(prefix='link-soak-test-') as directory:
            path = Path(directory) / 'snapshot.json'
            link_soak.write_json(path, self.rows[0])
            with self.assertRaises(FileExistsError):
                link_soak.write_json(path, self.rows[1])
            self.assertEqual(json.loads(path.read_text()), self.rows[0])
            result = subprocess.run([sys.executable, link_soak.__file__, 'check', str(path)],
                                    stdout=subprocess.PIPE, universal_newlines=True)
            self.assertEqual(result.returncode, 1)
            self.assertFalse(json.loads(result.stdout)['counter_window_complete'])


if __name__ == '__main__':
    unittest.main()
