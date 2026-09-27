"""Actual systemd sleep/resume regression in the isolated Silverblue QA VM.

Run as root on oscmix-silverblue-qa, with the matching core/GTK development
RPMs already booted and their manifests in --packages. --output must be a new
directory directly under /var/lib. This replaces only the test user's session
ExecStart with a signal receiver; it does not qualify mixer or hardware I/O.
It is an explicit VM test, never part of pytest or a host installation.
"""

import argparse
import hashlib
import json
import os
import pwd
import shutil
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path


def receiver():
    assert os.getuid() == 12510
    count = 0

    def hup(_signum, _frame):
        nonlocal count
        count += 1
        print('QA_HUP count=' + str(count), flush=True)

    signal.signal(signal.SIGHUP, hup)
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    address = os.environ['NOTIFY_SOCKET']
    if address.startswith('@'):
        address = '\0' + address[1:]
    with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as notify:
        notify.sendto(b'READY=1\nSTATUS=QA signal receiver; no device I/O', address)
    print('QA_READY', flush=True)
    while True:
        signal.pause()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--signal', action='store_true')
    parser.add_argument('--watchdog', type=Path)
    parser.add_argument('--output', type=Path)
    parser.add_argument('--packages', type=Path)
    parser.add_argument('--cycles', type=int, default=3)
    args = parser.parse_args()
    assert os.uname().nodename == 'oscmix-silverblue-qa'
    assert not any(p.read_text().strip() == '2a39'
                   for p in Path('/sys/bus/usb/devices').glob('*/idVendor'))
    if args.signal:
        receiver()
        return
    assert os.getuid() == 0
    if args.watchdog:
        frozen = 'frozen 1' in Path('/sys/fs/cgroup/user.slice/cgroup.events').read_text()
        thawed = False
        if frozen:
            thawed = subprocess.run(['systemctl', 'thaw', 'user.slice'],
                                    check=False, timeout=10).returncode == 0
        args.watchdog.write_text(json.dumps(dict(frozen=frozen, thawed=thawed)) + '\n')
        return
    assert args.output
    assert args.output.parent == Path('/var/lib')
    assert args.packages
    assert 2 <= args.cycles <= 5
    args.output.mkdir(mode=0o755)
    args.output.chmod(0o755)
    script = args.output / 'probe.py'
    shutil.copyfile(__file__, script)
    script.chmod(0o644)
    account = pwd.getpwnam('tester')
    assert account.pw_uid == 12510
    home = Path(account.pw_dir)

    def run(argv, *, user=False, check=True, timeout=60):
        extra = {}
        if user:
            extra = dict(user=account.pw_uid, group=account.pw_gid,
                         extra_groups=os.getgrouplist('tester', account.pw_gid), cwd=home,
                         env=dict(HOME=str(home), USER='tester', LOGNAME='tester',
                                  PATH='/usr/bin:/bin', LANG='C.UTF-8',
                                  XDG_RUNTIME_DIR='/run/user/12510',
                                  DBUS_SESSION_BUS_ADDRESS='unix:path=/run/user/12510/bus'))
        argv = list(map(str, argv))
        result = subprocess.run(argv, capture_output=True, text=True, timeout=timeout, **extra)
        with (args.output / 'commands.log').open('a') as log:
            log.write(json.dumps(argv) + '\n' + result.stdout + result.stderr + '\n')
        if check and result.returncode:
            raise RuntimeError(str(argv) + '\n' + result.stdout + result.stderr)
        return result

    def state():
        return {str(p.relative_to(home)): dict(
            sha256=hashlib.sha256(p.read_bytes()).hexdigest(), uid=p.stat().st_uid,
            gid=p.stat().st_gid, mode=p.stat().st_mode & 0o7777)
            for p in (home / '.config/oscmix').rglob('*') if p.is_file()}

    assert run(['getenforce']).stdout.strip() == 'Enforcing'
    for name in ('package-update', 'gtk-package-update'):
        assert not Path('/var/lib/oscmix-desk', name).exists()
    assert not (home / '.config/oscmix/service-allowed').exists()
    before = state()
    records = [json.loads(p.read_text()) for p in sorted(args.packages.glob('*.rpm.json'))]
    assert len(records) == 2
    assert {r['component'] for r in records} == {'core', 'gtk'}
    assert len({r['source_commit'] for r in records}) == 1
    assert len({r['package_version'] for r in records}) == 1
    for record in records:
        artifact = args.packages / record['artifact']
        assert hashlib.sha256(artifact.read_bytes()).hexdigest() == record['sha256']
        for name, digest in record['payload'].items():
            assert hashlib.sha256(Path('/' + name).read_bytes()).hexdigest() == digest, name
    result = dict(schema=1, hardware=False, signal_target_fixture=True, selinux='Enforcing',
                  installed_payloads_verified=True, packages=records,
                  boot=Path('/proc/sys/kernel/random/boot_id').read_text().strip(),
                  kernel=os.uname().release,
                  systemd=run(['systemctl', '--version']).stdout.splitlines()[0],
                  probe_sha256=hashlib.sha256(script.read_bytes()).hexdigest(),
                  cases=[], passed=False)
    dropin = home / '.config/systemd/user/oscmix.service.d/99-qa-resume.conf'
    assert not dropin.exists()
    created = []
    parent = dropin.parent
    while not parent.exists():
        created.append(parent)
        parent = parent.parent
    for directory in reversed(created):
        directory.mkdir(mode=0o755)
        os.chown(directory, account.pw_uid, account.pw_gid)
    definition = ('[Service]\nExecStart=\nExecStart=' + sys.executable
                  + ' -I ' + str(script) + ' --signal\n')
    dropin.write_text(definition)
    os.chown(dropin, account.pw_uid, account.pw_gid)
    login = 'oscmix-qa-resume-login.service'
    watchdogs = []
    pid = 0
    try:
        run(['systemd-run', '--unit=' + login, '--collect', '--property=User=tester',
             '--property=PAMName=login', '--property=Type=exec', '/usr/bin/sleep', 'infinity'])
        run(['systemctl', '--user', 'daemon-reload'], user=True)
        run(['/usr/bin/oscmix-setup', '--enable'], user=True)
        pid = int(run(['systemctl', '--user', 'show', 'oscmix.service',
                       '-p', 'MainPID', '--value'], user=True).stdout)
        assert pid > 1
        assert Path('/proc', str(pid)).stat().st_uid == 12510
        result['fixture_pid'] = pid

        def received():
            journal = run(['journalctl', '-b', '--no-pager', '-o', 'cat', '_PID=' + str(pid),
                           '_SYSTEMD_USER_UNIT=oscmix.service']).stdout
            return journal.count('QA_HUP count=')

        run(['systemctl', '--user', 'reload', 'oscmix.service'], user=True)
        deadline = time.monotonic() + 5
        while received() != 1 and time.monotonic() < deadline:
            time.sleep(.05)
        assert received() == 1
        expected = 1
        cases = ['normal-' + str(i + 1) for i in range(args.cycles)]
        for case in [*cases, 'reload-failure', 'recovered', 'inactive']:
            if case == 'reload-failure':
                dropin.write_text(definition + 'ExecReload=\nExecReload=/usr/bin/false\n')
                run(['systemctl', '--user', 'daemon-reload'], user=True)
            elif case == 'recovered':
                dropin.write_text(definition)
                run(['systemctl', '--user', 'daemon-reload'], user=True)
            elif case == 'inactive':
                run(['systemctl', '--user', 'stop', 'oscmix.service'], user=True)
            cursor = run(['journalctl', '-n', '1', '--show-cursor', '--no-pager', '-o', 'cat'])
            cursor = cursor.stdout.strip().split('-- cursor: ')[-1]
            assert cursor.startswith('s=')
            watchdog = 'oscmix-qa-resume-' + case
            watchdogs.append(watchdog + '.timer')
            watchdog_record = args.output / (case + '-watchdog.json')
            run(['systemd-run', '--unit=' + watchdog, '--on-active=20s', '--collect',
                 sys.executable, '-I', script, '--watchdog', watchdog_record])
            run(['rtcwake', '-m', 'disable'])
            run(['rtcwake', '-m', 'no', '-s', '6'])
            start = time.clock_gettime(time.CLOCK_BOOTTIME)
            run(['systemctl', 'suspend'])
            deadline = time.monotonic() + 45
            while True:
                journal = run(['journalctl', '-b', '--after-cursor', cursor,
                               '--no-pager', '-o', 'json']).stdout
                events = [json.loads(line) for line in journal.splitlines()]
                messages = [event.get('MESSAGE', '') for event in events]
                exited = any('PM: suspend exit' in message for message in messages)
                finished = any(message.startswith(('Finished oscmix-resume.service',
                                                   'Failed to start oscmix-resume.service'))
                               for message in messages)
                if exited and finished:
                    break
                assert time.monotonic() < deadline, 'resume work did not complete'
                time.sleep(.1)
            run(['systemctl', 'stop', watchdog + '.timer'])
            (args.output / (case + '-journal.jsonl')).write_text(journal)
            assert any('PM: suspend entry (deep)' in message for message in messages)
            thaw = next(int(e['__MONOTONIC_TIMESTAMP']) for e in events
                        if "Successfully thawed unit 'user.slice'" in e.get('MESSAGE', ''))
            began = next(int(e['__MONOTONIC_TIMESTAMP']) for e in events
                         if e.get('MESSAGE', '').startswith('Starting oscmix-resume.service'))
            assert began >= thaw, 'resume service started before user.slice thawed'
            failed = any(message.startswith('Failed to start oscmix-resume.service')
                         for message in messages)
            assert failed == (case == 'reload-failure')
            if case not in ('reload-failure', 'inactive'):
                expected += 1
            deadline = time.monotonic() + 5
            while received() != expected and time.monotonic() < deadline:
                time.sleep(.05)
            assert received() == expected
            actual_pid = int(run(['systemctl', '--user', 'show', 'oscmix.service',
                                  '-p', 'MainPID', '--value'], user=True).stdout)
            assert actual_pid == (0 if case == 'inactive' else pid)
            watchdog_data = (json.loads(watchdog_record.read_text())
                             if watchdog_record.exists() else None)
            assert not (watchdog_data or {}).get('thawed')
            result['cases'].append(dict(case=case, deep_cycle=True,
                                        boottime_seconds=round(time.clock_gettime(
                                            time.CLOCK_BOOTTIME) - start, 3),
                                        started_after_thaw=True, expected_hups=expected,
                                        received_hups=received(), expected_pid=actual_pid,
                                        reload_failed=failed, watchdog=watchdog_data, passed=True))
            print(json.dumps(result['cases'][-1]), flush=True)
        result['passed'] = True
    finally:
        for watchdog in watchdogs:
            run(['systemctl', 'stop', watchdog], check=False)
        run(['/usr/bin/oscmix-setup', '--disable'], user=True, check=False)
        dropin.unlink()
        for directory in created:
            directory.rmdir()
        run(['systemctl', '--user', 'daemon-reload'], user=True, check=False)
        run(['systemctl', 'stop', login], check=False)
        result['user_state_preserved'] = state() == before
        result['fixture_removed'] = not dropin.exists() and not Path('/proc', str(pid)).exists()
        result['passed'] &= result['user_state_preserved'] and result['fixture_removed']
        (args.output / 'result.json').write_text(json.dumps(result, indent=2) + '\n')
    assert result['passed']


if __name__ == '__main__':
    main()
