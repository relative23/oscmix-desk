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
    assert result.binary == '/gtk'
    assert result.connection == {'protocol': 'ODK1',
                                 'endpoint': 'selected and checked by oscmix-launch'}
    assert calls == [['/gtk', '--control-version']]


@pytest.mark.parametrize('content', ['', 'ODK2', 'ODK1 extra', 'ODK1\nODK1', '1'])
def test_incompatible_gtk_is_not_accepted(monkeypatch, content):
    monkeypatch.setattr(desktop, 'query', lambda _: content)
    result = desktop.inspect_desktop('/gtk')
    assert 'incompatible' in result.problem
    assert result.binary == '/gtk'
    assert result.connection == {}


@pytest.mark.parametrize('override', [False, True])
def test_gtk_diagnostics_resolve_the_actual_companion_before_inspection(
        tmp_path, monkeypatch, override):
    binary = tmp_path / ('selected-gtk' if override else 'oscmix-gtk')
    binary.write_text('#!/bin/sh\nexit 0\n')
    binary.chmod(0o755)
    monkeypatch.delenv('OSCMIX_BIN_GTK', raising=False)
    monkeypatch.setenv('HOME', str(tmp_path))
    monkeypatch.setenv('PATH', str(tmp_path))
    if override:
        monkeypatch.setenv('OSCMIX_BIN_GTK', str(binary))
    calls = []
    monkeypatch.setattr(desktop, 'query', lambda argv: calls.append(argv) or 'ODK1')
    result = desktop.inspect_desktop()
    assert result.binary == str(binary)
    assert result.problem is None
    assert calls == [[str(binary), '--control-version']]


def test_missing_gtk_is_diagnosed_before_protocol_query(monkeypatch):
    monkeypatch.setattr(desktop, 'resolve_binary', lambda *_: None)
    assert 'not installed' in desktop.inspect_desktop().problem


def test_service_query_preserves_all_requested_status_and_launcher_fields(monkeypatch):
    from oscmix_desk.constants import SERVICE_UNIT

    fields = {
        'LoadState': 'loaded', 'ActiveState': 'active', 'UnitFileState': 'enabled',
        'MainPID': '42', 'StatusText': 'waiting for device',
        'FragmentPath': '/usr/lib/systemd/user/oscmix.service',
        'ExecStart': '/usr/bin/oscmix-session', 'Environment': 'OSCMIX_CONFIG=/desk',
        'EnvironmentFiles': '/etc/private-desk', 'UnsetEnvironment': 'XDG_CONFIG_HOME',
    }

    def query(argv):
        assert argv[:4] == ['systemctl', '--user', 'show', '--no-pager']
        assert argv[-1] == SERVICE_UNIT
        requested = argv[4].removeprefix('--property=').split(',')
        return '\n'.join(key + '=' + fields[key] for key in requested if key in fields)

    monkeypatch.setattr(diagnostics, 'query', query)
    assert diagnostics.service_status() == {'state': 'observed', **fields}


@pytest.mark.parametrize('missing', ['LoadState', 'ActiveState'])
def test_service_report_missing_either_required_state_is_unknown(monkeypatch, missing):
    values = {'LoadState': 'loaded', 'ActiveState': 'active'}
    values.pop(missing)
    monkeypatch.setattr(diagnostics, 'query',
                        lambda _: '\n'.join(k + '=' + v for k, v in values.items()))
    assert diagnostics.service_status() == {
        'state': 'unknown', 'detail': 'incomplete systemd service report'}


@pytest.mark.parametrize('source', ['registration', 'manager'])
def test_unavailable_service_report_retains_the_failure_detail(monkeypatch, source):
    from oscmix_desk import hostservice

    def fail(*_args):
        raise OSError('cannot read selected manager')

    if source == 'registration':
        monkeypatch.setattr(hostservice, 'registered', fail)
    else:
        monkeypatch.setattr(diagnostics, 'query', fail)
    assert diagnostics.service_status() == {
        'state': 'unavailable', 'detail': 'cannot read selected manager'}


def test_unreadable_gtk_protocol_is_a_diagnostic():
    result = desktop.inspect_desktop('/gtk')
    assert 'cannot identify' in result.problem
    assert result.binary == '/gtk'
    assert result.connection == {}


@pytest.mark.parametrize('source', ['manager', 'unit'])
def test_launcher_config_identity_preserves_spaces_and_equals(tmp_path, monkeypatch, source):
    path = tmp_path / 'desk space=a=b.conf'
    path.write_text('')
    monkeypatch.setenv('HOME', str(tmp_path))
    manager_path = path if source == 'manager' else tmp_path / 'other.conf'
    monkeypatch.setattr(diagnostics, 'query', lambda _:
                        'HOME=%s\nOSCMIX_CONFIG=%s\n' % (tmp_path, manager_path))
    service = enabled()
    if source == 'unit':
        service['Environment'] = '"OSCMIX_CONFIG=%s"' % path
    assert diagnostics.service_start_problem(path, service) is None
    assert 'different' in diagnostics.service_start_problem(tmp_path / 'other.conf', service)
