"""Generate public, signed repository fixtures with disposable container-only keys.

The normal snapshots use the production generator. Explicit fault fixtures
re-sign metadata or RPMs separately to exercise actual client trust policy.
No secret key or passphrase is written into the output or an image layer.
"""

import argparse
import importlib.util
import json
import os
import secrets
import shutil
import subprocess
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location('repository_builder',
                                            ROOT / 'scripts/build-repository.py')
BUILDER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(BUILDER)


class TestKey:
    def __init__(self, directory, name, created, expiry='30d'):
        self.home = directory / name
        self.home.mkdir(mode=0o700)
        self.password = self.home / 'passphrase'
        self.password.write_text(secrets.token_hex(32) + '\n')
        self.password.chmod(0o600)
        self.command = ['gpg', '--batch', '--no-options', '--yes', '--homedir', str(self.home),
                        '--pinentry-mode', 'loopback', '--passphrase-file', str(self.password)]
        self.run(['--faked-system-time', str(created), '--quick-gen-key',
                  'oscmix-desk disposable qualification ' + name, 'rsa2048', 'cert', '90d'])
        self.primary = self.fingerprints()[0]
        self.signing = self.add_subkey(created, expiry)

    def run(self, arguments, **kwargs):
        return BUILDER.run(self.command + arguments, **kwargs)

    def fingerprints(self):
        return [line.split(':')[9] for line in self.run(['--with-colons', '--list-keys'])
                .splitlines() if line.startswith('fpr:')]

    def add_subkey(self, created, expiry='30d'):
        self.run(['--faked-system-time', str(created), '--quick-add-key', self.primary,
                  'rsa2048', 'sign', expiry])
        return self.fingerprints()[-1]

    def export(self, path):
        path.write_text(self.run(['--armor', '--export', self.primary]))

    def sign(self, source, destination, *, clearsign=False, epoch=None, ignore_expiration=False):
        clock = [] if epoch is None else ['--faked-system-time', str(epoch)]
        if ignore_expiration:
            # Construct an invalid test signature after expiry; production
            # staging never enables this GnuPG diagnostic-only override.
            clock += ['--debug-ignore-expiration']
        self.run(clock + ['--local-user', self.signing + '!', '--digest-algo', 'SHA256',
                          '--armor', '--output', str(destination),
                          '--clearsign' if clearsign else '--detach-sign', str(source)])

    def revoke_first_subkey(self):
        self.run(['--command-fd', '0', '--status-fd', '2', '--edit-key', self.primary],
                 input='key 1\nrevkey\ny\n1\n\ny\nsave\n')


def arguments(target, inputs, output, name, key, certificate, previous=None, packages=None):
    if previous:
        old_epoch = json.loads((previous / 'repository.json').read_text())['epoch']
        while int(time.time()) <= old_epoch:
            time.sleep(.05)
    commit = json.loads(next((inputs / 'package').glob('oscmix-desk[_-]0*.json'))
                        .read_text())['source_commit']
    return SimpleNamespace(target=target, packages=packages, previous=previous, output=output,
                           expected_commit=commit, snapshot=name, gnupghome=key.home,
                           public_key=certificate, signing_key=key.signing,
                           passphrase_file=key.password, epoch=int(time.time()),
                           valid_days=30, development=True)


def resign_metadata(directory, kind, key, epoch=None, ignore_expiration=False):
    options = dict(epoch=epoch, ignore_expiration=ignore_expiration)
    if kind == 'deb':
        source = directory / 'dists/stable/Release'
        key.sign(source, source.with_name('InRelease'), clearsign=True, **options)
        key.sign(source, source.with_name('Release.gpg'), **options)
    else:
        source = directory / 'repodata/repomd.xml'
        key.sign(source, source.with_suffix('.xml.asc'), **options)
        key.export(source.with_suffix('.xml.key'))
    key.export(directory / 'archive-key.asc')


def fault_rpm(inputs, channel, output, name, signing, trusted, certificate):
    """Trusted metadata must not mask a missing or foreign RPM package signature."""
    shutil.copytree(channel, output)
    manifest = json.loads((output / 'repository.json').read_text())
    core = [item for item in manifest['packages'] if item['name'] == 'oscmix-desk'][-1]
    path = output / core['path']
    BUILDER.run(['rpmsign', '--delsign', path])
    if signing:
        with tempfile.TemporaryDirectory(prefix='repo-fault-') as scratch:
            public = Path(scratch) / 'public.asc'
            signing.export(public)
            args = arguments(manifest['target'], inputs, output, name, signing, public)
            BUILDER.Signer(args, Path(scratch)).rpm(path)
    core['sha256'] = BUILDER.sha(path)
    destination = output / 'pool' / core['sha256'] / path.name
    destination.parent.mkdir()
    path.rename(destination)
    core['path'] = str(destination.relative_to(output))
    args = arguments(manifest['target'], inputs, output, name, trusted, certificate)
    with tempfile.TemporaryDirectory(prefix='repo-fault-') as scratch:
        BUILDER.rpm_metadata(args, BUILDER.Signer(args, Path(scratch)), manifest['packages'])
    # This is a deliberately inconsistent fixture, never generator provenance.
    manifest['fault_fixture'] = name
    (output / 'repository.json').write_text(json.dumps(manifest, indent=2) + '\n')


def exercise(args, temporary):
    past = int(time.time()) - 3 * 86400
    trusted = TestKey(temporary, 'trusted', past)
    foreign = TestKey(temporary, 'foreign', past)
    expired = TestKey(temporary, 'expired', past, '1d')
    certificates = args.output / 'certificates'
    certificates.mkdir(parents=True)
    trusted.export(certificates / 'original.asc')
    foreign.export(certificates / 'foreign.asc')
    expired.export(certificates / 'expired.asc')
    original_signing = trusted.signing
    rotated_signing = trusted.add_subkey(int(time.time()))
    trusted.export(certificates / 'rotated.asc')
    targets = args.target or list(BUILDER.TARGETS)
    result = dict(schema=1, targets=targets, development=True, public_only=True,
                  generator_sha256=BUILDER.sha(ROOT / 'scripts/build-repository.py'),
                  fixture_generator_sha256=BUILDER.sha(Path(__file__)),
                  keys=dict(primary=trusted.primary, original=original_signing,
                            rotated=rotated_signing, foreign=foreign.primary,
                            expired=expired.signing), scenarios={})
    for target in targets:
        inputs = args.inputs / target
        output = args.output / target
        output.mkdir()
        kind = BUILDER.TARGETS[target][1]
        trusted.signing = original_signing
        for name, package, previous in [('first', 'package', None),
                                        ('second', 'upgraded', 'first'),
                                        ('renewed', None, 'second')]:
            options = arguments(target, inputs, output / name, name, trusted,
                                certificates / 'original.asc',
                                output / previous if previous else None,
                                inputs / package if package else None)
            BUILDER.build(options)
        first = json.loads((output / 'first/repository.json').read_text())
        second = json.loads((output / 'second/repository.json').read_text())
        assert len(first['packages']) == 3
        assert len(second['packages']) == 6
        for package in first['packages']:
            assert package in second['packages']
            assert BUILDER.sha(output / 'second' / package['path']) == package['sha256']
        for path, digest in first['files'].items():
            if '/by-hash/' in path or (path.startswith('repodata/') and path.endswith('.gz')):
                assert BUILDER.sha(output / 'second' / path) == digest, path

        # A certificate/helper update must not force a new mixer/backend build.
        # The previously authenticated exact core/GTK pair stays in this branch.
        with tempfile.TemporaryDirectory(prefix='oscmix-client-update-') as temporary:
            client_input = Path(temporary)
            for manifest in (inputs / 'upgraded').glob('*.' + kind + '.json'):
                record = json.loads(manifest.read_text())
                if record['component'] == 'repository':
                    shutil.copyfile(manifest, client_input / manifest.name)
                    shutil.copyfile(BUILDER.relative_file(inputs / 'upgraded', record['artifact']),
                                    client_input / record['artifact'])
            options = arguments(target, inputs, output / 'client-only', 'client-only', trusted,
                                certificates / 'original.asc', output / 'first', client_input)
            client_update = BUILDER.build(options)
        assert len(client_update['packages']) == 4
        assert all(item in client_update['packages'] for item in first['packages'])
        added = [item for item in client_update['packages'] if item not in first['packages']]
        assert [item['name'] for item in added] == ['oscmix-desk-repository']

        for name, key, epoch in [('wrong-key', foreign, None),
                                  ('expired-key', expired, past + 3600)]:
            directory = output / name
            shutil.copytree(output / 'second', directory)
            resign_metadata(directory, kind, key, epoch)
        after_expiry = output / 'expired-new-signature'
        shutil.copytree(output / 'second', after_expiry)
        resign_metadata(after_expiry, kind, expired, ignore_expiration=True)
        if kind == 'rpm':
            for name, key in [('wrong-package-key', foreign), ('unsigned-package', None)]:
                fault_rpm(inputs, output / 'second', output / name, name, key, trusted,
                          certificates / 'original.asc')
        trusted.signing = rotated_signing
        options = arguments(target, inputs, output / 'rotated', 'rotated', trusted,
                            certificates / 'rotated.asc', output / 'first', inputs / 'upgraded')
        rotated = BUILDER.build(options)
        if kind == 'rpm':
            assert len(rotated['retained_packages']) == len(first['packages'])
            for old in first['packages']:
                assert old in rotated['retained_packages']
                current = next(row for row in rotated['packages']
                               if (row['name'], row['version']) == (old['name'], old['version']))
                assert current['path'] != old['path']
                assert BUILDER.sha(output / 'rotated' / old['path']) == old['sha256']
                assert BUILDER.rpm_payload_identity(output / 'rotated' / old['path']) == (
                    BUILDER.rpm_payload_identity(output / 'rotated' / current['path']))
        result['scenarios'][target] = dict(
            source_commit=first['source_commit'], original=BUILDER.sha(
                output / 'first/repository.json'), rotated=BUILDER.sha(
                    output / 'rotated/repository.json'))
        print(json.dumps(dict(target=target, staged=True)), flush=True)

    trusted.revoke_first_subkey()
    trusted.export(certificates / 'revoked.asc')
    # Verify real GnuPG outcomes in addition to client tests. The original
    # and rotated signatures are checked with the corresponding certificates.
    target = targets[0]
    kind = BUILDER.TARGETS[target][1]
    for name, certificate, accepted in [('first', 'original', True),
                                        ('expired-key', 'expired', False),
                                        ('first', 'revoked', False),
                                        ('rotated', 'revoked', True)]:
        directory = args.output / target / name
        source = directory / ('dists/stable/Release' if kind == 'deb' else 'repodata/repomd.xml')
        signature = source.with_name('Release.gpg') if kind == 'deb' else source.with_suffix(
            '.xml.asc')
        signing = expired.signing if certificate == 'expired' else trusted.signing
        options = SimpleNamespace(public_key=certificates / (certificate + '.asc'),
                                  gnupghome=trusted.home, signing_key=signing)
        # The original certificate intentionally predates the rotated key.
        if certificate == 'original':
            options.signing_key = original_signing
        with tempfile.TemporaryDirectory(prefix='repo-verify-') as scratch:
            verifier = BUILDER.Signer(options, Path(scratch))
            actual = True
            try:
                verifier.verify(signature, source)
            except (ValueError, subprocess.CalledProcessError):
                actual = False
            assert actual is accepted, (name, certificate, actual)
    result['gpgv_key_policy'] = 'current and rotated accepted; expired and revoked refused'
    result['files'] = {str(path.relative_to(args.output)): BUILDER.sha(path)
                       for path in sorted(args.output.rglob('*')) if path.is_file()}
    (args.output / 'fixture.json').write_text(json.dumps(result, indent=2) + '\n')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--inputs', required=True, type=Path,
                        help='TARGET/package and TARGET/upgraded native artifact directories')
    parser.add_argument('--output', required=True, type=Path,
                        help='new public-only output directory')
    parser.add_argument('--target', action='append', choices=BUILDER.TARGETS)
    args = parser.parse_args()
    if not Path('/.dockerenv').is_file() or os.getuid() != 0:
        raise RuntimeError('requires a disposable root container without host devices or state')
    args.inputs = args.inputs.resolve()
    args.output = args.output.resolve()
    args.output.mkdir(parents=True, exist_ok=False)
    with tempfile.TemporaryDirectory(prefix='oscmix-repository-keys-') as private:
        try:
            exercise(args, Path(private))
        finally:
            for home in Path(private).iterdir():
                if home.is_dir():
                    subprocess.run(['gpgconf', '--homedir', str(home), '--kill', 'gpg-agent'],
                                   check=True, capture_output=True, timeout=15)


if __name__ == '__main__':
    main()
