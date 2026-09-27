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

    def finish_importing(_):
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


def test_another_users_host_service_is_unavailable_not_inactive(native):
    service, proc, _ = native
    report = hostservice.report(replace(service, uid=service.uid + 1), proc)
    assert report['state'] == 'unavailable'
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
    monkeypatch.setattr(hostservice, '_root_file', lambda _: '12\n')
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
