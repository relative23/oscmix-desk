#!/usr/bin/env python3
"""Qualify a committed source tree or native package in a disposable container.

Requires Docker, Git and a clean checkout. Never mounts the host home,
sound devices or service bus. Only a Git bundle enters the build context;
local Git credentials, ignored files and uncommitted reviews stay outside.
"""

import argparse
import hashlib
import json
import re
import subprocess
import tempfile
import time
from pathlib import Path

APT = ('apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y '
       'python3 python3-venv git gcc make pkg-config libasound2-dev util-linux '
       'ca-certificates bash procps')
TARGETS = {
    'debian13': ('debian:13', APT, None, ''),
    'ubuntu2404': ('ubuntu:24.04', APT, 'deb',
                   ('apt-get update && DEBIAN_FRONTEND=noninteractive '
                    'apt-get install -y dpkg-dev binutils adduser libgtk-3-dev '
                    'libglib2.0-bin xvfb xauth dbus-x11 python3-gi '
                    'gir1.2-atspi-2.0 at-spi2-core gnome-shell plasma-workspace '
                    'xfce4-session xfwm4 xfce4-panel xfdesktop4 x11-utils mesa-utils')),
    'ubuntu2604': ('ubuntu:26.04', APT, None, ''),
    'fedora44': ('fedora:44',
                 ('dnf install -y python3 python3-pip git gcc make pkgconf-pkg-config '
                  'alsa-lib-devel util-linux diffutils ca-certificates bash '
                  'shadow-utils procps-ng'),
                 'rpm', ('dnf install -y rpm-build systemd gtk3-devel '
                         'xorg-x11-server-Xvfb xorg-x11-xauth dbus-daemon '
                         'python3-gobject at-spi2-core')),
    'opensuse16': ('opensuse/leap:16.0',
                   ('zypper --non-interactive install python3 python3-pip git gcc make pkg-config '
                    'alsa-devel util-linux diffutils ca-certificates bash shadow procps'),
                   'rpm', ('zypper --non-interactive install rpm-build systemd gtk3-devel '
                           'xorg-x11-server-Xvfb xvfb-run xauth dbus-1-x11 python3-gobject '
                           'typelib-1_0-Atspi-2_0')),
    'arch': ('archlinux:base',
             ('pacman -Syu --noconfirm python python-pip git gcc make pkgconf alsa-lib util-linux '
              'diffutils ca-certificates bash shadow procps-ng'),
             'arch', ('pacman -Syu --noconfirm base-devel zstd gtk3 '
                      'xorg-server-xvfb xorg-xauth dbus python-gobject at-spi2-core')),
    'alpine322': ('alpine:3.22',
                  ('apk add python3 py3-pip bash git build-base pkgconf alsa-lib-dev util-linux '
                   'coreutils ca-certificates shadow procps'), None, ''),
}


def read(*args):
    return subprocess.run(args, capture_output=True, text=True, check=True).stdout.strip()


def dockerfile(target, pin, commit, native, development, previous):
    base, dependencies, kind, packaging = TARGETS[target]
    text = ('FROM ' + base + '\nRUN ' + dependencies + '\n'
            + ('RUN ' + packaging + '\n' if native else '')
            + 'RUN useradd -m -u 12510 tester && '
            '(getent group audio || groupadd --system audio) && usermod -aG audio tester && '
            'install -d -o root -g audio -m 3770 /run/oscmix-desk\n'
            'COPY source.bundle /tmp/source.bundle\n'
            'RUN git clone /tmp/source.bundle /work && git -C /work checkout ' + commit + '\n'
            'RUN python3 -m venv /opt/qa && '
            '/opt/qa/bin/pip install --no-cache-dir -r /work/requirements-dev.txt\n'
            'RUN chown -R tester:tester /work\nUSER tester\nWORKDIR /work\n'
            'ENV PATH="/opt/qa/bin:${PATH}" OSCMIX_QUALIFY_CONTAINER=1\n'
            'RUN git clone --mirror https://github.com/michaelforney/oscmix '
            'build/oscmix-source.git && '
            'git clone --no-checkout /work/build/oscmix-source.git build/oscmix && '
            'git -C build/oscmix checkout ' + pin + '\n')
    if native:
        for directory, revision in (('package', 1), ('repeated', 1), ('upgraded', 2)):
            text += ('RUN python3 scripts/build-package.py --format ' + kind
                     + ' --backend-source build/oscmix --build-dir /tmp/oscmix-build'
                     + ' --output /work/build/qualification/' + directory
                     + ' --with-gtk'
                     + ' --package-revision ' + str(revision)
                     + (' --development' if development else '') + '\n')
        text += ('RUN git worktree add --detach /tmp/previous-source ' + previous + ' && '
                 'python3 /tmp/previous-source/scripts/build-package.py --format ' + kind
                 + ' --backend-source /work/build/oscmix --build-dir /tmp/previous-build'
                 + ' --output /work/build/qualification/previous\n')
    return text


def qualify(args, root):
    if read('git', '-C', str(root), 'status', '--porcelain'):
        raise ValueError('qualification requires a clean committed tree; save work first')
    commit = read('git', '-C', str(root), 'rev-parse', 'HEAD')
    previous = read('git', '-C', str(root), 'rev-parse', '--verify',
                    args.previous_tag + '^{commit}')
    pin = re.search(r'^OSCMIX_REF="\$\{OSCMIX_REF:-([a-f0-9]{40})\}"$',
                    (root / 'install.sh').read_text(), re.MULTILINE)
    if pin is None:
        raise ValueError('installer must pin the full backend commit')
    base, _, kind, _ = TARGETS[args.target]
    if args.native and kind is None:
        raise ValueError('no qualified native package recipe for ' + args.target)
    args.output.mkdir(parents=True, exist_ok=False)
    image = 'oscmix-desk-ci:%s-%s-%s' % (
        args.target, commit[:12], 'native' if args.native else 'source')
    started = time.monotonic()
    record = dict(target=args.target, platform='linux/amd64', base=base,
                  source_commit=commit, backend_commit=pin[1], native=args.native,
                  previous_tag=args.previous_tag, previous_source_commit=previous,
                  development=args.development, hardware=False, service_manager=False)
    with (args.output / 'qualification.log').open('w') as log:
        def run(*command, timeout=1200):
            print(' '.join(command), file=log, flush=True)
            subprocess.run(command, stdout=log, stderr=subprocess.STDOUT,
                           check=True, timeout=timeout)

        try:
            with tempfile.TemporaryDirectory(prefix='oscmix-distribution-') as temporary:
                context = Path(temporary)
                run('git', '-C', str(root), 'bundle', 'create',
                    str(context / 'source.bundle'), 'HEAD', 'refs/tags/' + args.previous_tag)
                recipe = dockerfile(args.target, pin[1], commit, args.native, args.development,
                                    previous)
                (context / 'Dockerfile').write_text(recipe)
                (args.output / 'Dockerfile').write_text(recipe)
                run('docker', 'pull', '--platform=linux/amd64', base)
                record['base_image'] = json.loads(read('docker', 'image', 'inspect', base))[0]
                run('docker', 'build', '--platform=linux/amd64', '-t', image, str(context))
            record['image_id'] = read('docker', 'image', 'inspect', '--format={{.Id}}', image)
            command = ['docker', 'run', '--rm', '--platform=linux/amd64', '--network=none',
                       '--cpus=2', '--memory=2g', '--pids-limit=512']
            if args.native:
                command += ['--user=root', image, 'python3', 'tests/package_lifecycle.py', kind]
            else:
                command += ['--cap-drop=ALL', '--security-opt=no-new-privileges', image,
                            'bash', 'scripts/test-distribution.sh']
            run(*command, timeout=600)
            if args.native:
                identifier = read('docker', 'create', image)
                try:
                    run('docker', 'cp', identifier + ':/work/build/qualification/package',
                        str(args.output))
                finally:
                    run('docker', 'rm', identifier)
                payload = args.output / 'package'
                (payload / ('SHA256SUMS-' + args.target)).write_text(''.join(
                    '%s  %s\n' % (hashlib.sha256(p.read_bytes()).hexdigest(), p.name)
                    for p in sorted(payload.iterdir()) if p.is_file()))
            record['result'] = 'passed'
        except (OSError, subprocess.SubprocessError):
            record['result'] = 'failed'
            raise
        finally:
            record['seconds'] = round(time.monotonic() - started, 2)
            (args.output / 'result.json').write_text(json.dumps(record, indent=2) + '\n')
    return record


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--target', required=True, choices=TARGETS)
    parser.add_argument('--native', action='store_true')
    parser.add_argument('--previous-tag', default='v0.7.2',
                        help='released core version for actual native upgrade/rollback')
    parser.add_argument('--development', action='store_true',
                        help='mark test packages as development versions')
    parser.add_argument('--output', required=True, type=Path,
                        help='new output directory; never overwrites earlier evidence')
    args = parser.parse_args()
    if not re.fullmatch(r'v\d+\.\d+\.\d+', args.previous_tag):
        parser.error('--previous-tag must be a version tag such as v0.7.2')
    args.output = args.output.resolve()
    try:
        result = qualify(args, Path(__file__).resolve().parent.parent)
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        parser.exit(1, 'distribution qualification failed: %s\n' % exc)
    print(json.dumps({k: v for k, v in result.items() if k != 'base_image'}, indent=2))


if __name__ == '__main__':
    main()
