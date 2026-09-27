"""Registered native supervisors retain identity, read-only status and reload scope."""
import json
import os
import signal
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from oscmix_desk import cli, diagnostics, hostservice, process
from oscmix_desk.model import Config


@pytest.fixture
def native(tmp_path, monkeypatch):
    service = hostservice.HostService('runit', 'tester', os.getuid(), tmp_path / 'home',
                                       tmp_path / 'home/.config/oscmix/routing.conf',
                                       tmp_path / 'home/.local/bin/oscmix-session')
    active = tmp_path / 'service'
    active.mkdir()
    monkeypatch.setattr(hostservice, 'RUNIT_SERVICE', active)
    adapter = SimpleNamespace(registered=lambda: service, main_pid=hostservice.main_pid,
                              reload=hostservice.reload, report=hostservice.report)
    monkeypatch.setattr(process, 'hostservice', adapter)
    monkeypatch.setattr(diagnostics, 'hostservice', adapter)
    (active / 'supervise').mkdir()
    (active / 'supervise/pid').write_text('1234\n')
    monkeypatch.setattr(hostservice, '_root_file', lambda path: path.read_text())
    proc = tmp_path / 'proc'
    entry = proc / '1234'
    entry.mkdir(parents=True)
    (entry / 'cmdline').write_bytes(b'python3\0' + os.fsencode(service.command)
                                  + b'\0--config\0' + os.fsencode(service.config) + b'\0')
    (entry / 'environ').write_bytes(b'HOME=' + os.fsencode(service.home)
                                  + b'\0OSCMIX_SERVICE_MANAGER=runit\0')
    (entry / 'cwd').symlink_to(service.home)
    (entry / 'status').write_text('PPid:\t12\nSigCgt:\t0000000000000001\n')
    parent = proc / '12'
    parent.mkdir()
    (parent / 'cmdline').write_bytes(b'runsv\0oscmix-desk\0')
    original_stat = Path.stat

    def parent_owner(path, *args, **kwargs):
        if path == parent:
            return SimpleNamespace(st_uid=0)
        return original_stat(path, *args, **kwargs)

    monkeypatch.setattr(Path, 'stat', parent_owner)
    monkeypatch.setenv('OSCMIX_PROC_ROOT', str(proc))
    return service, proc, entry


def test_native_profile_reload_uses_the_registered_command_context(native):
    service, proc, _ = native
    observed = process.unit_process(proc)
    assert observed.argv == ('python3', str(service.command), '--config', str(service.config))
    assert observed.environ['HOME'] == str(service.home)
    assert observed.cwd == service.home


@pytest.mark.parametrize('replacement', ['other-command', 'other-config', 'other-home',
                                         'missing-context', 'other-uid'])
def test_native_pid_is_not_authority_to_reload_a_different_session(native, replacement):
    service, proc, entry = native
    if replacement == 'other-command':
        (entry / 'cmdline').write_bytes(b'python3\0/some/other/oscmix-session\0')
    elif replacement == 'other-config':
        (entry / 'cmdline').write_bytes(b'python3\0' + os.fsencode(service.command)
                                       + b'\0--config\0/another/desk.conf\0')
    elif replacement == 'other-home':
        (entry / 'environ').write_bytes(b'HOME=/other/home\0OSCMIX_SERVICE_MANAGER=runit\0')
    elif replacement == 'missing-context':
        (entry / 'environ').write_bytes(b'HOME=' + os.fsencode(service.home) + b'\0')
    else:
        service = replace(service, uid=service.uid + 1)
    assert hostservice.main_pid(service, proc) is None
    assert hostservice.reload(service, proc) == 'not running'


def test_reload_targets_only_the_pinned_desk_process(native, monkeypatch):
    service, proc, _ = native
    calls = []
    monkeypatch.setattr(os, 'pidfd_open', lambda pid: calls.append(('open', pid)) or 39,
                        raising=False)
    monkeypatch.setattr(signal, 'pidfd_send_signal', lambda fd, sig: calls.append((fd, sig)),
                        raising=False)
    monkeypatch.setattr(os, 'close', lambda fd: calls.append(('close', fd)))
    assert hostservice.reload(service, proc) == 'reloaded'
    assert calls == [('open', 1234), (39, signal.SIGHUP), ('close', 39)]


def test_reload_revalidates_after_pinning_pid_and_closes_on_replacement(native, monkeypatch):
    service, proc, _ = native
    calls = []
    answers = iter((1234, 4321))
    monkeypatch.setattr(hostservice, 'main_pid', lambda *_: next(answers))
    monkeypatch.setattr(os, 'pidfd_open', lambda _: 39, raising=False)
    monkeypatch.setattr(signal, 'pidfd_send_signal', lambda *_: pytest.fail('wrong process'),
                        raising=False)
    monkeypatch.setattr(os, 'close', calls.append)
    assert hostservice.reload(service, proc) == 'failed'
    assert calls == [39]


@pytest.mark.parametrize('operation', ['open', 'send'])
@pytest.mark.parametrize('failure', ['unavailable', 'denied'])
def test_reload_refuses_without_safe_process_signalling(native, monkeypatch, operation, failure):
    service, proc, _ = native
    opened, closed = [], []
    # pidfd APIs depend on the interpreter build as well as the running kernel.
    monkeypatch.setattr(os, 'pidfd_open', lambda pid: opened.append(pid) or 39, raising=False)
    monkeypatch.setattr(signal, 'pidfd_send_signal', lambda *_: pytest.fail('must not signal'),
                        raising=False)
    monkeypatch.setattr(os, 'kill', lambda *_: pytest.fail('unsafe numeric PID fallback'))
    monkeypatch.setattr(os, 'close', closed.append)
    module, name = (os, 'pidfd_open') if operation == 'open' else (signal, 'pidfd_send_signal')
    if failure == 'unavailable':
        monkeypatch.delattr(module, name)
    else:
        def denied(*_):
            raise PermissionError('pidfd operation denied')
        monkeypatch.setattr(module, name, denied)
    assert hostservice.reload(service, proc) == 'failed'
    assert opened == ([1234] if operation == 'send' else [])
    assert closed == ([39] if operation == 'send' else [])


@pytest.mark.parametrize('status', ['SigCgt:\t0', 'SigCgt:\t2', 'SigCgt:\tbroken', '', None])
def test_reload_cannot_kill_a_python_process_before_its_handler_exists(
        native, monkeypatch, status):
    service, proc, entry = native
    if status is None:
        (entry / 'status').unlink()
    else:
        (entry / 'status').write_text('PPid:\t12\n' + status + '\n')
    # The supervisor identity is tested separately; pin this real startup
    # condition without hiding a missing/malformed signal-mask read.
    monkeypatch.setattr(hostservice, 'main_pid', lambda *_: 1234)
    clock = iter((0.0, hostservice.RELOAD_READY_TIMEOUT + 1))
    monkeypatch.setattr(hostservice.time, 'monotonic', lambda: next(clock))
    closed = []
    monkeypatch.setattr(os, 'pidfd_open', lambda _: 39, raising=False)
    monkeypatch.setattr(os, 'close', closed.append)
    monkeypatch.setattr(signal, 'pidfd_send_signal', lambda *_: pytest.fail('premature SIGHUP'),
                        raising=False)
    assert hostservice.reload(service, proc) == 'failed'
    assert closed == [39]


def test_reload_waits_for_handler_without_changing_the_pinned_process(native, monkeypatch):
    service, proc, entry = native
    (entry / 'status').write_text('PPid:\t12\nSigCgt:\t0\n')
    calls = []
    monkeypatch.setattr(os, 'pidfd_open', lambda pid: calls.append(('open', pid)) or 39,
                        raising=False)
    monkeypatch.setattr(os, 'close', lambda fd: calls.append(('close', fd)))
    monkeypatch.setattr(signal, 'pidfd_send_signal', lambda fd, sig: calls.append((fd, sig)),
                        raising=False)

    def finish_importing(seconds):
        assert 0 < seconds <= hostservice.RELOAD_READY_TIMEOUT
        assert calls == [('open', 1234)]
        (entry / 'status').write_text('PPid:\t12\nSigCgt:\t1\n')

    monkeypatch.setattr(hostservice.time, 'sleep', finish_importing)
    assert hostservice.reload(service, proc) == 'reloaded'
    assert calls == [('open', 1234), (39, signal.SIGHUP), ('close', 39)]


@pytest.mark.parametrize('condition', ['absent', 'other-user', 'disabled', 'maintenance',
                                      'unreadable', 'failed', 'not running', 'reloaded'])
def test_native_adapter_uses_registration_activation_and_shared_reload(
        native, monkeypatch, tmp_path, capsys, condition):
    service, _, _ = native
    monkeypatch.setattr(hostservice, 'STATE', tmp_path)
    if condition != 'disabled':
        (tmp_path / 'service-allowed').touch()
    if condition == 'maintenance':
        (tmp_path / 'service-update').touch()
    if condition == 'other-user':
        service = replace(service, uid=service.uid + 1)

    def registration():
        if condition == 'unreadable':
            raise OSError('registration unreadable')
        return None if condition == 'absent' else service

    monkeypatch.setattr(hostservice, 'registered', registration)
    calls = []
    monkeypatch.setattr(hostservice, 'reload',
                        lambda chosen, proc: calls.append((chosen, proc)) or condition)
    assert hostservice.request_native_reload() == (0 if condition == 'reloaded' else 1)
    assert bool(calls) == (condition in ('failed', 'not running', 'reloaded'))
    if calls:
        assert calls == [(service, Path('/proc'))]
    assert bool(capsys.readouterr().err) is (condition != 'reloaded')


def test_unknown_native_manager_state_is_not_inactive(native, monkeypatch):
    def denied(_):
        raise OSError('supervisor status unreadable')
    monkeypatch.setattr(hostservice, '_root_file', denied)
    assert process.reload_service() == 'failed'
    assert diagnostics.service_status()['state'] == 'unavailable'


def test_stopped_runit_service_does_not_report_a_running_desk(native):
    service, proc, _ = native
    (hostservice.RUNIT_SERVICE / 'supervise/pid').write_text('')
    assert hostservice.main_pid(service, proc) is None


@pytest.mark.parametrize('parent', [b'runsv\0another-service\0',
                                    b'other-supervisor\0oscmix-desk\0'])
def test_stale_runit_pid_is_not_authority_without_the_registered_supervisor(native, parent):
    service, proc, _ = native
    (proc / '12/cmdline').write_bytes(parent)
    with pytest.raises(OSError, match='supervisor identity'):
        hostservice.main_pid(service, proc)


def test_read_only_status_does_not_start_or_signal_native_manager(native, monkeypatch):
    service, _, _ = native
    monkeypatch.setattr(os, 'pidfd_open', lambda _: pytest.fail('status must not signal'),
                        raising=False)
    report = diagnostics.service_status()
    assert report['manager'] == 'runit'
    assert report['ActiveState'] == 'active'
    assert report['Configuration'] == str(service.config)
    assert report['UnitFileState'] == 'disabled'


@pytest.mark.parametrize('override', [False, True])
def test_native_status_passes_the_selected_process_root(native, monkeypatch, override):
    service, proc, _ = native
    if not override:
        monkeypatch.delenv('OSCMIX_PROC_ROOT')
    calls = []
    response = dict(state='observed', manager=service.manager)
    monkeypatch.setattr(diagnostics.hostservice, 'report',
                        lambda *args: calls.append(args) or response)
    assert diagnostics.service_status() == response
    assert calls == [(service, proc if override else Path('/proc'))]


def test_another_users_host_service_is_unavailable_not_inactive(native):
    service, proc, _ = native
    report = hostservice.report(replace(service, uid=service.uid + 1), proc)
    assert report['state'] == 'unavailable'
    assert report['manager'] == service.manager
    assert 'ActiveState' not in report
    assert 'another user' in report['detail']


@pytest.mark.parametrize('data', [[], {}, {'schema': 2}, {'schema': 1, 'manager': 'runit'},
                                  dict(schema=1, manager='runit', user='root', uid=0,
                                       home='/root', config='/root/desk', command='/bin/true'),
                                  dict(schema=1, manager='runit', user='tester', uid=1000,
                                       home='relative', config='/desk', command='/bin/true')])
def test_incomplete_or_root_registration_cannot_select_a_process(data, monkeypatch):
    monkeypatch.setattr(hostservice, '_root_file', lambda _: json.dumps(data))
    with pytest.raises(OSError, match=r'registration|paths'):
        hostservice.registered()


def test_user_written_registration_is_refused(tmp_path):
    path = tmp_path / 'service.json'
    path.write_text('{}')
    path.chmod(0o666)
    with pytest.raises(OSError, match='root-owned regular'):
        hostservice._root_file(path)


@pytest.mark.parametrize('changed', [None, 'user', 'pidfile', 'service', 'owner'])
def test_openrc_identity_uses_root_registration_and_readable_parent_identity(
        native, monkeypatch, changed):
    service, proc, entry = native
    service = replace(service, manager='openrc')
    (entry / 'environ').write_bytes(b'HOME=' + os.fsencode(service.home)
                                  + b'\0OSCMIX_SERVICE_MANAGER=openrc\0')
    parent = proc / '12'
    children = parent / 'task/12/children'
    children.parent.mkdir(parents=True)
    children.write_text('1234 5678')
    # No exe symlink: an ordinary audio user cannot read the root supervisor's.
    argv = ['supervise-daemon', 'oscmix-desk', '--start', '--pidfile',
            str(hostservice.SUPERVISOR_PID), '--user', service.user]
    if changed == 'user':
        argv[-1] = 'another-user'
    elif changed == 'pidfile':
        argv[4] = '/run/another-service.pid'
    elif changed == 'service':
        argv[1] = 'another-service'
    (parent / 'cmdline').write_bytes(b'\0'.join(os.fsencode(arg) for arg in argv) + b'\0')
    original_stat = Path.stat

    def stat_owner(path, *args, **kwargs):
        if path == parent:
            return SimpleNamespace(st_uid=1 if changed == 'owner' else 0)
        return original_stat(path, *args, **kwargs)

    monkeypatch.setattr(Path, 'stat', stat_owner)
    def supervisor_identity(path):
        assert path == hostservice.SUPERVISOR_PID
        return '12\n'

    monkeypatch.setattr(hostservice, '_root_file', supervisor_identity)
    if changed:
        with pytest.raises(OSError, match='supervisor identity'):
            hostservice.main_pid(service, proc)
    else:
        assert hostservice.main_pid(service, proc) == 1234


@pytest.mark.parametrize('action', [[], ['--profile', 'recording'], ['--no-profile'],
                                    ['--diff'], ['--snapshot'], ['--dump-config']])
def test_persistent_maintenance_fence_blocks_manual_device_paths(tmp_path, monkeypatch, action):
    monkeypatch.setattr(hostservice, 'STATE', tmp_path)
    (tmp_path / 'service-update').write_text('{"stopped":true}')
    monkeypatch.setattr(cli, '_desk_in_effect', lambda *_: Config())
    monkeypatch.setattr(cli, 'run_session', lambda *_: pytest.fail('must not activate'))
    monkeypatch.setattr(cli, '_switch_profile', lambda *_: pytest.fail('must not write'))
    monkeypatch.setattr(cli, '_snapshot', lambda *_: pytest.fail('must not refresh'))
    assert cli.main(action) == 2


def test_maintenance_does_not_block_status_or_pure_plan(tmp_path, monkeypatch):
    monkeypatch.setattr(hostservice, 'STATE', tmp_path)
    (tmp_path / 'service-update').write_text('pending')
    monkeypatch.setattr(cli, '_desk_in_effect', lambda *_: Config())
    monkeypatch.setattr(cli, 'print_status', lambda *_: 0)
    monkeypatch.setattr(cli, 'run_session', lambda args, _: 0 if args.dry_run else 1)
    assert cli.main(['--status', '--json']) == 0
    assert cli.main(['--dry-run', '--timeout', '0']) == 0


@pytest.mark.parametrize('damage', [None, 'file-owner', 'file-mode', 'file-type',
                                    'parent-owner', 'parent-mode'])
def test_registration_requires_a_trusted_file_and_parent_chain(tmp_path, monkeypatch, damage):
    path = tmp_path / 'service.json'
    path.write_text('root registration')
    original_stat = Path.stat

    def metadata(selected, *args, **kwargs):
        if selected not in (path, *path.parents):
            return original_stat(selected, *args, **kwargs)
        is_file = selected == path
        owner = int(damage == ('file-owner' if is_file else 'parent-owner'))
        mode = 0o100644 if is_file else 0o40755
        if damage == ('file-mode' if is_file else 'parent-mode'):
            mode |= 0o020
        if is_file and damage == 'file-type':
            mode = 0o120777
        return SimpleNamespace(st_uid=owner, st_mode=mode)

    monkeypatch.setattr(Path, 'stat', metadata)
    monkeypatch.setattr(Path, 'lstat', lambda selected: metadata(selected, follow_symlinks=False))
    if damage is None:
        assert hostservice._root_file(path) == 'root registration'
    else:
        with pytest.raises(OSError, match=r'untrusted parent|root-owned regular'):
            hostservice._root_file(path)


@pytest.mark.parametrize('changed', [None, {'manager': ''}, {'user': None},
                                    {'command': 1}, {'manager': 'unknown'},
                                    {'uid': True}, {'uid': 0}, {'config': 'relative'},
                                    {'home': 'relative'}, {'command': 'relative'}])
@pytest.mark.parametrize('manager', ['openrc', 'runit'])
def test_complete_registration_still_requires_exact_native_identity(monkeypatch, changed, manager):
    record = dict(schema=1, installed=True, manager=manager, user='tester', uid=12510,
                  home='/home/tester', config='/home/tester/.config/oscmix/routing.conf',
                  command='/home/tester/.local/bin/oscmix-session')
    record.update(changed or {})
    monkeypatch.setattr(hostservice, '_root_file', lambda _: json.dumps(record))
    if changed:
        with pytest.raises(OSError, match=r'incomplete|invalid'):
            hostservice.registered()
    else:
        assert hostservice.registered() == hostservice.HostService(
            manager, 'tester', 12510, Path(record['home']), Path(record['config']),
            Path(record['command']))


@pytest.mark.parametrize('present', [False, True])
def test_absent_registration_and_invalid_json_are_distinct(tmp_path, monkeypatch, present):
    path = tmp_path / 'registration.json'
    if present:
        path.write_text('{')
    monkeypatch.setattr(hostservice, '_root_file', lambda _: path.read_text())
    if present:
        with pytest.raises(OSError, match='invalid host service registration'):
            hostservice.registered()
    else:
        assert hostservice.registered() is None


@pytest.mark.parametrize('pid', [None, 'garbage', '0', '1'])
def test_native_pid_file_absence_is_distinct_from_invalid_identity(native, pid):
    service, proc, _ = native
    path = hostservice.RUNIT_SERVICE / 'supervise/pid'
    if pid is None:
        path.unlink()
        assert hostservice.main_pid(service, proc) is None
    else:
        path.write_text(pid)
        with pytest.raises(OSError, match='invalid runit child PID'):
            hostservice.main_pid(service, proc)


def test_another_users_native_process_is_never_selected(native):
    service, proc, _ = native
    assert hostservice.main_pid(replace(service, uid=service.uid + 1), proc) is None


@pytest.mark.parametrize('missing', ['supervisor', 'child-context'])
def test_disappearing_native_process_is_not_permission_to_signal(native, missing):
    service, proc, entry = native
    path = proc / '12/cmdline' if missing == 'supervisor' else entry / 'environ'
    path.unlink()
    assert hostservice.reload(service, proc) == 'not running'


@pytest.mark.parametrize('condition', ['missing-pidfile', 'bad-pid', 'init-pid', 'zero-pid',
                                      'missing-supervisor',
                                      'no-child', 'two-children'])
def test_openrc_requires_one_live_identified_child(native, monkeypatch, condition):
    service, proc, entry = native
    service = replace(service, manager='openrc')
    (entry / 'environ').write_bytes(b'HOME=' + os.fsencode(service.home)
                                  + b'\0OSCMIX_SERVICE_MANAGER=openrc\0')
    parent = proc / '12'
    (parent / 'cmdline').write_bytes(b'\0'.join([
        b'supervise-daemon', b'oscmix-desk', b'--start', b'--pidfile',
        os.fsencode(hostservice.SUPERVISOR_PID), b'--user', b'tester']) + b'\0')
    children = parent / 'task/12/children'
    children.parent.mkdir(parents=True)
    children.write_text('1234')
    if condition == 'missing-pidfile':
        def absent(_):
            raise FileNotFoundError('supervisor stopped')
        monkeypatch.setattr(hostservice, '_root_file', absent)
    else:
        supervisor = {'bad-pid': 'invalid', 'init-pid': '1', 'zero-pid': '0'}.get(condition, '12')
        monkeypatch.setattr(hostservice, '_root_file', lambda _: supervisor)
    if condition == 'missing-supervisor':
        (parent / 'cmdline').unlink()
    elif condition == 'no-child':
        children.write_text('gone 9999')
    elif condition == 'two-children':
        replacement = proc / '4321'
        replacement.mkdir()
        for name in ('cmdline', 'environ'):
            (replacement / name).write_bytes((entry / name).read_bytes())
        children.write_text('1234 4321')
    if condition in ('bad-pid', 'init-pid', 'zero-pid', 'two-children'):
        with pytest.raises(OSError, match=r'invalid OpenRC supervisor PID|multiple matching'):
            hostservice.main_pid(service, proc)
    else:
        assert hostservice.main_pid(service, proc) is None


def test_unreadable_maintenance_state_refuses_manual_activation(tmp_path, monkeypatch):
    monkeypatch.setattr(hostservice, 'STATE', tmp_path)
    original_stat = Path.stat

    def unreadable(path, *args, **kwargs):
        if path == tmp_path / 'package-update':
            raise PermissionError('maintenance state unreadable')
        return original_stat(path, *args, **kwargs)

    monkeypatch.setattr(Path, 'stat', unreadable)
    assert 'cannot establish installation maintenance state' in hostservice.maintenance_problem()


@pytest.mark.parametrize('changed', [None, {'Maintenance': 'true'}, {'UserID': '0'},
                                    {'ConfiguredHome': '/other/home'},
                                    {'Configuration': '/other/desk'},
                                    {'ActiveState': 'inactive'}])
@pytest.mark.parametrize('manager', ['openrc', 'runit'])
def test_native_launcher_requires_ready_registered_desk(native, monkeypatch, changed, manager):
    service, _, _ = native
    monkeypatch.setenv('HOME', str(service.home))
    report = dict(LoadState='loaded', UnitFileState='enabled', manager=manager,
                  Maintenance='false', UserID=str(service.uid), ConfiguredHome=str(service.home),
                  Configuration=str(service.config), ActiveState='active')
    report.update(changed or {})
    problem = diagnostics.service_start_problem(service.config, report)
    assert bool(problem) == bool(changed)
    assert diagnostics.service_start_problem(None, report) is not None


@pytest.mark.parametrize('manager', ['openrc', 'runit'])
@pytest.mark.parametrize(('running', 'allowed', 'linked', 'maintenance'), [
    (True, True, True, False), (False, True, True, False),
    (False, True, True, True), (False, False, True, False),
    (False, True, False, False),
])
def test_native_status_keeps_activation_process_and_maintenance_distinct(
        native, tmp_path, monkeypatch, manager, running, allowed, linked, maintenance):
    service, proc, _ = native
    service = replace(service, manager=manager)
    state = tmp_path / 'state'
    state.mkdir()
    selected = tmp_path / 'manager-enabled'
    other = tmp_path / 'other-manager-enabled'
    if linked:
        selected.touch()
    else:
        other.touch()
    monkeypatch.setattr(hostservice, 'STATE', state)
    monkeypatch.setattr(hostservice, 'OPENRC_ENABLED', selected if manager == 'openrc' else other)
    monkeypatch.setattr(hostservice, 'RUNIT_SERVICE', selected if manager == 'runit' else other)
    if allowed:
        (state / 'service-allowed').touch()
    if maintenance:
        (state / 'service-update').write_text('incomplete replacement')
    monkeypatch.setattr(hostservice, 'main_pid', lambda *_: 1234 if running else None)
    before = {p.name: p.read_bytes() for p in state.iterdir()}
    report = hostservice.report(service, proc)
    assert report['state'] == 'observed'
    assert report['manager'] == manager
    assert report['LoadState'] == 'loaded'
    assert report['ActiveState'] == ('active' if running else 'inactive')
    assert report['UnitFileState'] == ('enabled' if linked and allowed else 'disabled')
    assert report['MainPID'] == ('1234' if running else '0')
    assert report['FragmentPath'] == str(hostservice.REGISTRATION)
    assert report['ConfiguredHome'] == str(service.home)
    assert report['Configuration'] == str(service.config)
    assert report['UserID'] == str(service.uid)
    assert report['Maintenance'] == ('true' if maintenance else 'false')
    assert ('maintenance' if maintenance else 'no hardware verification') in report['StatusText']
    assert {p.name: p.read_bytes() for p in state.iterdir()} == before


def test_lookalike_native_child_owned_by_another_user_is_refused(native, monkeypatch):
    service, proc, entry = native
    original_stat = Path.stat

    def owner(path, *args, **kwargs):
        if path == entry:
            return SimpleNamespace(st_uid=service.uid + 1)
        return original_stat(path, *args, **kwargs)

    monkeypatch.setattr(Path, 'stat', owner)
    assert hostservice.main_pid(service, proc) is None
    assert hostservice.reload(service, proc) == 'not running'


def test_runit_lookalike_supervisor_must_belong_to_root(native, monkeypatch):
    service, proc, _ = native
    original_stat = Path.stat

    def owner(path, *args, **kwargs):
        if path == proc / '12':
            return SimpleNamespace(st_uid=1)
        return original_stat(path, *args, **kwargs)

    monkeypatch.setattr(Path, 'stat', owner)
    with pytest.raises(OSError, match='supervisor identity changed'):
        hostservice.main_pid(service, proc)


@pytest.mark.parametrize('mask', ['11', 'a1'])
def test_native_reload_interprets_proc_signal_mask_as_hexadecimal(native, monkeypatch, mask):
    service, proc, entry = native
    (entry / 'status').write_text('PPid:\t12\nSigCgt:\t' + mask + '\n')
    sent, closed = [], []
    monkeypatch.setattr(os, 'pidfd_open', lambda _: 39, raising=False)
    monkeypatch.setattr(signal, 'pidfd_send_signal', lambda *args: sent.append(args),
                        raising=False)
    monkeypatch.setattr(os, 'close', closed.append)
    monkeypatch.setattr(hostservice.time, 'sleep',
                        lambda _: pytest.fail('ready process must not wait'))
    assert hostservice.reload(service, proc) == 'reloaded'
    assert sent == [(39, signal.SIGHUP)]
    assert closed == [39]


@pytest.mark.parametrize('override', [False, True])
def test_profile_reload_retains_the_native_process_root(native, monkeypatch, override):
    service, proc, _ = native
    if not override:
        monkeypatch.delenv('OSCMIX_PROC_ROOT')
    calls = []
    monkeypatch.setattr(process.hostservice, 'reload',
                        lambda selected, root: calls.append((selected, root)) or 'reloaded')
    assert process.reload_service() == 'reloaded'
    assert calls == [(service, proc if override else Path('/proc'))]
