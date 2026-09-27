"""The desktop launcher, held to the same standard as the rest.

It was the least covered file in the repository (61%) for as long as it
lived in bin/ and duplicated its own helpers. Moving it into the package
put it inside the architecture test and the mutation scope; these tests
put it inside the coverage.

Every path here matters to somebody who double-clicked a desktop icon:
what they get instead of a traceback is a notification, and the only
thing that decides which notification is this module.
"""

import os
import subprocess
from pathlib import Path

import pytest

from oscmix_desk import paths as paths_mod

#: Read at collection, before any fixture patches them.
_IMPORTED_SYSTEM_CONFIG = paths_mod.SYSTEM_CONFIG


@pytest.fixture
def clean_env(monkeypatch, tmp_path_factory):
    """No inherited OSCMIX_* or XDG_CONFIG_HOME leaking into a test, and a
    HOME with nothing in it: without XDG_CONFIG_HOME the launcher reads
    ~/.config, which must not be the developer's."""
    for name in ("OSCMIX_CONFIG", "OSCMIX_NO_NOTIFY", "OSCMIX_BIN_GTK",
                 "XDG_CONFIG_HOME"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("HOME", str(tmp_path_factory.mktemp("home")))
    return monkeypatch


def write_conf(path, body):
    path.write_text(body, encoding="utf-8")
    return path


def test_the_launcher_finds_the_file_the_backend_would(launcher_world, tmp_path):
    """The shared config lookup is reached through the actual launcher.

    A relative XDG_CONFIG_HOME is ignored by both (0.6.10),
    an empty HOME is looked up, and a uid without a passwd entry has no
    user directory: `~/.config` is not read relative to the cwd."""
    import pwd

    launch_mod, clean_env, _, notices, _ = launcher_world
    found_paths = []
    backend_status = launch_mod.backend_status

    def inspect(config, proc, config_path):
        assert config.osc_port == 9005
        found_paths.append(config_path)
        return backend_status(config, proc, config_path)

    clean_env.setattr(launch_mod, 'backend_status', inspect)
    home = tmp_path / "home"
    xdg = tmp_path / "xdg"
    system = tmp_path / "etc" / "routing.conf"
    for path in (home / ".config" / "oscmix" / "routing.conf",
                 xdg / "oscmix" / "routing.conf", system,
                 tmp_path / "named.conf",
                 tmp_path / "rel" / "oscmix" / "routing.conf",
                 tmp_path / "~" / ".config" / "oscmix" / "routing.conf"):
        path.parent.mkdir(parents=True, exist_ok=True)
        write_conf(path, "[osc]\nport = 9005\n")
    clean_env.setenv("OSCMIX_SYSTEM_CONFIG", str(system))
    clean_env.chdir(tmp_path)
    passwd_home = type("pw", (), {"pw_dir": str(home)})()

    def this_user(uid):
        assert uid == os.getuid(), uid
        return passwd_home

    def no_entry(uid):
        raise KeyError(uid)

    cases = [
        ({}, passwd_home),
        ({"OSCMIX_CONFIG": str(tmp_path / "named.conf")}, passwd_home),
        ({"XDG_CONFIG_HOME": str(xdg)}, passwd_home),
        ({"XDG_CONFIG_HOME": "rel"}, passwd_home),
        ({"XDG_CONFIG_HOME": str(tmp_path / "empty")}, passwd_home),
        ({"HOME": str(tmp_path / "nohome")}, passwd_home),
        ({"HOME": ""}, passwd_home),
        ({"HOME": None}, passwd_home),
        ({"HOME": None}, no_entry),
    ]
    seen = set()
    for case, passwd in cases:
        clean_env.setenv("HOME", str(home))
        clean_env.setattr(pwd, "getpwuid",
                          passwd if callable(passwd) else this_user)
        for name in ("OSCMIX_CONFIG", "XDG_CONFIG_HOME"):
            clean_env.delenv(name, raising=False)
        for name, value in case.items():
            if value is None:
                clean_env.delenv(name, raising=False)
            else:
                clean_env.setenv(name, value)
        assert launch_mod.main() == 0
        assert notices == []
        found = found_paths[-1]
        assert found == paths_mod.discover_config_path(), case
        seen.add(found)
    assert seen == {home / ".config" / "oscmix" / "routing.conf",
                    tmp_path / "named.conf", xdg / "oscmix" / "routing.conf",
                    system}


def test_the_system_config_default_and_environment_are_resolved_in_one_place(clean_env, tmp_path):
    assert str(_IMPORTED_SYSTEM_CONFIG) == "/etc/oscmix/routing.conf"
    clean_env.delenv("OSCMIX_SYSTEM_CONFIG")
    clean_env.setattr(paths_mod, "SYSTEM_CONFIG", tmp_path / "a")
    assert paths_mod.system_config({}) == tmp_path / "a"
    assert paths_mod.system_config(
        {"OSCMIX_SYSTEM_CONFIG": str(tmp_path / "b")}) == tmp_path / "b"
    # An explicitly supplied environment, such as the unit's, is independent
    # of the environment inherited by this process.
    clean_env.setenv("OSCMIX_SYSTEM_CONFIG", str(tmp_path / "c"))
    assert paths_mod.system_config({}) == tmp_path / "a"


def test_launcher_uses_the_shared_system_config_default(launcher_world, tmp_path):
    mod, monkey, calls, notices, execs = launcher_world
    path = write_conf(tmp_path / 'system.conf', '[osc]\nport=9333\n')
    monkey.delenv('OSCMIX_SYSTEM_CONFIG')
    monkey.setattr(paths_mod, 'SYSTEM_CONFIG', path)
    selected = []
    original = mod.backend_status

    def inspect(config, proc, config_path):
        selected.append((config_path, config.osc_port))
        return original(config, proc, config_path)

    monkey.setattr(mod, 'backend_status', inspect)
    assert mod.main() == 0
    assert selected == [(path, 9333)]
    assert calls == notices == []
    assert len(execs) == 1


def test_notify_calls_notify_send_with_the_urgency_it_was_given(
        launch_mod, clean_env):
    calls = []
    clean_env.setattr(subprocess, "run",
                      lambda cmd, **kw: calls.append(cmd) or None)
    launch_mod.notify("summary", "body", urgency="critical")
    assert calls == [["notify-send", "--urgency", "critical",
                      "--icon", "oscmix", "summary", "body"]]


def test_notify_is_suppressed_by_the_environment(launch_mod, clean_env):
    clean_env.setenv("OSCMIX_NO_NOTIFY", "1")
    clean_env.setattr(subprocess, "run",
                      lambda *a, **kw: pytest.fail("notify-send was called"))
    launch_mod.notify("summary", "body")


def test_a_missing_notify_send_is_not_an_error(launch_mod, clean_env):
    # A headless session has no notification daemon. The mixer should
    # still start.
    def boom(*_a, **_kw):
        raise OSError("no notify-send")

    clean_env.setattr(subprocess, "run", boom)
    launch_mod.notify("summary", "body")


def test_systemctl_returns_the_exit_status_it_saw(launch_mod, clean_env):
    seen = []

    class Result:
        returncode = 3

    clean_env.setattr(subprocess, "run",
                      lambda cmd, **kw: seen.append(cmd) or Result())
    assert launch_mod.systemctl_user("is-active", "--quiet", "x.service") == 3
    assert seen == [["systemctl", "--user", "is-active", "--quiet",
                     "x.service"]]


def test_a_missing_systemctl_reports_failure_rather_than_raising(launch_mod,
                                                                 clean_env):
    def boom(*_a, **_kw):
        raise OSError("no systemctl")

    clean_env.setattr(subprocess, "run", boom)
    assert launch_mod.systemctl_user("start", "x.service") == 1


def test_an_executable_override_wins(launch_mod, clean_env, tmp_path):
    gtk = tmp_path / "oscmix-gtk"
    gtk.write_text("#!/bin/sh\n")
    gtk.chmod(0o755)
    clean_env.setenv("OSCMIX_BIN_GTK", str(gtk))
    assert launch_mod.resolve_gtk_binary() == str(gtk)


def test_a_non_executable_override_is_refused_rather_than_ignored(
        launch_mod, clean_env, tmp_path):
    # Falling back to PATH here would start a different binary than the
    # one the environment named, which is the opposite of an override.
    gtk = tmp_path / "oscmix-gtk"
    gtk.write_text("not executable\n")
    gtk.chmod(0o644)
    clean_env.setenv("OSCMIX_BIN_GTK", str(gtk))
    assert launch_mod.resolve_gtk_binary() is None


def test_the_binary_is_found_on_the_path(launch_mod, clean_env, tmp_path):
    # No pinned install (empty HOME): PATH serves, exactly as before.
    from oscmix_desk import discovery

    clean_env.setenv("HOME", str(tmp_path))
    clean_env.setattr(discovery.shutil, "which",
                      lambda _n: "/usr/bin/oscmix-gtk")
    assert launch_mod.resolve_gtk_binary() == "/usr/bin/oscmix-gtk"


def test_the_known_install_directories_are_searched_when_path_misses(
        launch_mod, clean_env, tmp_path):
    # install.sh puts it in ~/.local/bin, which is not on every desktop
    # session's PATH.
    from oscmix_desk import discovery

    local_bin = tmp_path / ".local" / "bin"
    local_bin.mkdir(parents=True)
    gtk = local_bin / "oscmix-gtk"
    gtk.write_text("#!/bin/sh\n")
    gtk.chmod(0o755)
    clean_env.setattr(discovery.shutil, "which", lambda _n: None)
    clean_env.setenv("HOME", str(tmp_path))
    assert launch_mod.resolve_gtk_binary() == str(gtk)


def test_a_stale_path_copy_does_not_shadow_the_pinned_gui(
        launch_mod, clean_env, tmp_path):
    # The backend pair had this measured on 2026-08-26; the GUI resolved
    # through its own PATH-first copy of the lookup and kept the hole.
    # Same rule now: the pinned install wins over whatever PATH names.
    from oscmix_desk import discovery

    local_bin = tmp_path / ".local" / "bin"
    local_bin.mkdir(parents=True)
    gtk = local_bin / "oscmix-gtk"
    gtk.write_text("#!/bin/sh\n")
    gtk.chmod(0o755)
    clean_env.setenv("HOME", str(tmp_path))
    clean_env.setattr(discovery.shutil, "which",
                      lambda _n: "/usr/local/bin/oscmix-gtk")
    assert launch_mod.resolve_gtk_binary() == str(gtk)


def test_no_binary_anywhere_resolves_to_none(launch_mod, clean_env, tmp_path):
    from oscmix_desk import discovery

    clean_env.setenv("HOME", str(tmp_path))
    clean_env.setattr(discovery.shutil, "which", lambda _n: None)
    clean_env.setattr(discovery.os, "access", lambda *_a: False)
    assert launch_mod.resolve_gtk_binary() is None


def test_the_backend_wait_is_configurable_from_the_environment(launch_mod):
    # BACKEND_WAIT is read at import time, so this asserts the shape of
    # the knob rather than re-importing the module: a float, and long
    # enough that a cold service start is not cut short.
    assert isinstance(launch_mod.BACKEND_WAIT, float)
    assert launch_mod.BACKEND_WAIT >= 1.0
    assert os.environ.get("OSCMIX_BACKEND_WAIT") is None



@pytest.fixture
def launcher_world(launch_mod, clean_env, tmp_path):
    from oscmix_desk.desktop import DesktopStatus
    from oscmix_desk.diagnostics import BackendStatus
    from oscmix_desk.discovery import Device

    calls, notifications, execs = [], [], []
    clean_env.setattr(launch_mod, 'resolve_gtk_binary', lambda: '/usr/bin/oscmix-gtk')
    clean_env.setattr(launch_mod, 'inspect_desktop', lambda *_: DesktopStatus(
        '/usr/bin/oscmix-gtk', None))
    clean_env.setattr(launch_mod, 'backend_status', lambda *_: BackendStatus(
        'ready', 'matched', Device('2a39:3fd9', '24216011', 24), 40000, '/isolated/control'))
    clean_env.setattr(launch_mod, 'systemctl_user', lambda *args: calls.append(args) or 0)
    clean_env.setattr(launch_mod, 'notify', lambda title, body, urgency='normal':
                      notifications.append(body))
    identity = ('OSCMIX_CONTROL_SOCKET', 'OSCMIX_BACKEND_PID', 'OSCMIX_DEVICE_SERIAL')
    clean_env.setattr(launch_mod.os, 'execve', lambda path, args, env:
                      execs.append((path, args, {k: env[k] for k in identity})))
    from oscmix_desk import hostservice
    clean_env.setattr(hostservice, 'STATE', tmp_path)
    return launch_mod, clean_env, calls, notifications, execs


def test_matching_manual_backend_opens_gui_without_systemd(launcher_world):
    mod, _, calls, notices, execs = launcher_world
    assert mod.main() == 0
    assert execs == [('/usr/bin/oscmix-gtk', ['/usr/bin/oscmix-gtk'],
                      {'OSCMIX_CONTROL_SOCKET': '/isolated/control', 'OSCMIX_BACKEND_PID': '40000',
                       'OSCMIX_DEVICE_SERIAL': '24216011'})]
    assert calls == []
    assert notices == []


@pytest.mark.parametrize('kind', ['binary', 'protocol', 'maintenance',
                                'gtk-maintenance', 'service-maintenance'])
def test_desktop_prerequisites_are_checked_before_starting_any_backend(launcher_world, kind,
                                                                      tmp_path):
    from oscmix_desk.desktop import DesktopStatus

    mod, monkey, calls, notices, execs = launcher_world
    monkey.setattr(mod, 'backend_status', lambda *_: pytest.fail('backend inspected too early'))
    if kind == 'binary':
        monkey.setattr(mod, 'resolve_gtk_binary', lambda: None)
    elif kind == 'maintenance':
        (tmp_path / 'package-update').write_text('pending')
    elif kind == 'gtk-maintenance':
        (tmp_path / 'gtk-package-update').write_text('pending')
    elif kind == 'service-maintenance':
        (tmp_path / 'service-update').write_text('pending')
    else:
        monkey.setattr(mod, 'inspect_desktop', lambda *_: DesktopStatus('gtk', kind + ' failed'))
    assert mod.main() == 1
    assert notices
    assert calls == []
    assert execs == []


@pytest.mark.parametrize('state', ['conflict', 'unknown'])
def test_unrelated_or_unidentified_listener_cannot_open_the_gui(launcher_world, state):
    from oscmix_desk.diagnostics import BackendStatus

    mod, monkey, calls, notices, execs = launcher_world
    monkey.setattr(mod, 'backend_status', lambda *_: BackendStatus(state, 'wrong target'))
    assert mod.main() == 1
    assert notices == ['wrong target']
    assert calls == []
    assert execs == []


def test_disconnected_interface_does_not_start_a_service(launcher_world):
    from oscmix_desk.diagnostics import BackendStatus
    from oscmix_desk.discovery import Device

    mod, monkey, calls, notices, _ = launcher_world
    monkey.setattr(mod, 'backend_status', lambda *_: BackendStatus(
        'absent', 'no listener', Device('2a39:3fd9', '', None)))
    assert mod.main() == 1
    assert 'not connected' in notices[0]
    assert calls == []


@pytest.mark.parametrize('missing', ['all', 'pid', 'endpoint', 'device', 'serial'])
def test_incomplete_identity_cannot_be_passed_to_gtk(launcher_world, missing):
    from dataclasses import replace

    from oscmix_desk.diagnostics import BackendStatus
    from oscmix_desk.discovery import Device

    mod, monkey, calls, notices, execs = launcher_world
    status = BackendStatus('ready', 'matched', Device('2a39:3fd9', '24216011', 24),
                           40000, '/isolated/control')
    if missing == 'all':
        status = BackendStatus('ready', 'incomplete')
    elif missing == 'serial':
        status = replace(status, device=replace(status.device, serial=''))
    else:
        status = replace(status, **{missing: None})
    monkey.setattr(mod, 'backend_status', lambda *_: status)
    assert mod.main() == 1
    assert 'identity is incomplete' in notices[0]
    assert execs == calls == []


def test_invalid_main_configuration_does_not_fall_back_to_another_device(
        launcher_world, tmp_path):
    mod, monkey, calls, notices, execs = launcher_world
    path = write_conf(tmp_path / 'routing.conf', '[device]\nusb-id=not-an-id\n')
    monkey.setenv('OSCMIX_CONFIG', str(path))
    assert mod.main() == 1
    assert 'usb-id' in notices[0]
    assert execs == calls == []


def test_profile_machine_values_do_not_redirect_the_launcher(launcher_world, tmp_path):
    mod, monkey, _, _, _ = launcher_world
    path = write_conf(tmp_path / 'routing.conf', '[osc]\nport=9001\nrecv-port=9002\n')
    (tmp_path / 'profiles').mkdir()
    write_conf(tmp_path / 'profiles/old.conf', '[osc]\nport=9003\n')
    write_conf(tmp_path / 'active-profile', 'old\n')
    monkey.setenv('OSCMIX_CONFIG', str(path))
    observed = []
    real = mod.backend_status

    def inspect(config, proc, config_path):
        observed.append((config.osc_port, config.osc_recv_port, config_path))
        return real(config, proc, config_path)

    monkey.setattr(mod, 'backend_status', inspect)
    assert mod.main() == 0
    assert observed == [(9001, 9002, path)]


def test_failing_exec_is_a_message_without_a_traceback(launcher_world, caplog):
    mod, monkey, _, notices, _ = launcher_world
    def fail(*_args):
        raise OSError('bad executable format')
    monkey.setattr(mod.os, 'execve', fail)
    assert mod.main() == 1
    assert 'could not execute' in notices[0]
    assert 'Traceback' not in caplog.text


@pytest.mark.parametrize('problem', [None, 'manual session required'])
@pytest.mark.parametrize('manager', ['systemd', 'openrc', 'runit'])
def test_only_an_enabled_matching_service_may_be_started(
        launcher_world, problem, manager, tmp_path):
    from oscmix_desk.diagnostics import BackendStatus
    from oscmix_desk.discovery import Device

    mod, monkey, calls, notices, execs = launcher_world
    device = Device('2a39:3fd9', '24216011', 24)
    path = write_conf(tmp_path / 'selected.conf', '[device]\nserial=24216011\n')
    monkey.setenv('OSCMIX_CONFIG', str(path))
    proc = Path(os.environ['OSCMIX_PROC_ROOT'])
    states = iter([BackendStatus('absent', 'no listener', device),
                   BackendStatus('ready', 'matched', device, 40000, '/isolated/control')])

    def inspect(config, proc_root, config_path):
        assert config.serial == '24216011'
        assert proc_root == proc
        assert config_path == path
        return next(states)

    monkey.setattr(mod, 'backend_status', inspect)
    monkey.setattr(mod, 'service_status', lambda: {'state': 'observed', 'manager': manager})
    monkey.setattr(mod, 'service_start_problem', lambda *_: problem)
    assert mod.main() == (1 if problem else 0)
    if problem:
        assert notices == [problem]
        assert calls == execs == []
    else:
        assert calls == ([('reset-failed', mod.SERVICE), ('start', '--no-block', mod.SERVICE)]
                         if manager == 'systemd' else [])
        assert len(execs) == 1


def test_launcher_inspects_the_selected_gtk_and_preserves_the_desktop_environment(launcher_world):
    from oscmix_desk.desktop import DesktopStatus

    mod, monkey, calls, notices, _ = launcher_world
    gtk = '/selected/companion/oscmix-gtk'
    inspected, launched = [], []
    monkey.setattr(mod, 'resolve_gtk_binary', lambda: gtk)
    monkey.setattr(mod, 'inspect_desktop',
                   lambda binary: inspected.append(binary) or DesktopStatus(binary, None))
    monkey.setenv('WAYLAND_DISPLAY', 'qa-wayland')
    monkey.setenv('DBUS_SESSION_BUS_ADDRESS', 'unix:path=/qa-bus')
    for key in ('OSCMIX_CONTROL_SOCKET', 'OSCMIX_BACKEND_PID', 'OSCMIX_DEVICE_SERIAL'):
        monkey.setenv(key, 'stale')
    monkey.setattr(mod.os, 'execve', lambda binary, argv, env:
                   launched.append((binary, argv, env)))
    assert mod.main() == 0
    assert inspected == [gtk]
    assert len(launched) == 1
    binary, argv, env = launched[0]
    assert (binary, argv) == (gtk, [gtk])
    assert env['WAYLAND_DISPLAY'] == 'qa-wayland'
    assert env['DBUS_SESSION_BUS_ADDRESS'] == 'unix:path=/qa-bus'
    assert env['OSCMIX_CONTROL_SOCKET'] == '/isolated/control'
    assert env['OSCMIX_BACKEND_PID'] == '40000'
    assert env['OSCMIX_DEVICE_SERIAL'] == '24216011'
    assert calls == notices == []


@pytest.mark.parametrize('failure', ['start', 'timeout', 'replaced'])
def test_backend_start_failure_never_falls_through_to_gtk(launcher_world, failure):
    from oscmix_desk.diagnostics import BackendStatus
    from oscmix_desk.discovery import Device

    mod, monkey, _, notices, execs = launcher_world
    device = Device('2a39:3fd9', '24216011', 24)
    replies = [BackendStatus('absent', 'no listener', device)]
    if failure == 'replaced':
        replies.append(BackendStatus('conflict', 'wrong backend', device))
    monkey.setattr(mod, 'backend_status', lambda *_: replies.pop(0) if len(replies) > 1
                    else replies[0])
    monkey.setattr(mod, 'service_status', dict)
    monkey.setattr(mod, 'service_start_problem', lambda *_: None)
    monkey.setattr(mod, 'BACKEND_WAIT', 0)
    if failure == 'start':
        monkey.setattr(mod, 'systemctl_user', lambda *_: 1)
    assert mod.main() == 1
    assert notices
    assert execs == []
