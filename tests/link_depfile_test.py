#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Unto Labs
# SPDX-License-Identifier: Apache-2.0
"""Real-link depfile replay, verification, refusal and namespace regression."""
import argparse
from concurrent.futures import ThreadPoolExecutor
import hashlib
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import tempfile


def main():
    if sys.platform != 'linux':
        print('SKIP link depfiles require the Linux tracer')
        return False
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('vcache', type=Path)
    parser.add_argument('--old-vcache', type=Path)
    parser.add_argument('--compiler', default='gcc')
    parser.add_argument('--linker', default='bfd')
    args = parser.parse_args()
    binary = str(args.vcache.resolve())
    compiler = shutil.which(args.compiler)
    if not compiler:
        raise RuntimeError('compiler not found: ' + args.compiler)
    work = Path(tempfile.mkdtemp(prefix='vcache-link-depfile-'))
    print('link depfile evidence:', work, flush=True)
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(('VCACHE_', 'CCACHE_')) and k != 'LD_PRELOAD'}
    (work / 'config.toml').write_text('[vcache]\n')
    env.update(VCACHE_CONFIG=str(work / 'config.toml'), VCACHE_DAEMON='off',
               VCACHE_MAP_CWD='0', VCACHE_LINK_CACHE='1', VCACHE_LINK_VERIFY='0',
               VCACHE_CACHE_SIZE='1G', VCACHE_READONLY='0', VCACHE_RECACHE='0')
    (work / 'main.c').write_text('int main(void) { return 0; }\n')
    subprocess.run([compiler, '-c', str(work / 'main.c'), '-o', str(work / 'main.o')],
                   env=env, check=True)
    for tree in ('a', 'b'):
        (work / tree).mkdir()
        shutil.copyfile(work / 'main.o', work / tree / 'main.o')
    # The wrapper changes a fresh result without changing its keyed command or inputs.
    wrapper = work / 'gcc'
    wrapper.write_text(r'''#!/bin/bash
forwarded=()
while (($#)); do
  case "$1" in
    -Xlinker) forwarded+=("$1" "$2"); shift 2 ;;
    --dependency-file) forwarded+=("-Wl,--dependency-file=$2"); shift 2 ;;
    --dependency-file=*) forwarded+=("-Wl,$1"); shift ;;
    *) forwarded+=("$1"); shift ;;
  esac
done
''' + shlex.quote(compiler) + ' "${forwarded[@]}" || exit $?\n' + r'''
case "${LINK_DEPFILE_CASE:-}" in
  mismatch) sed -i "s|$PWD/main.o|main.o|g" link.d ;;
  malformed) printf 'broken\\' > link.d ;;
  incomplete) printf '!' > "${VCACHE_TRACE_LOG}.incomplete" ;;
  mode) chmod u+x link.d ;;
  evict) for file in "$LINK_DEPFILE_STORE"/*/*.linkout; do rm -f "$file"; done ;;
esac
''')
    wrapper.chmod(0o755)
    failures = 0
    sequence = 0

    def check(ok, name):
        nonlocal failures
        failures += not ok
        print(('PASS ' if ok else 'FAIL ') + name, flush=True)

    colon_env = dict(env, VCACHE_DIR=str(work / 'colon-cache'),
                     VCACHE_ROOTS=str(work) + '=colon')
    colon_command = [binary, compiler, '-c', str(work / 'main.c'), '-o', str(work / 'colon.o'),
                     '-MMD', '-MF', str(work / 'colon.d'), '-MT', r'out\:name.o']
    subprocess.run(colon_command, env=colon_env, check=True)
    subprocess.run([binary, '--zero-stats'], env=colon_env, check=True, stdout=subprocess.DEVNULL)
    subprocess.run(colon_command, env=colon_env, check=True)
    colon_stats = subprocess.check_output([binary, '--show-stats'], env=colon_env,
                                         universal_newlines=True)
    colon_hit = any(line.startswith('cache hit (disk)') and int(line.split()[-1]) == 1
                    for line in colon_stats.splitlines())
    check(colon_hit and (work / 'colon.d').read_text().startswith(r'out\:name.o:'),
          'real compiler escaped-colon target survives a warm cache hit')

    def run(label, tree='a', store='store', verify=False, name='main.o', direct=False,
            cache=binary, variant=None, target='app', **settings):
        nonlocal sequence
        sequence += 1
        cwd = work / tree
        log = work / f'{sequence}-{label}.log'
        current = dict(env, VCACHE_DIR=str(work / store), VCACHE_ROOTS=str(cwd) + '=project',
                       VCACHE_LINK_VERIFY=str(int(verify)), VCACHE_LOG=str(log),
                       LINK_DEPFILE_STORE=str(work / store), **settings)
        flags = variant or ['-Wl,--dependency-file=link.d']
        cmd = [str(wrapper), '-fuse-ld=' + args.linker, *flags, str(cwd / name), '-o', target]
        result = subprocess.run(([] if direct else [cache]) + cmd, cwd=cwd, env=current,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                universal_newlines=True, timeout=45)
        if result.returncode:
            raise RuntimeError(f'{label}: status {result.returncode}: {result.stderr}')
        text = (cwd / 'link.d').read_text()
        digest = hashlib.sha256((cwd / target).read_bytes()).hexdigest()
        log_text = log.read_text() if log.exists() else ''
        return text, digest, log_text

    def rules(text):
        # Fixtures accepted for replay contain only ordinary path characters.
        return [line.strip().split() for line in text.replace('\\\n', ' ').splitlines()
                if line.strip()]

    def hit(row):
        return 'link hit on ' in row[2]

    cold = run('cold')
    check('link hit on ' not in cold[2], 'cold link runs')
    control = run('fresh-b', tree='b', direct=True)
    replay = run('replay-b', tree='b')
    check(hit(replay) and rules(replay[0]) == rules(control[0]) and replay[1] == control[1],
          'ordinary cross-tree hit replays destination depfile rules and exact executable')
    verified = run('verify-b', tree='b', verify=True)
    check('link verify: verified' in verified[2] and verified[:2] == control[:2],
          'cross-tree verification compares localized replay and keeps fresh bytes')
    run('recache-b', tree='b', VCACHE_RECACHE='1')
    control_a = run('fresh-a', direct=True)
    replay_a = run('replay-a')
    check(hit(replay_a) and rules(replay_a[0]) == rules(control_a[0]),
          'recache stores canonical depfiles for the next destination')
    read_only = run('read-only-verify', verify=True, VCACHE_READONLY='1')
    check('link verify: verified' in read_only[2] and read_only[:2] == control_a[:2],
          'read-only verification keeps fresh local outputs')
    changed = run('changed-target', target='different-app')
    check(not hit(changed) and changed[0].startswith('different-app:'),
          'changed output target refuses the earlier output set')

    # All currently recognized dependency option spellings must select typed output handling.
    for index, variant in enumerate([
        ['--dependency-file', 'link.d'], ['--dependency-file=link.d'],
        ['-Wl,--dependency-file=link.d'], ['-Wl,--dependency-file,link.d'],
        ['-Xlinker', '--dependency-file', '-Xlinker', 'link.d'],
        ['-Xlinker', '--dependency-file=link.d'],
    ]):
        store = f'option-{index}'
        run('option-cold', store=store, variant=variant)
        row = run('option-hit', tree='b', store=store, variant=variant)
        check(hit(row) and rules(row[0]) == rules(control[0]),
              'dependency option localizes: ' + ' '.join(variant))

    for name in ['space name.o', r'back\slash.o', r'back\#hash.o', r'back\ space.o',
                 r'back\:colon.o', 'colon:name.o', 'hash#name.o', 'dollar$name.o']:
        shutil.copyfile(work / 'main.o', work / 'a' / name)
        store = 'unsafe-' + str(sequence)
        run('unsupported-cold', store=store, name=name)
        row = run('unsupported-warm', store=store, name=name)
        check(not hit(row) and 'unsupported dependency file; not storing' in row[2],
              'ambiguous filename declines whole cached link: ' + repr(name))

    # Incorrect localization must not be hidden by canonicalizing both sides in verification.
    alias = work / 'alias'
    alias.symlink_to(work / 'a', target_is_directory=True)
    run('alias-cold', store='alias-store')
    row = run('alias-verify', tree='alias', store='alias-store', verify=True)
    check('link verify: verified' not in row[2],
          'root alias cannot certify different replay path spelling')

    for mode in ('mismatch', 'malformed', 'incomplete', 'mode'):
        store = 'fresh-' + mode
        run('fresh-cold', store=store)
        row = run('fresh-' + mode, store=store, verify=True, LINK_DEPFILE_CASE=mode)
        outcome = 'error' if mode in ('malformed', 'incomplete') else 'mismatch'
        check('link verify: ' + outcome in row[2], 'fresh depfile ' + mode + ' is not verified')
        if mode == 'mismatch':
            expected = run('replacement-control', tree='b', direct=True,
                           LINK_DEPFILE_CASE='mismatch')
            replay = run('replacement-replay', tree='b', store=store)
            check(hit(replay) and rules(replay[0]) == rules(expected[0]),
                  'mismatch replacement stores a canonical depfile')
        if mode == 'malformed':
            replay = run('malformed-preserves-candidate', tree='b', store=store)
            check(hit(replay) and rules(replay[0]) == rules(control[0]),
                  'unsupported fresh depfile does not replace the valid candidate')
        if mode == 'mode':
            replay = run('mode-replay', tree='b', store=store)
            check(hit(replay) and os.access(work / 'b' / 'link.d', os.X_OK),
                  'depfile replay preserves executable metadata')

    run('readonly-cold', store='readonly')
    row = run('readonly-mismatch', store='readonly', verify=True, VCACHE_READONLY='1',
              LINK_DEPFILE_CASE='mismatch')
    replay = run('readonly-replay', tree='b', store='readonly')
    check('link verify: mismatch' in row[2] and hit(replay) and
          rules(replay[0]) == rules(control[0]), 'read-only mismatch keeps the original candidate')

    run('eviction-cold', store='eviction')
    row = run('eviction-verify', tree='b', store='eviction', verify=True,
              LINK_DEPFILE_CASE='evict')
    check('link verify: verified' in row[2],
          'localized candidate snapshot survives sidecar eviction during fresh link')

    run('corrupt-cold', store='corrupt')
    sidecars = list((work / 'corrupt').glob('*/*.linkout'))
    dep_sidecars = [p for p in sidecars if p.read_bytes().startswith(b'app:')]
    if not dep_sidecars:
        raise RuntimeError('no depfile sidecar to corrupt')
    dep_sidecars[0].write_text('broken cached depfile')
    row = run('corrupt-verify', store='corrupt', verify=True)
    check(not hit(row) and 'link verify: candidate=' not in row[2],
          'corrupt depfile sidecar is a miss without verification')

    run('input-cold', store='input-change')
    check(hit(run('input-warm', store='input-change')), 'input-change fixture is warm')
    (work / 'changed.c').write_text('int main(void) { return 1; }\n')
    subprocess.run([compiler, '-c', str(work / 'changed.c'), '-o', str(work / 'a' / 'main.o')],
                   env=env, check=True)
    row = run('input-changed', store='input-change')
    check(not hit(row) and row[1] != cold[1], 'changed input misses and changes the executable')
    shutil.copyfile(work / 'main.o', work / 'a' / 'main.o')

    run('concurrent-cold', store='concurrent')

    def concurrent_tree(tree):
        expected = control_a if tree == 'a' else control
        for index in range(3):
            row = run(f'concurrent-{tree}-{index}', tree=tree, store='concurrent', verify=True)
            if 'link verify: verified' not in row[2] or row[:2] != expected[:2]:
                return False
            row = run(f'concurrent-hit-{tree}-{index}', tree=tree, store='concurrent')
            if not hit(row) or rules(row[0]) != rules(expected[0]) or row[1] != expected[1]:
                return False
        return True

    with ThreadPoolExecutor(max_workers=2) as pool:
        check(all(pool.map(concurrent_tree, ('a', 'b'))),
              'concurrent verification and replay retain each worktree depfile')

    if args.old_vcache:
        old = str(args.old_vcache.resolve())
        run('old-cold', store='migration', cache=old)
        check(hit(run('old-warm', store='migration', cache=old)), 'old namespace is warm')
        check(not hit(run('upgrade', store='migration')), 'upgrade rejects old raw depfile namespace')
        row = run('upgrade-warm', tree='b', store='migration')
        check(hit(row) and rules(row[0]) == rules(control[0]), 'upgraded namespace warms locally')
    print(f'link depfile failures: {failures}', flush=True)
    return bool(failures)


if __name__ == '__main__':
    raise SystemExit(main())
