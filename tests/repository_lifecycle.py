"""Real package-manager checks of a staged channel in a disposable native container.

Only loopback HTTP is used here. Published HTTPS, trust-anchor rotation/expiry
and the final 0.7.3-to-0.8.0 transition are separate required qualification.
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

from repository_client_setup import CERTIFICATE, trust

FENCE = Path('/var/lib/oscmix-desk/package-update')
GTK_FENCE = FENCE.with_name('gtk-package-update')


class Client:
    def __init__(self, target, output, url, key):
        self.output = output
        self.target = target
        if target.startswith(('debian', 'ubuntu')):
            self.kind = 'apt'
            source = output / 'channel.sources'
            source.write_text('Types: deb\nURIs: ' + url + '\nSuites: stable\n'
                              'Components: main\nArchitectures: amd64\nSigned-By: ' + str(key)
                              + '\n')
            lists = output / 'lists'
            (lists / 'partial').mkdir(parents=True)
            self.command = ['apt-get', '-y', '-o', 'Dir::Etc::sourcelist=' + str(source),
                            '-o', 'Dir::Etc::sourceparts=-',
                            '-o', 'Dir::State::lists=' + str(lists),
                            '-o', 'Acquire::Retries=0']
        else:
            self.kind = 'dnf' if target == 'fedora44' else 'zypper'
            repos = output / 'repos'
            repos.mkdir()
            services = output / 'services'
            services.mkdir()
            (repos / 'channel.repo').write_text(
                '[oscmix-test]\nname=oscmix-desk isolated qualification\nbaseurl=' + url
                + '\nenabled=1\nautorefresh=1\ngpgcheck=1\nrepo_gpgcheck=1\npkg_gpgcheck=1\n'
                'skip_if_unavailable=0\ngpgkey=file://' + str(key) + '\n')
            if self.kind == 'zypper' and key == CERTIFICATE:
                with (repos / 'channel.repo').open('a') as definition:
                    definition.write('repo_sigcheck_plugin=oscmix-desk\n')
            self.run(['rpm', '--import', str(key)])
            self.command = (['dnf', '-y', '--setopt=reposdir=' + str(repos),
                             '--setopt=cachedir=' + str(output / 'cache')]
                            if self.kind == 'dnf' else
                            ['zypper', '--non-interactive', '--reposd-dir', str(repos),
                             '--servicesd-dir', str(services),
                             '--cache-dir', str(output / 'cache')])

    def run(self, command, success=True):
        result = subprocess.run(command, capture_output=True, text=True, timeout=150)
        with (self.output / 'commands.jsonl').open('a') as stream:
            stream.write(json.dumps(dict(command=command, exit=result.returncode,
                                         stdout=result.stdout, stderr=result.stderr)) + '\n')
        if success is not None:
            assert (result.returncode == 0) is success, (command, result.stdout, result.stderr)
        return result

    def refresh(self, success=True):
        arguments = {'apt': ['update', '--error-on=any'], 'dnf': ['--refresh', 'makecache'],
                     'zypper': ['refresh', '--force']}[self.kind]
        return self.run(self.command + arguments, success)

    def install(self, version=None, gtk=True, success=True, downgrade=False):
        names = ['oscmix-desk', *(['oscmix-desk-gtk'] if gtk else [])]
        if version:
            separator = '-' if self.kind == 'dnf' else '='
            names = [name + separator + version for name in names]
        verb = 'downgrade' if downgrade and self.kind == 'dnf' else 'install'
        options = ({'apt': ['--allow-downgrades'], 'dnf': [],
                    'zypper': ['--oldpackage']}[self.kind] if downgrade else [])
        return self.run(self.command + [verb, *options, *names], success)

    def upgrade_command(self):
        return self.command + [{'apt': 'install', 'dnf': 'upgrade', 'zypper': 'update'}[self.kind],
                               'oscmix-desk', 'oscmix-desk-gtk']

    def remove(self, gtk=True):
        return self.run(self.command + ['remove', 'oscmix-desk',
                                       *(['oscmix-desk-gtk'] if gtk else [])])


class QuietServer(http.server.SimpleHTTPRequestHandler):
    def log_message(self, format, *args):  # noqa: A002 -- inherited method signature
        pass

    def log_request(self, code='-', size='-'):
        self.server.requests.append(dict(path=self.path, status=int(code), size=size,
                                         if_modified_since=self.headers.get('If-Modified-Since')))


def check_installed(repo, revision, gtk=True):
    for component in ('core', 'gtk') if gtk else ('core',):
        installed = Path('/usr/share/oscmix-desk',
                         'package.json' if component == 'core' else 'gtk-package.json')
        actual = json.loads(installed.read_text())
        assert actual['package_revision'] == revision, actual
        manifests = [path for path in (repo / 'provenance').glob('*.json')
                     if json.loads(path.read_text())['component'] == component
                     and json.loads(path.read_text())['package_revision'] == revision]
        assert len(manifests) == 1
        expected = json.loads(manifests[0].read_text())
        assert (actual['source_commit'], actual['backend_series_sha256']) == (
            expected['source_commit'], expected['backend_series_sha256'])
        for name, digest in expected['payload'].items():
            path = Path('/') / name
            if name.startswith('usr/share/doc/') and not path.exists():
                continue  # native container policy can omit optional documentation
            assert hashlib.sha256(path.read_bytes()).hexdigest() == digest, name
    assert not FENCE.exists()
    assert not GTK_FENCE.exists()


def interrupted_update(client):
    """Stop the real transaction after its package hook creates the fence."""
    assert not FENCE.exists()
    assert not GTK_FENCE.exists()
    with (client.output / 'interrupted.log').open('w') as log:
        child = subprocess.Popen(client.upgrade_command(), start_new_session=True,
                                 stdout=log, stderr=subprocess.STDOUT)
        stopped = False
        try:
            deadline = time.monotonic() + 45
            while time.monotonic() < deadline and child.poll() is None:
                if FENCE.exists() or GTK_FENCE.exists():
                    os.killpg(child.pid, signal.SIGSTOP)
                    stopped = True
                    break
                time.sleep(.001)
            assert stopped, 'transaction did not expose a maintenance-fenced interruption point'
        finally:
            if child.poll() is None:
                os.killpg(child.pid, signal.SIGKILL)
            child.wait(timeout=10)
        assert FENCE.exists() or GTK_FENCE.exists(), 'interruption lost the maintenance fence'
    # Pending helper descendants may need to be reaped by the container's init.
    time.sleep(.2)
    if client.kind == 'apt':
        # An interrupted unpack can leave the companion in reinstreq state;
        # configuration first drains dpkg's journal but cannot repair missing
        # files. Its failure is retained; only complete reinstallation passes.
        configured = client.run(['dpkg', '--configure', '-a'], success=None)
        assert configured.returncode in (0, 1), configured.stderr
        client.run(client.command + ['--fix-broken', 'install', '--reinstall',
                                     'oscmix-desk', 'oscmix-desk-gtk'])
    else:
        # The RPM %pre fence can exist before the rpmdb has changed. An install
        # completes either that state or a partially recorded upgrade.
        client.run(client.upgrade_command())
        if FENCE.exists() or GTK_FENCE.exists():
            option = ['reinstall'] if client.kind == 'dnf' else ['install', '--force']
            client.run(client.command + [*option, 'oscmix-desk', 'oscmix-desk-gtk'])
    assert not FENCE.exists()
    assert not GTK_FENCE.exists()


def exercise(args):
    args.output.mkdir(parents=True, exist_ok=False)
    served = args.output / 'served'
    served.symlink_to(args.fixtures / 'first', target_is_directory=True)
    trusted = args.output / 'trusted.asc'
    shutil.copyfile(args.fixtures / 'first/archive-key.asc', trusted)
    handler = functools.partial(QuietServer, directory=str(served))
    server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), handler)
    server.requests = []
    thread = threading.Thread(target=server.serve_forever)
    thread.start()
    state = Path('/home/tester/.config/oscmix')
    (state / 'profiles').mkdir(parents=True, exist_ok=True)
    for directory in (state.parent, state, state / 'profiles'):
        directory.chmod(0o700)
        os.chown(directory, 12510, 12510)
    preserved = {state / 'routing.conf': '# repository lifecycle\n',
                 state / 'profiles/quiet.conf': '[output:5]\nvolume=-30\n',
                 state / 'active-profile': 'quiet\n'}
    for path, content in preserved.items():
        path.write_text(content)
        path.chmod(0o640)
        os.chown(path, 12510, 12510)

    def serve(path):
        served.unlink()
        served.symlink_to(path, target_is_directory=True)

    key = trust(trusted) if args.strict else trusted
    client = Client(args.target, args.output, 'http://127.0.0.1:%d/' % server.server_port, key)
    result = {}
    try:
        # Altered authenticated metadata must not be accepted, even on the
        # first subscription when no previous signature is cached.
        damaged = args.output / 'damaged-metadata'
        shutil.copytree(args.fixtures / 'first', damaged)
        metadata = damaged / ('dists/stable/InRelease' if client.kind == 'apt'
                              else 'repodata/repomd.xml')
        content = metadata.read_bytes()
        if client.kind == 'apt':
            content = content.replace(b'Origin: oscmix-desk', b'Origin: oscmix-forged')
        else:
            content = content.replace(b'<revision>', b'<revision>9')
        assert content != metadata.read_bytes()
        metadata.write_bytes(content)
        serve(damaged)
        client.refresh(success=False)
        assert not Path('/usr/bin/oscmix-session').exists()
        result['altered_metadata'] = 'refused before installation'

        serve(args.fixtures / 'first')
        client.refresh()
        client.install(gtk=False)
        check_installed(args.fixtures / 'first', 1, gtk=False)
        assert not Path('/usr/bin/oscmix-gtk').exists()
        assert not (state / 'service-allowed').exists()
        client.install()
        check_installed(args.fixtures / 'first', 1)
        result['subscription'] = 'core alone and then exact GTK pair; no activation'

        for scenario in ('missing-package', 'altered-package'):
            damaged = args.output / scenario
            shutil.copytree(args.fixtures / 'second', damaged)
            manifest = json.loads((damaged / 'repository.json').read_text())
            package = [row for row in manifest['packages'] if row['name'] == 'oscmix-desk'][-1]
            path = damaged / package['path']
            if scenario == 'missing-package':
                path.unlink()
            else:
                path.write_bytes(path.read_bytes()[:100])
            serve(damaged)
            before = len(server.requests)
            client.refresh()
            if scenario == 'missing-package':
                metadata_path = ('/dists/stable/InRelease' if client.kind == 'apt'
                                 else '/repodata/repomd.xml')
                requests = [row for row in server.requests[before:]
                            if row['path'] == metadata_path]
                assert any(row['status'] == 200 for row in requests), requests
                if client.kind == 'apt':
                    assert any(row['if_modified_since'] for row in requests), requests
                result['changed_metadata'] = (
                    'new index fetched; conditional GET cannot retain old index')
            client.run(client.upgrade_command(), success=False)
            check_installed(args.fixtures / 'first', 1)
            result[scenario] = 'update refused; previous installed pair intact'

        serve(args.fixtures / 'second')
        client.refresh()
        interrupted_update(client)
        check_installed(args.fixtures / 'second', 2)
        result['interruption'] = 'actual package transaction killed after fence; recovery passed'
        first = json.loads((args.fixtures / 'first/repository.json').read_text())
        client.install(version=first['packages'][0]['version'], downgrade=True)
        check_installed(args.fixtures / 'second', 1)
        result['rollback'] = 'explicit older pair selected from retained channel metadata'
        client.remove()
        assert not Path('/usr/bin/oscmix-session').exists()
        assert not FENCE.exists()
        assert not GTK_FENCE.exists()
        for path, content in preserved.items():
            info = path.stat()
            assert (path.read_text(), info.st_uid, info.st_gid, info.st_mode & 0o777) == (
                content, 12510, 12510, 0o640)
        for directory in (state.parent, state, state / 'profiles'):
            assert directory.stat().st_uid == 12510
        result['user_state'] = 'configuration, profile and marker retained byte/owner/mode'
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
    client.refresh(success=False)
    (args.output / 'http.json').write_text(json.dumps(server.requests, indent=2) + '\n')
    result['unavailable_server'] = 'refresh refused'
    result.update(target=args.target, ok=True, published_https=False,
                  key_rotation_and_expiry=False, final_release_transition=False,
                  fixture_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
                  repositories={name: hashlib.sha256(
                      (args.fixtures / name / 'repository.json').read_bytes()).hexdigest()
                                for name in ('first', 'second')})
    (args.output / 'result.json').write_text(json.dumps(result, indent=2) + '\n')
    print(json.dumps(result), flush=True)


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
    if json.loads((args.fixtures / 'first/repository.json').read_text())['target'] != args.target:
        raise RuntimeError('repository fixture belongs to another target')
    exercise(args)


if __name__ == '__main__':
    main()
