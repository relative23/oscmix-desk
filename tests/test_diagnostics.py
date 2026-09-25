"""Host diagnosis uses identity evidence and does not control the host."""

import subprocess
import sys
from types import SimpleNamespace

import pytest

from oscmix_desk import desktop, diagnostics

# Exact endpoint, kernel owner and bridge association: test_control_identity.py.


@pytest.mark.parametrize('failure', [OSError('denied'), subprocess.TimeoutExpired('query', 3)])
def test_query_errors_are_bounded_and_actionable(monkeypatch, diagnostic_query, failure):
    def fail(*_args, **_kwargs):
        raise failure
    monkeypatch.setattr(subprocess, 'run', fail)
    with pytest.raises(OSError, match="gsettings"):
        diagnostic_query(['gsettings', 'list-recursively', 'oscmix'])


def test_query_is_an_argument_array_and_checks_exit_status(monkeypatch, diagnostic_query):
    calls = []
    answer = SimpleNamespace(returncode=0, stdout='  value\n', stderr='')
    monkeypatch.setattr(subprocess, 'run', lambda args, **kw:
                        calls.append((args, kw)) or answer)
    assert diagnostic_query(['systemctl', '--user', 'show']) == 'value'
    assert calls[0][1]['timeout'] == 3
    assert 'shell' not in calls[0][1]
    answer.returncode = 2
    with pytest.raises(OSError, match='exited 2'):
        diagnostic_query(['systemctl'])
    answer.stderr = 'specific error'
    with pytest.raises(OSError, match='specific error'):
        diagnostic_query(['systemctl'])


def test_query_captures_real_process_output_and_replaces_unusable_text(diagnostic_query):
    """Exercise subprocess semantics, without querying any host service or setting."""
    command = [sys.executable, '-c',
               ("import os,sys; os.write(1,b'  observed\\xff\\n'); "
                "os.write(2,b'problem \\xff'); sys.exit(int(sys.argv[1]))")]
    assert diagnostic_query([*command, '0']) == 'observed\ufffd'
    with pytest.raises(OSError, match='problem \ufffd'):
        diagnostic_query([*command, '7'])


def test_service_query_does_not_infer_verification(monkeypatch):
    monkeypatch.setattr(diagnostics, 'query', lambda _: 'LoadState=loaded\nActiveState=active\n')
    assert diagnostics.service_status()['ActiveState'] == 'active'
    monkeypatch.setattr(diagnostics, 'query', lambda _: 'MainPID=42\n')
    assert diagnostics.service_status()['state'] == 'unknown'


def test_service_query_failure_is_unavailable():
    assert diagnostics.service_status()['state'] == 'unavailable'


def enabled():
    return {'LoadState': 'loaded', 'UnitFileState': 'enabled',
            'ExecStart': '{ path=/usr/bin/oscmix-session ; argv[]=/usr/bin/oscmix-session ; '
                         'ignore_errors=no ; }'}


@pytest.mark.parametrize(('change', 'message'), [
    ({'LoadState': 'not-found'}, 'manual'),
    ({'UnitFileState': 'disabled'}, 'not enabled'),
    ({'EnvironmentFiles': '/etc/custom'}, 'environment files'),
    ({'UnsetEnvironment': 'XDG_CONFIG_HOME'}, 'unsets environment'),
    ({'ExecStart': 'unknown'}, 'custom or unknown'),
    ({'ExecStart': 'argv[]=/usr/bin/oscmix-session --config /other ; ignore_errors=no'}, 'custom'),
])
def test_launcher_never_starts_an_unapproved_or_custom_session(change, message):
    assert message in diagnostics.service_start_problem(None, {**enabled(), **change})


def test_service_and_launcher_must_resolve_the_same_desk(tmp_path, monkeypatch):
    path = tmp_path / 'routing.conf'
    path.write_text('')
    monkeypatch.setenv('HOME', str(tmp_path))
    monkeypatch.setattr(diagnostics, 'query', lambda _:
                        'HOME=%s\nOSCMIX_CONFIG=%s\n' % (tmp_path, path))
    assert diagnostics.service_start_problem(path, enabled()) is None
    changed = {**enabled(), 'Environment': 'OSCMIX_CONFIG=/other'}
    assert 'different' in diagnostics.service_start_problem(path, changed)
    monkeypatch.setattr(diagnostics, 'query', lambda _: 'HOME=/someone-else\n')
    assert 'different' in diagnostics.service_start_problem(path, enabled())


def test_unreadable_service_environment_is_not_permission_to_start():
    assert 'cannot establish' in diagnostics.service_start_problem(None, enabled())


def test_missing_service_command_cannot_be_treated_as_the_default_session():
    service = enabled()
    service.pop('ExecStart')
    assert 'unknown service command' in diagnostics.service_start_problem(None, service)


def test_gtk_protocol_is_queried_without_a_display_or_connection(monkeypatch):
    calls = []
    monkeypatch.setattr(desktop, 'query', lambda argv: calls.append(argv) or 'ODK1')
    result = desktop.inspect_desktop('/gtk')
    assert result.problem is None
    assert result.connection['protocol'] == 'ODK1'
    assert calls == [['/gtk', '--control-version']]


@pytest.mark.parametrize('content', ['', 'ODK2', 'ODK1 extra', 'ODK1\nODK1', '1'])
def test_incompatible_gtk_is_not_accepted(monkeypatch, content):
    monkeypatch.setattr(desktop, 'query', lambda _: content)
    assert 'incompatible' in desktop.inspect_desktop('/gtk').problem


def test_missing_gtk_is_diagnosed_before_protocol_query(monkeypatch):
    monkeypatch.setattr(desktop, 'resolve_binary', lambda *_: None)
    assert 'not installed' in desktop.inspect_desktop().problem


def test_unreadable_gtk_protocol_is_a_diagnostic():
    assert 'cannot identify' in desktop.inspect_desktop('/gtk').problem
