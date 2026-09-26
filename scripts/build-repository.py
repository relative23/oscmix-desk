#!/usr/bin/env python3
"""Stage one signed APT/RPM channel without publishing or changing a live repository.

Requires GnuPG, apt-ftparchive or createrepo_c, and RPM signing tools. Inputs
are native build artifacts and their manifests, authenticated separately by
the release workflow. Private keys stay in the explicitly selected keyring.
"""

import argparse
import datetime
import gzip
import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import tempfile
from email.utils import format_datetime
from pathlib import Path, PurePosixPath

TARGETS = {
    'debian13': ('debian13', 'deb', 'amd64'),
    'ubuntu2404': ('ubuntu24.04', 'deb', 'amd64'),
    'ubuntu2604': ('ubuntu26.04', 'deb', 'amd64'),
    'fedora44': ('fedora44', 'rpm', 'x86_64'),
    'opensuse16': ('opensuse-leap16.0', 'rpm', 'x86_64'),
}
MANIFEST = 'repository.json'
PACKAGE_NAMES = {'core': 'oscmix-desk', 'gtk': 'oscmix-desk-gtk',
                 'repository': 'oscmix-desk-repository'}


def run(command, **kwargs):
    return subprocess.run([str(part) for part in command], check=True,
                          capture_output=True, text=True, timeout=120, **kwargs).stdout


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def relative_file(root, name):
    """Manifest entries cannot copy symlinks, parents or absolute host files."""
    path = PurePosixPath(name)
    if not name or path.is_absolute() or '..' in path.parts or str(path) != name:
        raise ValueError('invalid repository path: ' + name)
    file = root / path
    if not file.is_file() or any(p.is_symlink() for p in (file, *file.parents)):
        raise ValueError('not a regular repository file: ' + name)
    return file


class Signer:
    def __init__(self, args, temporary):
        self.args = args
        armor = args.public_key.read_text()
        if (not armor.startswith('-----BEGIN PGP PUBLIC KEY BLOCK-----\n')
                or 'PRIVATE KEY' in armor):
            raise ValueError('--public-key must contain only a public OpenPGP certificate')
        self.keyring = temporary / 'public.gpg'
        self.verify_home = temporary / 'verify'
        self.verify_home.mkdir(mode=0o700)
        self.environment = dict(os.environ, GNUPGHOME=str(args.gnupghome))
        run(['gpg', '--batch', '--no-options', '--homedir', self.verify_home,
             '--output', self.keyring, '--dearmor', args.public_key])
        listing = run(['gpg', '--batch', '--no-options', '--homedir', self.verify_home,
                       '--with-colons', '--show-keys', args.public_key])
        if any(line.startswith(('sec:', 'ssb:')) for line in listing.splitlines()):
            raise ValueError('public certificate contains private key packets')
        fingerprints = {line.split(':')[9] for line in listing.splitlines()
                        if line.startswith('fpr:')}
        if args.signing_key not in fingerprints:
            raise ValueError('signing fingerprint is absent from the public certificate')
        self.rpm_db = temporary / 'rpmdb'

    def sign(self, source, destination, clearsign=False):
        run(['gpg', '--batch', '--no-options', '--yes', '--homedir', self.args.gnupghome,
             '--pinentry-mode', 'loopback', '--passphrase-file', self.args.passphrase_file,
             '--digest-algo', 'SHA256', '--local-user', self.args.signing_key + '!',
             '--armor', '--output', destination,
             '--clearsign' if clearsign else '--detach-sign', source])
        self.verify(destination, None if clearsign else source)

    def verify(self, signature, source=None):
        status = run(['gpgv', '--homedir', self.verify_home, '--keyring', self.keyring,
                      '--status-fd', '1', signature, *([source] if source else [])])
        codes = [line.split()[1] for line in status.splitlines()
                 if line.startswith('[GNUPG:] ') and len(line.split()) >= 2]
        # gpgv can return success and VALIDSIG for expired or revoked keys.
        # GOODSIG is replaced by EXPKEYSIG/REVKEYSIG in those cases. A plain
        # KEYEXPIRED may refer to another, unused subkey in the certificate.
        if (codes.count('VALIDSIG') != 1 or codes.count('GOODSIG') != 1
                or any(code in codes for code in ('EXPSIG', 'EXPKEYSIG', 'REVKEYSIG',
                                                 'BADSIG', 'ERRSIG', 'NO_PUBKEY'))):
            raise ValueError('missing current, unrevoked signature: ' + str(signature))

    def rpm(self, package):
        # A temporary RPM database imports only the selected public certificate.
        # The signing operation must preserve the immutable header and payload.
        if not self.rpm_db.exists():
            self.rpm_db.mkdir()
            run(['rpm', '--dbpath', self.rpm_db, '--import', self.args.public_key])
        query = ['rpm', '-qp', '--queryformat', '%{SHA256HEADER}\n%{PAYLOADDIGEST}\n', package]
        before = run(query)
        if not re.fullmatch(r'[0-9a-f]{64}\n[0-9a-f]{64,128}\n', before):
            raise ValueError('RPM lacks a strong immutable-header or payload digest')
        passphrase = str(self.args.passphrase_file)
        if '%' in passphrase or '\n' in passphrase:
            raise ValueError('passphrase path cannot contain RPM macro syntax or newlines')
        run(['rpmsign', '--define', '_gpg_name ' + self.args.signing_key + '!',
             '--define', '_gpg_digest_algo sha256', '--define',
             '_gpg_sign_cmd_extra_args --batch --pinentry-mode loopback --passphrase-file '
             + shlex.quote(passphrase), '--addsign', package], env=self.environment)
        if run(query) != before:
            raise ValueError('RPM signing changed the immutable header or payload')
        checked = run(['rpmkeys', '--dbpath', self.rpm_db, '--checksig', '--verbose', package])
        if not re.search(r'Signature.*: OK', checked, re.IGNORECASE):
            raise ValueError('RPM has no verified package signature')


def read_packages(directory, target, development, commit):
    os_target, kind, architecture = TARGETS[target]
    manifests = sorted(directory.glob('*.' + kind + '.json'))
    if not 1 <= len(manifests) <= 3:
        raise ValueError('expected one manifest per native package component in ' + str(directory))
    packages = []
    for manifest in manifests:
        record = json.loads(manifest.read_text())
        artifact = relative_file(directory, record['artifact'])
        if artifact.name != record['artifact'] or sha(artifact) != record['sha256']:
            raise ValueError('artifact name or digest differs from its build manifest')
        release = dict(line.split('=', 1) for line in record['os_release'].splitlines()
                       if '=' in line)
        platform = release['ID'].strip('"') + release['VERSION_ID'].strip('"')
        if (platform, record['format'], record['architecture']) != (os_target, kind, architecture):
            raise ValueError('package does not match selected distribution and architecture')
        if record['source_commit'] != commit:
            raise ValueError('package source differs from the expected commit')
        if not development and (record['development'] or record['dirty']):
            raise ValueError('development or dirty package cannot enter a release repository')
        if (record['component'] not in PACKAGE_NAMES
                or record['package_name'] != PACKAGE_NAMES[record['component']]):
            raise ValueError('unexpected package component')
        if kind == 'deb':
            identity = run(['dpkg-deb', '-f', artifact, 'Package', 'Version', 'Architecture'])
            fields = dict(line.split(': ', 1) for line in identity.strip().splitlines())
            if fields != dict(Package=record['package_name'], Version=record['package_version'],
                              Architecture=architecture):
                raise ValueError('DEB header does not match build manifest')
            dependency = run(['dpkg-deb', '-f', artifact, 'Depends'])
            exact = 'oscmix-desk (= ' + record['package_version'] + ')'
        else:
            identity = run(['rpm', '-qp', '--queryformat',
                            '%{NAME}\n%{VERSION}-%{RELEASE}\n%{ARCH}', artifact]).splitlines()
            if identity != [record['package_name'], record['package_version'], architecture]:
                raise ValueError('RPM header does not match build manifest')
            dependency = run(['rpm', '-qp', '--requires', artifact])
            exact = 'oscmix-desk(x86-64) = ' + record['package_version']
        dependencies = dependency.strip().split(', ') if kind == 'deb' else dependency.splitlines()
        if record['component'] == 'gtk' and exact not in dependencies:
            raise ValueError('GTK does not require the exact core package version')
        packages.append((artifact, manifest, record))
    components = {record['component'] for _, _, record in packages}
    if len(components) != len(packages) or components not in (
            {'core', 'gtk'}, {'repository'}, {'core', 'gtk', 'repository'}):
        raise ValueError('updates require a complete core/GTK pair or the repository client')
    pair = [record for _, _, record in packages if record['component'] in ('core', 'gtk')]
    for field in ('package_version', 'source_commit', 'backend_commit', 'backend_protocol',
                  'backend_series_sha256', 'development', 'dirty'):
        # Authenticated historical 0.7.3 packages predate the ODK1 series fields.
        if pair and pair[0].get(field) != pair[1].get(field):
            raise ValueError('core/GTK build identities differ: ' + field)
    return packages


def restore_previous(args, signer):
    if args.previous is None:
        return []
    manifest = relative_file(args.previous, MANIFEST)
    signer.verify(relative_file(args.previous, MANIFEST + '.asc'), manifest)
    previous = json.loads(manifest.read_text())
    if previous['target'] != args.target or previous['development'] != args.development:
        raise ValueError('previous repository belongs to another channel')
    if previous['snapshot'] == args.snapshot:
        raise ValueError('a new staging operation needs a new immutable snapshot identifier')
    if args.epoch <= previous['epoch']:
        raise ValueError('new repository epoch must be later than the previous publication')
    if args.packages is None and previous['source_commit'] != args.expected_commit:
        raise ValueError('metadata renewal cannot change the package source identity')
    for name, digest in previous['files'].items():
        source = relative_file(args.previous, name)
        if sha(source) != digest:
            raise ValueError('previous repository has a missing or altered file: ' + name)
        destination = args.output / name
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, destination)
    return previous['packages']


def apt_metadata(args, signer):
    root = args.output
    index = root / 'dists/stable/main/binary-amd64'
    index.mkdir(parents=True, exist_ok=True)
    packages = run(['apt-ftparchive', 'packages', 'pool'], cwd=root).encode()
    (index / 'Packages').write_bytes(packages)
    (index / 'Packages.gz').write_bytes(gzip.compress(packages, mtime=0))
    sums = []
    for name in ('Packages', 'Packages.gz'):
        path = index / name
        digest = sha(path)
        by_hash = index / 'by-hash/SHA256' / digest
        by_hash.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(path, by_hash)
        sums.append(' %s %d main/binary-amd64/%s\n' % (digest, path.stat().st_size, name))
    now = datetime.datetime.fromtimestamp(args.epoch, datetime.timezone.utc)
    release = root / 'dists/stable/Release'
    release.write_text('Origin: oscmix-desk\nLabel: oscmix-desk\nSuite: stable\n'
                       'Codename: stable\nArchitectures: amd64\nComponents: main\n'
                       'Acquire-By-Hash: yes\nDate: ' + format_datetime(now, usegmt=True)
                       + '\nValid-Until: ' + format_datetime(
                           now + datetime.timedelta(days=args.valid_days), usegmt=True)
                       + '\nSHA256:\n' + ''.join(sums))
    signer.sign(release, release.with_name('InRelease'), clearsign=True)
    signer.sign(release, release.with_name('Release.gpg'))


def rpm_metadata(args, signer):
    run(['createrepo_c', '--unique-md-filenames', '--retain-old-md', '1',
         '--checksum', 'sha256', '--compress-type', 'gz', '--no-database',
         '--revision', str(args.epoch), '--set-timestamp-to-revision', args.output])
    metadata = args.output / 'repodata/repomd.xml'
    signer.sign(metadata, metadata.with_suffix('.xml.asc'))
    shutil.copyfile(args.public_key, metadata.with_suffix('.xml.key'))


def build(args):
    output = args.output.resolve()
    for source in (args.previous, args.packages, args.gnupghome):
        if source is not None and output.is_relative_to(source.resolve()):
            raise ValueError('staging output must be outside inputs and the private keyring')
    packages = (read_packages(args.packages, args.target, args.development, args.expected_commit)
                if args.packages else [])
    if (args.previous is None
            and {row['component'] for _, _, row in packages} != set(PACKAGE_NAMES)):
        raise ValueError('an initial repository requires core, GTK and repository client packages')
    args.output.mkdir(parents=True, exist_ok=False)
    with tempfile.TemporaryDirectory(prefix='oscmix-sign-') as temporary:
        scratch = Path(temporary)
        signer = Signer(args, scratch)
        records = restore_previous(args, signer)
        available = {item['name'] for item in records} | {
            record['package_name'] for _, _, record in packages}
        if not set(PACKAGE_NAMES.values()) <= available:
            raise ValueError('repository snapshot would lack a required package component')
        for artifact, manifest, record in packages:
            identity = record['package_name'], record['package_version']
            if any((item['name'], item['version']) == identity for item in records):
                raise ValueError('version already published; increase the package revision')
            signed = scratch / artifact.name
            shutil.copyfile(artifact, signed)
            if record['format'] == 'rpm':
                signer.rpm(signed)
            digest = sha(signed)
            relative = Path('pool') / digest / artifact.name
            destination = args.output / relative
            destination.parent.mkdir(parents=True)
            shutil.copyfile(signed, destination)
            provenance = Path('provenance') / manifest.name
            (args.output / provenance).parent.mkdir(exist_ok=True)
            shutil.copyfile(manifest, args.output / provenance)
            records.append(dict(name=identity[0], version=identity[1], path=str(relative),
                                sha256=digest, unsigned_sha256=record['sha256'],
                                build_manifest=str(provenance)))
        shutil.copyfile(args.public_key, args.output / 'archive-key.asc')
        if TARGETS[args.target][1] == 'deb':
            apt_metadata(args, signer)
        else:
            rpm_metadata(args, signer)
        files = {str(path.relative_to(args.output)): sha(path)
                 for path in sorted(args.output.rglob('*')) if path.is_file()}
        result = dict(schema=1, target=args.target, snapshot=args.snapshot,
                      development=args.development, source_commit=args.expected_commit,
                      generator_sha256=sha(Path(__file__).resolve()),
                      signing_key=args.signing_key, epoch=args.epoch,
                      packages=records, files=files)
        manifest = args.output / MANIFEST
        manifest.write_text(json.dumps(result, indent=2) + '\n')
        signer.sign(manifest, manifest.with_suffix('.json.asc'))
        # HTTP If-Modified-Since has one-second resolution. Distinct mutable
        # indexes/signatures must not inherit an indistinguishable mtime when
        # a fast publication or tar/cp transport rounds away subsecond times.
        mutable = ['archive-key.asc', MANIFEST, MANIFEST + '.asc']
        mutable += (['dists/stable/Release', 'dists/stable/InRelease',
                     'dists/stable/Release.gpg', 'dists/stable/main/binary-amd64/Packages',
                     'dists/stable/main/binary-amd64/Packages.gz']
                    if TARGETS[args.target][1] == 'deb' else
                    ['repodata/repomd.xml', 'repodata/repomd.xml.asc', 'repodata/repomd.xml.key'])
        for name in mutable:
            os.utime(args.output / name, (args.epoch, args.epoch))
        return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--target', required=True, choices=TARGETS)
    parser.add_argument('--packages', type=Path,
                        help='new core/GTK pair; omit only when renewing previous metadata')
    parser.add_argument('--previous', type=Path)
    parser.add_argument('--output', required=True, type=Path)
    parser.add_argument('--expected-commit', required=True)
    parser.add_argument('--snapshot', required=True)
    parser.add_argument('--gnupghome', required=True, type=Path)
    parser.add_argument('--public-key', required=True, type=Path)
    parser.add_argument('--signing-key', required=True)
    parser.add_argument('--passphrase-file', required=True, type=Path)
    parser.add_argument('--epoch', required=True, type=int)
    parser.add_argument('--valid-days', type=int, default=30)
    parser.add_argument('--development', action='store_true')
    args = parser.parse_args()
    if not re.fullmatch('[0-9a-f]{40}', args.expected_commit):
        parser.error('--expected-commit must be a full source SHA')
    if not re.fullmatch('[0-9A-F]{40}', args.signing_key):
        parser.error('--signing-key must be the full uppercase signing fingerprint')
    if not re.fullmatch('[a-zA-Z0-9][a-zA-Z0-9._-]{0,95}', args.snapshot):
        parser.error('--snapshot must be a simple immutable version/date identifier')
    if not 1 <= args.valid_days <= 90 or args.epoch <= 0:
        parser.error('expiry must be 1-90 days and epoch must be positive')
    if args.packages is None and args.previous is None:
        parser.error('provide --packages or --previous')
    for name in ('packages', 'previous', 'output', 'gnupghome', 'public_key', 'passphrase_file'):
        if getattr(args, name) is not None:
            setattr(args, name, getattr(args, name).absolute())
    try:
        result = build(args)
    except (OSError, ValueError, KeyError, subprocess.SubprocessError) as exc:
        parser.exit(1, 'repository staging failed: %s\n' % exc)
    print(json.dumps(dict(target=result['target'], snapshot=result['snapshot'],
                          packages=len(result['packages']), output=str(args.output))))


if __name__ == '__main__':
    main()
