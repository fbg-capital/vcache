#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Unto Labs
# SPDX-License-Identifier: Apache-2.0
"""Collect live link-verification snapshots and check daily counter windows."""

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import socket
import subprocess
import sys


COUNTERS = ('link verified', 'link mismatch', 'link verify error')
PLATFORMS = ('host', 'el10')
MIN_SECONDS = 14 * 24 * 60 * 60
MAX_GAP_SECONDS = 36 * 60 * 60
MIN_VERIFIED = 200
CONTEXT_FIELDS = ('devtools_revision', 'sunbird_revision', 'compiler', 'linker',
                  'image', 'workload_evidence', 'reset_deployment_log')


def parse_stats(text):
    counters = {}
    for line in text.splitlines():
        for name in COUNTERS:
            if re.match(r'^' + re.escape(name) + r'\s', line):
                match = re.fullmatch(re.escape(name) + r'\s+([0-9]+)', line)
                if not match or name in counters:
                    raise ValueError('invalid or duplicate counter: ' + name)
                counters[name] = int(match[1])
    for name in COUNTERS:
        if name not in counters:
            raise ValueError('missing counter: ' + name)
    return counters


def config_store(text):
    for name, expected in (('disk cache', 'enabled'), ('link cache', 'on'), ('link verify', 'on')):
        if re.findall(r'^' + name + r':\s+(.+)$', text, re.MULTILINE) != [expected]:
            raise ValueError(name + ' must be ' + expected)
    match = re.search(r'^disk cache:.*\n  dir:\s+(.+)\n', text, re.MULTILINE)
    if not match or not Path(match[1]).is_absolute():
        raise ValueError('missing absolute disk cache directory')
    return Path(match[1])


def utc_now():
    return datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%S.%fZ')


def utc_time(value):
    return datetime.strptime(value, '%Y-%m-%dT%H:%M:%S.%fZ').replace(tzinfo=timezone.utc)


def file_digest(path):
    result = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            result.update(block)
    return result.hexdigest()


def file_identity(path):
    status = path.stat()
    return [status.st_dev, status.st_ino]


def write_json(path, value):
    with path.open('x') as stream:
        json.dump(value, stream, indent=2)
        stream.write('\n')


def validate_context(context):
    for name in CONTEXT_FIELDS:
        if not isinstance(context.get(name), str) or not context[name].strip():
            raise ValueError('missing context: ' + name)


def collect(args):
    context = json.loads(args.context.read_text())
    validate_context(context)
    if not args.epoch.strip():
        raise ValueError('empty counter epoch')
    args.output.mkdir(parents=True)
    binary = args.vcache.resolve(strict=True)
    tracer = binary.with_name('vcache-fstrace.so')
    started = utc_now()
    hashes = [file_digest(binary), file_digest(tracer)]

    def command(option, label=None, required=True):
        result = subprocess.run([str(binary), '--' + option], universal_newlines=True,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30)
        label = label or option
        with (args.output / (label + '.txt')).open('x') as stream:
            stream.write(result.stdout)
        with (args.output / (label + '.stderr')).open('x') as stream:
            stream.write(result.stderr)
        if required and result.returncode:
            raise ValueError(option + ' failed: exit ' + str(result.returncode))
        if not required:
            return {'exit_code': result.returncode, 'stdout': result.stdout,
                    'stderr': result.stderr}
        return result.stdout

    config = command('show-config')
    store = config_store(config).resolve(strict=True)
    identity = [file_identity(store), file_identity(store / 'stats')]
    stats = command('show-stats')
    counters = parse_stats(stats)
    # Retain daemon restarts as context; persisted counters can survive them.
    daemon_status = command('daemon-status', required=False)
    if (hashes != [file_digest(binary), file_digest(tracer)] or
            identity != [file_identity(store), file_identity(store / 'stats')] or
            config != command('show-config', 'show-config-after')):
        raise ValueError('collection identity changed')
    ended = utc_now()
    elapsed = (utc_time(ended) - utc_time(started)).total_seconds()
    if elapsed < 0 or elapsed > 60:
        raise ValueError('collection clock changed or collection exceeded 60 seconds')
    stamp = binary.with_name('vcache.commit')
    record = {'schema': 1, 'platform': args.platform, 'epoch': args.epoch,
              'started_utc': started, 'ended_utc': ended, 'hostname': args.host_id,
              'collector_hostname': socket.gethostname(),
              'store': str(store), 'store_identity': identity, 'binary': str(binary),
              'binary_sha256': hashes[0], 'tracer_sha256': hashes[1],
              'vcache_revision': stamp.read_text().strip() if stamp.exists() else 'unknown',
              'config': config, 'stats': stats, 'counters': counters,
              'daemon_status': daemon_status, 'context': context}
    validate_snapshot(record)
    write_json(args.output / 'snapshot.json', record)
    return record


def validate_snapshot(snapshot):
    if (type(snapshot['schema']) is not int or snapshot['schema'] != 1 or
            snapshot['platform'] not in PLATFORMS):
        raise ValueError('unsupported snapshot schema or platform')
    validate_context(snapshot['context'])
    for name in ('epoch', 'hostname', 'store', 'binary_sha256', 'tracer_sha256'):
        if not isinstance(snapshot[name], str) or not snapshot[name].strip():
            raise ValueError('missing snapshot field: ' + name)
    for name in ('binary_sha256', 'tracer_sha256'):
        if not re.fullmatch('[0-9a-f]{64}', snapshot[name]):
            raise ValueError('invalid digest: ' + name)
    identity = snapshot['store_identity']
    if (not isinstance(identity, list) or len(identity) != 2 or
            any(not isinstance(pair, list) or len(pair) != 2 or
                any(type(number) is not int or number < 0 for number in pair)
                for pair in identity)):
        raise ValueError('invalid store identity')
    config_store(snapshot['config'])
    if any(type(snapshot['counters'].get(name)) is not int for name in COUNTERS):
        raise ValueError('invalid counter type')
    if snapshot['counters'] != parse_stats(snapshot['stats']):
        raise ValueError('parsed counters disagree with retained stats')
    start, end = utc_time(snapshot['started_utc']), utc_time(snapshot['ended_utc'])
    if not 0 <= (end - start).total_seconds() <= 60:
        raise ValueError('invalid collection duration')
    if end > datetime.now(timezone.utc):
        raise ValueError('snapshot timestamp is in the future')
    return start, end


def check_window(snapshots):
    errors = []
    groups = {platform: [] for platform in PLATFORMS}
    for index, snapshot in enumerate(snapshots):
        try:
            start, end = validate_snapshot(snapshot)
            groups[snapshot['platform']].append((snapshot, start, end))
        except (KeyError, TypeError, ValueError, AttributeError) as error:
            errors.append('invalid snapshot {}: {}'.format(index + 1, error))
    platforms = {}
    stable_fields = ('epoch', 'hostname', 'store', 'store_identity', 'binary_sha256',
                     'tracer_sha256', 'config')
    for platform, rows in groups.items():
        problems = []
        totals = {name: 0 for name in COUNTERS}
        intervals = []
        if len(rows) < 2:
            problems.append('need at least two snapshots')
        for previous, current in zip(rows, rows[1:]):
            before, _, before_end = previous
            after, after_start, _ = current
            gap = (after_start - before_end).total_seconds()
            if gap <= 0:
                problems.append('snapshots are not strictly ordered')
            if gap > MAX_GAP_SECONDS:
                problems.append('daily observation gap exceeds 36 hours')
            for name in stable_fields:
                if before[name] != after[name]:
                    problems.append('window identity changed: ' + name)
            delta = {name: after['counters'][name] - before['counters'][name]
                     for name in COUNTERS}
            for name, count in delta.items():
                if count < 0:
                    problems.append('counter reset: ' + name)
                if name != 'link verified' and count > 0:
                    problems.append('nonzero ' + name)
                totals[name] += count
            intervals.append({'before': before['ended_utc'], 'after': after['started_utc'],
                              'delta': delta})
        elapsed = (rows[-1][1] - rows[0][2]).total_seconds() if len(rows) > 1 else 0
        if elapsed < MIN_SECONDS:
            problems.append('observation window shorter than 14 days')
        if totals['link verified'] == 0:
            problems.append('zero successful verifications')
        if totals['link verified'] < MIN_VERIFIED:
            problems.append('fewer than 200 successful verifications')
        errors.extend(platform + ': ' + message for message in dict.fromkeys(problems))
        platforms[platform] = {'snapshots': len(rows), 'elapsed_seconds': elapsed,
                               'delta': totals, 'intervals': intervals}
    if groups['host'] and groups['el10']:
        host = groups['host'][0][0]
        el10 = groups['el10'][0][0]
        if host['store_identity'] == el10['store_identity']:
            errors.append('host and el10 must use separate stores')
    return {'counter_window_complete': not errors, 'errors': errors, 'platforms': platforms,
            'remaining_review': [
                'Review reset/deployment history; resets that recover between snapshots are invisible.',
                'Confirm actual launcher routing, workload and map/cross-worktree coverage.',
                'Complete host/el10 control audits and controlled ordinary-hit output comparisons.',
                'Record the owner rollout decision; this report never enables ordinary hits.']}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command')
    snapshot = commands.add_parser('snapshot', help='collect in the current workload environment')
    snapshot.add_argument('--platform', choices=PLATFORMS, required=True)
    snapshot.add_argument('--epoch', required=True,
                          help='new label after a reset or corrective deployment')
    snapshot.add_argument('--host-id', default=socket.gethostname(),
                          help='stable physical host identity; set explicitly inside containers')
    snapshot.add_argument('--vcache', type=Path, required=True)
    snapshot.add_argument('--context', type=Path, required=True)
    snapshot.add_argument('--output', type=Path, required=True, help='new evidence directory')
    check = commands.add_parser('check', help='check snapshots in chronological order per platform')
    check.add_argument('snapshots', nargs='+', type=Path)
    args = parser.parse_args()
    try:
        if args.command == 'snapshot':
            record = collect(args)
            print(json.dumps({'snapshot': str(args.output / 'snapshot.json'),
                              'counters': record['counters']}, indent=2))
            return 0
        if args.command == 'check':
            report = check_window([json.loads(path.read_text()) for path in args.snapshots])
            print(json.dumps(report, indent=2))
            return 0 if report['counter_window_complete'] else 1
        parser.error('choose snapshot or check')
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        print('link soak: ' + str(error), file=sys.stderr)
        return 2


if __name__ == '__main__':
    sys.exit(main())
