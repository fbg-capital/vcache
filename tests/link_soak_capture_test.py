#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Unto Labs
# SPDX-License-Identifier: Apache-2.0
"""Exercise snapshot collection against a real vcache and an idle private store."""

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import link_soak

BINARY = Path(sys.argv.pop(1)).resolve()


class LinkSoakCaptureTest(unittest.TestCase):
    def setUp(self):
        self.scratch = tempfile.TemporaryDirectory(prefix='link-soak-capture-')
        self.addCleanup(self.scratch.cleanup)
        self.root = Path(self.scratch.name)
        self.store = self.root / 'store'
        self.store.mkdir()
        self.stats = self.store / 'stats'
        self.stats.write_text('0\n' * 14)
        self.config = self.root / 'config.toml'
        self.config.write_text('[vcache]\n')
        self.context = self.root / 'context.json'
        context = {name: 'isolated collector regression' for name in link_soak.CONTEXT_FIELDS}
        link_soak.write_json(self.context, context)
        self.env = {key: value for key, value in os.environ.items()
                    if not key.startswith('VCACHE_')}
        self.env.update(VCACHE_CONFIG=str(self.config), VCACHE_DIR=str(self.store),
                        VCACHE_DAEMON='off', VCACHE_LINK_CACHE='1', VCACHE_LINK_VERIFY='1')

    def capture(self):
        return subprocess.run([sys.executable, link_soak.__file__, 'snapshot',
                               '--platform', 'host', '--epoch', 'test', '--vcache', str(BINARY),
                               '--context', str(self.context),
                               '--output', str(self.root / 'capture')],
                              env=self.env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              universal_newlines=True)

    def test_idle_daemon_collects_without_modifying_counters(self):
        before = self.stats.read_bytes()
        result = self.capture()
        self.assertEqual(result.returncode, 0, 'idle daemon snapshot: ' + result.stderr)
        record = json.loads((self.root / 'capture/snapshot.json').read_text())
        self.assertEqual(record['daemon_status']['exit_code'], 1)
        self.assertEqual(record['counters'], dict.fromkeys(link_soak.COUNTERS, 0))
        link_soak.validate_snapshot(record)
        self.assertEqual(self.stats.read_bytes(), before)
        self.assertEqual(record['stats'], (self.root / 'capture/show-stats.txt').read_text())

    def test_existing_evidence_is_preserved(self):
        result = self.capture()
        self.assertEqual(result.returncode, 0, result.stderr)
        path = self.root / 'capture/snapshot.json'
        before = path.read_bytes()
        result = self.capture()
        self.assertEqual(result.returncode, 2)
        self.assertIn('File exists', result.stderr)
        self.assertEqual(path.read_bytes(), before)

    def test_disabled_verify_cannot_make_snapshot(self):
        self.env['VCACHE_LINK_VERIFY'] = '0'
        result = self.capture()
        self.assertEqual(result.returncode, 2)
        self.assertIn('link verify must be on', result.stderr)
        self.assertFalse((self.root / 'capture/snapshot.json').exists())


if __name__ == '__main__':
    unittest.main()
