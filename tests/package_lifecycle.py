"""Actual core/GTK package transitions; run only in a disposable CI container."""

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
FENCE = Path('/var/lib/oscmix-desk/package-update')
GTK_FENCE = FENCE.with_name('gtk-package-update')


def run(args, expected=0):
    result = subprocess.run(args, capture_output=True, text=True, timeout=120)
    item = dict(command=[str(arg) for arg in args], exit=result.returncode,
                stdout=result.stdout, stderr=result.stderr)
    print(json.dumps(item), flush=True)
    if expected is not None:
        assert result.returncode == expected, item
    return result


def artifact(directory, gtk=False):
    suffix = {'deb': '*.deb', 'rpm': '*.rpm', 'arch': '*.pkg.tar.zst'}[kind]
    paths = [path for path in Path('/work/build/qualification', directory).glob(suffix)
             if path.name.startswith('oscmix-desk-gtk') is gtk]
    assert len(paths) == 1, paths
    return paths[0]


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def install(*paths, expected=0):
    command = {'deb': ['dpkg', '-i'], 'rpm': ['rpm', '-Uvh', '--oldpackage', '--replacepkgs'],
               'arch': ['pacman', '-U', '--noconfirm']}[kind]
    return run(command + [str(path) for path in paths], expected)


def remove(*names, expected=0):
    command = {'deb': ['dpkg', '--purge'], 'rpm': ['rpm', '-e'],
               'arch': ['pacman', '-R', '--noconfirm']}[kind]
    return run(command + list(names), expected)


def installed_revision(gtk=False):
    name = 'gtk-package.json' if gtk else 'package.json'
    return json.loads(Path('/usr/share/oscmix-desk', name).read_text())['package_revision']


def cli():
    version = json.loads(Path('/usr/share/oscmix-desk/package.json').read_text())['version']
    assert run(['/usr/bin/oscmix-session', '--version']).stdout.strip() == version


def payload(package):
    return json.loads(package.with_suffix(package.suffix + '.json').read_text())['payload']


def verify_files(package):
    for relative, digest in payload(package).items():
        path = Path('/') / relative
        if relative.startswith('usr/share/doc/') and not path.exists():
            print(json.dumps({'optional_docs_excluded_by_container_policy': relative}), flush=True)
            continue
        assert sha(path) == digest, relative


previous = artifact('previous')
first, repeated, second = [artifact(name) for name in ('package', 'repeated', 'upgraded')]
gtk, gtk_repeated, gtk_second = [artifact(name, True)
                                for name in ('package', 'repeated', 'upgraded')]
assert not payload(first).keys() & payload(gtk).keys(), 'core and GTK own the same file'
for a, b in ((first, repeated), (gtk, gtk_repeated)):
    equal = sha(a) == sha(b)
    print(json.dumps(dict(reproducible=equal, artifact=a.name,
                          first=sha(a), repeated=sha(b))), flush=True)
    assert equal, 'independent builds were not byte-identical'

config = Path('/home/tester/.config/oscmix')
(config / 'profiles').mkdir(parents=True, exist_ok=True)
preserved = {config / 'routing.conf': '# custom routing\n',
             config / 'profiles/tracking.conf': '# custom profile\n',
             config / 'active-profile': 'tracking\n'}
for path, content in preserved.items():
    path.write_text(content)
install(previous)
verify_files(previous)
cli()
install(first)
verify_files(first)
cli()
assert not Path('/usr/bin/oscmix-gtk').exists()
assert not (config / 'service-allowed').exists()
run(['runuser', '-u', 'tester', '--', '/opt/qa/bin/python', 'tests/package_migration.py'])
install(gtk)
verify_files(gtk)
verify_files(first)
assert installed_revision() == 1
assert not FENCE.exists()
assert not GTK_FENCE.exists()
# dpkg can unpack before checking Depends: the GTK fence must then stay
# until a matching package is successfully configured.
rejected = install(gtk_second, expected=None)
assert rejected.returncode != 0
install(gtk)
assert not GTK_FENCE.exists()
remove('oscmix-desk-gtk')
assert not Path('/usr/bin/oscmix-gtk').exists()
assert not Path('/usr/share/glib-2.0/schemas/oscmix.gschema.xml').exists()
verify_files(first)
install(gtk)
run(['runuser', '-u', 'tester', '--', 'env', 'OSCMIX_QUALIFY_DESKTOP=1', 'GDK_BACKEND=x11',
     'XDG_CURRENT_DESKTOP=Xvfb', 'dbus-run-session', '--', 'xvfb-run', '-a',
     '/usr/bin/python3', 'tests/gtk_lifecycle.py', '--gtk', '/usr/bin/oscmix-gtk',
     '--schema', '/usr/share/glib-2.0/schemas/oscmix.gschema.xml',
     '--output', '/work/build/qualification/desktop'])
if 'ID=ubuntu' in Path('/etc/os-release').read_text():
    for desktop in ('gnome', 'kde', 'xfce'):
        run(['runuser', '-u', 'tester', '--', 'env', 'OSCMIX_QUALIFY_DESKTOP=1',
             'xvfb-run', '-a', 'dbus-run-session', '--', '/usr/bin/python3',
             'tests/desktop_session.py', '--desktop', desktop,
             '--output', '/work/build/qualification/desktop-' + desktop])
preserved[config / 'service-allowed'] = 'activation opt-in fixture\n'
(config / 'service-allowed').write_text(preserved[config / 'service-allowed'])
helper = Path(tempfile.mkdtemp(prefix='oscmix-lifecycle-')) / 'oscmix-gtk'
helper.write_text('#!/usr/bin/python3\nimport time\ntime.sleep(60)\n')
helper.chmod(0o755)
busy = subprocess.Popen([str(helper)])
try:
    time.sleep(0.2)
    refusal = install(second, gtk_second, expected=None)
    assert refusal.returncode != 0
    assert 'still running PIDs' in refusal.stderr + refusal.stdout
    assert installed_revision() == 1
    assert installed_revision(True) == 1
    refused_remove = remove('oscmix-desk-gtk', expected=None)
    assert refused_remove.returncode != 0
finally:
    busy.terminate()
    busy.wait(timeout=5)
install(second, gtk_second)
verify_files(second)
verify_files(gtk_second)
assert installed_revision() == 2
assert installed_revision(True) == 2
cli()
assert not FENCE.exists()
assert not GTK_FENCE.exists()
FENCE.touch()
GTK_FENCE.touch()
install(gtk_second)
assert FENCE.exists(), 'GTK repair cleared the incomplete core transaction'
assert not GTK_FENCE.exists()
install(second)
assert not FENCE.exists()
install(first, gtk)
assert installed_revision() == 1
assert installed_revision(True) == 1
cli()
remove('oscmix-desk-gtk')
install(previous)
verify_files(previous)
cli()
assert all(path.read_text() == content for path, content in preserved.items())
install(first, gtk)
verify_files(first)
verify_files(gtk)
remove('oscmix-desk-gtk')
remove('oscmix-desk')
assert not Path('/usr/bin/oscmix-session').exists()
assert not FENCE.exists()
assert not GTK_FENCE.exists()
assert all(path.read_text() == content for path, content in preserved.items())
remaining = list(Path('/usr/lib/oscmix-desk').rglob('*'))
assert not any(path.is_file() for path in remaining), remaining
print(json.dumps(dict(lifecycle='passed', reproducible=True, companion=True,
                      actual_previous_version=previous.name)), flush=True)
