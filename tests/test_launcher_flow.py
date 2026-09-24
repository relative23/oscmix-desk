"""Launch decisions across real desk/discovery helpers, with only OS I/O simulated."""

import shutil
import subprocess
from types import SimpleNamespace

import pytest
from support import fake_proc
from two_boxes import A, B

from oscmix_desk import desktop, diagnostics, launcher


@pytest.mark.parametrize('scenario', [
    'manual', 'cold', 'runtime-enabled', 'disabled', 'wrong-desk',
    'reply-busy', 'replaced', 'timeout',
])
def test_launch_uses_the_selected_desk_through_discovery_and_service_start(
        tmp_path, monkeypatch, diagnostic_query, scenario):
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
    bound = [(9444, 'oscmix', wanted_client)]
    if scenario == 'reply-busy':
        bound.append((9555, 'oscmix-gtk', None))
    ready = fake_proc(tmp_path / 'ready', boxes=[A, B], bound=bound)
    (ready / '40000/cmdline').write_bytes(
        b'\0'.join([b'oscmix', b'-s', b'udp', b'127.0.0.1', b'9555', b'']))

    def connect():
        shutil.copytree(ready, proc, symlinks=True, dirs_exist_ok=True)

    if scenario in ('manual', 'reply-busy'):
        connect()
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
    monkeypatch.setattr(launcher.os, 'execv', lambda binary, argv: execs.append((binary, argv)))

    def run(command, **kwargs):
        calls.append(command)
        assert kwargs.get('timeout') == 3
        assert not kwargs.get('shell')
        if command == ['gsettings', 'list-recursively', 'oscmix']:
            output = ("oscmix send-host '127.0.0.1'\noscmix recv-host '127.0.0.1'\n"
                      'oscmix send-port uint32 9444\noscmix recv-port uint32 9555\n')
        elif command[:4] == ['systemctl', '--user', 'show', '--no-pager']:
            assert scenario not in ('manual', 'reply-busy')
            enabled = 'enabled-runtime' if scenario == 'runtime-enabled' else 'enabled'
            if scenario == 'disabled':
                enabled = 'disabled'
            environment = ('OSCMIX_CONFIG=' + str(home / 'other.conf')
                           if scenario == 'wrong-desk' else '')
            output = ('LoadState=loaded\nActiveState=inactive\nUnitFileState=%s\n'
                      'ExecStart={ path=/usr/bin/oscmix-session ; '
                      'argv[]=/usr/bin/oscmix-session ; ignore_errors=no ; }\n'
                      'Environment=%s\n' % (enabled, environment))
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
    success = scenario in ('manual', 'cold', 'runtime-enabled')
    assert launcher.main() == (0 if success else 1)
    assert execs == ([(str(gtk), [str(gtk)])] if success else [])
    assert bool(notices) is not success
    starts = [command for command in calls if 'start' in command]
    assert len(starts) == int(scenario in ('cold', 'runtime-enabled', 'replaced', 'timeout'))
    if scenario in ('cold', 'runtime-enabled', 'replaced'):
        assert len(waits) == 1
    if scenario == 'timeout':
        assert 1 <= clock[0] <= launcher.BACKEND_WAIT + 1
    if scenario == 'reply-busy':
        assert '9555' in notices[0]
