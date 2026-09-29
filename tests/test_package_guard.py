"""Package maintenance refusal and recovery, without root or a live package manager."""

import importlib.machinery
import importlib.util
import json
import os

import pytest
from support import repo_file


@pytest.fixture
def guard(tmp_path, monkeypatch):
    loader = importlib.machinery.SourceFileLoader(
        'package_guard', str(repo_file('packaging', 'package-guard')))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    monkeypatch.setattr(module, 'FENCE', tmp_path / 'maintenance')
    monkeypatch.setattr(module, 'GTK_FENCE', tmp_path / 'gtk-maintenance')
    monkeypatch.setattr(module, 'BACKUPS', tmp_path / 'backups')
    monkeypatch.setattr(module, 'OSTREE_BOOTED', tmp_path / 'ostree-booted')
    monkeypatch.setattr(module, 'running', list)
    return module


@pytest.mark.parametrize('action', ['check', 'install', 'remove', 'finish'])
@pytest.mark.parametrize('component', ['core', 'gtk'])
def test_ostree_composition_does_not_touch_live_fences_or_inspect_host_processes(
        guard, monkeypatch, action, component):
    guard.OSTREE_BOOTED.touch()
    guard.FENCE.touch()
    guard.GTK_FENCE.touch()
    monkeypatch.setenv('SYSTEMD_OFFLINE', '1')
    monkeypatch.setattr(guard.os, 'getuid', lambda: 0)
    monkeypatch.setattr(guard, 'running',
                        lambda: pytest.fail('composition inspected live PID set'))
    monkeypatch.setattr(guard, 'preserve_unowned',
                        lambda: pytest.fail('composition backed up host'))
    monkeypatch.setattr(guard, 'clean_bytecode', lambda: pytest.fail('composition cleaned host'))
    monkeypatch.setattr(guard.sys, 'argv',
                        ['guard', action, '--component', component, '--rpm-scriptlet'])
    assert guard.main() == 0
    assert guard.FENCE.exists()
    assert guard.GTK_FENCE.exists()


@pytest.mark.parametrize(('marker', 'offline', 'hook'), [
    (True, '1', False), (False, '1', True), (True, '0', True), (True, None, True),
])
def test_only_the_offline_ostree_rpm_hook_can_defer_maintenance(
        guard, monkeypatch, marker, offline, hook):
    if marker:
        guard.OSTREE_BOOTED.touch()
    if offline is None:
        monkeypatch.delenv('SYSTEMD_OFFLINE', raising=False)
    else:
        monkeypatch.setenv('SYSTEMD_OFFLINE', offline)
    monkeypatch.setattr(guard.os, 'getuid', lambda: 0)
    monkeypatch.setattr(guard, 'running', lambda: ['1234'])
    monkeypatch.setattr(guard.sys, 'argv',
                        ['guard', 'install', *(['--rpm-scriptlet'] if hook else [])])
    assert guard.main() == 1
    assert not guard.FENCE.exists()


@pytest.mark.parametrize('component', ['core', 'gtk'])
def test_finishing_one_component_does_not_clear_an_incomplete_other(
        guard, monkeypatch, component):
    guard.FENCE.touch()
    guard.GTK_FENCE.touch()
    monkeypatch.setattr(guard.os, 'getuid', lambda: 0)
    monkeypatch.setattr(guard.sys, 'argv', ['guard', 'finish', '--component', component])
    assert guard.main() == 0
    assert guard.FENCE.exists() is (component == 'gtk')
    assert guard.GTK_FENCE.exists() is (component == 'core')


def test_gtk_maintenance_preserves_the_core_bytecode_and_prior_fence(guard, monkeypatch):
    guard.FENCE.touch()
    monkeypatch.setattr(guard.os, 'getuid', lambda: 0)
    monkeypatch.setattr(guard.sys, 'argv', ['guard', 'remove', '--component', 'gtk'])
    monkeypatch.setattr(guard, 'clean_bytecode', lambda: pytest.fail('GTK touched core files'))
    assert guard.main() == 0
    assert guard.FENCE.exists()
    assert guard.GTK_FENCE.exists()


@pytest.mark.parametrize('previous_failure', [False, True])
def test_busy_transaction_changes_no_files_and_keeps_an_existing_fence(
        guard, tmp_path, monkeypatch, previous_failure):
    if previous_failure:
        guard.FENCE.touch()
    monkeypatch.setattr(guard, 'running', lambda: ['124'])
    monkeypatch.setattr(guard, 'preserve_unowned',
                        lambda: pytest.fail('busy update reached file maintenance'))
    with pytest.raises(ValueError, match='still running PIDs: 124'):
        guard.begin(backup=True)
    assert guard.FENCE.exists() is previous_failure
    assert not guard.BACKUPS.exists()


def test_unowned_system_files_are_copied_with_modes_and_manifest(guard, tmp_path, monkeypatch):
    legacy = tmp_path / 'old-root-install'
    legacy.write_text('custom legacy resume hook\n')
    legacy.chmod(0o750)
    monkeypatch.setattr(guard, 'SYSTEM_FILES', [str(legacy)])
    monkeypatch.setattr(guard, 'owned', lambda path: False)
    guard.begin(backup=True)
    assert guard.FENCE.exists()
    manifest, = guard.BACKUPS.glob('*/manifest.json')
    item, = json.loads(manifest.read_text())
    saved = guard.Path(item['backup'])
    assert saved.read_bytes() == legacy.read_bytes()
    assert saved.stat().st_mode == legacy.stat().st_mode
    assert saved.stat().st_uid == os.getuid()
    assert item['original'] == str(legacy)


def test_a_file_is_owned_when_any_package_database_claims_it(guard, monkeypatch):
    # A Fedora host with dpkg installed answered from dpkg's empty database,
    # and the package's own files were moved aside as legacy (until 0.8.1).
    asked = []

    def run(command, **_kwargs):
        asked.append(command[0])
        return guard.subprocess.CompletedProcess(command, 0 if command[0] == 'rpm' else 1)

    monkeypatch.setattr(guard.shutil, 'which', lambda tool: '/usr/bin/' + tool)
    monkeypatch.setattr(guard.subprocess, 'run', run)
    assert guard.owned(guard.Path('/usr/bin/oscmix-session'))
    assert asked == ['dpkg-query', 'rpm']
    monkeypatch.setattr(guard.subprocess, 'run',
                        lambda command, **_k: guard.subprocess.CompletedProcess(command, 1))
    assert not guard.owned(guard.Path('/usr/bin/oscmix-session'))


def test_unreadable_proc_data_cannot_be_taken_as_no_running_mixer(guard, tmp_path):
    loader = importlib.machinery.SourceFileLoader(
        'guard_process_scan', str(repo_file('packaging', 'package-guard')))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    entry = tmp_path / '1234'
    (entry / 'cmdline').mkdir(parents=True)
    with pytest.raises(IsADirectoryError):
        module.running(tmp_path)


def test_cleanup_removes_only_generated_bytecode(guard, tmp_path, monkeypatch):
    cache = tmp_path / '__pycache__'
    cache.mkdir()
    (cache / 'model.cpython-314.pyc').write_bytes(b'cached')
    custom = cache / 'custom-note.txt'
    custom.write_text('preserve unknown data')
    monkeypatch.setattr(guard, 'CACHE', cache)
    guard.clean_bytecode()
    assert list(cache.iterdir()) == [custom]
    assert custom.read_text() == 'preserve unknown data'


def test_interpreted_cli_and_backend_are_both_detected(tmp_path):
    loader = importlib.machinery.SourceFileLoader(
        'guard_detect', str(repo_file('packaging', 'package-guard')))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    for pid, args in [('11', b'/usr/bin/python3\0/usr/bin/oscmix-session\0'),
                      ('12', b'/usr/bin/oscmix\0-r\0udp!127.0.0.1!22752\0'),
                      ('13', b'/usr/bin/unrelated\0')]:
        entry = tmp_path / pid
        entry.mkdir()
        (entry / 'cmdline').write_bytes(args)
    assert sorted(module.running(tmp_path)) == ['11', '12']
