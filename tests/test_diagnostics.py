"""Host diagnosis uses identity evidence and does not control the host."""

import subprocess
from dataclasses import replace
from types import SimpleNamespace

import pytest
from support import fake_proc
from two_boxes import A, B

from oscmix_desk import desktop, diagnostics
from oscmix_desk.model import Config


@pytest.mark.parametrize(('bound', 'state'), [
    ([], 'absent'), ([(7222, 'oscmix', A[0])], 'ready'),
    ([(7222, 'oscmix', B[0])], 'conflict'),
    ([(7222, 'python3', None)], 'conflict'),
    ([(7222, 'oscmix', None)], 'unknown'),
])
def test_backend_is_associated_with_the_exact_device(tmp_path, bound, state):
    proc = fake_proc(tmp_path, boxes=[A, B], bound=bound)
    result = diagnostics.backend_status(Config(serial=A[1]), proc)
    assert result.state == state
    assert result.device.serial == A[1]


def test_ambiguous_device_is_unknown_even_if_a_backend_is_listening(tmp_path):
    proc = fake_proc(tmp_path, boxes=[A, B], bound=[(7222, 'oscmix', A[0])])
    assert diagnostics.backend_status(Config(), proc).state == 'unknown'


@pytest.mark.parametrize(('arguments', 'port', 'state'), [
    ([], 8222, 'ready'), (['-s', 'udp!127.0.0.1!9000'], 9000, 'ready'),
    (['-sudp!127.0.0.1!9000'], 8222, 'conflict'), (['-m'], 8222, 'conflict'),
    (['-m', '-s', 'udp!127.0.0.1!8222'], 8222, 'ready'),
    (['-s', 'udp!127.0.0.1!8222', '-m'], 8222, 'conflict'),
    (['-s'], 8222, 'unknown'), (['extra'], 8222, 'unknown'),
    (['-r', 'udp', '127.0.0.1', '7222', '-s', 'udp', '127.0.0.1', '8222'], 8222, 'ready'),
    (['-sudp', '127.0.0.1', '9000'], 9000, 'ready'),
    (['-s', 'udp', '127.0.0.1', '9000'], 8222, 'conflict'),
    (['-s', 'udp', '192.0.2.1', '8222'], 8222, 'conflict'),
    (['-s', 'udp', '', '8222'], 8222, 'conflict'),
])
def test_backend_reply_endpoint_must_match_the_desks_receiver(tmp_path, arguments, port, state):
    proc = fake_proc(tmp_path, boxes=[A], bound=[(7222, 'oscmix', A[0])])
    (proc / '40000/cmdline').write_bytes(b'oscmix\0' + b'\0'.join(a.encode() for a in arguments))
    result = diagnostics.backend_status(replace(Config(), osc_recv_port=port), proc)
    assert result.state == state


def test_disappearing_backend_arguments_are_unknown(tmp_path):
    proc = fake_proc(tmp_path, boxes=[A], bound=[(7222, 'oscmix', A[0])])
    (proc / '40000/cmdline').write_bytes(b'')
    result = diagnostics.backend_status(Config(), proc)
    assert result.state == 'unknown'


def test_occupied_port_with_unreadable_owner_is_not_free(tmp_path):
    proc = fake_proc(tmp_path, boxes=[A], bound=[(7222, 'oscmix', A[0])])
    (proc / '40000/fd/3').unlink()
    result = diagnostics.backend_status(Config(), proc)
    assert result.state == 'unknown'
    assert 'owner' in result.detail


@pytest.mark.parametrize('content', ['', 'invalid header\n',
    'local_address\nmalformed row\n',
    'local_address\n0: 0100007F:ZZZZ 0 0 0 0 0 0 0 0\n'])
def test_malformed_udp_table_is_not_an_available_port(tmp_path, content):
    (tmp_path / 'net').mkdir()
    (tmp_path / 'net/udp').write_text(content)
    with pytest.raises(OSError, match="UDP table"):
        diagnostics.port_state(7222, tmp_path)


def test_missing_udp_table_is_unknown(tmp_path):
    with pytest.raises(FileNotFoundError):
        diagnostics.port_state(7222, tmp_path)
    assert diagnostics.backend_status(Config(), tmp_path).state == 'unknown'


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


SETTINGS = ("oscmix send-host '127.0.0.1'\noscmix send-port uint32 7222\n"
            "oscmix recv-host '127.0.0.1'\noscmix recv-port uint32 8222\n")


def test_gtk_connection_is_read_from_its_actual_schema(monkeypatch):
    calls = []
    monkeypatch.setattr(desktop, 'query', lambda argv: calls.append(argv) or SETTINGS)
    result = desktop.inspect_desktop(Config(), '/gtk')
    assert result.problem is None
    assert result.connection['recv-port'] == 8222
    assert calls == [['gsettings', 'list-recursively', 'oscmix']]


def test_gtk_mismatch_provides_explicit_changes_but_makes_none(monkeypatch):
    monkeypatch.setattr(desktop, 'query', lambda _: SETTINGS)
    result = desktop.inspect_desktop(replace(Config(), osc_port=9444), '/gtk')
    assert 'gsettings set oscmix send-port 9444' in result.problem
    assert result.connection['send-port'] == 7222


@pytest.mark.parametrize('content', [
    '', SETTINGS + "oscmix send-host 'duplicate'\n", SETTINGS.replace('7222', '0'),
    SETTINGS.replace('8222', '65536'), SETTINGS.replace('8222', 'True'),
    SETTINGS.replace("'127.0.0.1'", 'None'), SETTINGS.replace("'127.0.0.1'", "''"),
    SETTINGS.replace('7222', 'invalid!'),
])
def test_invalid_or_incomplete_gtk_settings_are_not_accepted(monkeypatch, content):
    monkeypatch.setattr(desktop, 'query', lambda _: content)
    assert desktop.inspect_desktop(Config(), '/gtk').problem


def test_missing_gtk_is_diagnosed_before_schema_queries(monkeypatch):
    monkeypatch.setattr(desktop, 'resolve_binary', lambda *_: None)
    assert 'not installed' in desktop.inspect_desktop().problem


def test_missing_schema_or_tool_is_a_diagnostic():
    assert 'schema/connection' in desktop.inspect_desktop(Config(), '/gtk').problem
