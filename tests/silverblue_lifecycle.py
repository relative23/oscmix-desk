"""Five-phase lifecycle check in an isolated Silverblue VM without audio hardware.

Run as root on oscmix-silverblue-qa with normal user tester (UID 12510),
the source at /root/oscmix-source, and two matching core/GTK RPM pairs in separate
directories. Start with prepare --initial DIR --upgrade DIR, then reboot
between installed, upgraded, rolled-back and removed. Each phase records
its result and prepares the next deployment. This never runs in pytest.
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
parser.add_argument('phase',
                    choices=['prepare', 'installed', 'upgraded', 'rolled-back', 'removed'])
parser.add_argument('--initial', type=Path, help='directory with the first core/GTK RPM pair')
parser.add_argument('--upgrade', type=Path, help='directory with the second core/GTK RPM pair')
args = parser.parse_args()
assert os.getuid() == 0
assert os.uname().nodename == 'oscmix-silverblue-qa'
assert not any(p.read_text().strip() == '2a39'
               for p in Path('/sys/bus/usb/devices').glob('*/idVendor'))
account = pwd.getpwnam('tester')
assert account.pw_uid == 12510
home = Path(account.pw_dir)
fence = Path('/var/lib/oscmix-desk/package-update')
guard = Path('/usr/lib/oscmix-desk/package-guard')
entry = '/usr/bin/oscmix-session'
state_file = Path('/root/oscmix-silverblue-lifecycle.json')
log_file = Path('/root/oscmix-silverblue-' + args.phase + '.log')
saved_guard = Path('/var/lib/oscmix-desk/removal-guard')

def run(argv, user=False, check=True, timeout=240):
    extra = {}
    if user:
        extra = dict(user=account.pw_uid, group=account.pw_gid,
                     extra_groups=os.getgrouplist('tester', account.pw_gid), cwd=home,
                     env=dict(HOME=str(home), USER='tester', LOGNAME='tester',
                              PATH='/usr/bin:/bin', LANG='C.UTF-8',
                              XDG_RUNTIME_DIR='/run/user/12510',
                              DBUS_SESSION_BUS_ADDRESS='unix:path=/run/user/12510/bus'))
    result = subprocess.run(argv, capture_output=True, text=True, timeout=timeout, **extra)
    with log_file.open('a') as log:
        log.write(json.dumps([str(a) for a in argv]) + '\n' + result.stdout + result.stderr + '\n')
    if check and result.returncode:
        raise RuntimeError(str(argv) + '\n' + result.stdout + result.stderr)
    return result

def service(*verb, check=True):
    return run(['systemctl', '--user', *verb, 'oscmix.service'], user=True, check=check)

def status():
    data = json.loads(run([entry, '--status', '--json'], user=True).stdout)
    assert data['read_only']
    assert data['verification'] == 'not-performed'
    return data['sections']['service']

def identity(pid):
    path = Path('/proc') / str(pid)
    argv = [os.fsdecode(a) for a in path.joinpath('cmdline').read_bytes().split(b'\0')[:-1]]
    assert argv[1:] == [entry], argv
    assert path.stat().st_uid == account.pw_uid
    fields = dict(line.split(':', 1) for line in path.joinpath('status').read_text().splitlines()
                  if ':' in line)
    groups = list(map(int, fields['Groups'].split()))
    assert grp.getgrnam('audio').gr_gid in groups
    # /proc exposes the Python argv before imports and handler setup finish.
    # Send reload only once it can actually be handled during discovery.
    caught = int(fields['SigCgt'].strip(), 16)
    assert caught & (1 << (signal.SIGHUP - 1))
    return dict(pid=pid, argv=argv, uid=account.pw_uid, groups=groups,
                hup_handler_installed=True)

def wait_pid(exclude=0):
    deadline = time.monotonic() + 12
    while time.monotonic() < deadline:
        pid = int(status().get('MainPID', 0))
        if pid and pid != exclude:
            try:
                return identity(pid)
            except (FileNotFoundError, AssertionError):
                pass
        time.sleep(.15)
    raise AssertionError('no expected supervisor')

def start():
    service('reset-failed')
    service('start', '--no-block')
    return wait_pid()

def user_state():
    base = home / '.config/oscmix'
    return {str(p): dict(sha256=hashlib.sha256(p.read_bytes()).hexdigest(),
                         uid=p.stat().st_uid, gid=p.stat().st_gid)
            for p in (base/'routing.conf', base/'profiles/qa-state.conf', base/'active-profile')
            if p.exists()}

def deployment():
    data = json.loads(run(['rpm-ostree', 'status', '--json']).stdout)
    assert data['transaction'] is None
    return next(d for d in data['deployments'] if d['booted'])

def binary_identity():
    series = hashlib.sha256(
        Path('/root/oscmix-source/patches/backend-series.json').read_bytes()).hexdigest()
    result = {}
    for name in ('oscmix', 'alsaseqio', 'oscmix-gtk'):
        path = Path('/usr/bin') / name
        assert run([path, '--desk-build-id'], user=True).stdout.strip() == series
        assert run([path, '--control-version'], user=True).stdout.strip() == 'ODK1'
        result[name] = hashlib.sha256(path.read_bytes()).hexdigest()
    return result


def package_pair(directory):
    assert directory is not None
    assert directory.is_absolute()
    rpms = sorted(directory.glob('*.rpm'))
    assert len(rpms) == 2
    records = [json.loads(path.with_suffix('.rpm.json').read_text()) for path in rpms]
    assert {record['component'] for record in records} == {'core', 'gtk'}
    assert len({record['package_version'] for record in records}) == 1
    for path, record in zip(rpms, records):
        assert hashlib.sha256(path.read_bytes()).hexdigest() == record['sha256']
    return dict(files=[str(path) for path in rpms], records=records)


def verify_pair(pair):
    for record in pair['records']:
        version = run(['rpm', '-q', '--queryformat', '%{VERSION}-%{RELEASE}',
                       record['package_name']]).stdout
        assert version == record['package_version']
        for name, digest in record['payload'].items():
            assert hashlib.sha256(Path('/' + name).read_bytes()).hexdigest() == digest, name

assert run(['getenforce']).stdout.strip() == 'Enforcing'
run(['systemd-run', '--unit=oscmix-qualification-login', '--collect',
     '--property=User=tester', '--property=PAMName=login', '--property=Type=exec',
     '/usr/bin/sleep', 'infinity'])
if args.phase == 'prepare':
    assert not state_file.exists()
    assert not Path(entry).exists()
    initial, upgrade = package_pair(args.initial), package_pair(args.upgrade)
    assert initial['records'][0]['package_version'] != upgrade['records'][0]['package_version']
    bootstrap = '/root/oscmix-source/packaging/package-guard'
    run(['python3', '-I', bootstrap, 'install'])
    run(['install', '-D', '-m', '700', bootstrap, saved_guard])
    state_data = dict(schema=1, scope='development, actual Silverblue, no audio hardware',
                      initial_packages=initial, upgrade_packages=upgrade,
                      base_deployment=deployment(),
                      before_install=user_state(),
                      boot=Path('/proc/sys/kernel/random/boot_id').read_text().strip(),
                      phase='prepared; reboot required')
    run(['rpm-ostree', 'install', *initial['files']])
    assert fence.exists()
    assert not Path(entry).exists()
elif args.phase == 'installed':
    state_data = json.loads(state_file.read_text())
    assert state_data['phase'] == 'prepared; reboot required'
    assert fence.exists()
    boot = Path('/proc/sys/kernel/random/boot_id').read_text().strip()
    assert boot != state_data['boot']
    current = deployment()
    assert current['checksum'] != state_data['base_deployment']['checksum']
    verify_pair(state_data['initial_packages'])
    assert not (home/'.config/oscmix/service-allowed').exists()
    assert status()['MainPID'] == '0'
    lock = Path('/run/oscmix-desk').stat()
    assert lock.st_uid == 0
    assert lock.st_gid == grp.getgrnam('audio').gr_gid
    assert lock.st_mode & 0o7777 == 0o3770
    for path, metadata in state_data['before_install'].items():
        assert user_state().get(path) == metadata
    if not (home/'.config/oscmix/routing.conf').exists():
        run(['/usr/bin/oscmix-setup', '--init-config'], user=True)
    run(['python3', '-c', '''from pathlib import Path
p=Path.home()/'.config/oscmix'
(p/'profiles').mkdir(exist_ok=True)
if not (p/'profiles/qa-state.conf').exists():
    (p/'profiles/qa-state.conf').write_bytes((p/'routing.conf').read_bytes())
if not (p/'active-profile').exists():
    (p/'active-profile').write_text('qa-state\\n')
'''], user=True)
    state = user_state()
    run([entry, '--dry-run', '--timeout', '0'], user=True)
    refused = run(['/usr/bin/oscmix-setup', '--enable'], user=True, check=False)
    assert refused.returncode
    assert 'transaction' in refused.stderr
    assert not (home/'.config/oscmix/service-allowed').exists()
    refused = run([entry, '--profile', 'qa-state'], user=True, check=False)
    assert refused.returncode == 2
    assert 'maintenance' in refused.stderr
    run([guard, 'finish'])
    # Enabling is a real setup invocation; without hardware it reaches the
    # normal clean absent-device result after the discovery timeout.
    start_time = time.monotonic()
    run(['/usr/bin/oscmix-setup', '--enable'], user=True)
    enable_seconds = time.monotonic() - start_time
    assert service('is-enabled', check=False).returncode == 0
    process = start()
    pid = process['pid']
    descriptor = os.pidfd_open(pid)
    try:
        signal.pidfd_send_signal(descriptor, signal.SIGHUP)
    finally:
        os.close(descriptor)
    time.sleep(.3)
    assert wait_pid()['pid'] == pid
    restarts = []
    for _ in range(3):
        identity(pid)
        descriptor = os.pidfd_open(pid)
        start_time = time.monotonic()
        try:
            signal.pidfd_send_signal(descriptor, signal.SIGKILL)
        finally:
            os.close(descriptor)
        changed = wait_pid(exclude=pid)['pid']
        seconds = time.monotonic() - start_time
        assert seconds >= 2.8
        restarts.append(dict(before=pid, after=changed, seconds=round(seconds, 3)))
        pid = changed
    refused = run([guard, 'install'], check=False)
    assert refused.returncode
    assert 'still running PIDs' in refused.stderr
    assert not fence.exists()
    service('stop')
    assert service('reload', check=False).returncode != 0
    assert status()['MainPID'] == '0'
    run([guard, 'install'])
    state_data.update(boot=boot, initial_deployment=current, user_state=state,
                      process=process, crash_restarts=restarts,
                      installed_inactive=True, persistent_fence_survived_install_reboot=True,
                      explicit_activation_fenced=True, running_maintenance_refused=True,
                      setup_enable_seconds=round(enable_seconds, 3), binaries=binary_identity(),
                      reload_kept_stopped=True, phase='installed; reboot required')
    run(['rpm-ostree', 'install', '--uninstall=oscmix-desk', '--uninstall=oscmix-desk-gtk',
         *state_data['upgrade_packages']['files']])
    assert fence.exists()
    verify_pair(state_data['initial_packages'])
elif args.phase in ('upgraded', 'rolled-back'):
    state_data = json.loads(state_file.read_text())
    assert fence.exists()
    assert status()['MainPID'] == '0'
    assert user_state() == state_data['user_state']
    current = deployment()
    if args.phase == 'upgraded':
        assert state_data['phase'] == 'installed; reboot required'
        assert current['checksum'] != state_data['initial_deployment']['checksum']
        verify_pair(state_data['upgrade_packages'])
        state_data['upgrade_deployment'] = current
    else:
        assert state_data['phase'] == 'upgraded; reboot required'
        assert current['checksum'] == state_data['initial_deployment']['checksum']
        verify_pair(state_data['initial_packages'])
        state_data['rollback_deployment'] = current
    binary_identity()
    run([guard, 'finish'])
    state_data[args.phase + '_process'] = start()
    service('stop')
    if args.phase == 'rolled-back':
        run(['/usr/bin/oscmix-setup', '--disable'], user=True)
        assert not (home/'.config/oscmix/service-allowed').exists()
        service('start')
        assert status()['MainPID'] == '0'
        state_data['disable_refused_start'] = True
    run([guard, 'install'])
    run(['rpm-ostree', 'rollback'] if args.phase == 'upgraded' else
        ['rpm-ostree', 'uninstall', 'oscmix-desk-gtk', 'oscmix-desk'])
    assert fence.exists()
    state_data['phase'] = args.phase + '; reboot required'
else:
    state_data = json.loads(state_file.read_text())
    assert state_data['phase'] == 'rolled-back; reboot required'
    assert fence.exists()
    assert not Path(entry).exists()
    assert not guard.exists()
    assert user_state() == state_data['user_state']
    assert not (home/'.config/oscmix/service-allowed').exists()
    run([saved_guard, 'finish'])
    assert not fence.exists()
    run([saved_guard, 'check'])
    state_data.update(phase='passed', removal_preserved_user_state=True,
                      final_deployment=deployment())
state_file.write_text(json.dumps(state_data, indent=2) + '\n')
print(json.dumps(state_data, indent=2), flush=True)
run(['systemctl', 'stop', 'oscmix-qualification-login.service'])
