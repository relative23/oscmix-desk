"""Native bootstrap package for the opt-in project repository subscription."""

import hashlib
import json
import os
import shlex
import shutil
import subprocess
from pathlib import Path

from package_formats import archive, run, target_name


def stage(root, destination, record):
    source = root / 'packaging/repository'
    client = destination / 'usr/lib/oscmix-desk-repository'
    data = destination / 'usr/share/oscmix-desk-repository'
    client.mkdir(parents=True)
    data.mkdir(parents=True)
    for name in ('verify.py', 'configure.py', 'apt-method.py', 'zypp-sigcheck.py'):
        shutil.copyfile(source / name, client / name)
        (client / name).chmod(0o755)
    command = destination / 'usr/bin/oscmix-repository'
    command.parent.mkdir(parents=True)
    command.symlink_to('../lib/oscmix-desk-repository/configure.py')
    transcript = ''
    if record['manager'] == 'apt':
        for name in ('gpgv', 'sqv'):
            (client / ('apt-' + name)).symlink_to('apt-method.py')
    elif record['manager'] == 'zypp':
        link = destination / 'usr/lib/zypp/plugins/sigcheck/oscmix-desk'
        link.parent.mkdir(parents=True)
        link.symlink_to('../../../oscmix-desk-repository/zypp-sigcheck.py')
        link = destination / 'usr/lib/zypp/plugins/commit/oscmix-desk-key-update'
        link.parent.mkdir(parents=True)
        link.symlink_to('../../../oscmix-desk-repository/zypp-sigcheck.py')
    else:
        output = destination / 'usr/lib64/libdnf5/plugins/oscmix-repository.so'
        output.parent.mkdir(parents=True)
        compiler = shlex.split(os.environ.get('CXX', 'g++'))
        flags = ['-std=c++20', '-Wall', '-Wextra', '-Werror', '-O2', '-fPIC', '-shared', '-s']
        flags += shlex.split(run(['pkg-config', '--cflags', '--libs', 'libdnf5']))
        record['compiler'] = run([*compiler, '--version']).splitlines()[0]
        record['libdnf5_build_version'] = run(['pkg-config', '--modversion', 'libdnf5'])
        command_line = [*compiler, str(source / 'dnf.cpp'), '-o', str(output), *flags]
        transcript = shlex.join(command_line) + '\n' + run(command_line)
    public = root / 'packaging/keys/oscmix-desk-archive.asc'
    key = json.loads((root / 'packaging/keys/archive-key.json').read_text())
    if hashlib.sha256(public.read_bytes()).hexdigest() != key['public_armor_sha256']:
        raise ValueError('project certificate differs from its recorded public identity')
    record['primary'] = key['primary_fingerprint']
    record['url'] = key['endpoint'] + record['target'] + '/'
    shutil.copyfile(public, data / 'archive-key.asc')
    license_file = destination / 'usr/share/licenses/oscmix-desk-repository/LICENSE'
    license_file.parent.mkdir(parents=True)
    shutil.copyfile(root / 'LICENSE', license_file)
    record['owned_payload'] = {
        str(path.relative_to(destination)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(destination.rglob('*')) if path.is_file() and not path.is_symlink()}
    record['owned_links'] = {str(path.relative_to(destination)): os.readlink(path)
                             for path in sorted(destination.rglob('*')) if path.is_symlink()}
    (data / 'package.json').write_text(json.dumps(record, indent=2) + '\n')
    for path in destination.rglob('*'):
        os.utime(path, (record['source_date_epoch'], record['source_date_epoch']),
                 follow_symlinks=False)
    return transcript


def guard(root, action):
    source = (root / 'packaging/repository/configure.py').read_text()
    return source.replace('args = parser.parse_args()',
                          'args = parser.parse_args(' + repr([action]) + ')')


def deb(root, payload, _work, output, record):
    version = (record['version'] + ('~dev' if record['development'] else '') +
               '-' + str(record['package_revision']))
    record['package_version'] = version
    record['architecture'] = run(['dpkg', '--print-architecture'])
    control = payload / 'DEBIAN'
    control.mkdir()
    (control / 'control').write_text(
        'Package: oscmix-desk-repository\nVersion: ' + version + '\nArchitecture: '
        + record['architecture'] + '\nMaintainer: oscmix-desk contributors <noreply@github.com>\n'
        'Section: admin\nPriority: optional\nPre-Depends: python3 (>= 3.9)\n'
        'Depends: apt, python3-apt, gnupg, gpgv\n'
        'Homepage: https://github.com/relative23/oscmix-desk\n'
        'Description: Scoped signed repository integration for oscmix-desk\n'
        ' Installs verifiers without subscribing or starting a mixer.\n'
        ' Enable the package source explicitly with oscmix-repository enable.\n')
    for name, action in (('preinst', 'package-begin'), ('prerm', 'package-remove')):
        content = guard(root, action)
        # prerm also runs on upgrades; preinst owns the resumable update
        # journal, while removal alone explicitly disables the subscription.
        if name == 'prerm':
            content = ('#!/bin/sh\nset -eu\nif [ "$1" = remove ]; then\n'
                       "/usr/bin/python3 -I <<'PYTHON_REPOSITORY'\n" + content +
                       '\nPYTHON_REPOSITORY\nfi\n')
        (control / name).write_text(content)
        (control / name).chmod(0o755)
    (control / 'postinst').write_text(
        '#!/bin/sh\nset -eu\nif [ "$1" = configure ]; then\n'
        ' /usr/bin/python3 -I /usr/lib/oscmix-desk-repository/configure.py package-finish\nfi\n')
    (control / 'postinst').chmod(0o755)
    artifact = output / ('oscmix-desk-repository_' + version + '_' + record['architecture']
                          + '_' + target_name(record) + '.deb')
    subprocess.run(['dpkg-deb', '--build', '--root-owner-group', '--uniform-compression',
                    str(payload), str(artifact)], check=True,
                   env=dict(os.environ, SOURCE_DATE_EPOCH=str(record['source_date_epoch'])))
    return artifact


def rpm(root, payload, work, output, record):
    top = work / 'rpm'
    for directory in ('SOURCES', 'SPECS', 'BUILD', 'RPMS', 'SRPMS', 'BUILDROOT'):
        (top / directory).mkdir(parents=True)
    archive(payload, top / 'SOURCES/payload.tar', record['source_date_epoch'])
    release = ('0.dev.' if record['development'] else '') + str(record['package_revision'])
    record['package_version'] = record['version'] + '-' + release
    dependency = ('gnupg2\nRequires: libdnf5 >= ' + record['libdnf5_build_version']
                   if record['manager'] == 'dnf' else 'gpg2\nRequires: libzypp >= 17.38.15')
    spec = '''Name: oscmix-desk-repository
Version: @VERSION@
Release: @RELEASE@
Summary: Scoped signed repository integration for oscmix-desk
License: MIT
URL: https://github.com/relative23/oscmix-desk
Source0: payload.tar
Requires: python3 >= 3.9
Requires: @DEPENDENCY@
Requires(pre): /usr/bin/python3
%global debug_package %{nil}
%global __brp_python_bytecompile %{nil}
%global __brp_strip %{nil}
%global __brp_strip_comment_note %{nil}
%global _build_id_links none

%description
Installs native verifiers without subscribing or starting a mixer.
Enable the package source explicitly with oscmix-repository enable.

%prep
%build
%install
mkdir -p %{buildroot}
tar -xf %{SOURCE0} -C %{buildroot}

%pre
/usr/bin/python3 -I <<'PYTHON_REPOSITORY'
@BEGIN@
PYTHON_REPOSITORY

%preun
if [ "$1" -eq 0 ]; then
/usr/bin/python3 -I <<'PYTHON_REPOSITORY'
@REMOVE@
PYTHON_REPOSITORY
fi

%posttrans
/usr/bin/python3 -I /usr/lib/oscmix-desk-repository/configure.py package-finish

%files
%defattr(-,root,root)
/usr/bin/oscmix-repository
/usr/lib/oscmix-desk-repository
/usr/share/oscmix-desk-repository
@PLUGIN@
%license /usr/share/licenses/oscmix-desk-repository
'''
    for key, value in dict(VERSION=record['version'], RELEASE=release, DEPENDENCY=dependency,
                            BEGIN=guard(root, 'package-begin').replace('%', '%%'),
                            REMOVE=guard(root, 'package-remove').replace('%', '%%'),
                            PLUGIN=('/usr/lib64/libdnf5/plugins/oscmix-repository.so'
                                    if record['manager'] == 'dnf' else
                                    '/usr/lib/zypp/plugins/sigcheck/oscmix-desk\n'
                                    '/usr/lib/zypp/plugins/commit/oscmix-desk-key-update')).items():
        spec = spec.replace('@' + key + '@', value)
    spec_file = top / 'SPECS/repository.spec'
    spec_file.write_text(spec)
    subprocess.run(['rpmbuild', '-bb', '--define', '_topdir ' + str(top),
                    '--define', '_buildhost reproducible.oscmix-desk',
                    '--define', 'use_source_date_epoch_as_buildtime 1',
                    '--define', 'clamp_mtime_to_source_date_epoch 1', str(spec_file)],
                   check=True, capture_output=True, text=True,
                   env=dict(os.environ, SOURCE_DATE_EPOCH=str(record['source_date_epoch'])))
    packages = list((top / 'RPMS').rglob('*.rpm'))
    if len(packages) != 1:
        raise ValueError('expected one repository integration RPM')
    record['architecture'] = run(['rpm', '-qp', '--queryformat', '%{ARCH}', packages[0]])
    artifact = output / (packages[0].stem + '_' + target_name(record) + '.rpm')
    shutil.copyfile(packages[0], artifact)
    artifact.with_suffix('.spec').write_text(spec)
    return artifact


def build(root, work, output, metadata, kind, packaged_hashes):
    if kind not in ('deb', 'rpm'):
        raise ValueError('repository bootstrap is only available for APT/RPM targets')
    record = {key: value for key, value in metadata.items() if not key.startswith('backend_')}
    record.update(component='repository', package_name='oscmix-desk-repository', format=kind,
                  os_release=Path('/etc/os-release').read_text())
    platform = target_name(record)
    targets = {'debian13': ('debian13', 'apt'), 'ubuntu24.04': ('ubuntu2404', 'apt'),
               'ubuntu26.04': ('ubuntu2604', 'apt'), 'fedora44': ('fedora44', 'dnf'),
               'opensuse-leap16.0': ('opensuse16', 'zypp')}
    record['target'], record['manager'] = targets[platform]
    payload = work / 'client-stage'
    transcript = stage(root, payload, record)
    artifact = (deb if kind == 'deb' else rpm)(root, payload, work, output, record)
    record['payload'] = packaged_hashes(artifact, kind, payload)
    for name, digest in record['owned_payload'].items():
        if record['payload'][name] != digest:
            raise ValueError('native postprocessing changed a guarded client file: ' + name)
    record.update(artifact=artifact.name, sha256=hashlib.sha256(artifact.read_bytes()).hexdigest())
    artifact.with_suffix(artifact.suffix + '.json').write_text(json.dumps(record, indent=2) + '\n')
    artifact.with_suffix(artifact.suffix + '.build.log').write_text(transcript + '\n')
    return artifact
