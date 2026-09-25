"""Launch decisions across real desk/discovery helpers, with only OS I/O simulated."""

import shutil
import socket
import subprocess
from types import SimpleNamespace

import pytest
from support import control_owner, fake_proc
from two_boxes import A, B

from oscmix_desk import desktop, diagnostics, launcher, locking


@pytest.mark.parametrize('scenario', [
    'manual', 'cold', 'runtime-enabled', 'disabled', 'wrong-desk',
    'gtk-present', 'replaced', 'timeout',
])
def test_launch_uses_the_selected_desk_through_discovery_and_service_start(
        tmp_path, tmp_path_factory, monkeypatch, diagnostic_query, scenario, request):
    home = tmp_path / 'home'
    home.mkdir()
    # '=' is a legal filename character; environment parsing must preserve it.
    path = home / 'desk=tracking.conf'
    path.write_text('[device]\nserial=%s\n[osc]\nport=9444\nrecv-port=9555\n' % B[1])
    gtk = home / 'oscmix-gtk'
    gtk.write_text('#!/bin/sh\nexit 0\n')
    gtk.chmod(0o755)
    proc = fake_proc(tmp_path / 'proc', boxes=[A, B])
    wanted_client = A[0] if scenario == 'replaced' else B[0]
    shared = tmp_path_factory.mktemp('launch')
    monkeypatch.setenv('OSCMIX_LOCK_DIR', str(shared))
    endpoint = locking.control_path(None, '2a39-3fd9-' + B[1])
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET)
    request.addfinalizer(sock.close)
    ready = fake_proc(tmp_path / 'ready', boxes=[A, B])
    control_owner(ready, endpoint, 40000, wanted_client)

    def connect():
        sock.bind(str(endpoint))
        sock.listen(1)
        shutil.copytree(ready, proc, symlinks=True, dirs_exist_ok=True)

    if scenario in ('manual', 'gtk-present'):
        connect()
    if scenario == 'gtk-present':
        # Another consumer's unrelated socket does not own desk's read channel.
        companion = socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET)
        request.addfinalizer(companion.close)
        companion.bind(str(shared / 'gui'))
    for key, value in {'HOME': home, 'OSCMIX_CONFIG': path, 'OSCMIX_BIN_GTK': gtk,
                       'OSCMIX_PROC_ROOT': proc}.items():
        monkeypatch.setenv(key, str(value))
    monkeypatch.setattr(launcher, 'MAINTENANCE_FILE', tmp_path / 'maintenance')
    monkeypatch.setattr(launcher, 'GTK_MAINTENANCE_FILE', tmp_path / 'gtk-maintenance')
    monkeypatch.setattr(diagnostics, 'query', diagnostic_query)
    monkeypatch.setattr(desktop, 'query', diagnostic_query)
    clock = [0.]
    waits, calls, notices, execs = [], [], [], []

    def sleep(seconds):
        assert 0 < seconds <= 1, 'startup polling must be bounded'
        waits.append(seconds)
        clock[0] += seconds
        if scenario != 'timeout' and len(waits) == 1:
            connect()

    monkeypatch.setattr(launcher.time, 'monotonic', lambda: clock[0])
    monkeypatch.setattr(launcher.time, 'sleep', sleep)
    monkeypatch.setattr(launcher, 'notify', lambda title, body, urgency: notices.append(body))
    monkeypatch.setattr(launcher.os, 'execve',
                        lambda binary, argv, env: execs.append((binary, argv, env)))

    def run(command, **kwargs):
        calls.append(command)
        assert kwargs.get('timeout') == 3
        assert not kwargs.get('shell')
        if command == [str(gtk), '--control-version']:
            output = 'ODK1'
        elif command[:4] == ['systemctl', '--user', 'show', '--no-pager']:
            assert scenario not in ('manual', 'gtk-present')
            assert len(command) == 6
            assert command[-1] == 'oscmix.service'
            assert command[4].startswith('--property=')
            enabled = 'enabled-runtime' if scenario == 'runtime-enabled' else 'enabled'
            if scenario == 'disabled':
                enabled = 'disabled'
            environment = ('OSCMIX_CONFIG=' + str(home / 'other.conf')
                           if scenario == 'wrong-desk' else '')
            properties = {'LoadState': 'loaded', 'ActiveState': 'inactive',
                          'UnitFileState': enabled, 'Environment': environment,
                          'ExecStart': '{ path=/usr/bin/oscmix-session ; '
                          'argv[]=/usr/bin/oscmix-session ; ignore_errors=no ; }'}
            # Like systemctl, only return properties actually requested.
            requested = command[4].partition('=')[2].split(',')
            output = '\n'.join(key + '=' + properties[key] for key in requested
                               if key in properties)
        elif command == ['systemctl', '--user', 'show-environment']:
            output = 'HOME=%s\nOSCMIX_CONFIG=%s\n' % (home, path)
        elif command in (['systemctl', '--user', 'reset-failed', 'oscmix.service'],
                         ['systemctl', '--user', 'start', '--no-block', 'oscmix.service']):
            assert scenario in ('cold', 'runtime-enabled', 'replaced', 'timeout')
            output = ''
        else:
            pytest.fail('unexpected host command: %r' % command)
        return SimpleNamespace(returncode=0, stdout=output, stderr='')

    monkeypatch.setattr(subprocess, 'run', run)
    success = scenario in ('manual', 'cold', 'runtime-enabled', 'gtk-present')
    assert launcher.main() == (0 if success else 1)
    assert [(binary, argv) for binary, argv, _ in execs] == (
        [(str(gtk), [str(gtk)])] if success else [])
    if success:
        env = execs[0][2]
        assert env['OSCMIX_CONTROL_SOCKET'] == str(endpoint)
        assert env['OSCMIX_BACKEND_PID'] == '40000'
        assert env['OSCMIX_DEVICE_SERIAL'] == B[1]
    assert bool(notices) is not success
    starts = [command for command in calls if 'start' in command]
    assert len(starts) == int(scenario in ('cold', 'runtime-enabled', 'replaced', 'timeout'))
    if scenario in ('cold', 'runtime-enabled', 'replaced'):
        assert len(waits) == 1
    if scenario == 'timeout':
        assert 1 <= clock[0] <= launcher.BACKEND_WAIT + 1
