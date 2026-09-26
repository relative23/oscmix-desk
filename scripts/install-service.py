#!/usr/bin/env python3
"""Register and maintain one unprivileged desk under OpenRC or runit.

Run as root for host integration, after the ordinary user's source installation.
Installation alone never enables or starts hardware. The native manager owns
process supervision; this tool only implements explicit lifecycle actions.
"""

import argparse
import contextlib
import errno
import fcntl
import grp
import json
import os
import pwd
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

REGISTRATION = Path('/etc/oscmix-desk/service.json')
STATE = Path('/var/lib/oscmix-desk')
RUNNER = Path('/usr/local/libexec/oscmix-desk/run-session')
RELOADER = Path('/usr/local/libexec/oscmix-desk/reload-session.py')
ADMIN = Path('/usr/local/sbin/oscmix-service')
OPENRC = Path('/etc/init.d/oscmix-desk')
RUNIT = Path('/etc/sv/oscmix-desk')
ACTIVE = Path('/var/service/oscmix-desk')
OPENRC_ENABLED = Path('/etc/runlevels/default/oscmix-desk')
RESUME_HOOKS = (Path('/etc/zzz.d/resume/90-oscmix-desk'),
                Path('/etc/elogind/system-sleep/oscmix-desk'))
RUNIT_LOG = Path('/var/log/oscmix-desk')
SOURCE = Path(__file__).resolve().parents[1]


def run(command, *, check=True, **kwargs):
    return subprocess.run([str(arg) for arg in command], capture_output=True,
                          text=True, timeout=30, check=check, **kwargs)


def atomic(path, content, mode=0o644):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o755)
    fd, name = tempfile.mkstemp(prefix='.' + path.name + '-', dir=path.parent)
    try:
        with os.fdopen(fd, 'w') as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(name, mode)
        os.replace(name, path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        Path(name).unlink(missing_ok=True)


def registration():
    value = json.loads(REGISTRATION.read_text())
    if value.get('schema') != 1 or value.get('manager') not in ('openrc', 'runit'):
        raise ValueError('invalid service registration')
    return value


def as_user(record, arguments, *, check=True):
    account = pwd.getpwnam(record['user'])
    if account.pw_uid == 0 or account.pw_uid != record['uid']:
        raise ValueError('registered user identity changed')
    env = dict(HOME=record['home'], USER=account.pw_name, LOGNAME=account.pw_name,
               XDG_CONFIG_HOME=str(Path(record['config']).parent.parent),
               XDG_DATA_HOME=str(Path(record['home']) / '.local/share'),
               PATH='/usr/local/bin:/usr/bin:/bin', LANG='C.UTF-8')
    return run(arguments, check=check, env=env, cwd=record['home'], user=account.pw_uid,
               group=account.pw_gid, extra_groups=os.getgrouplist(account.pw_name, account.pw_gid))


def validate(record):
    """All installed/user-controlled code runs after dropping root privileges."""
    account = pwd.getpwnam(record['user'])
    audio = grp.getgrnam('audio').gr_gid
    if audio not in os.getgrouplist(account.pw_name, account.pw_gid):
        raise ValueError('add the selected user to audio before activation')
    command = Path(record['command'])
    as_user(record, [command, '--dry-run', '--config', record['config'], '--timeout', '0'])
    ids = []
    for name in ('oscmix', 'alsaseqio', 'oscmix-gtk'):
        binary = command.parent / name
        if name == 'oscmix-gtk' and not binary.exists():
            continue
        if as_user(record, [binary, '--control-version']).stdout.strip() != 'ODK1':
            raise ValueError('incompatible coordinated backend: ' + str(binary))
        ids.append(as_user(record, [binary, '--desk-build-id']).stdout.strip())
    if not ids or not re.fullmatch('[a-f0-9]{64}', ids[0]) or len(set(ids)) != 1:
        raise ValueError('backend, bridge and GTK must share one exact build series')
    return ids[0]


@contextlib.contextmanager
def installation_lock(record):
    """Exclude a concurrent source replacement while validating activation."""
    path = Path(record['home']) / '.local/lib/oscmix-desk/.install.lock'
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        if os.fstat(fd).st_uid != record['uid']:
            raise ValueError('source installation lock belongs to another user')
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield
    finally:
        os.close(fd)


def native(record, action, *, check=True):
    if action == 'reload':
        # Share the runtime's identity/readiness/pidfd decision. No root
        # process imports user-controlled code or sends a blind SIGHUP.
        return as_user(record, ['python3', RELOADER], check=check)
    if record['manager'] == 'openrc':
        return run(['rc-service', 'oscmix-desk', action], check=check)
    verb = dict(start='up', stop='down', status='status')[action]
    # The configured directory is also addressable while disabled. runsv only
    # exists after explicit enable adds it to the active runsvdir.
    return run(['sv', '-w', '20', verb, RUNIT], check=check)


def enabled(record):
    manager_enabled = (OPENRC_ENABLED.exists()
                       if record['manager'] == 'openrc' else ACTIVE.exists())
    return manager_enabled and (STATE / 'service-allowed').exists()


def runit_supervised():
    """A stale FIFO pathname does not mean a live runsv still owns it."""
    try:
        fd = os.open(RUNIT / 'supervise/ok', os.O_WRONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
    except OSError as exc:
        if exc.errno in (errno.ENOENT, errno.ENXIO):
            return False
        raise
    os.close(fd)
    return True


def active(record):
    result = native(record, 'status', check=False)
    if record['manager'] == 'openrc':
        if result.returncode not in (0, 3):
            raise ValueError('cannot establish OpenRC state: ' + result.stderr + result.stdout)
        return result.returncode == 0
    if result.returncode == 0 and result.stdout.startswith(('run:', 'down:', 'finish:')):
        return not result.stdout.startswith('down:')
    if not ACTIVE.exists() and not runit_supervised():
        return False
    raise ValueError('cannot establish runit state: ' + result.stderr + result.stdout)


def install_files(record):
    """Replace only this registration's adapters while activation is fenced."""
    account = pwd.getpwnam(record['user'])
    if account.pw_uid == 0 or account.pw_uid != record['uid']:
        raise ValueError('registered user identity changed')
    hooks = [str(path) for path in RESUME_HOOKS if path.parent.is_dir()]
    previous_hooks = record.get('resume_hooks', [])
    for name in hooks:
        if name not in previous_hooks and (Path(name).exists() or Path(name).is_symlink()):
            raise ValueError('refusing to overwrite an unregistered resume hook: ' + name)
    record['resume_hooks'] = hooks
    record['installed'] = False
    atomic(REGISTRATION, json.dumps(record, indent=2) + '\n')
    atomic(RUNNER, (SOURCE / 'service/run-session').read_text(), 0o755)
    # This stdlib-only leaf is copied from its single canonical source.
    # It runs as the registered user, independent of Python's import timing
    # in the child and of the user's selected entry-point layout.
    atomic(RELOADER, (SOURCE / 'src/oscmix_desk/hostservice.py').read_text())
    atomic(ADMIN, Path(__file__).read_text(), 0o755)
    if record['manager'] == 'openrc':
        template = (SOURCE / 'service/openrc/oscmix-desk').read_text()
        atomic(OPENRC, template.replace('@USER@', account.pw_name), 0o755)
    else:
        groups = ':%d:%d:%d' % (account.pw_uid, account.pw_gid, grp.getgrnam('audio').gr_gid)
        atomic(RUNIT / 'run', (SOURCE / 'service/runit/run').read_text().replace(
            '@GROUPS@', groups), 0o755)
        atomic(RUNIT / 'finish', (SOURCE / 'service/runit/finish').read_text(), 0o755)
        atomic(RUNIT / 'control/h', (SOURCE / 'service/runit/hup').read_text(), 0o755)
        atomic(RUNIT / 'log/run', (SOURCE / 'service/runit/log-run').read_text(), 0o755)
        # runsv defaults to 0700 here. Expose its 0644 pid/status metadata;
        # control/ok FIFOs remain root-only. Runtime status never opens them.
        (RUNIT / 'supervise').mkdir(mode=0o755, exist_ok=True)
        (RUNIT / 'supervise').chmod(0o755)
        atomic(RUNIT / 'down', '')
        RUNIT_LOG.mkdir(mode=0o755, exist_ok=True)
    for name in hooks:
        atomic(Path(name), (SOURCE / 'service/resume').read_text(), 0o755)
    STATE.mkdir(mode=0o755, parents=True, exist_ok=True)
    record['installed'] = True
    atomic(REGISTRATION, json.dumps(record, indent=2) + '\n')


def install(args):
    account = pwd.getpwnam(args.user)
    if account.pw_uid == 0:
        raise ValueError('the runtime user must not be root')
    home = Path(account.pw_dir)
    command = args.command or home / '.local/bin/oscmix-session'
    config = args.config or home / '.config/oscmix/routing.conf'
    if not command.is_absolute() or not config.is_absolute() or not home.is_absolute():
        raise ValueError('command, config and home must be absolute')
    required = ('rc-service', 'rc-update', 'supervise-daemon') if args.manager == 'openrc' else (
        'sv', 'chpst', 'svlogd')
    if any(shutil.which(tool) is None for tool in required):
        raise ValueError('native service-manager commands are missing: ' + ', '.join(required))
    record = dict(schema=1, manager=args.manager, user=account.pw_name, uid=account.pw_uid,
                  home=str(home), config=str(config), command=str(command))
    if not re.fullmatch(r'[a-zA-Z_][a-zA-Z0-9_-]*', account.pw_name):
        raise ValueError('unsupported service username')
    with installation_lock(record):
        record['backend_series'] = validate(record)
    if REGISTRATION.exists():
        previous = registration()
        if previous.get('installed') is not False or any(
                previous.get(key) != value for key, value in record.items()):
            raise ValueError('a host desk is already registered; use maintenance for upgrades')
    else:
        for path in (RUNNER, RELOADER, ADMIN, OPENRC if args.manager == 'openrc' else RUNIT):
            if path.exists() or path.is_symlink():
                raise ValueError('refusing to overwrite an unregistered service file: '
                                 + str(path))
    install_files(record)
    print('Registered ' + args.manager + ' desk for ' + account.pw_name + '; not enabled.')


def lifecycle(action, record):
    fence = STATE / 'service-update'
    allowed = STATE / 'service-allowed'
    package_maintenance = any((STATE / name).exists()
                              for name in ('package-update', 'gtk-package-update'))
    if record.get('installed') is not True and action not in ('status', 'update'):
        raise ValueError('service installation incomplete; rerun the original install command')
    if action == 'status':
        state = native(record, 'status', check=False)
        print(json.dumps(dict(manager=record['manager'], user=record['user'],
                              installed=record.get('installed') is True,
                              enabled=enabled(record), maintenance=fence.exists(),
                              manager_status=state.stdout.strip(),
                              manager_error=state.stderr.strip()), indent=2))
    elif action == 'maintenance-begin':
        if fence.exists():
            previous = json.loads(fence.read_text())
        else:
            previous = dict(enabled=enabled(record), active=active(record), stopped=False)
            atomic(fence, json.dumps(previous) + '\n')
        if record['manager'] == 'runit':
            atomic(RUNIT / 'down', '')
        if active(record):
            native(record, 'stop')
        if active(record):
            raise ValueError('service did not stop; maintenance fence retained')
        previous['stopped'] = True
        atomic(fence, json.dumps(previous) + '\n')
        print('Service stopped; persistent maintenance fence installed. '
              'Upgrade as the audio user.')
    elif action in ('maintenance-finish', 'update'):
        if package_maintenance:
            raise ValueError('finish package maintenance before host service activation')
        previous = json.loads(fence.read_text())
        if previous.get('stopped') is not True or active(record):
            raise ValueError('run maintenance-begin successfully before finishing')
        with installation_lock(record):
            record['backend_series'] = validate(record)
            if action == 'update':
                install_files(record)
                print('Host adapters updated; maintenance fence retained. '
                      'Run oscmix-service maintenance-finish after verification.')
                return
            atomic(REGISTRATION, json.dumps(record, indent=2) + '\n')
            fence.unlink()
        if previous['active'] and previous['enabled'] and enabled(record):
            if record['manager'] == 'runit':
                (RUNIT / 'down').unlink(missing_ok=True)
            native(record, 'start')
        print('Maintenance complete; previous activation state retained.')
    elif action == 'disable':
        allowed.unlink(missing_ok=True)
        if active(record):
            native(record, 'stop')
        if active(record):
            raise ValueError('service did not stop; automatic operation remains disallowed')
        if record['manager'] == 'openrc':
            run(['rc-update', 'del', 'oscmix-desk', 'default'], check=False)
        else:
            atomic(RUNIT / 'down', '')
            if ACTIVE.is_symlink() and ACTIVE.resolve() == RUNIT.resolve():
                ACTIVE.unlink()
            if runit_supervised():
                run(['sv', '-w', '20', 'shutdown', RUNIT])
        print('Automatic operation disabled; user configuration preserved.')
    elif action == 'enable':
        if fence.exists() or package_maintenance:
            raise ValueError('finish maintenance before enabling')
        with installation_lock(record):
            validate(record)
        atomic(allowed, 'Explicitly enabled with oscmix-service\n')
        if record['manager'] == 'openrc':
            run(['rc-update', 'add', 'oscmix-desk', 'default'])
        else:
            (RUNIT / 'down').unlink(missing_ok=True)
            if ACTIVE.exists() or ACTIVE.is_symlink():
                if ACTIVE.resolve() != RUNIT.resolve():
                    raise ValueError('another service owns the runit activation path')
            else:
                ACTIVE.symlink_to(RUNIT)
            deadline = time.monotonic() + 10
            while not (RUNIT / 'supervise/ok').exists():
                if time.monotonic() >= deadline:
                    raise ValueError('runsvdir did not supervise the enabled service')
                time.sleep(0.1)
        native(record, 'start')
    elif action in ('start', 'reload'):
        if fence.exists() or package_maintenance or not allowed.exists():
            raise ValueError('service is disabled or maintenance is incomplete')
        native(record, action)
    elif action == 'resume':
        # A wake event must not enable an inactive desk or escape maintenance.
        if not fence.exists() and not package_maintenance and enabled(record) and active(record):
            native(record, 'reload')
    elif action == 'stop':
        if fence.exists():
            previous = json.loads(fence.read_text())
            previous['active'] = False
            atomic(fence, json.dumps(previous) + '\n')
        native(record, 'stop')
    elif action == 'remove':
        if (enabled(record) or active(record)
                or (record['manager'] == 'runit' and runit_supervised())):
            raise ValueError('disable the registered service before removing its integration')
        if record['manager'] == 'openrc':
            OPENRC.unlink()
        else:
            shutil.rmtree(RUNIT)
        for path in RESUME_HOOKS:
            if str(path) in record.get('resume_hooks', []):
                path.unlink(missing_ok=True)
        REGISTRATION.unlink()
        RUNNER.unlink()
        RELOADER.unlink(missing_ok=True)
        ADMIN.unlink()
        allowed.unlink(missing_ok=True)
        fence.unlink(missing_ok=True)
        print('Host service removed; runtime, desk and shared device locks preserved.')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('install', 'update', 'enable', 'disable', 'start',
                                          'stop', 'reload', 'resume', 'status',
                                          'maintenance-begin', 'maintenance-finish', 'remove'))
    parser.add_argument('--manager', choices=('openrc', 'runit'))
    parser.add_argument('--user')
    parser.add_argument('--config', type=Path)
    parser.add_argument('--command', type=Path)
    args = parser.parse_args()
    if os.getuid() != 0 and args.action != 'status':
        parser.error('host integration changes require root; the mixer never runs as root')
    lock = None
    try:
        if args.action != 'status':
            STATE.mkdir(parents=True, exist_ok=True, mode=0o755)
            lock = os.open(STATE / '.service.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if args.action == 'install':
            if not args.manager or not args.user:
                parser.error('install requires --manager and --user')
            install(args)
        else:
            if any((args.manager, args.user, args.config, args.command)):
                parser.error('selection arguments apply only to install')
            lifecycle(args.action, registration())
    except (OSError, ValueError, KeyError, subprocess.SubprocessError) as exc:
        print('oscmix-service: ' + str(exc), file=sys.stderr)
        return 1
    finally:
        if lock is not None:
            os.close(lock)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
