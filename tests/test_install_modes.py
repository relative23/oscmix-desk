"""First activation, upgrade state and systemd-free source installation."""

from test_install_sh import make_fake_home, plug_in, run, session_home_stub

from oscmix_desk.constants import __version__


def test_preflight_does_not_install_or_start_anything(tmp_path):
    home, env, log = make_fake_home(tmp_path)
    plug_in(tmp_path, env)
    before = {p.relative_to(home): p.read_bytes() for p in home.rglob('*') if p.is_file()}
    result = run('install.sh', ['--check', '--no-build'], env)
    assert result.returncode == 0, result.stderr + result.stdout
    assert 'Preflight passed' in result.stdout
    assert 'candidate: ' + __version__ in result.stdout
    assert before == {p.relative_to(home): p.read_bytes() for p in home.rglob('*') if p.is_file()}
    calls = log.read_text()
    assert 'enable' not in calls
    assert 'restart' not in calls
    assert 'sudo' not in calls


def test_fresh_install_with_device_present_never_applies_example(tmp_path):
    home, env, log = make_fake_home(tmp_path)
    session_home_stub(tmp_path, str(home))
    plug_in(tmp_path, env)
    result = run('install.sh', ['--no-build'], env)
    assert result.returncode == 0, result.stderr + result.stdout
    calls = log.read_text()
    for forbidden in ('enable --quiet', 'restart', 'udevadm', 'udev.rules', 'sleep-hook'):
        assert forbidden not in calls
    assert 'automatic operation has not been enabled' in result.stdout


def test_manual_install_does_not_need_a_user_manager(tmp_path):
    home, env, log = make_fake_home(tmp_path)
    (tmp_path / 'stub-bin/systemctl').write_text('#!/bin/sh\nexit 1\n')
    result = run('install.sh', ['--no-build', '--no-udev', '--manual'], env)
    assert result.returncode == 0, result.stderr + result.stdout
    assert not (home / '.config/systemd/user/oscmix.service').exists()
    assert (home / '.local/bin/oscmix-session').is_file()
    assert 'manual foreground' in result.stdout
    assert not log.exists()


def test_missing_manager_selects_manual_without_failing_install(tmp_path):
    home, env, _ = make_fake_home(tmp_path)
    (tmp_path / 'stub-bin/systemctl').write_text('#!/bin/sh\nexit 1\n')
    result = run('install.sh', ['--no-build', '--no-udev'], env)
    assert result.returncode == 0, result.stderr + result.stdout
    assert not (home / '.config/systemd/user/oscmix.service').exists()


def test_enable_refuses_an_unrelated_user_manager_before_changes(tmp_path):
    home, env, log = make_fake_home(tmp_path)
    session_home_stub(tmp_path, '/another/home')
    result = run('install.sh', ['--no-build', '--enable'], env)
    assert result.returncode != 0
    assert 'needs the systemd user manager for' in result.stdout + result.stderr
    assert not (home / '.local/bin/oscmix-session').exists()
    assert 'sudo' not in log.read_text()


def test_manual_refuses_an_enabled_service_before_changes(tmp_path):
    home, env, log = make_fake_home(tmp_path)
    session_home_stub(tmp_path, str(home), enabled=True)
    result = run('install.sh', ['--no-build', '--manual'], env)
    assert result.returncode != 0
    assert 'stop and disable it' in result.stderr
    assert not (home / '.local/bin/oscmix-session').exists()
    assert 'stop oscmix.service' not in log.read_text()


def test_enabled_but_stopped_upgrade_does_not_restart(tmp_path):
    home, env, log = make_fake_home(tmp_path)
    session_home_stub(tmp_path, str(home), enabled=True)
    plug_in(tmp_path, env)
    result = run('install.sh', ['--no-build', '--no-udev'], env)
    assert result.returncode == 0, result.stderr + result.stdout
    assert 'restart' not in log.read_text()
    assert 'enable --quiet' not in log.read_text()


def test_active_but_disabled_upgrade_restarts_without_enabling(tmp_path):
    home, env, log = make_fake_home(tmp_path)
    session_home_stub(tmp_path, str(home), active=True)
    result = run('install.sh', ['--no-build', '--no-udev'], env)
    assert result.returncode == 0, result.stderr + result.stdout
    calls = log.read_text()
    assert 'stop oscmix.service' in calls
    assert 'restart oscmix.service' in calls
    assert 'enable --quiet' not in calls


def test_same_home_other_config_cannot_enable_another_desk(tmp_path):
    home, env, log = make_fake_home(tmp_path)
    session_home_stub(tmp_path, str(home), active=True)
    env['XDG_CONFIG_HOME'] = str(tmp_path / 'another-config')
    result = run('install.sh', ['--check', '--no-build', '--enable'], env)
    assert result.returncode != 0
    assert 'HOME and XDG_CONFIG_HOME' in result.stdout
    assert not (home / '.local/bin/oscmix-session').exists()
    assert 'stop oscmix.service' not in log.read_text()


def test_uninstall_cannot_stop_a_manager_using_another_config(tmp_path):
    home, env, log = make_fake_home(tmp_path)
    session_home_stub(tmp_path, str(home), active=True)
    env['XDG_CONFIG_HOME'] = str(tmp_path / 'another-config')
    result = run('uninstall.sh', [], env)
    assert result.returncode != 0
    assert 'stop oscmix.service' not in log.read_text()


def test_unknown_manager_identity_never_permits_enable(tmp_path):
    _, env, log = make_fake_home(tmp_path)
    session_home_stub(tmp_path, '')
    result = run('install.sh', ['--no-build', '--enable'], env)
    assert result.returncode != 0
    assert 'enable --quiet' not in log.read_text()
