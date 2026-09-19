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


# --------------------------------------------------------------------------
# load_settings -- must never fail, whatever the file contains
# --------------------------------------------------------------------------

def test_settings_fall_back_to_the_compiled_in_defaults(launch_mod, clean_env,
                                                        tmp_path):
    clean_env.setenv("XDG_CONFIG_HOME", str(tmp_path))
    usb_id, ports = launch_mod.load_settings()
    assert usb_id == launch_mod.DEFAULT_USB_ID
    assert ports == (launch_mod.DEFAULT_OSC_PORT,)


def test_settings_are_read_from_the_configured_file(launch_mod, clean_env,
                                                    tmp_path):
    conf = write_conf(tmp_path / "routing.conf",
                      "[device]\nusb-id = 2A39:3FD9\n\n[osc]\nport = 9001\n")
    clean_env.setenv("OSCMIX_CONFIG", str(conf))
    assert launch_mod.load_settings() == ("2a39:3fd9", (9001,))


def test_an_unparseable_usb_id_is_ignored_rather_than_fatal(launch_mod,
                                                            clean_env,
                                                            tmp_path):
    # The backend reports config problems properly and exits 2. The
    # launcher's job is to open the mixer, so a bad value falls back.
    conf = write_conf(tmp_path / "routing.conf",
                      "[device]\nusb-id = not-a-usb-id\n")
    clean_env.setenv("OSCMIX_CONFIG", str(conf))
    usb_id, ports = launch_mod.load_settings()
    assert usb_id == launch_mod.DEFAULT_USB_ID
    assert ports == (launch_mod.DEFAULT_OSC_PORT,)


def test_a_broken_config_warns_and_keeps_the_defaults(launch_mod, clean_env,
                                                      tmp_path, caplog):
    conf = write_conf(tmp_path / "routing.conf",
                      "[osc]\nport = not-a-number\n")
    clean_env.setenv("OSCMIX_CONFIG", str(conf))
    with caplog.at_level("WARNING"):
        assert launch_mod.load_settings() == (launch_mod.DEFAULT_USB_ID,
                                              (launch_mod.DEFAULT_OSC_PORT,))
    assert "ignoring unreadable config" in caplog.text


def test_only_the_first_existing_config_is_read(launch_mod, clean_env,
                                                tmp_path):
    # OSCMIX_CONFIG comes before the XDG path; a second file must not be
    # able to override what the first one said (or did not say).
    first = write_conf(tmp_path / "explicit.conf", "[osc]\nport = 9002\n")
    xdg = tmp_path / "xdg"
    (xdg / "oscmix").mkdir(parents=True)
    write_conf(xdg / "oscmix" / "routing.conf",
               "[device]\nusb-id = 1111:2222\n[osc]\nport = 9003\n")
    clean_env.setenv("OSCMIX_CONFIG", str(first))
    clean_env.setenv("XDG_CONFIG_HOME", str(xdg))
    assert launch_mod.load_settings() == (launch_mod.DEFAULT_USB_ID, (9002,))
    # A missing OSCMIX_CONFIG is not a reason to look further: the backend
    # refuses to start on it, and the XDG file is not what it would run.
    clean_env.setenv("OSCMIX_CONFIG", str(tmp_path / "missing.conf"))
    assert launch_mod.load_settings() == (launch_mod.DEFAULT_USB_ID,
                                          (launch_mod.DEFAULT_OSC_PORT,))


def test_the_xdg_config_is_used_when_no_override_is_set(launch_mod, clean_env,
                                                        tmp_path):
    (tmp_path / "oscmix").mkdir()
    write_conf(tmp_path / "oscmix" / "routing.conf", "[osc]\nport = 9004\n")
    clean_env.setenv("XDG_CONFIG_HOME", str(tmp_path))
    assert launch_mod.load_settings()[1] == (9004,)


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


# --------------------------------------------------------------------------
# notify / systemctl_user -- thin, and both swallow OSError on purpose
# --------------------------------------------------------------------------

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


# --------------------------------------------------------------------------
# ensure_backend -- the poll that bounds the wait
# --------------------------------------------------------------------------

def test_a_running_backend_is_not_started_again(launch_mod, clean_env,
                                                tmp_path):
    verbs = []
    clean_env.setattr(launch_mod, "systemctl_user",
                      lambda *v: verbs.append(v) or 0)
    clean_env.setattr(launch_mod, "udp_port_listening", lambda *_a: True)
    assert launch_mod.ensure_backend((7222,), tmp_path) is True
    assert verbs == [("is-active", "--quiet", launch_mod.SERVICE)]


def test_an_inactive_backend_is_started_without_blocking(launch_mod,
                                                         clean_env, tmp_path):
    verbs = []

    def systemctl(*verb):
        verbs.append(verb)
        return 1 if verb[0] == "is-active" else 0

    clean_env.setattr(launch_mod, "systemctl_user", systemctl)
    clean_env.setattr(launch_mod, "udp_port_listening", lambda *_a: True)
    assert launch_mod.ensure_backend((7222,), tmp_path) is True
    # --no-block: the unit is Type=notify and a plain start would block
    # until READY, which is what the port poll is for.
    assert ("start", "--no-block", launch_mod.SERVICE) in verbs
    assert ("reset-failed", launch_mod.SERVICE) in verbs


def test_a_backend_that_never_listens_gives_up_after_the_wait(launch_mod,
                                                              clean_env,
                                                              tmp_path):
    clean_env.setattr(launch_mod, "systemctl_user", lambda *_v: 0)
    clean_env.setattr(launch_mod, "udp_port_listening", lambda *_a: False)
    clean_env.setattr(launch_mod, "BACKEND_WAIT", 0.05)
    clean_env.setattr(launch_mod.time, "sleep", lambda _s: None)
    assert launch_mod.ensure_backend((7222,), tmp_path) is False


def test_a_backend_that_appears_late_is_still_found(launch_mod, clean_env,
                                                    tmp_path):
    states = iter([False, False, True])
    clean_env.setattr(launch_mod, "systemctl_user", lambda *_v: 0)
    clean_env.setattr(launch_mod, "udp_port_listening",
                      lambda *_a: next(states, True))
    clean_env.setattr(launch_mod, "BACKEND_WAIT", 5.0)
    clean_env.setattr(launch_mod.time, "sleep", lambda _s: None)
    assert launch_mod.ensure_backend((7222,), tmp_path) is True


def test_a_backend_on_any_of_the_ports_is_found(launch_mod, clean_env,
                                                tmp_path):
    """The active profile's port first, then routing.conf's: a profile that
    no longer loads leaves the backend on the second (ADR 0018)."""
    polled = []
    clean_env.setattr(launch_mod, "systemctl_user", lambda *_v: 0)
    clean_env.setattr(launch_mod, "udp_port_listening",
                      lambda port, root: polled.append((port, root))
                      or port == 9001)
    clean_env.setattr(launch_mod.time, "sleep", lambda _s: None)
    assert launch_mod.ensure_backend((9100, 9001), tmp_path) is True
    assert polled[:2] == [(9100, tmp_path), (9001, tmp_path)], \
        "each port, in the proc tree it was given"


# --------------------------------------------------------------------------
# resolve_gtk_binary -- including the refusals
# --------------------------------------------------------------------------

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


# --------------------------------------------------------------------------
# main -- what a user sees when something is missing
# --------------------------------------------------------------------------

@pytest.fixture
def launcher_world(launch_mod, clean_env, tmp_path):
    """main() with every outside effect captured rather than performed."""
    notifications = []
    execs = []
    clean_env.setattr(launch_mod, "load_settings",
                      lambda: (launch_mod.DEFAULT_USB_ID, (7222,)))
    clean_env.setattr(launch_mod, "notify",
                      lambda s, b, urgency="normal":
                      notifications.append((s, b, urgency)))
    clean_env.setattr(launch_mod, "usb_device_present", lambda *_a: True)
    clean_env.setattr(launch_mod, "ensure_backend", lambda *_a: True)
    clean_env.setattr(launch_mod, "resolve_gtk_binary", lambda: "/usr/bin/oscmix-gtk")
    clean_env.setattr(launch_mod.os, "execv",
                      lambda path, argv: execs.append((path, argv)))
    return launch_mod, clean_env, notifications, execs


def test_a_disconnected_interface_is_reported_and_nothing_is_started(
        launcher_world):
    mod, monkey, notifications, execs = launcher_world
    monkey.setattr(mod, "usb_device_present", lambda *_a: False)
    assert mod.main() == 1
    assert execs == []
    assert len(notifications) == 1
    assert notifications[0][2] == "critical"
    assert "not connected" in notifications[0][1]


def test_an_unreachable_backend_still_opens_the_mixer(launcher_world):
    # The GUI is useful without the backend -- it says so itself -- and a
    # user who asked for the mixer should get the mixer.
    mod, monkey, notifications, execs = launcher_world
    monkey.setattr(mod, "ensure_backend", lambda *_a: False)
    mod.main()
    assert execs == [("/usr/bin/oscmix-gtk", ["/usr/bin/oscmix-gtk"])]
    assert any("journalctl" in body for _s, body, _u in notifications)


def test_a_missing_gtk_binary_is_reported_as_such(launcher_world):
    mod, monkey, notifications, execs = launcher_world
    monkey.setattr(mod, "resolve_gtk_binary", lambda: None)
    assert mod.main() == 1
    assert execs == []
    assert any("not installed" in body for _s, body, _u in notifications)


def test_the_happy_path_replaces_the_process_with_the_mixer(launcher_world):
    mod, _monkey, notifications, execs = launcher_world
    mod.main()
    assert execs == [("/usr/bin/oscmix-gtk", ["/usr/bin/oscmix-gtk"])]
    assert notifications == []


def test_a_failing_exec_reports_the_reason_instead_of_a_traceback(
        launcher_world, caplog):
    # execv only returns by failing. 0.1.3 dumped a traceback on a user
    # who had launched this from a desktop icon.
    mod, monkey, notifications, _execs = launcher_world

    def boom(_path, _argv):
        raise OSError("Exec format error")

    monkey.setattr(mod.os, "execv", boom)
    with caplog.at_level("ERROR"):
        assert mod.main() == 1
    assert "Exec format error" in caplog.text
    assert "Traceback" not in caplog.text
    assert any("Exec format error" in body for _s, body, _u in notifications)


def test_the_device_and_proc_roots_are_overridable(launcher_world):
    # The integration tests and this suite both need to point the
    # launcher at a fake /sys and /proc.
    mod, monkey, _notifications, _execs = launcher_world
    seen = {}
    monkey.setattr(mod, "usb_device_present",
                   lambda usb_id, root: seen.setdefault("sysfs", root) or True)
    monkey.setattr(mod, "ensure_backend",
                   lambda ports, root: seen.setdefault("proc", root) or True)
    monkey.setenv("OSCMIX_SYSFS_USB", "/fake/sys")
    monkey.setenv("OSCMIX_PROC_ROOT", "/fake/proc")
    mod.main()
    assert str(seen["sysfs"]) == "/fake/sys"
    assert str(seen["proc"]) == "/fake/proc"


def test_the_launcher_logs_to_stderr_not_stdout(launch_mod, capsys, clean_env):
    # It is exec'd from a .desktop entry; stdout goes to the journal only
    # by accident, and a mixer that prints to stdout confuses anything
    # piping it.
    clean_env.setattr(launch_mod, "load_settings",
                      lambda: (launch_mod.DEFAULT_USB_ID, (7222,)))
    clean_env.setattr(launch_mod, "usb_device_present", lambda *_a: False)
    clean_env.setattr(launch_mod, "notify", lambda *_a, **_kw: None)
    assert launch_mod.main() == 1
    assert capsys.readouterr().out == ""


def test_the_backend_wait_is_configurable_from_the_environment(launch_mod):
    # BACKEND_WAIT is read at import time, so this asserts the shape of
    # the knob rather than re-importing the module: a float, and long
    # enough that a cold service start is not cut short.
    assert isinstance(launch_mod.BACKEND_WAIT, float)
    assert launch_mod.BACKEND_WAIT >= 1.0
    assert os.environ.get("OSCMIX_BACKEND_WAIT") is None


def test_the_port_of_the_active_profile_wins(launch_mod, clean_env, tmp_path):
    """The backend runs on the active profile's port; the launcher polled
    routing.conf's (0.6.9), warned, and started the GUI against nothing."""
    conf = write_conf(tmp_path / "routing.conf", "[osc]\nport = 9001\n")
    (tmp_path / "profiles").mkdir()
    write_conf(tmp_path / "profiles" / "live.conf", "[osc]\nport = 9100\n")
    write_conf(tmp_path / "profiles" / "quiet.conf", "[route:x]\noutput = 1/2\nplayback = 1/2\n")
    clean_env.setenv("OSCMIX_CONFIG", str(conf))
    (tmp_path / "active-profile").write_text("live\n")
    # The profile's port first, routing.conf's after it: where the
    # backend runs if that profile no longer loads.
    assert launch_mod.load_settings()[1] == (9100, 9001)
    (tmp_path / "active-profile").write_text("quiet\n")      # inherits
    assert launch_mod.load_settings()[1] == (9001,)
    (tmp_path / "active-profile").write_text("../evil\n")   # never a path
    assert launch_mod.load_settings()[1] == (9001,)


def test_settings_read_like_the_backend_reads_them(launch_mod, clean_env,
                                                  tmp_path):
    """Inline comments are comments, as in config.load_config, and a profile
    name may start with a capital, as paths_mod.profile_path allows."""
    conf = write_conf(tmp_path / "routing.conf",
                      "[device]\nusb-id = 2a39:3fd9  # UCX II\n"
                      "[osc]\nport = 9001 ; the desk\n")
    (tmp_path / "profiles").mkdir()
    write_conf(tmp_path / "profiles" / "Live.conf",
               "[osc]\nport = 9100  # the live rig\n")
    clean_env.setenv("OSCMIX_CONFIG", str(conf))
    assert launch_mod.load_settings() == ("2a39:3fd9", (9001,))
    (tmp_path / "active-profile").write_text("Live\n")
    assert launch_mod.load_settings() == ("2a39:3fd9", (9100, 9001))


def test_the_backend_poll_asks_the_given_proc(launch_mod, clean_env, tmp_path):
    asked = []
    clean_env.setattr(launch_mod, "systemctl_user", lambda *_v: 0)
    clean_env.setattr(launch_mod, "udp_port_listening",
                      lambda port, root: asked.append((port, root)) and False)
    clean_env.setattr(launch_mod, "BACKEND_WAIT", 0.0)
    assert launch_mod.ensure_backend((9100, 9001), tmp_path) is False
    assert asked == [(9100, tmp_path), (9001, tmp_path)]


def test_the_launcher_and_the_session_agree_on_what_a_profile_name_is():
    import inspect

    from oscmix_desk import launcher

    rule = 'r"[A-Za-z0-9][A-Za-z0-9._-]*"'
    assert rule in inspect.getsource(paths_mod.profile_path)
    assert rule in inspect.getsource(launcher._active_profile_port)
