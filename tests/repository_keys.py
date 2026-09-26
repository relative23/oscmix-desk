"""Exercise actual APT/DNF/zypper key policy with disposable signed fixtures."""

import argparse
import functools
import hashlib
import http.server
import json
import os
import shutil
import threading
from pathlib import Path

from repository_client_setup import CLIENT, CONFIG, trust
from repository_lifecycle import Client, QuietServer, check_installed


def exercise(args):
    args.output.mkdir(parents=True, exist_ok=False)
    repositories = args.fixtures / args.target
    certificates = args.fixtures / 'certificates'
    identity = json.loads((args.fixtures / 'fixture.json').read_text())['keys']
    served = args.output / 'served'
    served.symlink_to(repositories / 'first', target_is_directory=True)
    server = http.server.ThreadingHTTPServer(
        ('127.0.0.1', 0), functools.partial(QuietServer, directory=str(served)))
    server.requests = []
    thread = threading.Thread(target=server.serve_forever)
    thread.start()
    result = dict(target=args.target, ok=True, checks={}, failures={}, published_https=False,
                  fixture_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
                  inputs_sha256=hashlib.sha256((args.fixtures / 'fixture.json')
                                              .read_bytes()).hexdigest())

    def serve(name):
        served.unlink()
        served.symlink_to(repositories / name, target_is_directory=True)

    def client(name, certificate, *, scoped=True):
        output = args.output / name
        output.mkdir()
        trusted = output / 'trusted.asc'
        shutil.copyfile(certificates / (certificate + '.asc'), trusted)
        key = trust(trusted) if args.strict and scoped else trusted
        return Client(args.target, output, 'http://127.0.0.1:%d/' % server.server_port, key)

    def attempt(name, action):
        try:
            action()
            result['checks'][name] = 'passed'
        except AssertionError as exc:
            result['ok'] = False
            result['failures'][name] = str(exc)
        print(json.dumps(dict(case=name, passed=name in result['checks'])), flush=True)

    def replace_rpm_certificate(checker, certificate):
        # rpm --import alone need not replace an existing certificate with
        # the same primary key. Remove only this fixture's exact fingerprint.
        trusted = checker.output / 'trusted.asc'
        shutil.copyfile(certificates / (certificate + '.asc'), trusted)
        if args.strict:
            trust(trusted)
        if checker.kind == 'dnf':
            identifier = identity['primary']
        else:
            listed = checker.run(['rpmkeys', '--list']).stdout.splitlines()
            identifiers = [line.split(':', 1)[0] for line in listed
                           if line.startswith(identity['primary'][-8:].lower() + '-')]
            assert len(identifiers) == 1, listed
            identifier = identifiers[0]
            certificate_file = checker.output / 'installed-certificate.asc'
            certificate_file.write_text(checker.run(
                ['rpm', '-q', 'gpg-pubkey-' + identifier, '--queryformat', '%{DESCRIPTION}']
            ).stdout)
            home = checker.output / 'gpg-home'
            home.mkdir(mode=0o700)
            listing = checker.run(['gpg', '--batch', '--no-options', '--homedir', str(home),
                                   '--with-colons', '--show-keys', str(certificate_file)]).stdout
            primary = next(line.split(':')[9] for line in listing.splitlines()
                           if line.startswith('fpr:'))
            assert primary == identity['primary']
        checker.run(['rpmkeys', '--delete', identifier])
        checker.run(['rpmkeys', '--import', str(trusted)])
        checker.run(checker.command + ['clean', '--all' if checker.kind == 'zypper' else 'all'])
        # DNF retains a separate metadata keyring across `clean all`. Give
        # the updated certificate a new repo identity so that it is loaded.
        definition = checker.output / 'repos/channel.repo'
        old = definition.read_text()
        identity_suffix = hashlib.sha256(trusted.read_bytes()).hexdigest()[:16]
        definition.write_text(old.replace('[oscmix-test]',
                                          '[oscmix-test-' + identity_suffix + ']'))

    try:
        baseline = client('baseline', 'original')
        baseline.refresh()
        baseline.install()
        check_installed(repositories / 'first', 1)
        result['checks']['baseline'] = 'installed exact pair with initial certificate'

        def refused_metadata(name, certificate, repository):
            serve(repository)
            checker = client(name, certificate)
            checker.refresh(success=False)
            check_installed(repositories / 'first', 1)

        for name, certificate, repository in [('wrong-key', 'original', 'wrong-key'),
                                              ('expired-key', 'expired', 'expired-key'),
                                              ('expired-new-signature', 'expired',
                                               'expired-new-signature'),
                                              ('unknown-subkey', 'original', 'rotated')]:
            attempt(name, lambda n=name, c=certificate, r=repository: refused_metadata(n, c, r))

        if baseline.kind != 'apt':
            def refused_package(name):
                serve(name)
                checker = client(name, 'original')
                checker.refresh()
                checker.run(checker.upgrade_command(), success=False)
                check_installed(repositories / 'first', 1)

            for name in ('wrong-package-key', 'unsigned-package'):
                attempt(name, lambda n=name: refused_package(n))

        def rotate():
            serve('rotated')
            trusted = baseline.output / 'trusted.asc'
            shutil.copyfile(certificates / 'rotated.asc', trusted)
            if args.strict:
                trust(trusted)
            if baseline.kind != 'apt':
                replace_rpm_certificate(baseline, 'rotated')
            baseline.refresh()
            baseline.run(baseline.upgrade_command())
            check_installed(repositories / 'rotated', 2)

        attempt('rotation', rotate)

        def revoke():
            # A newly imported revocation must reject the formerly valid
            # signature, while the new, unrevoked subkey remains usable.
            serve('first')
            checker = client('revoked-key', 'revoked')
            if checker.kind != 'apt':
                replace_rpm_certificate(checker, 'revoked')
            checker.refresh(success=False)
            serve('rotated')
            checker.refresh()

        attempt('revoked-key', revoke)

        if args.strict:
            def other_repository():
                serve('wrong-key')
                checker = client('other-repository', 'foreign', scoped=False)
                checker.refresh()
                check_installed(repositories / 'rotated', 2)

            attempt('other-repository', other_repository)

            def integration_failure(name):
                serve('rotated')
                checker = client(name, 'revoked')
                checker.refresh()
                if name == 'missing-verifier':
                    original = CLIENT / 'verify.py'
                    held = CLIENT / 'verify.py.absent'
                    original.rename(held)
                    try:
                        checker.refresh(success=False)
                        check_installed(repositories / 'rotated', 2)
                    finally:
                        held.rename(original)
                else:
                    original = CONFIG.read_bytes()
                    try:
                        CONFIG.write_text(json.dumps(dict(schema=1, primary='0' * 40)))
                        checker.refresh(success=False)
                        check_installed(repositories / 'rotated', 2)
                    finally:
                        CONFIG.write_bytes(original)
                checker.refresh()

            for name in ('missing-verifier', 'wrong-trust-configuration'):
                attempt(name, lambda n=name: integration_failure(n))
        baseline.remove()
        assert not Path('/usr/bin/oscmix-session').exists()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        (args.output / 'http.json').write_text(json.dumps(server.requests, indent=2) + '\n')
        (args.output / 'result.json').write_text(json.dumps(result, indent=2) + '\n')
    assert result['ok'], result['failures']


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--target', required=True,
                        choices=['debian13', 'ubuntu2404', 'ubuntu2604', 'fedora44', 'opensuse16'])
    parser.add_argument('--fixtures', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--strict', action='store_true',
                        help='qualify installed extra verification')
    args = parser.parse_args()
    if not Path('/.dockerenv').is_file() or os.getuid() != 0:
        raise RuntimeError('requires a disposable root container without host devices or state')
    release = dict(line.split('=', 1) for line in Path('/etc/os-release').read_text().splitlines()
                   if '=' in line)
    platform = release['ID'].strip('"') + release['VERSION_ID'].strip('"')
    expected = {'debian13': 'debian13', 'ubuntu2404': 'ubuntu24.04',
                'ubuntu2604': 'ubuntu26.04', 'fedora44': 'fedora44',
                'opensuse16': 'opensuse-leap16.0'}
    if platform != expected[args.target]:
        raise RuntimeError('container OS does not match selected repository target')
    args.fixtures = args.fixtures.resolve()
    args.output = args.output.resolve()
    manifest = json.loads((args.fixtures / 'fixture.json').read_text())
    assert args.target in manifest['targets']
    for name, digest in manifest['files'].items():
        assert hashlib.sha256((args.fixtures / name).read_bytes()).hexdigest() == digest, name
    exercise(args)


if __name__ == '__main__':
    main()
