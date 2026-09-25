"""Development lifecycle evidence in a disposable, hardware-free native VM."""
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

p = argparse.ArgumentParser()
p.add_argument('phase', choices=('before-reboot', 'after-reboot'))
args = p.parse_args()
assert os.getuid() == 0
assert Path('/etc/hostname').read_text().strip() in ('oscmix-alpine-qa', 'oscmix-void-qa')
assert not any(path.read_text().strip() == '2a39'
               for path in Path('/sys/bus/usb/devices').glob('*/idVendor'))
record = json.loads(Path('/etc/oscmix-desk/service.json').read_text())
account = pwd.getpwnam(record['user'])
assert account.pw_name == 'tester'
assert account.pw_uid == 12510
home = Path(record['home'])
config = Path(record['config'])
admin = '/usr/local/sbin/oscmix-service'
entry = record['command']
fence = Path('/var/lib/oscmix-desk/service-update')
result_path = Path('/root/native-absent-result.json')


def command(arguments, user=False, check=True, timeout=45):
    extra = {}
    if user:
        extra = dict(user=account.pw_uid, group=account.pw_gid,
                     extra_groups=os.getgrouplist(account.pw_name, account.pw_gid),
                     env=dict(HOME=str(home), USER=account.pw_name, LOGNAME=account.pw_name,
                              XDG_CONFIG_HOME=str(config.parent.parent),
                              PATH=str(home / '.local/bin') + ':/usr/local/bin:/usr/bin:/bin',
                              LANG='C.UTF-8'), cwd=home)
    completed = subprocess.run(arguments, capture_output=True, text=True,
                               timeout=timeout, **extra)
    if check and completed.returncode:
        raise RuntimeError(str(arguments) + '\n' + completed.stdout + completed.stderr)
    return completed


def status():
    report = json.loads(command([entry, '--status', '--json', '--config', str(config)],
                                user=True).stdout)
    assert report['read_only']
    assert report['verification'] == 'not-performed'
    return report['sections']['service']


def wait_pid(exclude=None, timeout=15):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        report = status()
        pid = int(report.get('MainPID', '0'))
        if pid and pid != exclude:
            assert report['ActiveState'] == 'active', report
            return pid
        time.sleep(0.15)
    raise AssertionError('no registered desk process: ' + json.dumps(report))


def identity(pid):
    proc = Path('/proc') / str(pid)
    assert proc.stat().st_uid == account.pw_uid
    argv = proc.joinpath('cmdline').read_bytes().split(b'\0')[:-1]
    assert argv[-3:] == [os.fsencode(entry), b'--config', os.fsencode(config)], argv
    fields = dict(line.split(':', 1) for line in proc.joinpath('status').read_text().splitlines()
                  if ':' in line)
    assert all(int(uid) == account.pw_uid for uid in fields['Uid'].split())
    assert grp.getgrnam('audio').gr_gid in map(int, fields['Groups'].split())
    return dict(pid=pid, uid=account.pw_uid, groups=fields['Groups'].split(),
                argv=[os.fsdecode(value) for value in argv])


def user_state():
    paths = [config, config.parent / 'profiles/qa-state.conf', config.parent / 'active-profile']
    return {str(path): dict(sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
                            uid=path.stat().st_uid, gid=path.stat().st_gid)
            for path in paths}


if args.phase == 'before-reboot':
    command(['python3', '-c',
             'from pathlib import Path; p=Path(' + repr(str(config)) + '); '
             'd=p.parent/"profiles"; d.mkdir(exist_ok=True); '
             '(d/"qa-state.conf").write_bytes(p.read_bytes()); '
             '(p.parent/"active-profile").write_text("qa-state\\n")'], user=True)
    before = user_state()
    command([admin, 'enable'])
    pid = wait_pid()
    initial = identity(pid)
    # Let the fresh Python entry point finish importing and enter discovery.
    time.sleep(0.3)
    command([admin, 'reload'])
    script = ('import sys; sys.path.insert(0, ' + repr(str(home / '.local/lib/oscmix-desk'))
              + '); from oscmix_desk.process import reload_service; print(reload_service())')
    assert command(['python3', '-c', script], user=True).stdout.strip() == 'reloaded'
    time.sleep(0.3)
    assert wait_pid() == pid
    restarts = []
    for _ in range(3):
        identity(pid)
        fd = os.pidfd_open(pid)
        try:
            assert wait_pid() == pid
            start = time.monotonic()
            signal.pidfd_send_signal(fd, signal.SIGKILL)
        finally:
            os.close(fd)
        changed = wait_pid(exclude=pid)
        delay = time.monotonic() - start
        assert delay >= 4.5, delay
        restarts.append(dict(before=pid, after=changed, seconds=round(delay, 3)))
        pid = changed
    denied = command(['bash', '/work/install.sh', '--manual', '--no-udev', '--no-build'],
                     user=True, check=False)
    assert denied.returncode
    assert 'maintenance-begin' in denied.stdout + denied.stderr
    assert user_state() == before
    command([admin, 'maintenance-begin'])
    assert int(status().get('MainPID', '0')) == 0
    assert json.loads(fence.read_text())['stopped'] is True
    refused = command([entry, '--profile', 'qa-state', '--config', str(config)],
                      user=True, check=False)
    assert refused.returncode == 2
    assert 'maintenance' in refused.stderr
    command(['bash', '/work/install.sh', '--manual', '--no-udev', '--no-build'], user=True)
    command(['python3', '/work/scripts/install-service.py', 'update'])
    assert fence.exists()
    assert command([admin, 'start'], check=False).returncode != 0
    assert user_state() == before
    result = dict(schema=1, scope='development, actual manager, no connected audio hardware',
                  manager=record['manager'], kernel=os.uname().release,
                  boot_before=Path('/proc/sys/kernel/random/boot_id').read_text().strip(),
                  process=initial, crash_restarts=restarts, user_state=before,
                  reload_during_discovery=True, activation_during_maintenance_refused=True,
                  source_replacement_while_running_refused=True,
                  source_and_adapter_replacement_while_fenced=True,
                  phase='maintenance awaiting reboot')
else:
    result = json.loads(result_path.read_text())
    assert result['boot_before'] != Path('/proc/sys/kernel/random/boot_id').read_text().strip()
    assert fence.exists()
    assert int(status().get('MainPID', '0')) == 0
    assert user_state() == result['user_state']
    command([admin, 'maintenance-finish'])
    pid = wait_pid()
    identity(pid)
    assert not fence.exists()
    start = time.monotonic()
    command([admin, 'stop'])
    stop_seconds = time.monotonic() - start
    assert stop_seconds < 20
    assert int(status().get('MainPID', '0')) == 0
    command([admin, 'resume'])
    assert int(status().get('MainPID', '0')) == 0
    command([admin, 'start'])
    wait_pid()
    command([admin, 'disable'])
    assert command([admin, 'start'], check=False).returncode
    command([admin, 'remove'])
    assert not Path('/etc/oscmix-desk/service.json').exists()
    assert Path('/run/oscmix-desk').is_dir()
    assert user_state() == result['user_state']
    command(['python3', '/work/scripts/install-service.py', 'install', '--manager',
             record['manager'], '--user', account.pw_name])
    assert json.loads(command([admin, 'status']).stdout)['enabled'] is False
    command([admin, 'enable'])
    identity(wait_pid())
    result.update(phase='passed', maintenance_survived_reboot=True,
                  stop_seconds=round(stop_seconds, 3), resume_kept_stopped=True,
                  disable_remove_and_reregister_preserved_user_state=True,
                  final_boot=Path('/proc/sys/kernel/random/boot_id').read_text().strip(),
                  final_kernel=os.uname().release)
result_path.write_text(json.dumps(result, indent=2) + '\n')
print(json.dumps(result, indent=2))
