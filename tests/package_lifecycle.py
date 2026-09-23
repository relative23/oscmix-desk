"""Actual package-manager transitions; run only in a disposable CI container."""

import hashlib
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

if not Path('/.dockerenv').is_file() or os.getuid() != 0:
    raise RuntimeError('package lifecycle checks require a disposable root container')
kind = sys.argv[1]
record = []


def run(args, expected=0):
    result = subprocess.run(args, capture_output=True, text=True, timeout=120)
    item = dict(command=[str(arg) for arg in args], exit=result.returncode,
                stdout=result.stdout, stderr=result.stderr)
    record.append(item)
    print(json.dumps(item), flush=True)
    if expected is not None:
        assert result.returncode == expected, item
    return result


def artifact(directory):
    suffix = {'deb': '*.deb', 'rpm': '*.rpm', 'arch': '*.pkg.tar.zst'}[kind]
    paths = list(Path(directory).glob(suffix))
    assert len(paths) == 1, paths
    return paths[0]


first, repeated, second = [artifact('/work/build/qualification/' + directory)
                            for directory in ('package', 'repeated', 'upgraded')]
def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()

reproducible = sha(first) == sha(repeated)
print(json.dumps(dict(reproducible=reproducible, first=sha(first), repeated=sha(repeated))),
      flush=True)

def install(path, expected=0):
    command = {'deb': ['dpkg', '-i'], 'rpm': ['rpm', '-Uvh', '--oldpackage', '--replacepkgs'],
               'arch': ['pacman', '-U', '--noconfirm']}[kind]
    return run(command + [str(path)], expected)


def installed_revision():
    return json.loads(Path('/usr/share/oscmix-desk/package.json').read_text())['package_revision']


def cli():
    version = json.loads(Path('/usr/share/oscmix-desk/package.json').read_text())['version']
    assert run(['/usr/bin/oscmix-session', '--version']).stdout.strip() == version


def verify_files(package):
    receipt = json.loads(package.with_suffix(package.suffix + '.json').read_text())
    for relative, digest in receipt['payload'].items():
        path = Path('/') / relative
        if relative.startswith('usr/share/doc/') and not path.exists():
            print(json.dumps({'optional_docs_excluded_by_container_policy': relative}), flush=True)
            continue
        assert sha(path) == digest, relative


config = Path('/home/tester/.config/oscmix')
(config / 'profiles').mkdir(parents=True, exist_ok=True)
preserved = {config / 'routing.conf': '# custom routing\n',
             config / 'profiles/tracking.conf': '# custom profile\n',
             config / 'active-profile': 'tracking\n'}
for path, content in preserved.items():
    path.write_text(content)
install(first)
verify_files(first)
cli()
run(['runuser', '-u', 'tester', '--', '/opt/qa/bin/python', 'tests/package_migration.py'])
assert installed_revision() == 1
assert not (config / 'service-allowed').exists()
assert not Path('/var/lib/oscmix-desk/package-update').exists()
helper = Path(tempfile.mkdtemp(prefix='oscmix-lifecycle-')) / 'oscmix'
helper.write_text('#!/usr/bin/python3\nimport time\ntime.sleep(60)\n')
helper.chmod(0o755)
busy = subprocess.Popen([str(helper)])
try:
    time.sleep(0.2)
    refusal = install(second, expected=None)
    assert refusal.returncode != 0, 'package manager reported success during running mixer'
    assert 'still running PIDs' in refusal.stderr + refusal.stdout
    assert installed_revision() == 1
finally:
    busy.terminate()
    busy.wait(timeout=5)
install(second)
verify_files(second)
assert installed_revision() == 2
cli()
assert not Path('/var/lib/oscmix-desk/package-update').exists()
# Emulate an interrupted transaction: an existing fence must be kept
# until an actual successful repair/reinstallation completes.
Path('/var/lib/oscmix-desk/package-update').touch()
install(second)
assert not Path('/var/lib/oscmix-desk/package-update').exists()
install(first)
assert installed_revision() == 1
cli()
remove = {'deb': ['dpkg', '--purge', 'oscmix-desk'],
          'rpm': ['rpm', '-e', 'oscmix-desk'],
          'arch': ['pacman', '-R', '--noconfirm', 'oscmix-desk']}[kind]
busy = subprocess.Popen([str(helper)])
try:
    time.sleep(0.2)
    result = run(remove, expected=None)
    assert result.returncode != 0
    assert installed_revision() == 1
finally:
    busy.terminate()
    busy.wait(timeout=5)
run(remove)
assert not Path('/usr/bin/oscmix-session').exists()
assert not Path('/var/lib/oscmix-desk/package-update').exists()
assert all(path.read_text() == content for path, content in preserved.items())
run(['find', '/usr/lib/oscmix-desk', '-type', 'f'], expected=None)
remaining = list(Path('/usr/lib/oscmix-desk').rglob('*'))
assert not any(path.is_file() for path in remaining), remaining
print(json.dumps(dict(lifecycle='passed', reproducible=reproducible)), flush=True)
assert reproducible, 'independent builds were not byte-identical'
