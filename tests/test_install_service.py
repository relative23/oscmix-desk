"""Host maintenance decisions, interruption recovery and privilege boundaries."""
import importlib.util
import json
import os
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest


@pytest.fixture
def admin(tmp_path, monkeypatch):
    path = Path(__file__).resolve().parents[1] / 'scripts/install-service.py'
    spec = importlib.util.spec_from_file_location('install_service', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    for name in ('REGISTRATION', 'RUNNER', 'ADMIN', 'OPENRC', 'RUNIT', 'ACTIVE',
                 'OPENRC_ENABLED', 'STATE', 'RUNIT_LOG'):
        monkeypatch.setattr(module, name, tmp_path / name.lower())
    monkeypatch.setattr(module, 'RESUME_HOOKS', (tmp_path / 'hooks/resume',))
    module.STATE.mkdir()
    home = tmp_path / 'home'
    lock = home / '.local/lib/oscmix-desk/.install.lock'
    lock.parent.mkdir(parents=True)
    lock.touch()
    record = dict(schema=1, manager='openrc', user='tester', uid=os.getuid(),
                  home=str(home), config=str(home / '.config/oscmix/routing.conf'),
                  command=str(home / '.local/bin/oscmix-session'), installed=True)
    calls = []
    state = dict(active=True)

    def native(_record, action, *, check=True):
        calls.append(action)
        if action in ('start', 'stop'):
            state['active'] = action == 'start'
        code = 0 if state['active'] else 3
        return subprocess.CompletedProcess([], code if action == 'status' else 0, '', '')

    monkeypatch.setattr(module, 'native', native)
    monkeypatch.setattr(module, 'validate', lambda _: 'a' * 64)
    module.OPENRC_ENABLED.touch()
    (module.STATE / 'service-allowed').touch()
    return module, record, calls, state


def test_maintenance_preserves_activation_across_repeated_begin(admin):
    module, record, calls, state = admin
    fence = module.STATE / 'service-update'
    module.lifecycle('maintenance-begin', record)
    original = fence.read_bytes()
    assert not state['active']
    module.lifecycle('maintenance-begin', record)
    assert fence.read_bytes() == original
    assert json.loads(original) == dict(enabled=True, active=True, stopped=True)
    module.lifecycle('maintenance-finish', record)
    assert state['active']
    assert not fence.exists()
    assert calls.count('stop') == calls.count('start') == 1


@pytest.mark.parametrize(('enabled', 'active'), [(False, False), (True, False), (False, True)])
def test_maintenance_never_introduces_activation(admin, enabled, active):
    module, record, calls, state = admin
    state['active'] = active
    if not enabled:
        module.OPENRC_ENABLED.unlink()
    module.lifecycle('maintenance-begin', record)
    module.lifecycle('maintenance-finish', record)
    assert not state['active']
    assert 'start' not in calls


def test_failed_validation_retains_fence_and_stopped_service(admin, monkeypatch):
    module, record, calls, state = admin
    module.lifecycle('maintenance-begin', record)

    def mismatch(_):
        raise ValueError('GTK has another build series')

    monkeypatch.setattr(module, 'validate', mismatch)
    with pytest.raises(ValueError, match='build series'):
        module.lifecycle('maintenance-finish', record)
    assert (module.STATE / 'service-update').exists()
    assert not state['active']
    assert 'start' not in calls


@pytest.mark.parametrize('action', ['stop', 'disable'])
def test_explicit_stop_during_maintenance_overrides_previous_activation(
        admin, monkeypatch, action):
    module, record, calls, state = admin
    monkeypatch.setattr(module, 'run', lambda *_a, **_kw: None)
    module.lifecycle('maintenance-begin', record)
    module.lifecycle(action, record)
    module.lifecycle('maintenance-finish', record)
    assert not state['active']
    assert 'start' not in calls


def test_failed_stop_cannot_authorize_source_replacement(admin, monkeypatch):
    module, record, _, _ = admin

    def failed_stop(_record, action, *, check=True):
        if action == 'stop':
            raise subprocess.CalledProcessError(1, ['stop'])
        return subprocess.CompletedProcess([], 0, 'started', '')

    monkeypatch.setattr(module, 'native', failed_stop)
    with pytest.raises(subprocess.CalledProcessError):
        module.lifecycle('maintenance-begin', record)
    assert json.loads((module.STATE / 'service-update').read_text())['stopped'] is False
    with pytest.raises(ValueError, match='maintenance-begin'):
        module.lifecycle('maintenance-finish', record)


def test_adapter_update_is_fenced_and_recovers_an_interrupted_replacement(admin, monkeypatch):
    module, record, calls, state = admin
    module.lifecycle('maintenance-begin', record)
    completed = []

    def install_files(value):
        completed.append(value.copy())
        value['installed'] = True

    monkeypatch.setattr(module, 'install_files', install_files)
    record['installed'] = False
    module.lifecycle('update', record)
    assert completed
    assert record['installed'] is True
    assert (module.STATE / 'service-update').exists()
    assert not state['active']
    assert 'start' not in calls
    module.lifecycle('maintenance-finish', record)
    assert state['active']


@pytest.mark.parametrize(('manager', 'code', 'output'), [
    ('openrc', 1, 'crashed'), ('runit', 1, 'unable to open supervise/ok'),
    ('runit', 0, 'unexpected response')])
def test_unknown_manager_state_is_not_safe_maintenance(admin, monkeypatch, manager, code, output):
    module, record, _, _ = admin
    record['manager'] = manager
    module.ACTIVE.mkdir()
    monkeypatch.setattr(module, 'native', lambda *_a, **_kw:
                        subprocess.CompletedProcess([], code, output, ''))
    with pytest.raises(ValueError, match='cannot establish'):
        module.lifecycle('maintenance-begin', record)
    assert not (module.STATE / 'service-update').exists()


def test_runit_restart_delay_is_still_an_active_supervision_request(admin, monkeypatch):
    module, record, _, _ = admin
    record['manager'] = 'runit'
    monkeypatch.setattr(module, 'native', lambda *_a, **_kw:
                        subprocess.CompletedProcess([], 0, 'finish: desk: (pid 123) 2s', ''))
    assert module.active(record)


def test_stale_runit_fifo_after_shutdown_is_not_a_live_supervisor(admin):
    module, _, _, _ = admin
    directory = module.RUNIT / 'supervise'
    directory.mkdir(parents=True)
    os.mkfifo(directory / 'ok', 0o600)
    assert module.runit_supervised() is False


def test_validation_runs_user_code_only_after_selecting_uid_groups_and_clean_home(admin,
                                                                               monkeypatch):
    module, record, _, _ = admin
    record['uid'] = 12510
    account = SimpleNamespace(pw_name='tester', pw_uid=12510, pw_gid=12510)
    monkeypatch.setattr(module.pwd, 'getpwnam', lambda _: account)
    monkeypatch.setattr(module.os, 'getgrouplist', lambda *_: [12510, 29])
    calls = []
    monkeypatch.setattr(module, 'run',
                        lambda arguments, **kwargs: calls.append((arguments, kwargs)))
    module.as_user(record, [record['command'], '--dry-run'])
    _, options = calls[0]
    assert options['user'] == options['group'] == 12510
    assert options['extra_groups'] == [12510, 29]
    assert options['cwd'] == options['env']['HOME'] == record['home']
    assert 'PYTHONPATH' not in options['env']
    assert 'LD_PRELOAD' not in options['env']


@pytest.mark.parametrize('condition', ['active', 'stopped', 'disabled', 'service', 'core', 'gtk'])
def test_resume_only_reloads_an_active_enabled_desk_outside_maintenance(admin, condition):
    module, record, calls, state = admin
    if condition == 'stopped':
        state['active'] = False
    elif condition == 'disabled':
        (module.STATE / 'service-allowed').unlink()
    elif condition in ('service', 'core', 'gtk'):
        name = dict(service='service-update', core='package-update', gtk='gtk-package-update')
        (module.STATE / name[condition]).touch()
    module.lifecycle('resume', record)
    assert ('reload' in calls) == (condition == 'active')
    assert 'start' not in calls
