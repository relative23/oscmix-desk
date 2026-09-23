"""Native-package opt-in and reversible migration, without a real user bus."""

import importlib.machinery
import importlib.util
import json
import subprocess

import pytest
from support import repo_file


@pytest.fixture
def setup(tmp_path, monkeypatch):
    path = repo_file('packaging', 'oscmix-setup')
    loader = importlib.machinery.SourceFileLoader('package_setup', str(path))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    module.original_manager_for = module.manager_for
    monkeypatch.setattr(module, 'manager_for', lambda *args: None)
    monkeypatch.setattr(module, 'live_processes', list)
    monkeypatch.setattr(module, 'service', lambda *args, **kwargs:
                        subprocess.CompletedProcess(args, 3 if args[0].startswith('is-') else 0))
    monkeypatch.setenv('XDG_STATE_HOME', str(tmp_path / 'state'))
    return module


def test_migration_and_return_preserve_custom_payload_and_desk(setup, tmp_path):
    home, config, data = tmp_path / 'home', tmp_path / 'config', tmp_path / 'data'
    package = home / '.local/lib/oscmix-desk/oscmix_desk'
    package.mkdir(parents=True)
    (package / 'custom.py').write_text('custom local module\n')
    unit = config / 'systemd/user/oscmix.service'
    unit.parent.mkdir(parents=True)
    unit.write_text('custom local service\n')
    desk = config / 'oscmix'
    desk.mkdir()
    preserved = {desk / 'routing.conf': 'custom desk\n',
                 desk / 'active-profile': 'recording\n',
                 desk / 'recording.conf': 'custom profile\n'}
    for path, content in preserved.items():
        path.write_text(content)
    setup.migrate(home, config, data)
    backup = next((tmp_path / 'state/oscmix-desk').glob('source-install-*'))
    assert not package.exists()
    assert not unit.exists()
    assert all(path.read_text() == value for path, value in preserved.items())
    setup.restore(backup, home, config)
    assert (package / 'custom.py').read_text() == 'custom local module\n'
    assert unit.read_text() == 'custom local service\n'
    assert all(path.read_text() == value for path, value in preserved.items())


def test_restore_recovers_a_migration_interrupted_between_moves(setup, tmp_path):
    home, config = tmp_path / 'home', tmp_path / 'config'
    first, second = home / 'first', home / 'second'
    home.mkdir()
    backup = tmp_path / 'backup'
    backup.mkdir()
    (backup / 'first').write_text('already moved')
    second.write_text('not yet moved')
    (backup / 'manifest.json').write_text(json.dumps([
        {'original': str(first), 'backup': str(backup / 'first')},
        {'original': str(second), 'backup': str(backup / 'second')},
    ]))
    setup.restore(backup, home, config)
    assert first.read_text() == 'already moved'
    assert second.read_text() == 'not yet moved'


def test_restore_checks_all_conflicts_before_moving_anything(setup, tmp_path):
    home, config = tmp_path / 'home', tmp_path / 'config'
    home.mkdir()
    backup = tmp_path / 'backup'
    backup.mkdir()
    (backup / 'first').write_text('first')
    (backup / 'second').write_text('second')
    (home / 'second').write_text('new user file')
    (backup / 'manifest.json').write_text(json.dumps([
        {'original': str(home / name), 'backup': str(backup / name)}
        for name in ('first', 'second')]))
    with pytest.raises(ValueError, match='overwrite'):
        setup.restore(backup, home, config)
    assert not (home / 'first').exists()
    assert (home / 'second').read_text() == 'new user file'


def test_migration_refuses_a_running_session(setup, tmp_path, monkeypatch):
    home = tmp_path / 'home'
    package = home / '.local/lib/oscmix-desk'
    package.mkdir(parents=True)
    monkeypatch.setattr(setup, 'live_processes', lambda: ['1234'])
    with pytest.raises(ValueError, match='stop manual mixer processes'):
        setup.migrate(home, home / '.config', home / '.local/share')
    assert package.is_dir()


def test_manager_must_match_both_home_and_config(setup, tmp_path, monkeypatch):
    home = tmp_path / 'home'
    monkeypatch.setattr(setup, 'service', lambda *args, **kwargs:
                        subprocess.CompletedProcess(
                            args, 0, stdout='HOME=' + str(home)
                            + '\nXDG_CONFIG_HOME=/other/config\n'))
    with pytest.raises(ValueError, match='XDG_CONFIG_HOME'):
        setup.original_manager_for(home, home / '.config')


def test_enable_requires_reviewable_config_and_cleared_overrides(setup, tmp_path):
    home = tmp_path / 'home'
    config = home / '.config'
    desk = config / 'oscmix/routing.conf'
    with pytest.raises(ValueError, match='create and review'):
        setup.enable(home, config, desk, [])
    desk.parent.mkdir(parents=True)
    desk.write_text('[device]\nname=Fireface UCX II\n')
    with pytest.raises(ValueError, match='migrate the per-user'):
        setup.enable(home, config, desk, [home / '.local/bin/oscmix-session'])
    assert not desk.with_name('service-allowed').exists()


def test_enable_sets_opt_in_before_requesting_activation(setup, tmp_path, monkeypatch):
    home = tmp_path / 'home'
    config = home / '.config'
    desk = config / 'oscmix/routing.conf'
    desk.parent.mkdir(parents=True)
    desk.write_text('[device]\nname=Fireface UCX II\n')
    monkeypatch.setattr(setup.os, 'access', lambda *args: True)
    calls = []

    def service(*args, **kwargs):
        assert desk.with_name('service-allowed').is_file()
        calls.append(args)

    monkeypatch.setattr(setup, 'service', service)
    setup.enable(home, config, desk, [])
    assert calls == [('daemon-reload',), ('enable', '--now', 'oscmix.service')]


def test_missing_shared_lock_access_prevents_activation(setup, tmp_path, monkeypatch):
    home = tmp_path / 'home'
    config = home / '.config'
    desk = config / 'oscmix/routing.conf'
    desk.parent.mkdir(parents=True)
    desk.write_text('[device]\nname=Fireface UCX II\n')
    monkeypatch.setattr(setup.os, 'access', lambda *args: False)
    with pytest.raises(ValueError, match='root:audio mode 3770'):
        setup.enable(home, config, desk, [])
    assert not desk.with_name('service-allowed').exists()


def test_source_restore_requires_removing_native_hotplug_opt_in(setup, tmp_path):
    config = tmp_path / '.config'
    allowed = config / 'oscmix/service-allowed'
    allowed.parent.mkdir(parents=True)
    allowed.write_text('still allowed despite systemctl stop')
    with pytest.raises(ValueError, match='--disable'):
        setup.restore(tmp_path / 'nonexistent-backup', tmp_path, config)
