#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Unto Labs
# SPDX-License-Identifier: Apache-2.0
"""Manual link verification on real builds in two retained git worktrees."""

import argparse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import time


COUNTERS = ('link verified', 'link mismatch', 'link verify error', 'cache miss')


def write_json(path, value):
    with path.open('x') as stream:
        json.dump(value, stream, indent=2)
        stream.write('\n')


def digest(path):
    with path.open('rb') as stream:
        result = hashlib.sha256()
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            result.update(block)
    return result.hexdigest()


def verification_log(path):
    processes = {}
    for line in path.read_text().splitlines() if path.exists() else []:
        match = re.search(r'\[(\d+)\] link verify: (.*)', line)
        if not match:
            continue
        process = processes.setdefault(match[1], {'outputs': [], 'outcome': None})
        message = match[2]
        if message.startswith('output='):
            process['outputs'].append(os.path.normpath(message[7:].split(' cached=')[0]))
        elif message.startswith(('verified candidate=', 'mismatch candidate=', 'error candidate=')):
            process['outcome'] = message.split()[0]
    return list(processes.values())


def phase_errors(report):
    errors = []
    delta = {}
    for counter in COUNTERS:
        if counter not in report['before'] or counter not in report['after']:
            errors.append('missing counter: ' + counter)
        else:
            delta[counter] = report['after'][counter] - report['before'][counter]
            if delta[counter] < 0:
                errors.append('counter reset: ' + counter)
    for counter in ('link mismatch', 'link verify error'):
        if delta.get(counter, 0) > 0:
            errors.append('nonzero ' + counter)
    attempts = sum(delta.get(counter, 0) for counter in COUNTERS[:3])
    if delta.get('cache miss', 0) < attempts:
        errors.append('verification outcomes exceed real link misses')
    verified = 0
    for build in report['builds']:
        if build['exit_code'] != 0:
            errors.append('build failed: ' + build['tree'])
        if build['link_hits']:
            errors.append('served cached link output: ' + build['tree'])
        outcomes = build['verification']
        successes = [item for item in outcomes if item['outcome'] == 'verified']
        verified += len(successes)
        if any(item['outcome'] in ('mismatch', 'error') for item in outcomes):
            errors.append('verification log reports failure: ' + build['tree'])
        covered = {name for item in successes for name in item['outputs']}
        for name in report['outputs']:
            if name not in build['artifacts']:
                errors.append('missing output: ' + build['tree'] + '/' + name)
            if report['require_each_tree'] and name not in covered:
                errors.append('output has no successful verification: ' + build['tree'] + '/' + name)
    if report['require_verification'] and verified == 0:
        errors.append('zero successful verifications')
    if verified != delta.get('link verified', 0):
        errors.append('verification logs and counter delta disagree')
    builds = report['builds']
    if len(builds) == 2:
        if max(item['start_monotonic'] for item in builds) >= min(
                item['end_monotonic'] for item in builds):
            errors.append('build processes did not overlap')
        for name in report['outputs']:
            if builds[0]['artifacts'].get(name) != builds[1]['artifacts'].get(name):
                errors.append('cross-tree output differs: ' + name)
    return errors


def run_harness(args):
    user = os.environ.get('USER', str(os.getuid()))
    scratch = Path('/build') / user / 'codex-1000'
    scratch.mkdir(parents=True, exist_ok=True)
    evidence = Path(tempfile.mkdtemp(prefix='link-verify-worktrees-', dir=scratch))
    print('Evidence: ' + str(evidence), flush=True)
    shutil.copyfile(__file__, evidence / 'harness.py')
    shutil.copyfile(Path(__file__).with_name('link_verify_compiler.sh'),
                    evidence / 'link_verify_compiler.sh')
    repo = args.repo.resolve()
    binary = args.vcache.resolve()
    revision = subprocess.check_output(['git', '-C', str(repo), 'rev-parse', args.ref],
                                       text=True).strip()
    tree_root = Path('/build') / user / 'claude-tmp/worktrees' / repo.name / evidence.name
    tree_root.mkdir(parents=True)
    trees = [tree_root / name for name in ('a', 'b')]
    env = {key: value for key, value in os.environ.items()
           if not key.startswith('VCACHE_') and key != 'RUSTC_WRAPPER'}
    env['PATH'] = ':'.join(part for part in env['PATH'].split(':')
                           if 'ccache' not in part.lower() and 'sccache' not in part.lower())
    (evidence / 'tmp').mkdir()
    (evidence / 'config.toml').write_text('[vcache]\n')
    env.update(VCACHE=str(binary), VCACHE_CONFIG=str(evidence / 'config.toml'),
               LINK_VERIFY_COMPILER=str(evidence / 'link_verify_compiler.sh'),
               TMPDIR=str(evidence / 'tmp'), VCACHE_CACHE_SIZE=args.cache_size,
               VCACHE_LINK_CACHE='1', VCACHE_LINK_VERIFY='1', VCACHE_MAP_CWD='1',
               VCACHE_DAEMON='auto', VCACHE_DAEMON_SINGLE_FLIGHT='1',
               VCACHE_DAEMON_ADMISSION='1', VCACHE_READONLY='0', VCACHE_RECACHE='0')
    write_json(evidence / 'run.json', {
        'started_utc': datetime.now(timezone.utc).isoformat(), 'revision': revision,
        'repo': str(repo), 'ref': args.ref, 'trees': list(map(str, trees)),
        'vcache': str(binary), 'vcache_sha256': digest(binary),
        'vcache_revision': Path(str(binary) + '.commit').read_text().strip()
                           if Path(str(binary) + '.commit').exists() else None,
        'tracer_sha256': digest(binary.with_name('vcache-fstrace.so')),
        'commands': {'setup': args.setup, 'build': args.build, 'relink': args.relink,
                     'describe_tools': args.describe_tools},
        'rounds': args.rounds, 'outputs': args.output, 'cache_size': args.cache_size,
        'cgroup': Path('/proc/self/cgroup').read_text(), 'makeflags': env.get('MAKEFLAGS'),
    })

    def tree_env(tree, store):
        return dict(env, VCACHE_DIR=str(store), VCACHE_ROOTS=f'{tree}=link-verify-project',
                    LINK_VERIFY_ROOT=str(tree))

    def command(tree, store, directory, command_text):
        directory.mkdir()
        build_env = tree_env(tree, store)
        build_env['VCACHE_LOG'] = str(directory / 'vcache.log')
        with (directory / 'config.txt').open('x') as stream:
            subprocess.run([str(binary), '--show-config'], cwd=tree, env=build_env,
                           stdout=stream, check=True)
        started = time.monotonic()
        with (directory / 'build.log').open('x') as stream:
            result = subprocess.run(['/usr/bin/time', '-f', 'wall=%e user=%U sys=%S',
                                     '-o', str(directory / 'time.txt'),
                                     'bash', '-c', command_text], cwd=tree, env=build_env,
                                    stdout=stream, stderr=subprocess.STDOUT)
        ended = time.monotonic()
        artifacts = {}
        for name in args.output:
            source = tree / name
            if source.is_file():
                destination = directory / 'outputs' / name
                destination.parent.mkdir(parents=True, exist_ok=True)
                subprocess.run(['cp', '--reflink=auto', '--preserve=mode', str(source),
                                str(destination)], check=True)
                artifacts[name] = {'sha256': digest(destination),
                                   'executable': bool(destination.stat().st_mode & 0o100)}
        return {'tree': tree.name, 'exit_code': result.returncode,
                'start_monotonic': started, 'end_monotonic': ended,
                'verification': verification_log(directory / 'vcache.log'),
                'link_hits': (directory / 'vcache.log').read_text().count('] link hit on ')
                             if (directory / 'vcache.log').exists() else 0,
                'artifacts': artifacts}

    def stats(store, path):
        output = subprocess.check_output([str(binary), '--show-stats'],
                                         env=tree_env(trees[0], store), text=True)
        path.write_text(output)
        return {match[1]: int(match[2]) for line in output.splitlines()
                if (match := re.fullmatch(r'(.+?)\s{2,}(\d+)', line))}

    def phase(name, store, selected, command_text, required, each):
        directory = evidence / name
        directory.mkdir()
        # Keep candidates before verification can replace a mismatching entry.
        if store.exists():
            subprocess.run(['cp', '-a', '--reflink=auto', str(store),
                            str(directory / 'store-before')], check=True)
        before = stats(store, directory / 'stats-before.txt')
        started_utc = datetime.now(timezone.utc).isoformat()
        with ThreadPoolExecutor(max_workers=len(selected)) as pool:
            builds = list(pool.map(lambda tree: command(tree, store, directory / tree.name,
                                                       command_text), selected))
        report = {'phase': name, 'store': str(store), 'outputs': args.output,
                  'require_verification': required, 'require_each_tree': each,
                  'before': before, 'after': stats(store, directory / 'stats-after.txt'),
                  'builds': builds, 'started_utc': started_utc,
                  'ended_utc': datetime.now(timezone.utc).isoformat()}
        report['errors'] = phase_errors(report)
        write_json(directory / 'report.json', report)
        print(name + ': ' + ('FAIL ' + '; '.join(report['errors']) if report['errors'] else
                            'PASS; verified delta=' + str(report['after'].get('link verified', 0) -
                                                          before.get('link verified', 0))), flush=True)
        if report['errors']:
            raise RuntimeError('evidence gate failed: ' + name)

    stores = [evidence / name for name in ('warm-store', 'cold-store')]
    try:
        with (evidence / 'toolchain.txt').open('x') as stream:
            subprocess.run(['bash', '-c', args.describe_tools], cwd=repo, env=env,
                           stdout=stream, stderr=subprocess.STDOUT, check=True)
        for tree in trees:
            # Tracker hooks must not write through an unattached experimental tree.
            subprocess.run(['git', '-C', str(repo), '-c', 'core.hooksPath=/dev/null',
                            'worktree', 'add', '--detach',
                            str(tree), args.ref], check=True)
            actual = subprocess.check_output(['git', '-C', str(tree), 'rev-parse', 'HEAD'],
                                             text=True).strip()
            if actual != revision:
                raise RuntimeError('source ref changed while creating worktrees')
            if args.setup:
                result = command(tree, stores[0], evidence / ('setup-' + tree.name), args.setup)
                if result['exit_code']:
                    raise RuntimeError('setup failed: ' + tree.name)
        phase('warmup-a', stores[0], trees[:1], args.build, False, False)
        phase('cross-tree-b', stores[0], trees[1:], args.build, True, True)
        for number in range(args.rounds):
            phase(f'warm-concurrent-{number + 1}', stores[0], trees, args.relink, True, True)
        phase('cold-concurrent', stores[1], trees, args.relink, True, False)
        write_json(evidence / 'complete.json', {'finished_utc': datetime.now(timezone.utc).isoformat(),
                                               'result': 'pass', 'soak_complete': False})
    finally:
        for store in stores:
            if store.exists():
                subprocess.run([str(binary), '--stop-daemon'], env=tree_env(trees[0], store),
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    print('PASS: controlled concurrent-worktree verification; not a completed soak.', flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--repo', type=Path, required=True)
    parser.add_argument('--ref', default='origin/main')
    parser.add_argument('--vcache', type=Path, required=True)
    parser.add_argument('--setup', default='', help='run once in each newly created worktree')
    parser.add_argument('--build', required=True, help='real project build command')
    parser.add_argument('--relink', required=True, help='force real links using existing objects')
    parser.add_argument('--output', action='append', required=True, help='relative output; repeatable')
    parser.add_argument('--rounds', type=int, default=3)
    parser.add_argument('--cache-size', default='10G')
    parser.add_argument('--describe-tools', default='c++ --version && ld --version',
                        help='record the compiler/linker or container toolchain used by the recipe')
    args = parser.parse_args()
    if args.rounds < 1:
        parser.error('--rounds must be positive')
    for name in args.output:
        if Path(name).is_absolute() or '..' in Path(name).parts or name == '.':
            parser.error('--output must stay inside the worktree')
    args.output = list(dict.fromkeys(os.path.normpath(name) for name in args.output))
    if 'buildlimit.slice' not in Path('/proc/self/cgroup').read_text():
        limiter = Path.home() / 'bin/build-limited'
        os.execv(str(limiter), [str(limiter), sys.executable, str(Path(__file__).resolve()),
                               *sys.argv[1:]])
    try:
        run_harness(args)
    except (OSError, RuntimeError, subprocess.CalledProcessError) as error:
        print('FAIL: ' + str(error), file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
