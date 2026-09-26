#!/usr/bin/python3 -I
"""Install/remove a scoped repository subscription without touching the mixer.

The package embeds this same stdlib-only file in its pre-install/remove hooks,
so an interrupted replacement does not depend on partially unpacked helpers.
"""

import argparse
import fcntl
import hashlib
import json
import os
import re
import stat
import subprocess
import sys
import tempfile
import time
from pathlib import Path

CLIENT = Path('/usr/lib/oscmix-desk-repository')
DATA = Path('/usr/share/oscmix-desk-repository')
STATE = Path('/var/lib/oscmix-desk-repository')
CONFIG = Path('/etc/oscmix-desk/repository.json')
CERTIFICATE = Path('/usr/share/keyrings/oscmix-desk-repository.asc')
FENCE = STATE / 'update.json'
PENDING_KEY = STATE / 'native-key.json'
REGISTRATION = STATE / 'state.json'
ENVIRONMENT = {'PATH': '/usr/bin:/bin:/usr/sbin:/sbin', 'LC_ALL': 'C'}
SOURCES = {'apt': Path('/etc/apt/sources.list.d/oscmix-desk.sources'),
           'dnf': Path('/etc/yum.repos.d/oscmix-desk.repo'),
           'zypp': Path('/etc/zypp/repos.d/oscmix-desk.repo')}
HOOKS = {'apt': Path('/etc/apt/apt.conf.d/90oscmix-repository'),
         'dnf': Path('/etc/dnf/libdnf5-plugins/oscmix-repository.conf')}


def sha(data):
    return hashlib.sha256(data).hexdigest()


def root_path(path, *, missing=False):
    for current in (path, *path.parents):
        try:
            info = current.lstat()
        except FileNotFoundError:
            if missing:
                continue
            raise
        if info.st_uid != 0 or info.st_mode & 0o022 or stat.S_ISLNK(info.st_mode):
            raise ValueError('untrusted repository installation path: ' + str(current))
    return path


def atomic(path, data):
    root_path(path, missing=True)
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=path.parent, prefix='.' + path.name,
                                     delete=False) as temporary:
        pending = Path(temporary.name)
        try:
            temporary.write(data)
            temporary.flush()
            os.fchmod(temporary.fileno(), 0o644)
            os.fsync(temporary.fileno())
            pending.replace(path)
            fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
        finally:
            pending.unlink(missing_ok=True)


def write_json(path, record):
    atomic(path, (json.dumps(record, indent=2) + '\n').encode())


def registration():
    try:
        record = json.loads(root_path(REGISTRATION).read_text())
    except FileNotFoundError:
        return dict(schema=1, enabled=False, files={})
    if record.get('schema') != 1:
        raise ValueError('unknown repository registration version')
    return record


def remove_definitions(record):
    # Remove the subscription before its verification hooks. An interrupted
    # package replacement must never leave an enabled, unprotected channel.
    for path in (*SOURCES.values(), *HOOKS.values()):
        root_path(path, missing=True)
        if not path.exists():
            continue
        expected = record['files'].get(str(path))
        if expected is None:
            raise ValueError('refusing unregistered repository file: ' + str(path))
        current = sha(path.read_bytes())
        if current != expected:
            saved = path.with_name(path.name + '.saved-' + current[:16])
            if saved.exists():
                raise ValueError('repository configuration backup already exists: ' + str(saved))
            path.rename(saved)
            record['enabled'] = False
            print('Preserved modified definition as ' + str(saved), file=sys.stderr)
        else:
            path.unlink()


def begin(record, *, removing=False):
    if not FENCE.exists():
        write_json(FENCE, dict(schema=1, enabled=record['enabled']))
    if removing:
        record['enabled'] = False
    remove_definitions(record)
    write_json(REGISTRATION, record)


def run(command, *, home=None):
    prefix = (['gpg', '--batch', '--no-options', '--no-autostart', '--homedir', str(home)]
              if home else [])
    return subprocess.run(prefix + [str(x) for x in command], env=ENVIRONMENT,
                          check=True, capture_output=True, text=True, timeout=30).stdout


def certificate_identity(certificate, home):
    data = certificate.read_bytes()
    if (len(data) > 1024 * 1024
            or not data.startswith(b'-----BEGIN PGP PUBLIC KEY BLOCK-----\n')):
        raise ValueError('expected a public ASCII-armored repository certificate')
    lines = run(['--with-colons', '--show-keys', certificate], home=home).splitlines()
    if any(line.startswith(('sec:', 'ssb:')) for line in lines):
        raise ValueError('repository trust input contains private key packets')
    primaries = []
    previous = ''
    for line in lines:
        fields = line.split(':')
        if fields[0] == 'fpr' and previous == 'pub':
            primaries.append(fields[9])
        if fields[0] != 'fpr':
            previous = fields[0]
    if len(primaries) != 1:
        raise ValueError('repository trust input must contain one primary certificate')
    return primaries[0]


def update_certificate(record, candidate, expected):
    if not re.fullmatch(r'[0-9A-F]{40}', expected):
        raise ValueError('a full uppercase primary fingerprint is required')
    with tempfile.TemporaryDirectory(prefix='oscmix-repository-key-') as directory:
        home = Path(directory)
        if certificate_identity(candidate, home) != expected:
            raise ValueError('repository certificate differs from the expected '
                             'primary fingerprint')
        if CERTIFICATE.exists():
            root_path(CERTIFICATE)
            existing = certificate_identity(CERTIFICATE, home)
            if existing == expected:
                # Merge, never discard prior revocations or earlier subkeys
                # when an older helper package is installed for recovery.
                run(['--import', CERTIFICATE], home=home)
            else:
                previous = STATE / 'keys' / (existing + '.asc')
                atomic(previous, CERTIFICATE.read_bytes())
        run(['--import', candidate], home=home)
        exported = run(['--armor', '--export', expected], home=home).encode()
        if not exported.startswith(b'-----BEGIN PGP PUBLIC KEY BLOCK-----\n'):
            raise ValueError('could not export the merged public certificate')
    atomic(CERTIFICATE, exported)
    record['primary'] = expected
    record['certificate_sha256'] = sha(exported)
    write_json(CONFIG, dict(schema=1, primary=expected))


def replace_rpm_certificate(record):
    primary = record['primary']
    if record['manager'] == 'dnf':
        identifiers = [line.split()[0] for line in run(['rpmkeys', '--list']).splitlines()
                       if line.split() and line.split()[0].upper() == primary]
    else:
        identifiers = []
        # RPM 4.20's deletion selector is keyid-creation, not a full fingerprint.
        # Check the complete certificate before deleting an exact match.
        for line in run(['rpmkeys', '--list']).splitlines():
            identifier = line.split(':', 1)[0]
            if not identifier.startswith(primary[-8:].lower() + '-'):
                continue
            with tempfile.TemporaryDirectory(prefix='oscmix-rpm-key-') as directory:
                home = Path(directory)
                public = home / 'installed.asc'
                public.write_text(run(['rpm', '-q', 'gpg-pubkey-' + identifier,
                                       '--queryformat', '%{DESCRIPTION}']))
                if certificate_identity(public, home) != primary:
                    raise ValueError('RPM short key ID conflicts with another primary fingerprint')
            identifiers.append(identifier)
    for identifier in identifiers:
        run(['rpmkeys', '--delete', identifier])
    run(['rpmkeys', '--import', CERTIFICATE])


def clear_apt_metadata(record):
    # Ask the installed APT binding for its configured lists directory and
    # exact URI escaping. Do not guess a cache path or clear other sources.
    query = ('import apt_pkg,json,pwd; apt_pkg.init_config(); '
             'print(json.dumps([apt_pkg.config.find_dir("Dir::State::lists"),'
             'apt_pkg.uri_to_filename(' + repr(record['url'] + 'dists/stable/') + '),'
             'pwd.getpwnam(apt_pkg.config.find("APT::Sandbox::User") or "_apt").pw_uid]))')
    directory, prefix, apt_uid = json.loads(run(['/usr/bin/python3', '-I', '-c', query]))
    directory = root_path(Path(directory), missing=True)
    directory.mkdir(parents=True, exist_ok=True)
    # An already running apt update may have authenticated the old certificate
    # before maintenance began. Wait for its native list lock, then remove its
    # result too. The lock is different from dpkg's transaction/frontend locks.
    root_path(directory / 'lock', missing=True)
    with (directory / 'lock').open('a') as lock:
        deadline = time.monotonic() + 10
        while True:
            try:
                fcntl.lockf(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise ValueError('APT metadata is busy; repository maintenance '
                                     'remains active') from None
                time.sleep(.05)
        remove_apt_lists(directory, prefix, apt_uid)


def remove_apt_lists(directory, prefix, apt_uid):
    for parent in (directory, directory / 'partial'):
        if not parent.exists():
            continue
        info = parent.lstat()
        if (info.st_uid not in (0, apt_uid) or info.st_mode & 0o022
                or not stat.S_ISDIR(info.st_mode)):
            raise ValueError('untrusted APT cache directory: ' + str(parent))
        for path in parent.iterdir():
            if path.name.startswith(prefix):
                if not path.is_file() or path.is_symlink():
                    raise ValueError('unexpected project APT cache entry: ' + str(path))
                path.unlink()


def installed():
    record = json.loads(root_path(DATA / 'package.json').read_text())
    if record.get('component') != 'repository':
        raise ValueError('repository client package provenance is missing')
    for name, digest in record['owned_payload'].items():
        path = Path('/') / name
        if not path.is_absolute() or '..' in path.parts:
            raise ValueError('invalid repository client payload path')
        if sha(root_path(path).read_bytes()) != digest:
            raise ValueError('repository client installation is incomplete: ' + str(path))
    for name, target in record['owned_links'].items():
        path = Path('/') / name
        root_path(path.parent)
        if path.lstat().st_uid != 0 or not path.is_symlink() or os.readlink(path) != target:
            raise ValueError('repository client link is incomplete: ' + str(path))
    return record


def define(record, path, content):
    data = content.encode()
    if path.exists() and str(path) not in record['files']:
        raise ValueError('refusing to replace unregistered repository configuration: ' + str(path))
    # Journal ownership before publishing the file so an interrupted first
    # subscription can be resumed without treating its own file as foreign.
    record['files'][str(path)] = sha(data)
    write_json(REGISTRATION, record)
    atomic(path, data)


def finish(record, *, candidate=None, fingerprint=None, rpm_trust=False):
    package = installed()
    if record.get('target', package['target']) != package['target']:
        raise ValueError('repository client package belongs to a different target')
    record.update(target=package['target'], manager=package['manager'], url=package['url'])
    if candidate is None:
        candidate = DATA / 'archive-key.asc'
        fingerprint = package['primary']
        if record.get('primary', fingerprint) != fingerprint:
            # An explicit primary-key change survives helper-package rollback.
            candidate, fingerprint = CERTIFICATE, record['primary']
    update_certificate(record, candidate, fingerprint)
    if record['manager'] == 'apt':
        clear_apt_metadata(record)
    elif rpm_trust:
        # Package scriptlets execute while their parent's RPM transaction owns
        # the database lock. Explicit subscription/key commands run outside it.
        replace_rpm_certificate(record)
        record['rpm_certificate_sha256'] = record['certificate_sha256']
        PENDING_KEY.unlink(missing_ok=True)
    elif (record['enabled'] and record['manager'] == 'zypp'
          and record.get('rpm_certificate_sha256') != record['certificate_sha256']):
        # Complete the import at libzypp's transaction end, outside rpm's
        # scriptlets. Its current process already holds a keyring snapshot;
        # the next repository operation then sees the updated certificate.
        write_json(PENDING_KEY, dict(schema=1, certificate_sha256=record['certificate_sha256']))
    if not record['enabled']:
        PENDING_KEY.unlink(missing_ok=True)
    # A fresh certificate gets a new native RPM cache/keyring identity. No
    # unrelated key or package cache is deleted, including during recovery.
    record['repo_id'] = 'oscmix-desk-' + record['certificate_sha256'][:16]
    if record['enabled']:
        if record['manager'] == 'apt':
            define(record, HOOKS['apt'],
                   'Dir::Bin::Methods::gpgv "' + str(CLIENT / 'apt-gpgv') + '";\n'
                   'Dir::Bin::Methods::sqv "' + str(CLIENT / 'apt-sqv') + '";\n')
            content = ('Types: deb\nURIs: ' + record['url']
                       + '\nSuites: stable\nComponents: main\n'
                       'Architectures: amd64\nSigned-By: ' + str(CERTIFICATE) + '\n')
        else:
            if record['manager'] == 'dnf':
                define(record, HOOKS['dnf'], '[main]\nname=oscmix-repository\nenabled=yes\n')
            content = ('[' + record['repo_id'] + ']\nname=oscmix-desk\nbaseurl=' + record['url']
                       + '\nenabled=1\nautorefresh=1\ngpgcheck=1\nrepo_gpgcheck=1\n'
                       'pkg_gpgcheck=1\nskip_if_unavailable=0\ngpgkey=file://' + str(CERTIFICATE)
                       + '\n' + ('repo_sigcheck_plugin=oscmix-desk\n'
                                  if record['manager'] == 'zypp' else ''))
        define(record, SOURCES[record['manager']], content)
    write_json(REGISTRATION, record)
    FENCE.unlink(missing_ok=True)


def sync_native_key(record):
    if FENCE.exists():
        raise ValueError('repository installation maintenance is incomplete')
    pending = json.loads(root_path(PENDING_KEY).read_text())
    installed()
    if (record['manager'] != 'zypp' or pending.get('schema') != 1
            or pending['certificate_sha256'] != record['certificate_sha256']
            or sha(root_path(CERTIFICATE).read_bytes()) != record['certificate_sha256']):
        raise ValueError('pending native repository key differs from the installed certificate')
    replace_rpm_certificate(record)
    record['rpm_certificate_sha256'] = record['certificate_sha256']
    write_json(REGISTRATION, record)
    PENDING_KEY.unlink()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['status', 'enable', 'disable', 'refresh-key',
                                          'package-begin', 'package-finish', 'package-remove',
                                          'native-key-sync'])
    parser.add_argument('--certificate', type=Path)
    parser.add_argument('--fingerprint')
    args = parser.parse_args()
    if args.action == 'status':
        result = registration()
        result['maintenance'] = FENCE.exists()
        result['native_key_pending'] = PENDING_KEY.exists()
        print(json.dumps(result, indent=2))
        return 0
    if os.getuid() != 0:
        parser.error('repository subscription changes require the administrator')
    if args.action == 'refresh-key' and (not args.certificate or not args.fingerprint):
        parser.error('refresh-key requires a public --certificate and full --fingerprint')
    try:
        root_path(STATE, missing=True).mkdir(parents=True, exist_ok=True)
        root_path(STATE / 'setup.lock', missing=True)
        with (STATE / 'setup.lock').open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            record = registration()
            if args.action == 'package-begin':
                begin(record)
            elif args.action == 'native-key-sync':
                sync_native_key(record)
            elif args.action in ('disable', 'package-remove'):
                begin(record, removing=True)
                # Disabled sources are safe without a working verifier. A
                # later enable still validates every installed payload file.
                FENCE.unlink(missing_ok=True)
                PENDING_KEY.unlink(missing_ok=True)
            else:
                begin(record)
                if args.action == 'enable':
                    record['enabled'] = True
                    write_json(REGISTRATION, record)
                finish(record, candidate=args.certificate, fingerprint=args.fingerprint,
                       rpm_trust=args.action in ('enable', 'refresh-key'))
    except (OSError, ValueError, KeyError, subprocess.SubprocessError) as exc:
        print('oscmix-repository: ' + str(exc), file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
