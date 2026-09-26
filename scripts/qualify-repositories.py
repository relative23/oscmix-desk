#!/usr/bin/env python3
"""Qualify signed repositories from an already passed native build image.

Requires Docker. Only explicit public scripts/artifacts enter containers;
no host home, devices, bus, credentials or signing keys are mounted. Test
keys are generated in the tool container's ephemeral /tmp at runtime.
"""

import argparse
import hashlib
import json
import subprocess
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TARGETS = ('debian13', 'ubuntu2404', 'ubuntu2604', 'fedora44', 'opensuse16')


def qualify(args):
    native = json.loads((args.native_result / 'result.json').read_text())
    if native['result'] != 'passed' or not native['native'] or native['target'] not in TARGETS:
        raise ValueError('requires a passed native APT/RPM target qualification')
    target = native['target']
    args.output.mkdir(parents=True, exist_ok=False)
    result = dict(target=target, source_commit=native['source_commit'],
                  native_image=native['image_id'], hardware=False, published_https=False,
                  final_release_transition=False, containers=[], strict=not args.native_only)
    result['scripts'] = {
        name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest()
        for name in ('scripts/qualify-repositories.py', 'scripts/build-repository.py',
                     'tests/repository_fixtures.py', 'tests/repository_lifecycle.py',
                     'tests/repository_keys.py', 'tests/repository_client_setup.py',
                     'packaging/repository/Dockerfile', 'packaging/repository/Client.Dockerfile',
                     'packaging/repository/verify.py', 'packaging/repository/apt-method.py',
                     'packaging/repository/zypp-sigcheck.py', 'packaging/repository/dnf.cpp')}
    started = time.monotonic()
    with (args.output / 'qualification.log').open('w') as log:
        def run(*command, capture=False, timeout=600, check=True):
            print(' '.join(str(part) for part in command), file=log, flush=True)
            completed = subprocess.run([str(part) for part in command], check=check,
                                       stdout=subprocess.PIPE if capture else log,
                                       stderr=log, text=True, timeout=timeout)
            return completed.stdout.strip() if capture else completed.returncode

        def create(image, *, temporary=False):
            options = ['--init', '--network=none', '--cpus=2', '--memory=2g',
                       '--pids-limit=256', '--user=root']
            if temporary:
                options += ['--tmpfs', '/tmp:rw,mode=1777']  # noqa: S108 -- private container tmpfs
            identifier = run('docker', 'create', *options, image, 'sleep', 'infinity',
                             capture=True)
            result['containers'].append(identifier)
            run('docker', 'start', identifier)
            return identifier

        try:
            if args.tools_image is None:
                args.tools_image = 'oscmix-desk-repository-tools:debian13'
                run('docker', 'build', '--pull', '-t', args.tools_image,
                    ROOT / 'packaging/repository')
            result['tools_image'] = run('docker', 'image', 'inspect', '--format={{.Id}}',
                                        args.tools_image, capture=True)
            client_image = args.client_image or native['image_id']
            if args.client_image:
                base_layers = json.loads(run('docker', 'image', 'inspect', '--format',
                                              '{{json .RootFS.Layers}}', native['image_id'],
                                              capture=True))
                client_layers = json.loads(run('docker', 'image', 'inspect', '--format',
                                                '{{json .RootFS.Layers}}', client_image,
                                                capture=True))
                if client_layers[:len(base_layers)] != base_layers:
                    raise ValueError('client image does not extend the qualified native image')  # noqa: TRY301
            result['client_image'] = run('docker', 'image', 'inspect', '--format={{.Id}}',
                                         client_image, capture=True)
            client = create(result['client_image'])
            tools = create(result['tools_image'], temporary=True)
            manager = ('apt-get' if target.startswith(('debian', 'ubuntu')) else
                       'dnf' if target == 'fedora44' else 'zypper')
            result['manager_version'] = run('docker', 'exec', client, manager, '--version',
                                             capture=True).splitlines()
            result['gpg_version'] = run('docker', 'exec', tools, 'gpg', '--version',
                                         capture=True).splitlines()[:2]
            run('docker', 'exec', tools, 'mkdir', '-p', '/work/scripts', '/work/tests',
                '/work/inputs/' + target)
            with tempfile.TemporaryDirectory(prefix='oscmix-repository-inputs-') as scratch:
                for name in ('package', 'upgraded'):
                    run('docker', 'cp', client + ':/work/build/qualification/' + name, scratch)
                    run('docker', 'cp', Path(scratch) / name,
                        tools + ':/work/inputs/' + target + '/' + name)
            for name in ('scripts/build-repository.py', 'tests/repository_fixtures.py'):
                run('docker', 'cp', ROOT / name, tools + ':/work/' + name)
            run('docker', 'exec', tools, 'python3', '/work/tests/repository_fixtures.py',
                '--target', target, '--inputs', '/work/inputs',
                '--output', '/work/public-fixtures')
            run('docker', 'cp', tools + ':/work/public-fixtures', args.output / 'fixtures')
            run('docker', 'cp', args.output / 'fixtures', client + ':/work/repository-fixtures')
            for name in ('repository_lifecycle.py', 'repository_keys.py',
                         'repository_client_setup.py'):
                run('docker', 'cp', ROOT / 'tests' / name, client + ':/work/tests/' + name)
            run('docker', 'exec', client, 'mkdir', '-p', '/work/packaging/repository')
            for name in ('verify.py', 'apt-method.py', 'zypp-sigcheck.py', 'dnf.cpp'):
                run('docker', 'cp', ROOT / 'packaging/repository' / name,
                    client + ':/work/packaging/repository/' + name)
            if not args.native_only:
                run('docker', 'exec', client, 'python3', '/work/tests/repository_client_setup.py',
                    target)
            for name, fixture in [('lifecycle', '/work/repository-fixtures/' + target),
                                  ('keys', '/work/repository-fixtures')]:
                exit_code = run('docker', 'exec', client, 'python3',
                                '/work/tests/repository_' + name + '.py', '--target', target,
                                '--fixtures', fixture, '--output', '/work/repository-' + name,
                                *([] if args.native_only else ['--strict']),
                                check=False)
                run('docker', 'cp', client + ':/work/repository-' + name, args.output / name)
                if exit_code:
                    # Record the failure before cleaning up this run's containers.
                    raise ValueError(name + ' qualification failed; see client commands')  # noqa: TRY301
            result['result'] = 'passed'
        except (OSError, ValueError, subprocess.SubprocessError):
            result['result'] = 'failed'
            raise
        finally:
            for identifier in reversed(result['containers']):
                run('docker', 'rm', '--force', identifier, check=False, timeout=30)
            result['seconds'] = round(time.monotonic() - started, 2)
            (args.output / 'result.json').write_text(json.dumps(result, indent=2) + '\n')
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--native-result', required=True, type=Path,
                        help='passed qualify-distribution.py --native output directory')
    parser.add_argument('--output', required=True, type=Path, help='new evidence directory')
    parser.add_argument('--tools-image', help='reuse an explicitly selected local tools image')
    parser.add_argument('--client-image',
                        help='native image extended only with verification build dependencies')
    parser.add_argument('--native-only', action='store_true',
                        help='diagnostic baseline without extra hooks; never a release gate')
    args = parser.parse_args()
    args.native_result = args.native_result.resolve()
    args.output = args.output.resolve()
    try:
        result = qualify(args)
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        parser.exit(1, 'repository qualification failed: %s\n' % exc)
    print(json.dumps(result, indent=2))


if __name__ == '__main__':
    main()
