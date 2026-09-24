#!/usr/bin/env python3
"""Build a native package from this source tree and an exact backend commit."""

import argparse
import contextlib
import hashlib
import io
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path

from package_formats import arch, guard, installed_metadata, rpm


def run(args, cwd=None):
    return subprocess.run([str(arg) for arg in args], cwd=cwd, check=True,
                          capture_output=True, text=True).stdout.strip()


@contextlib.contextmanager
def workspace(path):
    if path is None:
        with tempfile.TemporaryDirectory(prefix='oscmix-package-') as temporary:
            yield Path(temporary)
    else:
        if not path.is_absolute():
            raise ValueError('--build-dir must be absolute')
        # Only remove a directory this invocation successfully created.
        # Stable paths let makepkg record reproducible, truthful BUILDINFO.
        path.mkdir(mode=0o700)
        try:
            yield path
        finally:
            shutil.rmtree(path)


def git_metadata(root):
    if Path(run(['git', '-C', root, 'rev-parse', '--show-toplevel'])).resolve() != root:
        raise ValueError('source is inside another checkout; it is not that checkout')
    return (run(['git', '-C', root, 'rev-parse', 'HEAD']),
            bool(run(['git', '-C', root, 'status', '--porcelain'])),
            int(run(['git', '-C', root, 'show', '-s', '--format=%ct', 'HEAD'])))


def metadata(root, development):
    version = re.search(r'^__version__ = "([^"]+)"',
                        (root / 'src/oscmix_desk/constants.py').read_text(), re.MULTILINE)[1]
    pin = re.search(r'^OSCMIX_REF="\$\{OSCMIX_REF:-([^}]+)',
                    (root / 'install.sh').read_text(), re.MULTILINE)[1]
    if not re.fullmatch(r'[a-f0-9]{40}', pin):
        raise ValueError('the backend pin must be a full commit SHA')
    try:
        commit, dirty, epoch = git_metadata(root)
    except (ValueError, subprocess.CalledProcessError):
        commit, dirty, epoch = None, True, 0
    if dirty and not development:
        raise ValueError('release packages require a clean source tree; '
                         'use --development for qualification')
    return {'version': version, 'source_commit': commit, 'dirty': dirty,
            'development': development, 'backend_commit': pin, 'source_date_epoch': epoch}


def build_backend(source, pin, work, gtk):
    archive = work / 'backend.tar'
    run(['git', '-C', source, 'archive', '--format=tar', '--prefix=oscmix/',
         '--output=' + str(archive), pin])
    run(['tar', '-xf', archive, '-C', work])
    backend = work / 'oscmix'
    command = ['make', '-C', backend, 'CC=' + os.environ.get('CC', 'cc -std=c11'),
               'GTK=' + ('y' if gtk else 'n'),
               'oscmix', 'alsaseqio', *(['gtk'] if gtk else [])]
    transcript = run(command)
    return backend, transcript


def packaged_hashes(artifact, kind, stage):
    """Hash actual packaged files, including distribution post-processing."""
    if kind == 'rpm':
        algorithm = run(['rpm', '-qp', '--queryformat', '%{FILEDIGESTALGO}', artifact])
        if algorithm != '8':
            raise ValueError('RPM file digests must use SHA256, got algorithm ' + algorithm)
        lines = run(['rpm', '-qp', '--queryformat',
                     '[%{FILENAMES}\t%{FILEDIGESTS}\n]', artifact]).splitlines()
        return {path.lstrip('/'): digest for line in lines
                for path, digest in [line.split('\t')] if digest}
    if kind == 'deb':
        data = subprocess.run(['dpkg-deb', '--fsys-tarfile', str(artifact)],
                              capture_output=True, check=True).stdout
        with tarfile.open(fileobj=io.BytesIO(data)) as archive:
            return {member.name.removeprefix('./'): hashlib.sha256(
                archive.extractfile(member).read()).hexdigest()
                for member in archive.getmembers() if member.isfile()}
    result = {}
    for path in sorted(stage.rglob('*')):
        if path.is_file():
            relative = str(path.relative_to(stage))
            data = subprocess.run(['bsdtar', '-xOf', str(artifact), relative],
                                  capture_output=True, check=True).stdout
            result[relative] = hashlib.sha256(data).hexdigest()
    return result


def deb(root, stage, work, output, record, gtk):
    name = 'oscmix-desk-gtk' if gtk else 'oscmix-desk'
    version = (record['version'] + ('~dev' if record['development'] else '')
               + '-' + str(record['package_revision']))
    architecture = run(['dpkg', '--print-architecture'])
    debian = work / 'debian'
    debian.mkdir()
    (debian / 'control').write_text(
        'Source: oscmix-desk\nMaintainer: oscmix-desk contributors '
        '<noreply@github.com>\nSection: sound\nPriority: optional\n\n'
        'Package: ' + name + '\nArchitecture: any\nDescription: Declarative UCX II mixer state\n')
    binaries = ['oscmix-gtk'] if gtk else ['oscmix', 'alsaseqio']
    dependencies = run(['dpkg-shlibdeps', '-O', *['-e' + str(stage / 'usr/bin' / binary)
                                               for binary in binaries]], cwd=work)
    dependencies = dependencies.removeprefix('shlibs:Depends=')
    control = stage / 'DEBIAN'
    control.mkdir()
    (control / 'control').write_text(
        'Package: ' + name + '\nVersion: ' + version + '\nArchitecture: ' + architecture
        + '\nMaintainer: oscmix-desk contributors <noreply@github.com>\nSection: sound\n'
        'Priority: optional\nPre-Depends: python3 (>= 3.9)\nDepends: ' + dependencies
        + (', oscmix-desk (= ' + version + '), libglib2.0-bin' if gtk else ', adduser')
        + '\nRecommends: systemd, udev\nHomepage: https://github.com/relative23/oscmix-desk\n'
        + ('Description: Optional upstream GTK mixer for oscmix-desk\n'
           ' Uses the matching core backend; installing starts no mixer.\n' if gtk else
           'Description: Declarative mixer state and lifecycle for the Fireface UCX II\n'
           ' Includes the pinned oscmix backend. Review the desk, then activate it\n'
           ' explicitly with oscmix-setup; installing starts no desk.\n'))
    for script_name, action in [('preinst', 'install'), ('prerm', 'remove')]:
        (control / script_name).write_text(guard(root, action, gtk))
        (control / script_name).chmod(0o755)
    (control / 'postinst').write_text('''#!/bin/sh
set -eu
case "$1" in
  configure)
    getent group audio >/dev/null || addgroup --system audio
    py3compile -p oscmix-desk
    if command -v systemd-tmpfiles >/dev/null 2>&1; then
        systemd-tmpfiles --create /usr/lib/tmpfiles.d/oscmix-desk.conf
    fi
    if command -v udevadm >/dev/null 2>&1; then udevadm control --reload-rules || true; fi
    if command -v glib-compile-schemas >/dev/null 2>&1; then
        glib-compile-schemas /usr/share/glib-2.0/schemas
    fi
    /usr/lib/oscmix-desk/package-guard finish
    echo 'Run oscmix-setup as your audio user; no service was started.'
    ;;
esac
''')
    if gtk:
        (control / 'postinst').write_text('''#!/bin/sh
set -eu
if [ "$1" = configure ]; then
    glib-compile-schemas /usr/share/glib-2.0/schemas
    /usr/lib/oscmix-desk/package-guard finish --component gtk
fi
''')
    (control / 'postinst').chmod(0o755)
    # postrm runs after its installed helper has gone. Keep its tiny
    # completion operation self-contained, and preserve user config.
    (control / 'postrm').write_text('''#!/bin/sh
set -eu
case "$1" in
  remove|purge)
    if command -v glib-compile-schemas >/dev/null 2>&1; then
        glib-compile-schemas /usr/share/glib-2.0/schemas
    fi
    /usr/bin/python3 -I <<'PYTHON_GUARD'
''' + guard(root, 'finish', gtk) + '''
PYTHON_GUARD
    ;;
esac
''')
    (control / 'postrm').chmod(0o755)
    record['package_version'] = version
    record['architecture'] = architecture
    release = dict(line.split('=', 1) for line in record['os_release'].splitlines() if '=' in line)
    target = release.get('ID', '').strip('"') + release.get('VERSION_ID', '').strip('"')
    if not re.fullmatch(r'[A-Za-z0-9.]+', target):
        raise ValueError('cannot name a distribution-specific artifact from os-release')
    artifact = output / (name + '_' + version + '_' + architecture + '_' + target + '.deb')
    environment = dict(os.environ, SOURCE_DATE_EPOCH=str(record['source_date_epoch']))
    subprocess.run(['dpkg-deb', '--build', '--root-owner-group', '--uniform-compression',
                    str(stage), str(artifact)], check=True, env=environment)
    return artifact


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--format', choices=['deb', 'rpm', 'arch'], required=True)
    parser.add_argument('--backend-source', type=Path, required=True,
                        help='local oscmix git checkout containing the pinned commit')
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--development', action='store_true')
    parser.add_argument('--with-gtk', action='store_true',
                        help='build the core and a separate, exactly dependent GTK companion')
    parser.add_argument('--package-revision', type=int, default=1)
    parser.add_argument('--build-dir', type=Path,
                        help='unused absolute scratch path, removed afterward; '
                             'use the same path in clean Arch build environments')
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    record = metadata(root, args.development)
    if args.package_revision < 1:
        parser.error('--package-revision must be positive')
    record['package_revision'] = args.package_revision
    args.output.mkdir(parents=True, exist_ok=True)
    with workspace(args.build_dir) as work:
        if not args.development:
            # A release payload contains only this commit's files. An
            # ignored local module must not enter a signed native package.
            archive = work / 'project.tar'
            run(['git', '-C', root, 'archive', '--format=tar', '--prefix=project/',
                 '--output=' + str(archive), record['source_commit']])
            run(['tar', '-xf', archive, '-C', work])
            root = work / 'project'
        backend, transcript = build_backend(
            args.backend_source.resolve(), record['backend_commit'], work, args.with_gtk)
        record['format'] = args.format
        record['build_flags'] = {key: os.environ.get(key) for key in
                                 ('CC', 'CFLAGS', 'CPPFLAGS', 'LDFLAGS')}
        record['build_flags']['CC'] = os.environ.get('CC', 'cc -std=c11')
        record['compiler'] = run([*shlex.split(record['build_flags']['CC']),
                                  '--version']).splitlines()[0]
        record['os_release'] = Path('/etc/os-release').read_text()
        for gtk in ([False, True] if args.with_gtk else [False]):
            package(root, backend, work, args.output.resolve(), record, transcript, gtk)


def package(root, backend, workspace_root, output, metadata_record, transcript, gtk):
    record = dict(metadata_record, component='gtk' if gtk else 'core',
                  package_name='oscmix-desk-gtk' if gtk else 'oscmix-desk')
    work = workspace_root / record['component']
    work.mkdir()
    stage = work / 'stage'
    run(['bash', root / 'scripts/stage-install.sh', '--destdir', stage,
         '--backend', backend, *(['--gtk-only'] if gtk else [])], cwd=root)
    installed_metadata(stage, record)
    record['staged_payload'] = {
        str(path.relative_to(stage)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(stage.rglob('*')) if path.is_file()}
    for path in stage.rglob('*'):
        os.utime(path, (record['source_date_epoch'], record['source_date_epoch']))
    build = {'deb': deb, 'rpm': rpm, 'arch': arch}[record['format']]
    artifact = build(root, stage, work, output, record, gtk)
    record['payload'] = packaged_hashes(artifact, record['format'], stage)
    artifact.with_suffix(artifact.suffix + '.build.log').write_text(transcript + '\n')
    record['artifact'] = artifact.name
    record['sha256'] = hashlib.sha256(artifact.read_bytes()).hexdigest()
    manifest = artifact.with_suffix(artifact.suffix + '.json')
    manifest.write_text(json.dumps(record, indent=2) + '\n')
    print('Built ' + str(artifact) + '; provenance: ' + str(manifest))


if __name__ == '__main__':
    try:
        main()
    except subprocess.CalledProcessError as error:
        sys.stderr.write(error.stderr or str(error))
        raise SystemExit(1) from error
    except (OSError, ValueError) as error:
        sys.stderr.write('cannot build package: ' + str(error) + '\n')
        raise SystemExit(1) from error
