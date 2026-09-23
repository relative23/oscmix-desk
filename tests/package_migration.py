"""Actual 0.7.0 source payload -> installed package -> source, with a fake bus.

Run by the native container check as an ordinary user. This checks the
installed setup CLI and file recovery on each distribution; real user
manager behavior is qualified separately in a VM.
"""

import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

from test_install_sh import make_fake_home

if not Path('/.dockerenv').is_file() or os.getuid() == 0:
    raise RuntimeError('migration checks require an ordinary user in a disposable container')

root = Path(tempfile.mkdtemp(prefix='oscmix-native-migration-'))
home, env, _log = make_fake_home(root)
env['PATH'] = str(home / '.local/bin') + ':' + env['PATH']
env['XDG_STATE_HOME'] = str(home / '.local/state')
proc = root / 'proc'
(proc / 'asound/seq').mkdir(parents=True)
(proc / 'asound/cards').touch()
(proc / 'asound/seq/clients').touch()
env['OSCMIX_PROC_ROOT'] = str(proc)
stub = root / 'stub-bin/systemctl'
stub.write_text('''#!/bin/sh
case "$*" in
  *show-environment*) echo "HOME=$HOME"; echo "XDG_CONFIG_HOME=$XDG_CONFIG_HOME" ;;
  *is-active*|*is-enabled*) exit 3 ;;
esac
exit 0
''')
for name in ('oscmix', 'alsaseqio'):
    shutil.copy2(Path('/usr/bin') / name, home / '.local/bin' / name)
(home / '.local/bin/oscmix-gtk').unlink()


def run(*args):
    return subprocess.run(args, env=env, capture_output=True, text=True, check=True,
                          timeout=60).stdout


previous = root / 'previous'
previous.mkdir()
archive = subprocess.run(['git', '-C', '/work', 'archive',
                          '25eb57dff7a305b0e6452010ce15992c90ad144b'],
                         capture_output=True, check=True).stdout
subprocess.run(['tar', '-xf', '-', '-C', str(previous)], input=archive, check=True)
run('bash', str(previous / 'install.sh'), '--no-build', '--no-udev')
assert run(str(home / '.local/bin/oscmix-session'), '--version').strip() == '0.7.0'
desk = home / '.config/oscmix'
(desk / 'profiles').mkdir(exist_ok=True)
preserved = {
    desk / 'routing.conf': '[device]\nname = Fireface UCX II\n',
    desk / 'profiles/tracking.conf': '[route:desk]\nplayback = 1/2\noutput = 5/6\n',
    desk / 'active-profile': 'tracking\n',
}
for path, content in preserved.items():
    path.write_text(content)
    path.chmod(0o640)
old = {path: (path.read_bytes(), path.stat().st_mode)
       for directory in (home / '.local/lib/oscmix-desk', home / '.local/bin',
                         home / '.config/systemd/user')
       for path in directory.rglob('*') if path.is_file()}
output = run('/usr/bin/oscmix-setup', '--migrate-source')
print(output, end='')
assert not (home / '.local/bin/oscmix-session').exists()
assert not (home / '.config/systemd/user/oscmix.service').exists()
native_version = json.loads(Path('/usr/share/oscmix-desk/package.json').read_text())['version']
assert run('oscmix-session', '--version').strip() == native_version
backup = next((home / '.local/state/oscmix-desk').glob('source-install-*'))
print(run('/usr/bin/oscmix-setup', '--restore-source', str(backup)), end='')
for path, (content, mode) in old.items():
    assert path.read_bytes() == content, path
    assert path.stat().st_mode == mode, path
for path, content in preserved.items():
    assert path.read_text() == content, path
    assert path.stat().st_mode & 0o777 == 0o640, path
assert run('oscmix-session', '--version').strip() == '0.7.0'
print(run('oscmix-session', '--dry-run', '--timeout', '0'), end='')
print('ACTUAL 0.7.0 SOURCE / INSTALLED NATIVE SETUP / SOURCE RECOVERY PASSED (simulated user bus)')
