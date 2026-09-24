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

from oscmix_desk import launcher as _launcher
from oscmix_desk import paths as paths_mod

#: Read at collection, before any fixture patches them.
_IMPORTED_SYSTEM_CONFIGS = (paths_mod.SYSTEM_CONFIG, _launcher.SYSTEM_CONFIG)


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


def test_the_launcher_finds_the_file_the_backend_would(launch_mod, clean_env,
                                                       tmp_path):
    """paths_mod.discover_config_path's rule, repeated in a module that may
    not import config -- held equal here across the environments that
    decide it. A relative XDG_CONFIG_HOME is ignored by both (0.6.10),
    an empty HOME is looked up, and a uid without a passwd entry has no
    user directory: `~/.config` is not read relative to the cwd."""
    import pwd


    home = tmp_path / "home"
    xdg = tmp_path / "xdg"
    system = tmp_path / "etc" / "routing.conf"
    for path in (home / ".config" / "oscmix" / "routing.conf",
                 xdg / "oscmix" / "routing.conf", system,
                 tmp_path / "rel" / "oscmix" / "routing.conf",
                 tmp_path / "~" / ".config" / "oscmix" / "routing.conf"):
        path.parent.mkdir(parents=True)
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
        found = launch_mod.config_file()
        assert found == paths_mod.discover_config_path(), case
        seen.add(found)
    assert seen == {home / ".config" / "oscmix" / "routing.conf",
                    tmp_path / "named.conf", xdg / "oscmix" / "routing.conf",
                    system}


def test_both_modules_default_to_the_same_system_config(launch_mod,
                                                        clean_env, tmp_path):
    """The two literals, as imported -- the suite patches both, so only
    the values read before any fixture can show them drifting apart."""

    in_config, in_launcher = _IMPORTED_SYSTEM_CONFIGS
    assert in_config == Path("/etc/oscmix/routing.conf")
    assert in_launcher == in_config
    clean_env.delenv("OSCMIX_SYSTEM_CONFIG")
    clean_env.setattr(paths_mod, "SYSTEM_CONFIG", tmp_path / "a")
    assert paths_mod.system_config({}) == tmp_path / "a"
    assert paths_mod.system_config(
        {"OSCMIX_SYSTEM_CONFIG": str(tmp_path / "b")}) == tmp_path / "b"
    # From the environment it is given, like the rest of the search: the
    # unit's, when the CLI resolves the unit's desk.
    clean_env.setenv("OSCMIX_SYSTEM_CONFIG", str(tmp_path / "c"))
    assert paths_mod.system_config({}) == tmp_path / "a"


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
        'ready', 'matched', Device('2a39:3fd9', '24216011', 24), 40000))
    clean_env.setattr(launch_mod, 'port_state', lambda *_: 'free')
    clean_env.setattr(launch_mod, 'systemctl_user', lambda *args: calls.append(args) or 0)
    clean_env.setattr(launch_mod, 'notify', lambda title, body, urgency='normal':
                      notifications.append(body))
    clean_env.setattr(launch_mod.os, 'execv', lambda path, args: execs.append((path, args)))
    clean_env.setattr(launch_mod, 'MAINTENANCE_FILE', tmp_path / 'maintenance')
    clean_env.setattr(launch_mod, 'GTK_MAINTENANCE_FILE', tmp_path / 'gtk-maintenance')
    return launch_mod, clean_env, calls, notifications, execs


def test_matching_manual_backend_opens_gui_without_systemd(launcher_world):
    mod, _, calls, notices, execs = launcher_world
    assert mod.main() == 0
    assert execs == [('/usr/bin/oscmix-gtk', ['/usr/bin/oscmix-gtk'])]
    assert calls == []
    assert notices == []


@pytest.mark.parametrize('kind', ['binary', 'schema', 'connection', 'maintenance',
                                'gtk-maintenance'])
def test_desktop_prerequisites_are_checked_before_starting_any_backend(launcher_world, kind):
    from oscmix_desk.desktop import DesktopStatus

    mod, monkey, calls, notices, execs = launcher_world
    monkey.setattr(mod, 'backend_status', lambda *_: pytest.fail('backend inspected too early'))
    if kind == 'binary':
        monkey.setattr(mod, 'resolve_gtk_binary', lambda: None)
    elif kind == 'maintenance':
        mod.MAINTENANCE_FILE.write_text('pending')
    elif kind == 'gtk-maintenance':
        mod.GTK_MAINTENANCE_FILE.write_text('pending')
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


def test_busy_reply_port_is_an_actionable_refusal(launcher_world):
    mod, monkey, calls, notices, execs = launcher_world
    monkey.setattr(mod, 'port_state', lambda *_: 'occupied')
    assert mod.main() == 1
    assert '8222' in notices[0]
    assert 'verification' in notices[0]
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
    from oscmix_desk.desktop import DesktopStatus
    monkey.setattr(mod, 'inspect_desktop', lambda config, gtk:
                    observed.append((config.osc_port, config.osc_recv_port)) or
                    DesktopStatus(gtk, None))
    assert mod.main() == 0
    assert observed == [(9001, 9002)]


def test_failing_exec_is_a_message_without_a_traceback(launcher_world, caplog):
    mod, monkey, _, notices, _ = launcher_world
    def fail(*_args):
        raise OSError('bad executable format')
    monkey.setattr(mod.os, 'execv', fail)
    assert mod.main() == 1
    assert 'could not execute' in notices[0]
    assert 'Traceback' not in caplog.text


@pytest.mark.parametrize('problem', [None, 'manual session required'])
def test_only_an_enabled_matching_service_may_be_started(launcher_world, problem):
    from oscmix_desk.diagnostics import BackendStatus
    from oscmix_desk.discovery import Device

    mod, monkey, calls, notices, execs = launcher_world
    device = Device('2a39:3fd9', '24216011', 24)
    states = iter([BackendStatus('absent', 'no listener', device),
                   BackendStatus('ready', 'matched', device)])
    monkey.setattr(mod, 'backend_status', lambda *_: next(states))
    monkey.setattr(mod, 'service_status', lambda: {'state': 'observed'})
    monkey.setattr(mod, 'service_start_problem', lambda *_: problem)
    assert mod.main() == (1 if problem else 0)
    if problem:
        assert notices == [problem]
        assert calls == execs == []
    else:
        assert calls == [('reset-failed', mod.SERVICE), ('start', '--no-block', mod.SERVICE)]
        assert len(execs) == 1


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
