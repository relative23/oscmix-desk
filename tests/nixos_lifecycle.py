"""Two-phase lifecycle qualification in an isolated NixOS VM without audio hardware.

Run as root, with the source at /work and normal user tester (UID 12510).
Reboot the VM between before-reboot and after-reboot. This deliberately
changes the disposable VM's NixOS configuration and never runs in pytest.
"""

import argparse
import grp
import hashlib
import json
import os
import pwd
import signal
import subprocess
import time
from pathlib import Path

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('phase', choices=('before-reboot', 'after-reboot'))
args = parser.parse_args()
assert os.getuid() == 0
assert os.uname().nodename == 'oscmix-nixos-qa'
assert not any(path.read_text().strip() == '2a39'
               for path in Path('/sys/bus/usb/devices').glob('*/idVendor'))
account = pwd.getpwnam('tester')
assert account.pw_uid == 12510
home = Path(account.pw_dir)
entry = Path('/run/current-system/sw/bin/oscmix-session')
guard = Path('/run/current-system/sw/bin/oscmix-package-guard')
fence = Path('/var/lib/oscmix-desk/package-update')
qa = Path('/etc/nixos/oscmix-qualification.nix')
result_path = Path('/root/nixos-lifecycle.json')
transcript = Path('/root/nixos-lifecycle-' + args.phase + '.log')


def command(argv, user=False, check=True):
    extra = {}
    if user:
        extra = dict(user=account.pw_uid, group=account.pw_gid,
                     extra_groups=os.getgrouplist(account.pw_name, account.pw_gid), cwd=home,
                     env=dict(HOME=str(home), USER='tester', LOGNAME='tester',
                              PATH='/run/current-system/sw/bin', LANG='C.UTF-8',
                              XDG_RUNTIME_DIR='/run/user/12510',
                              DBUS_SESSION_BUS_ADDRESS='unix:path=/run/user/12510/bus'))
    result = subprocess.run(argv, capture_output=True, text=True, timeout=240, **extra)
    with transcript.open('a') as log:
        log.write(json.dumps([str(arg) for arg in argv]) + '\n')
        log.write(result.stdout + result.stderr + '\n')
    if check and result.returncode:
        raise RuntimeError(str(argv) + '\n' + result.stdout + result.stderr)
    return result


def service(*verb, **kwargs):
    return command(['systemctl', '--user', *verb, 'oscmix.service'], user=True, **kwargs)


def status():
    report = json.loads(command([entry, '--status', '--json'], user=True).stdout)
    assert report['read_only']
    assert report['verification'] == 'not-performed'
    return report['sections']['service']


def identity(pid):
    proc = Path('/proc') / str(pid)
    argv = [os.fsdecode(arg) for arg in proc.joinpath('cmdline').read_bytes().split(b'\0')[:-1]]
    package = entry.resolve().parent.parent
    assert argv[1:] == [str(package / 'libexec/oscmix-session')], argv
    assert proc.stat().st_uid == account.pw_uid
    fields = dict(line.split(':', 1) for line in proc.joinpath('status').read_text().splitlines()
                  if ':' in line)
    assert grp.getgrnam('audio').gr_gid in map(int, fields['Groups'].split())
    return dict(pid=pid, argv=argv, uid=account.pw_uid,
                groups=fields['Groups'].split(), package=str(package))


def wait_pid(exclude=0):
    deadline = time.monotonic() + 12
    while time.monotonic() < deadline:
        report = status()
        pid = int(report.get('MainPID', 0))
        if pid and pid != exclude:
            try:
                identity(pid)
            except (OSError, AssertionError):
                pass  # systemd reports MainPID before the child has exec'd
            else:
                return pid
        time.sleep(0.15)
    raise AssertionError('no expected supervisor: ' + json.dumps(report))


def start_identity():
    service('reset-failed')
    service('start', '--no-block')
    return identity(wait_pid())


def user_state():
    base = home / '.config/oscmix'
    return {str(path): dict(sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
                            uid=path.stat().st_uid, gid=path.stat().st_gid)
            for path in (base / 'routing.conf', base / 'profiles/qa-state.conf',
                         base / 'active-profile')}


# A real PAM/logind session keeps the manager reachable when this test disables
# lingering. Starting user@ directly races removal of its runtime directory.
command(['systemd-run', '--unit=oscmix-qualification-login', '--collect',
         '--property=User=tester', '--property=PAMName=login', '--property=Type=exec',
         '/run/current-system/sw/bin/sleep', 'infinity'])

if args.phase == 'before-reboot':
    assert not result_path.exists()
    assert not qa.exists()
    assert not Path('/etc/oscmix-desk/nixos.json').exists()
    configuration = Path('/etc/nixos/configuration.nix')
    backup = configuration.with_name('before-oscmix-qualification.nix')
    assert not backup.exists()
    backup.write_bytes(configuration.read_bytes())
    original = configuration.read_text()
    assert './hardware-configuration.nix' in original
    configuration.write_text(original.replace('./hardware-configuration.nix',
                                              './hardware-configuration.nix ./' + qa.name))
    qa.write_text('''{ ... }: {
  imports = [ /work/packaging/nix/module.nix ];
  services.oscmix-desk = {
    enable = true;
    user = "tester";
    withGtk = false;
    activate = false;
  };
}
''')
    # Refuse a user override that would bypass the global unit's activation.
    command(['python3', '-c', '''from pathlib import Path
p=Path.home()/'.config/systemd/user/oscmix.service.d'
assert not p.exists()
p.mkdir(parents=True)
(p/'qa.conf').write_text('[Service]\\nEnvironment=QA=1\\n')
'''], user=True)
    refused = command(['nixos-rebuild', 'switch'], check=False)
    assert refused.returncode
    assert 'migrate the existing user unit' in refused.stderr
    command(['python3', '-c', '''from pathlib import Path
p=Path.home()/'.config/systemd/user/oscmix.service.d'
(p/'qa.conf').unlink()
p.rmdir()
'''], user=True)
    command(['nixos-rebuild', 'switch'])
    command(['python3', '-c', '''from pathlib import Path
p=Path.home()/'.config/oscmix'
p.mkdir(parents=True, exist_ok=True)
sample=Path('/run/current-system/sw/share/oscmix-desk/routing.conf.example').read_bytes()
if not (p/'routing.conf').exists():
    (p/'routing.conf').write_bytes(sample)
(p/'profiles').mkdir(exist_ok=True)
if not (p/'profiles/qa-state.conf').exists():
    (p/'profiles/qa-state.conf').write_bytes((p/'routing.conf').read_bytes())
if not (p/'active-profile').exists():
    (p/'active-profile').write_text('qa-state\\n')
'''], user=True)
    state = user_state()
    command([entry, '--dry-run', '--timeout', '0'], user=True)
    service('start')
    assert status()['MainPID'] == '0'
    assert not Path('/etc/oscmix-desk/nixos-active').exists()
    qa.write_text(qa.read_text().replace('activate = false;', 'activate = true;'))
    refused = command(['nixos-rebuild', 'switch'], check=False)
    assert refused.returncode
    assert 'before changing this deployment' in refused.stderr
    assert not Path('/etc/oscmix-desk/nixos-active').exists()
    command([guard, 'install'])
    command(['nixos-rebuild', 'switch'])
    assert fence.exists()
    service('start')
    assert status()['MainPID'] == '0'
    command([guard, 'finish'])
    process = start_identity()
    pid = process['pid']
    time.sleep(0.4)
    descriptor = os.pidfd_open(pid)
    try:
        assert wait_pid() == pid
        signal.pidfd_send_signal(descriptor, signal.SIGHUP)
    finally:
        os.close(descriptor)
    time.sleep(0.3)
    assert wait_pid() == pid
    restarts = []
    for _ in range(3):
        identity(pid)
        descriptor = os.pidfd_open(pid)
        start = time.monotonic()
        try:
            signal.pidfd_send_signal(descriptor, signal.SIGKILL)
        finally:
            os.close(descriptor)
        changed = wait_pid(exclude=pid)
        elapsed = time.monotonic() - start
        assert elapsed >= 2.8, elapsed
        restarts.append(dict(before=pid, after=changed, seconds=round(elapsed, 3)))
        pid = changed
    refused = command([guard, 'install'], check=False)
    assert refused.returncode
    assert 'still running PIDs' in refused.stderr
    assert not fence.exists()
    service('stop')
    command([guard, 'install'])
    refused = command([entry, '--profile', 'qa-state'], user=True, check=False)
    assert refused.returncode == 2
    assert 'maintenance' in refused.stderr
    core_generation = os.readlink('/run/current-system')
    qa.write_text(qa.read_text().replace('withGtk = false;', 'withGtk = true;'))
    command(['nixos-rebuild', 'switch'])
    assert fence.exists()
    assert status()['MainPID'] == '0'
    assert state == user_state()
    result = dict(schema=1, scope='development, actual NixOS, no audio hardware',
                  shadowing_user_unit_refused=True, installed_inactive=True,
                  explicit_activation_fenced=True, running_maintenance_refused=True,
                  process=process, crash_restarts=restarts, user_state=state,
                  core_generation=core_generation,
                  boot=Path('/proc/sys/kernel/random/boot_id').read_text().strip(),
                  phase='maintenance awaiting reboot')
else:
    result = json.loads(result_path.read_text())
    boot = Path('/proc/sys/kernel/random/boot_id').read_text().strip()
    assert boot != result['boot']
    assert fence.exists()
    assert status()['MainPID'] == '0'
    assert user_state() == result['user_state']
    package = entry.resolve().parent.parent
    assert str(package) != result['process']['package']
    series = hashlib.sha256(Path('/work/patches/backend-series.json').read_bytes()).hexdigest()
    for binary in ('oscmix', 'alsaseqio', 'oscmix-gtk'):
        assert command([package / 'bin' / binary, '--desk-build-id']).stdout.strip() == series
        assert command([package / 'bin' / binary, '--control-version']).stdout.strip() == 'ODK1'
    command([guard, 'finish'])
    gtk_process = start_identity()
    # The same globally installed unit is unavailable to the root user.
    command(['systemctl', '--user', 'start', 'oscmix.service'])
    assert command(['systemctl', '--user', 'show', '-p', 'MainPID', '--value',
                    'oscmix.service']).stdout.strip() == '0'
    service('stop')
    command([guard, 'install'])
    command(['nixos-rebuild', 'switch', '--rollback'])
    assert os.readlink('/run/current-system') == result['core_generation']
    assert str(entry.resolve().parent.parent) == result['process']['package']
    assert fence.exists()
    assert status()['MainPID'] == '0'
    assert user_state() == result['user_state']
    command([guard, 'finish'])
    core_process = start_identity()
    start = time.monotonic()
    service('stop')
    stop_seconds = time.monotonic() - start
    assert stop_seconds < 12
    assert service('reload', check=False).returncode != 0
    assert status()['MainPID'] == '0'
    command([guard, 'install'])
    qa.write_text(qa.read_text().replace('withGtk = true;', 'withGtk = false;')
                  .replace('activate = true;', 'activate = false;'))
    command(['nixos-rebuild', 'switch'])
    assert not Path('/etc/oscmix-desk/nixos-active').exists()
    command([guard, 'finish'])
    service('start')
    assert status()['MainPID'] == '0'
    command([guard, 'install'])
    old_guard = guard.resolve()
    qa.write_text(qa.read_text().replace('enable = true;', 'enable = false;'))
    command(['nixos-rebuild', 'switch'])
    assert not entry.exists()
    assert not Path('/etc/oscmix-desk/nixos.json').exists()
    assert fence.exists()
    command([old_guard, 'finish'])
    assert not fence.exists()
    assert user_state() == result['user_state']
    command([old_guard, 'check'])
    result.update(phase='passed', reboot=boot, maintenance_survived_reboot=True,
                  gtk_generation=gtk_process, rollback_generation=core_process,
                  foreign_user_activation_refused=True, stop_seconds=round(stop_seconds, 3),
                  reload_kept_stopped=True, disable_refused_start=True,
                  removal_preserved_user_state=True)
result_path.write_text(json.dumps(result, indent=2) + '\n')
print(json.dumps(result, indent=2), flush=True)
command(['systemctl', 'stop', 'oscmix-qualification-login.service'])
