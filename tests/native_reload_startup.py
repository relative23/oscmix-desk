"""Hold a real OpenRC/runit child before Python installs its SIGHUP handler."""
import argparse
import hashlib
import json
import os
import pwd
import signal
import subprocess
import time
from pathlib import Path

assert os.getuid() == 0
assert Path('/etc/hostname').read_text().strip() in ('oscmix-alpine-qa', 'oscmix-void-qa')
assert not any(path.read_text().strip() == '2a39'
               for path in Path('/sys/bus/usb/devices').glob('*/idVendor'))
record = json.loads(Path('/etc/oscmix-desk/service.json').read_text())
account = pwd.getpwnam(record['user'])
assert account.pw_name == 'tester'
assert account.pw_uid == 12510
admin = '/usr/local/sbin/oscmix-service'
expected = [os.fsencode(record['command']), b'--config', os.fsencode(record['config'])]
parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--output', type=Path, required=True)
args = parser.parse_args()
assert not args.output.exists()
result = dict(manager=record['manager'], hardware=False, cases=[], ok=False,
              fixture_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest())

def run(command, user=False):
    extra = {}
    if user:
        extra = dict(user=account.pw_uid, group=account.pw_gid,
                     extra_groups=os.getgrouplist(account.pw_name, account.pw_gid),
                     env=dict(HOME=record['home'], USER=account.pw_name, LOGNAME=account.pw_name,
                              PATH='/usr/local/bin:/usr/bin:/bin', LANG='C.UTF-8'),
                     cwd=record['home'])
    completed = subprocess.run(command, capture_output=True, text=True, timeout=30, **extra)
    return dict(command=command, code=completed.returncode,
                stdout=completed.stdout, stderr=completed.stderr)

def state(pid):
    try:
        return dict(line.split(':', 1) for line in Path('/proc', str(pid), 'status')
                    .read_text().splitlines() if ':' in line)
    except FileNotFoundError:
        return {}

def catch_start():
    for _ in range(5):
        stopped = run([admin, 'stop'])
        assert stopped['code'] == 0, stopped
        with open('/root/early-reload-start.log', 'w') as log:
            starter = subprocess.Popen([admin, 'start'], stdout=log, stderr=subprocess.STDOUT)
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                for entry in Path('/proc').iterdir():
                    if not entry.name.isdigit():
                        continue
                    try:
                        argv = (entry / 'cmdline').read_bytes().split(b'\0')[:-1]
                        if argv[-3:] != expected or entry.stat().st_uid != account.pw_uid:
                            continue
                        pid = int(entry.name)
                        if int(state(pid).get('SigCgt', '1'), 16) & 1:
                            continue
                        fd = os.pidfd_open(pid)
                        signal.pidfd_send_signal(fd, signal.SIGSTOP)
                        held = state(pid)
                        if int(held.get('SigCgt', '1'), 16) & 1:
                            signal.pidfd_send_signal(fd, signal.SIGCONT)
                            os.close(fd)
                            continue
                        starter.wait(timeout=10)
                        assert starter.returncode == 0
                        return pid, fd, held  # noqa: TRY300 -- retry only a vanished process
                    except FileNotFoundError:
                        continue
                time.sleep(.001)
            starter.wait(timeout=10)
    raise AssertionError('could not hold the real interpreter before SIGHUP handler setup')

try:
    for path in ('unprivileged', 'admin', 'native'):
        pid, fd, before = catch_start()
        try:
            if path == 'unprivileged':
                script = ('import sys; sys.path.insert(0, ' + repr(
                    str(Path(record['home']) / '.local/lib/oscmix-desk'))
                    + '); from oscmix_desk.process import reload_service; print(reload_service())')
                outcome = run(['python3', '-c', script], user=True)
            elif path == 'admin':
                outcome = run([admin, 'reload'])
            else:
                command = (['rc-service', 'oscmix-desk', 'reload'] if record['manager'] == 'openrc'
                           else ['sv', 'hup', '/etc/sv/oscmix-desk'])
                outcome = run(command)
                if record['manager'] == 'runit':
                    # `sv hup` enqueues a custom control hook. Keep the child
                    # stopped past the five-second refusal deadline so the
                    # hook cannot hide a fallback SIGHUP by racing our resume.
                    time.sleep(6)
            pending = state(pid)
            signal.pidfd_send_signal(fd, signal.SIGCONT)
            deadline = time.monotonic() + 1
            while time.monotonic() < deadline:
                after = state(pid)
                if not after or after['State'].lstrip().startswith('Z'):
                    break
                time.sleep(.01)
            survived = bool(after) and not after['State'].lstrip().startswith('Z')
            result['cases'].append(dict(path=path, pid=pid, before=before, pending=pending,
                                        outcome=outcome, survived=survived, after=after))
            print(path, 'survived', survived, 'reported', outcome, flush=True)
            assert survived, (path, outcome)
            assert not int(pending.get('SigPnd', '0'), 16) & 1, pending
            assert not int(pending.get('ShdPnd', '0'), 16) & 1, pending
            if path == 'unprivileged':
                assert outcome['stdout'].strip() == 'failed', outcome
            elif path == 'admin' or record['manager'] == 'openrc':
                assert outcome['code'] != 0, outcome
            deadline = time.monotonic() + 5
            while not int(state(pid).get('SigCgt', '0'), 16) & 1:
                assert time.monotonic() < deadline, 'resumed child did not install its handler'
                time.sleep(.01)
            ready = run([admin, 'reload'])
            assert ready['code'] == 0, ready
            assert state(pid), 'ready reload replaced the child'
            result['cases'][-1]['ready_reload'] = ready
        finally:
            try:
                signal.pidfd_send_signal(fd, signal.SIGCONT)
            except ProcessLookupError:
                pass
            os.close(fd)
    result['ok'] = True
finally:
    result['stopped'] = run([admin, 'stop'])
    args.output.write_text(json.dumps(result, indent=2) + '\n')
