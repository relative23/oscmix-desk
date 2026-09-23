#!/usr/bin/env python3
"""Read-only prerequisite report for a per-user source installation."""

import argparse
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path


def package_hint():
    """Read data, never source os-release as a shell program."""
    try:
        release = Path('/etc/os-release').read_text()
    except OSError:
        release = ''
    names = {}
    for line in release.splitlines():
        key, _, value = line.partition('=')
        names[key] = value.strip('"\'')
    family = names.get('ID', '')
    commands = {
        'debian': 'apt install python3 git build-essential pkg-config libasound2-dev util-linux',
        'ubuntu': 'apt install python3 git build-essential pkg-config libasound2-dev util-linux',
        'fedora': ('dnf install python3 git gcc make pkgconf-pkg-config '
                   'alsa-lib-devel util-linux diffutils'),
        'opensuse-tumbleweed': ('zypper install python3 git gcc make pkg-config '
                                'alsa-devel util-linux diffutils'),
        'opensuse-leap': ('zypper install python3 git gcc make pkg-config '
                          'alsa-devel util-linux diffutils'),
        'arch': 'pacman -S --needed python git base-devel alsa-lib util-linux',
        'alpine': 'apk add python3 bash git build-base pkgconf alsa-lib-dev util-linux coreutils',
    }
    return names.get('PRETTY_NAME', family or 'unknown Linux'), commands.get(family)


def command(args):
    try:
        return subprocess.run(args, capture_output=True, text=True, timeout=10,
                              check=False)
    except (OSError, subprocess.TimeoutExpired):
        return None


def writable_parent(path):
    while not path.exists() and path != path.parent:
        path = path.parent
    return path.is_dir() and os.access(str(path), os.W_OK | os.X_OK)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--no-build', action='store_true')
    parser.add_argument('--manual', action='store_true')
    parser.add_argument('--enable', action='store_true')
    args = parser.parse_args()
    failures = []

    def check(ok, label):
        print(('OK      ' if ok else 'MISSING ') + label)
        if not ok:
            failures.append(label)

    root = Path(__file__).resolve().parents[1]
    home = Path(os.environ.get('HOME', ''))
    check(home.is_absolute(), 'absolute HOME: ' + str(home))
    check(sys.version_info >= (3, 9), 'Python >= 3.9: ' + sys.version.split()[0])
    source = (root / 'src/oscmix_desk/constants.py').read_text()
    version = re.search(r'^__version__\s*=\s*[\'"]([^\'"]+)', source, re.MULTILINE)
    print('INFO    candidate: ' + (version[1] if version else 'unknown') + ' at ' + str(root))
    print('INFO    backend pin: ' + re.search(
        r'^OSCMIX_REF="\$\{OSCMIX_REF:-([^}]+)',
        (root / 'install.sh').read_text(), re.MULTILINE)[1])
    check(not Path('/usr/lib/oscmix-desk/package-guard').exists(),
          'source installation must not shadow a native package; '
          'use its package manager to upgrade, or remove it before switching to source')
    distro, hint = package_hint()
    print('INFO    distribution: ' + distro)
    tools = ['flock', 'install', 'mktemp', 'sed', 'cmp']
    if not args.no_build:
        check(writable_parent(root / 'build'), 'writable backend build directory')
        tools += ['git', 'make', 'cc', 'pkg-config']
    for tool in tools:
        check(shutil.which(tool) is not None, 'command: ' + tool)
    if not args.no_build:
        alsa = command(['pkg-config', '--exists', 'alsa'])
        check(alsa is not None and alsa.returncode == 0,
              'ALSA development files (pkg-config alsa)')
        gtk = command(['pkg-config', '--exists', 'gtk+-3.0'])
        if gtk is not None and gtk.returncode == 0:
            for tool in ('glib-compile-resources', 'glib-compile-schemas'):
                check(shutil.which(tool) is not None, 'optional GTK build command: ' + tool)
        else:
            print('INFO    GTK 3 headers absent; the headless backend can still be built')
    else:
        for tool in ('oscmix', 'alsaseqio'):
            search = (home / '.local/bin', Path('/usr/local/bin'), Path('/usr/bin'))
            found = next((p / tool for p in search
                          if os.access(str(p / tool), os.X_OK) and (p / tool).is_file()), None)
            check(found is not None, 'existing backend: ' + str(found or tool))
    config = Path(os.environ.get('XDG_CONFIG_HOME', ''))
    if not config.is_absolute():
        config = home / '.config'
    data = Path(os.environ.get('XDG_DATA_HOME', ''))
    if not data.is_absolute():
        data = home / '.local/share'
    for destination in (home / '.local/bin', home / '.local/lib/oscmix-desk', config, data):
        check(writable_parent(destination), 'writable destination: ' + str(destination))
    print('INFO    configuration: ' + str(config / 'oscmix/routing.conf'))
    installed = home / '.local/lib/oscmix-desk/oscmix_desk/constants.py'
    if installed.is_file():
        previous = re.search(r'^__version__\s*=\s*[\'"]([^\'"]+)',
                             installed.read_text(), re.MULTILINE)
        print('INFO    installed: ' + (previous[1] if previous else 'unknown')
              + ' at ' + str(installed.parent))
    manager = command(['systemctl', '--user', 'show-environment'])
    managed = manager is not None and manager.returncode == 0
    if args.enable:
        check(managed, '--enable needs a systemd user manager')
        if managed:
            values = dict(line.split('=', 1) for line in manager.stdout.splitlines()
                          if '=' in line)
            manager_config = Path(values.get('XDG_CONFIG_HOME', ''))
            if not manager_config.is_absolute():
                manager_config = Path(values.get('HOME', '/')) / '.config'
            check(values.get('HOME') == str(home) and manager_config == config,
                  '--enable needs the systemd user manager for this HOME and XDG_CONFIG_HOME')
    print('INFO    session mode: ' + ('systemd available' if managed and not args.manual
                                      else 'manual foreground'))
    if not managed or args.manual:
        print('INFO    manual mode has no automatic start on login, hotplug or resume')
    if not Path('/dev/snd/seq').exists():
        print('INFO    /dev/snd/seq absent; offline installation is possible; '
              'live operation needs ALSA sequencer support')
    elif not os.access('/dev/snd/seq', os.R_OK | os.W_OK):
        print('INFO    /dev/snd/seq is not accessible to this user; '
              'live operation needs device permissions')
    lock = Path('/run/oscmix-desk')
    print('INFO    shared device lock: ' + ('present at ' + str(lock) if lock.is_dir() else
          'not provisioned; create root:audio /run/oscmix-desk with mode 3770 at every boot'))
    if hint:
        print('INFO    build prerequisites (administrator privileges): ' + hint)
    else:
        print('INFO    no package names guessed for this distribution; '
              'install the commands and ALSA headers listed above')
    print('Preflight ' + ('failed' if failures else 'passed')
          + '; no installation files changed.')
    return bool(failures)


if __name__ == '__main__':
    raise SystemExit(main())
