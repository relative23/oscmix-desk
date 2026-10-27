"""Source service opt-in, legacy migration and interrupted deactivation."""

import os
import shutil
import subprocess

import pytest
from test_install_sh import (
    backend_identity_stub,
    make_fake_home,
    plug_in,
    run,
    session_home_stub,
)

GUARD = 'ConditionPathExists=%E/oscmix/service-allowed\n'

pytestmark = pytest.mark.skipif(
    bool(os.environ.get('MUTANT_UNDER_TEST')),
    reason='subprocess tests cannot observe mutants',
)


def _installed(tmp_path, *, legacy=False, allowed=True):
    home, env, log = make_fake_home(tmp_path)
    result = run('install.sh', ['--no-build', '--no-udev'], env)
    assert result.returncode == 0, result.stdout + result.stderr
    unit = home / '.config/systemd/user/oscmix.service'
    contents = unit.read_text().replace(
        'ExecStart=%h/.local/bin/oscmix-session',
        'ExecStart=%h/.local/bin/oscmix-session --timeout=5')
    unit.write_text(contents.replace(GUARD, '') if legacy else contents)
    marker = home / '.config/oscmix/service-allowed'
    if allowed:
        marker.write_text('previous explicit activation\n')
    profiles = marker.parent / 'profiles'
    profiles.mkdir()
    (profiles / 'recording.conf').write_text('# selected desk\n')
    (marker.parent / 'active-profile').write_text('recording\n')
    log.write_text('')
    return home, env, log, unit, marker


@pytest.mark.parametrize('legacy', [False, True])
@pytest.mark.parametrize(('active', 'enabled'), [(False, False), (False, True),
                                               (True, False), (True, True)])
def test_disable_keeps_the_payload_and_configuration_without_needing_a_backend(
        tmp_path, legacy, active, enabled):
    home, env, log, unit, marker = _installed(tmp_path, legacy=legacy)
    session_home_stub(tmp_path, str(home), active=active, enabled=enabled)
    plug_in(tmp_path, env)
    # Deactivation must work even when the backend installation is broken.
    for binary in ('oscmix', 'alsaseqio', 'oscmix-gtk'):
        (home / '.local/bin' / binary).unlink()
    before = {path: path.read_bytes() for path in home.rglob('*') if path.is_file()}
    for _ in range(2):
        result = run('install.sh', ['--disable'], env)
        assert result.returncode == 0, result.stdout + result.stderr
        assert 'automatic operation disabled' in result.stdout
    assert not marker.exists()
    assert unit.read_text().count(GUARD) == 1
    for path, contents in before.items():
        if path not in (unit, marker):
            assert path.read_bytes() == contents, path
    if legacy:
        assert unit.read_text().encode() == before[unit] + ('\n[Unit]\n' + GUARD).encode()
        assert [path.read_bytes() for path in unit.parent.glob('oscmix.service.bak.*')] == [
            before[unit]]
    else:
        assert unit.read_bytes() == before[unit]
    calls = log.read_text()
    assert calls.index('daemon-reload') < calls.index('stop oscmix.service')
    assert calls.index('stop oscmix.service') < calls.index('disable --quiet oscmix.service')
    for forbidden in ('restart', 'enable --quiet', 'udevadm', 'sudo', 'systemd-tmpfiles'):
        assert forbidden not in calls


def test_disable_check_is_read_only_and_does_not_require_the_backend(tmp_path):
    home, env, log, _, marker = _installed(tmp_path, legacy=True)
    (home / '.local/bin/oscmix').unlink()
    before = {path: path.read_bytes() for path in home.rglob('*') if path.is_file()}
    result = run('install.sh', ['--check', '--disable'], env)
    assert result.returncode == 0, result.stdout + result.stderr
    assert 'Preflight passed' in result.stdout
    assert marker.is_file()
    assert before == {path: path.read_bytes() for path in home.rglob('*') if path.is_file()}
    calls = log.read_text()
    assert 'daemon-reload' not in calls
    assert 'stop oscmix.service' not in calls
    assert 'disable --quiet' not in calls


@pytest.mark.parametrize('manager', ['absent', 'unknown', 'other-home', 'other-config'])
def test_disable_refuses_an_unrelated_manager_before_changes(tmp_path, manager):
    home, env, log, _, marker = _installed(tmp_path, legacy=True)
    if manager == 'absent':
        (tmp_path / 'stub-bin/systemctl').write_text('#!/bin/sh\nexit 1\n')
    else:
        session_home_stub(tmp_path, {'unknown': '', 'other-home': '/another/home'}.get(
            manager, str(home)), active=True, enabled=True)
    if manager == 'other-config':
        env['XDG_CONFIG_HOME'] = str(tmp_path / 'another-config')
    before = {path: path.read_bytes() for path in home.rglob('*') if path.is_file()}
    result = run('install.sh', ['--disable'], env)
    assert result.returncode != 0
    assert '--disable needs' in result.stdout + result.stderr
    assert marker.is_file()
    assert before == {path: path.read_bytes() for path in home.rglob('*') if path.is_file()}
    assert 'stop oscmix.service' not in log.read_text()
    assert 'disable --quiet' not in log.read_text()


@pytest.mark.parametrize('other', ['--enable', '--manual'])
def test_disable_conflicts_are_refused_before_preflight(tmp_path, other):
    _, env, log = make_fake_home(tmp_path)
    result = run('install.sh', ['--disable', other], env)
    assert result.returncode == 2
    assert 'cannot be combined' in result.stderr
    assert not log.exists()


def test_disable_without_a_source_unit_changes_nothing(tmp_path):
    home, env, log = make_fake_home(tmp_path)
    before = {path: path.read_bytes() for path in home.rglob('*') if path.is_file()}
    result = run('install.sh', ['--disable'], env)
    assert result.returncode != 0
    assert 'installed source service unit' in result.stdout + result.stderr
    assert before == {path: path.read_bytes() for path in home.rglob('*') if path.is_file()}
    assert 'disable --quiet' not in log.read_text()


@pytest.mark.parametrize('tool', ['cat', 'cp', 'install'])
def test_failure_to_gate_a_legacy_unit_keeps_permission_and_reports_failure(tmp_path, tool):
    _, env, log, unit, marker = _installed(tmp_path, legacy=True)
    before = unit.read_bytes()
    stub = tmp_path / 'stub-bin' / tool
    stub.write_text('#!/bin/sh\nexit 28\n')
    stub.chmod(0o755)
    result = run('install.sh', ['--disable'], env)
    assert result.returncode != 0
    assert 'could not add' in result.stderr
    assert unit.read_bytes() == before
    assert marker.is_file()
    assert 'daemon-reload' not in log.read_text()
    assert 'stop oscmix.service' not in log.read_text()


@pytest.mark.parametrize('failure', ['daemon-reload', 'stop oscmix.service',
                                   'disable --quiet oscmix.service'])
def test_interrupted_disable_is_reported_and_an_upgrade_cannot_recreate_permission(
        tmp_path, failure):
    home, env, log, unit, marker = _installed(tmp_path, legacy=True)
    session_home_stub(tmp_path, str(home), active=True, enabled=True)
    stub = tmp_path / 'stub-bin/systemctl'
    stub.write_text(stub.read_text().replace(
        'for a in "$@"; do\n',
        'case "$*" in *"' + failure + '"*) exit 1 ;; esac\nfor a in "$@"; do\n'))
    result = run('install.sh', ['--disable'], env)
    assert result.returncode != 0
    assert 'automatic operation disabled' not in result.stdout
    assert GUARD in unit.read_text()
    assert marker.exists() == (failure == 'daemon-reload')
    if failure == 'daemon-reload':
        session_home_stub(tmp_path, str(home), enabled=True)
        assert run('install.sh', ['--disable'], env).returncode == 0
    # Enable links can survive a failed disable. The removed opt-in wins.
    session_home_stub(tmp_path, str(home), enabled=True)
    plug_in(tmp_path, env)
    log.write_text('')
    upgrade = run('install.sh', ['--no-build'], env)
    assert upgrade.returncode == 0, upgrade.stdout + upgrade.stderr
    assert not marker.exists()
    assert 'restart' not in log.read_text()
    assert 'udevadm' not in log.read_text()
    log.write_text('')
    enable = run('install.sh', ['--no-build', '--no-udev', '--enable'], env)
    assert enable.returncode == 0, enable.stdout + enable.stderr
    assert marker.is_file()
    assert 'enable --quiet oscmix.service' in log.read_text()
    assert 'restart oscmix.service' in log.read_text()


def test_manual_mode_requires_the_opt_in_to_be_removed_even_if_the_unit_is_stopped(tmp_path):
    home, env, log, unit, marker = _installed(tmp_path)
    result = run('install.sh', ['--no-build', '--no-udev', '--manual'], env)
    assert result.returncode != 0
    assert './install.sh --disable' in result.stderr
    assert marker.is_file()
    assert 'stop oscmix.service' not in log.read_text()
    assert run('install.sh', ['--disable'], env).returncode == 0
    manual = run('install.sh', ['--no-build', '--no-udev', '--manual'], env)
    assert manual.returncode == 0, manual.stdout + manual.stderr
    assert 'manual foreground' in manual.stdout
    assert not marker.exists()
    assert GUARD in unit.read_text()
    assert (home / '.local/bin/oscmix-session').is_file()


def test_source_uninstall_removes_permission_but_keeps_the_selected_desk(tmp_path):
    home, env, _, _, marker = _installed(tmp_path)
    config = marker.parent
    before = {path: path.read_bytes() for path in config.rglob('*')
              if path.is_file() and path != marker}
    result = run('uninstall.sh', [], env)
    assert result.returncode == 0, result.stdout + result.stderr
    assert not marker.exists()
    assert all(path.read_bytes() == contents for path, contents in before.items())
    # The fixture's backend binaries were uninstalled as well.
    for name in ('oscmix', 'alsaseqio'):
        binary = home / '.local/bin' / name
        binary.write_text(backend_identity_stub())
        binary.chmod(0o755)
    reinstall = run('install.sh', ['--no-build', '--no-udev'], env)
    assert reinstall.returncode == 0, reinstall.stdout + reinstall.stderr
    assert not marker.exists()


@pytest.mark.parametrize('legacy', [False, True])
def test_deactivation_guard_survives_a_custom_condition_reset(tmp_path, legacy):
    if shutil.which('systemd-analyze') is None:
        pytest.skip('systemd-analyze is not installed')
    _, env, _, unit, marker = _installed(tmp_path, legacy=legacy, allowed=False)
    unit.write_text(unit.read_text().replace('After=sound.target',
                                           'ConditionPathExists=\nAfter=sound.target'))
    manual = run('install.sh', ['--no-build', '--no-udev', '--manual'], env)
    assert manual.returncode != 0
    assert './install.sh --disable' in manual.stderr
    result = run('install.sh', ['--disable'], env)
    assert result.returncode == 0, result.stdout + result.stderr
    assert not marker.exists()
    env['SYSTEMD_UNIT_PATH'] = str(unit.parent)
    for permission in (False, True, False):
        if permission:
            marker.touch()
        else:
            marker.unlink(missing_ok=True)
        evaluated = subprocess.run(['systemd-analyze', 'condition', '--user',
                                    '--unit=oscmix.service'], env=env,
                                   capture_output=True, text=True, timeout=10)
        assert evaluated.returncode == (0 if permission else 1), (
            evaluated.stdout + evaluated.stderr)
    before = unit.read_bytes()
    assert run('install.sh', ['--disable'], env).returncode == 0
    assert unit.read_bytes() == before
