"""Qualify the native subscription package, not copied development hooks.

Runs only in a disposable root container with public signed test repositories.
The two development packages embed disposable certificates and loopback URLs;
no production signing material, host services or devices are involved.
"""

import argparse
import functools
import hashlib
import http.server
import json
import os
import shutil
import signal
import subprocess
import threading
import time
from pathlib import Path

from repository_client_setup import CLIENT, container
from repository_lifecycle import Client, QuietServer, check_installed

ROOT = Path(__file__).resolve().parents[1]
STATE = Path('/var/lib/oscmix-desk-repository')
FENCE = STATE / 'update.json'
COMMAND = ['/usr/bin/oscmix-repository']


class Native(Client):
    def __init__(self, target, output):
        self.target, self.output = target, output
        self.kind = ('apt' if target.startswith(('debian', 'ubuntu')) else
                     'dnf' if target == 'fedora44' else 'zypper')
        self.command = {'apt': ['apt-get', '-y', '-o', 'Acquire::Retries=0'],
                        'dnf': ['dnf', '-y'], 'zypper': ['zypper', '--non-interactive']}[self.kind]
        self.source = Path({'apt': '/etc/apt/sources.list.d/oscmix-desk.sources',
                            'dnf': '/etc/yum.repos.d/oscmix-desk.repo',
                            'zypper': '/etc/zypp/repos.d/oscmix-desk.repo'}[self.kind])

    def local_install(self, package):
        if self.kind == 'zypper':
            # These reproducibility fixtures are locally built unsigned RPMs;
            # ordinary channel commands below retain all signature checks.
            return ['zypper', '--non-interactive', '--no-refresh', '--no-gpg-checks',
                    'install', '--force', '--oldpackage', str(package)]
        return (['dpkg', '-i', str(package)] if self.kind == 'apt' else
                ['rpm', '-Uvh', '--oldpackage', '--replacepkgs', str(package)])

    def status(self):
        return json.loads(self.run(COMMAND + ['status']).stdout)


def interrupt(client, package):
    assert not FENCE.exists()
    with (client.output / 'interrupted.log').open('w') as log:
        child = subprocess.Popen(client.local_install(package), start_new_session=True,
                                 stdout=log, stderr=subprocess.STDOUT)
        stopped = False
        try:
            deadline = time.monotonic() + 45
            while time.monotonic() < deadline and child.poll() is None:
                if FENCE.exists():
                    os.killpg(child.pid, signal.SIGSTOP)
                    stopped = True
                    break
                time.sleep(.001)
            assert stopped, 'native client update did not expose its interruption fence'
        finally:
            if child.poll() is None:
                os.killpg(child.pid, signal.SIGKILL)
            child.wait(timeout=10)
        assert FENCE.exists(), 'interruption lost the repository maintenance fence'
    time.sleep(.2)


def busy_apt(client, certificate, primary, package):
    # Exercise the real POSIX lock that apt update uses, not a mocked busy flag.
    code = ('import apt_pkg,fcntl,time; apt_pkg.init_config(); '
            'p=apt_pkg.config.find_dir("Dir::State::lists"); '
            'f=open(p+"lock","a"); fcntl.lockf(f,fcntl.LOCK_EX); '
            'print("locked",flush=True); time.sleep(30)')
    child = subprocess.Popen(['/usr/bin/python3', '-I', '-c', code],
                             stdout=subprocess.PIPE, text=True)
    try:
        assert child.stdout.readline().strip() == 'locked'
        failed = client.run(COMMAND + ['refresh-key', '--certificate', str(certificate),
                                      '--fingerprint', primary], success=False)
        assert 'APT metadata is busy' in failed.stderr
        assert FENCE.exists()
        assert not client.source.exists()
    finally:
        child.terminate()
        child.wait(timeout=5)
        child.stdout.close()
    client.run(client.local_install(package))
    assert not FENCE.exists()


def exercise(args):
    container()
    args.output.mkdir(parents=True, exist_ok=False)
    client = Native(args.target, args.output)
    certificates = args.fixtures / 'certificates'
    repositories = args.fixtures / args.target
    primary = json.loads((args.fixtures / 'fixture.json').read_text())['keys']['primary']
    served = args.output / 'served'
    served.mkdir()
    link = served / args.target
    link.symlink_to(repositories / 'first', target_is_directory=True)
    server = http.server.ThreadingHTTPServer(
        ('127.0.0.1', 0), functools.partial(QuietServer, directory=str(served)))
    server.requests = []
    thread = threading.Thread(target=server.serve_forever)
    thread.start()
    result = dict(target=args.target, ok=False, checks={}, packages={}, hardware=False,
                  published_https=False, development=True)

    def serve(name):
        link.unlink()
        link.symlink_to(repositories / name, target_is_directory=True)

    def passed(name, detail=True):
        result['checks'][name] = detail
        print(json.dumps(dict(case=name, result=detail)), flush=True)

    def build(name, revision, certificate):
        public = ROOT / 'packaging/keys/oscmix-desk-archive.asc'
        public.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(certificates / (certificate + '.asc'), public)
        (public.parent / 'archive-key.json').write_text(json.dumps(dict(
            primary_fingerprint=primary,
            public_armor_sha256=hashlib.sha256(public.read_bytes()).hexdigest(),
            endpoint='http://127.0.0.1:%d/' % server.server_port)))
        output = args.output / name
        kind = 'deb' if client.kind == 'apt' else 'rpm'
        client.run(['python3', str(ROOT / 'scripts/build-package.py'), '--format', kind,
                    '--repository-client-only', '--development', '--package-revision',
                    str(revision), '--output', str(output)])
        packages = list(output.glob('*.' + kind))
        assert len(packages) == 1
        record = json.loads(packages[0].with_suffix('.' + kind + '.json').read_text())
        result['packages'][name] = record
        return packages[0]

    # Only the disposable container's own repositories are set aside so native
    # commands can run offline with their normal cache/configuration paths.
    # Their bytes are checked afterward; the subscription does not alter them.
    held = args.output / 'distribution-definitions'
    held.mkdir()
    preserved = {}
    patterns = (['/etc/apt/sources.list', '/etc/apt/sources.list.d/*'] if client.kind == 'apt'
                else ['/etc/yum.repos.d/*'] if client.kind == 'dnf' else
                ['/etc/zypp/repos.d/*', '/etc/zypp/services.d/*'])
    for pattern in patterns:
        for path in Path('/').glob(pattern.lstrip('/')):
            if path.is_file():
                destination = held / str(path).lstrip('/').replace('/', '_')
                preserved[destination] = path.read_bytes()
                path.rename(destination)
    try:
        first = build('first', 1, 'original')
        repeated = build('repeated', 1, 'original')
        second = build('second', 2, 'rotated')
        assert first.read_bytes() == repeated.read_bytes(), 'bootstrap package not reproducible'
        passed('repeat-build')
        client.run(client.local_install(first))
        assert client.status()['enabled'] is False
        assert not FENCE.exists()
        assert not client.source.exists()
        assert not Path('/usr/bin/oscmix-session').exists()
        passed('install-without-subscription-or-mixer')

        client.run(COMMAND + ['enable'])
        assert client.status()['enabled'] is True
        client.refresh()
        client.install()
        check_installed(repositories / 'first', 1)
        passed('explicit-enable-and-native-install')

        # Upgrade the actual helper package, including its embedded next
        # certificate, then use normal native commands with no test key copies.
        previous_id = client.status()['repo_id']
        client.run(client.local_install(second))
        assert client.status()['enabled'] is True
        assert client.status()['native_key_pending'] is False
        if client.kind != 'apt':
            assert client.status()['repo_id'] != previous_id
        serve('rotated')
        client.refresh()
        client.run(client.upgrade_command())
        check_installed(repositories / 'rotated', 2)
        passed('packaged-subkey-rotation')

        # A revocation must discard the already populated native metadata.
        # With the old server still selected, a normal install cannot fall
        # back to previously authenticated indexes after the trust update.
        serve('first')
        client.refresh()
        client.run(COMMAND + ['refresh-key', '--certificate', str(certificates / 'revoked.asc'),
                              '--fingerprint', primary])
        client.remove()
        client.install(success=False)
        assert not Path('/usr/bin/oscmix-session').exists()
        client.refresh(success=False)
        passed('revocation-invalidates-native-cache')
        serve('rotated')
        client.refresh()
        client.install()
        check_installed(repositories / 'rotated', 2)

        if client.kind == 'apt':
            unrelated = Path('/var/lib/apt/lists/unrelated-source-sentinel')
            unrelated.write_text('preserve unrelated cached metadata')
            busy_apt(client, certificates / 'revoked.asc', primary, second)
            # Check before apt update performs its own garbage collection of
            # list files with no matching source. The helper must preserve it.
            assert unrelated.read_text() == 'preserve unrelated cached metadata'
            client.refresh()
            passed('busy-native-apt-lock-refusal-and-recovery')

        # Installing an older helper must preserve the accumulated revocation.
        client.run(client.local_install(first))
        serve('first')
        client.refresh(success=False)
        serve('rotated')
        client.refresh()
        passed('helper-rollback-retains-revocation')

        interrupt(client, second)
        assert FENCE.exists()
        if (CLIENT / 'verify.py').exists():
            index = (repositories / 'rotated' / 'dists/stable/InRelease' if client.kind == 'apt'
                     else repositories / 'rotated/repodata/repomd.xml.asc')
            command = ['python3', str(CLIENT / 'verify.py'), '--signature', str(index)]
            if client.kind != 'apt':
                command += ['--source', str(index.with_suffix(''))]
            failed = client.run(command, success=False)
            assert 'maintenance' in failed.stderr, failed.stderr
        client.run(client.local_install(second))
        assert not FENCE.exists()
        assert client.status()['enabled'] is True
        client.refresh()
        check_installed(repositories / 'rotated', 2)
        passed('actual-interrupted-helper-upgrade-and-repair')

        # A partial payload refuses reactivation and remains fenced until the
        # native package manager replaces all registered helper bytes.
        broken = CLIENT / 'verify.py'
        broken.write_bytes(broken.read_bytes() + b'\n# damaged installation\n')
        client.run(COMMAND + ['enable'], success=False)
        assert FENCE.exists()
        assert not client.source.exists()
        client.run(client.local_install(second))
        assert not FENCE.exists()
        client.refresh()
        passed('incomplete-payload-refusal-and-reinstallation')

        original = client.source.read_bytes()
        edited = original + b'\n# local administrator change\n'
        client.source.write_bytes(edited)
        client.run(client.local_install(second))
        assert client.status()['enabled'] is False
        assert not client.source.exists()
        backups = list(client.source.parent.glob(client.source.name + '.saved-*'))
        assert len(backups) == 1
        assert backups[0].read_bytes() == edited
        passed('modified-definition-preserved-and-disabled')

        client.run(COMMAND + ['enable'])
        client.refresh()
        client.remove()
        client.run(COMMAND + ['disable'])
        assert not client.source.exists()
        assert not FENCE.exists()
        client.run(['dpkg', '--remove', 'oscmix-desk-repository'] if client.kind == 'apt'
                   else ['rpm', '-e', 'oscmix-desk-repository'])
        assert not Path(COMMAND[0]).exists()
        assert not FENCE.exists()
        assert not client.source.exists()
        assert all(path.read_bytes() == content for path, content in preserved.items())
        passed('disable-remove-and-unrelated-source-preservation')
        result['ok'] = True
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        (args.output / 'http.json').write_text(json.dumps(server.requests, indent=2) + '\n')
        (args.output / 'result.json').write_text(json.dumps(result, indent=2) + '\n')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--target', required=True)
    parser.add_argument('--fixtures', required=True, type=Path)
    parser.add_argument('--output', required=True, type=Path)
    args = parser.parse_args()
    args.fixtures, args.output = args.fixtures.resolve(), args.output.resolve()
    exercise(args)


if __name__ == '__main__':
    main()
