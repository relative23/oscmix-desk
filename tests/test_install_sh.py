"""Functional tests for install.sh / uninstall.sh.

Both scripts run against a throwaway HOME with stubbed systemctl/sudo on
PATH, so no real service, udev rule, or user file is touched. The build
step is skipped (--no-build) with fake oscmix binaries pre-installed.
"""

import os
import stat
import subprocess

import pytest
from support import repo_file

PROJECT_ROOT = repo_file("install.sh").parent

# These drive real subprocesses. The entry point resolves the package from
# its own location, so a subprocess loads the checked-out source and never
# the mutated copy: such a test cannot kill a mutant, and at ~35 s per run
# it would dominate a mutation pass for nothing.
pytestmark = pytest.mark.skipif(
    bool(os.environ.get("MUTANT_UNDER_TEST")),
    reason="subprocess tests cannot observe mutants",
)


#: Every command the scripts run through $SUDO that does not take one of
#: the overridable paths below: as root $SUDO is empty, and these would
#: run for real. systemd-tmpfiles did until 0.6.10 -- on a copy of the
#: conf whose lines still name /run/oscmix-desk.
STUBBED_TOOLS = ("systemctl", "udevadm", "sudo", "systemd-tmpfiles")

#: What $SUDO install and $SUDO rm may write or remove: only the targets
#: make_fake_home points into the scratch directory.
OVERRIDDEN_TARGETS = ('"$UDEV_RULE"', '"$SLEEP_HOOK"', '"$TMPFILES_CONF"')


def make_fake_home(tmp_path):
    home = tmp_path / "home"
    bin_dir = home / ".local" / "bin"
    bin_dir.mkdir(parents=True)
    for tool in ("oscmix", "alsaseqio", "oscmix-gtk"):
        fake = bin_dir / tool
        fake.write_text("#!/bin/sh\nexit 0\n")
        fake.chmod(0o755)

    stub_bin = tmp_path / "stub-bin"
    stub_bin.mkdir()
    log = tmp_path / "calls.log"
    for tool in STUBBED_TOOLS:
        stub = stub_bin / tool
        # The sudo stub must never execute its arguments -- uninstall.sh
        # would otherwise touch the real /etc/udev rule on dev machines.
        stub.write_text('#!/bin/sh\necho "%s $@" >> "%s"\nexit 0\n'
                        % (tool, log))
        stub.chmod(0o755)

    env = dict(os.environ)
    env.update({
        "HOME": str(home),
        "XDG_CONFIG_HOME": str(home / ".config"),
        "XDG_DATA_HOME": str(home / ".local" / "share"),
        "PATH": "%s:%s" % (stub_bin, env["PATH"]),
        # Never the real system files, even when the suite runs as root
        # and install.sh's $SUDO is empty.
        "OSCMIX_UDEV_RULE": str(tmp_path / "system" / "udev.rules"),
        "OSCMIX_SLEEP_HOOK": str(tmp_path / "system" / "sleep-hook"),
        "OSCMIX_TMPFILES_CONF": str(tmp_path / "system" / "tmpfiles.conf"),
        # No interface, unless a test plugs one in: install.sh reads this
        # since 0.6.10, and inherited from the suite's own fixture it put
        # every install on the restart path and its 2 s sleep -- 30 s a
        # run on CI, which pushed the flakiness gate past its timeout.
        "OSCMIX_SYSFS_USB": str(tmp_path / "no-usb"),
    })
    (tmp_path / "system").mkdir(exist_ok=True)
    (tmp_path / "no-usb").mkdir(exist_ok=True)
    return home, env, log


def plug_in(tmp_path, env):
    """A sysfs where the interface is connected, for the test that asks."""
    device = tmp_path / "usb" / "5-2"
    device.mkdir(parents=True)
    (device / "idVendor").write_text("2a39\n")
    (device / "idProduct").write_text("3fd9\n")
    env["OSCMIX_SYSFS_USB"] = str(device.parent)


def run(script, args, env):
    return subprocess.run(
        ["bash", str(PROJECT_ROOT / script)] + args,
        env=env, capture_output=True, text=True, timeout=60,
        cwd=str(PROJECT_ROOT),
    )


def test_install_no_build_installs_everything(tmp_path):
    home, env, log = make_fake_home(tmp_path)
    result = run("install.sh", ["--no-build", "--no-udev"], env)
    assert result.returncode == 0, result.stderr + result.stdout

    bin_dir = home / ".local" / "bin"
    for script in ("oscmix-session", "oscmix-launch"):
        installed = bin_dir / script
        assert installed.is_file()
        assert installed.stat().st_mode & stat.S_IXUSR

    config = home / ".config" / "oscmix" / "routing.conf"
    example = PROJECT_ROOT / "config" / "routing.conf.example"
    assert config.read_text() == example.read_text()
    assert (home / ".config" / "oscmix" / "routing.conf.example").is_file()

    unit = home / ".config" / "systemd" / "user" / "oscmix.service"
    assert "Type=notify" in unit.read_text()

    desktop = home / ".local" / "share" / "applications" / "oscmix-gtk.desktop"
    assert ("Exec=%s/oscmix-launch" % bin_dir) in desktop.read_text()
    icon = (home / ".local" / "share" / "icons" / "hicolor" / "scalable"
            / "apps" / "oscmix.svg")
    assert icon.is_file()

    calls = log.read_text()
    assert "systemctl --user daemon-reload" in calls
    assert "systemctl --user enable --quiet oscmix.service" in calls
    assert "udevadm" not in calls  # --no-udev


def test_relative_xdg_base_directories_are_ignored_both_ways(tmp_path):
    """The session and the launcher ignore a relative XDG_CONFIG_HOME
    (0.6.10); the installer has to put routing.conf where they look, and
    the uninstaller has to remove the unit from there."""
    home, env, _ = make_fake_home(tmp_path)
    env.update({"XDG_CONFIG_HOME": "relative-config",
                "XDG_DATA_HOME": "relative-data"})
    result = run("install.sh", ["--no-build", "--no-udev"], env)
    assert result.returncode == 0, result.stderr + result.stdout
    unit = home / ".config" / "systemd" / "user" / "oscmix.service"
    assert (home / ".config" / "oscmix" / "routing.conf").is_file()
    assert unit.is_file()
    assert (home / ".local" / "share" / "applications"
            / "oscmix-gtk.desktop").is_file()
    assert not (PROJECT_ROOT / "relative-config").exists()
    assert not (PROJECT_ROOT / "relative-data").exists()
    assert run("uninstall.sh", [], env).returncode == 0
    assert not unit.exists()


def test_install_is_idempotent_and_keeps_user_config(tmp_path):
    home, env, _ = make_fake_home(tmp_path)
    assert run("install.sh", ["--no-build", "--no-udev"], env).returncode == 0

    config = home / ".config" / "oscmix" / "routing.conf"
    config.write_text("# customized by the user\n")
    result = run("install.sh", ["--no-build", "--no-udev"], env)
    assert result.returncode == 0
    assert config.read_text() == "# customized by the user\n"
    assert "keeping existing" in result.stdout


def test_uninstall_removes_files_but_keeps_config(tmp_path):
    home, env, _ = make_fake_home(tmp_path)
    assert run("install.sh", ["--no-build", "--no-udev"], env).returncode == 0

    result = run("uninstall.sh", [], env)
    assert result.returncode == 0, result.stderr
    bin_dir = home / ".local" / "bin"
    for script in ("oscmix-session", "oscmix-launch", "oscmix", "alsaseqio"):
        assert not (bin_dir / script).exists()
    assert not (home / ".config" / "systemd" / "user"
                / "oscmix.service").exists()
    # User configuration survives a plain uninstall.
    assert (home / ".config" / "oscmix" / "routing.conf").is_file()


def test_uninstall_purge_removes_config(tmp_path):
    home, env, _ = make_fake_home(tmp_path)
    assert run("install.sh", ["--no-build", "--no-udev"], env).returncode == 0
    result = run("uninstall.sh", ["--purge"], env)
    assert result.returncode == 0, result.stderr
    assert not (home / ".config" / "oscmix").exists()


# --------------------------------------------------------------------------
# Roadmap item J: nothing proved an install actually works.
#
# The tests above assert the *file set*. That is not the same as the
# installed tree being runnable: this release moved the runtime from
# lib/ to src/ and rewrote that path in three places, and the entry
# points resolve their package from their own location -- a layout that
# only exists after an install. Running them from the checkout, which
# every other test does, exercises the other branch of that lookup.
# --------------------------------------------------------------------------

SEQ_CLIENTS = """\
Client info
  cur  clients : 2

Client   0 : "System" [Kernel]
Client  42 : "Fireface UCX II (00000000)" [Kernel]
  Port   1 : "Port" (RWeX) [In/Out]
"""


def fake_proc_and_sysfs(tmp_path, *, with_usb):
    proc_root = tmp_path / "proc"
    (proc_root / "asound" / "seq").mkdir(parents=True)
    (proc_root / "asound" / "seq" / "clients").write_text(SEQ_CLIENTS)
    sysfs = tmp_path / "sysfs"
    sysfs.mkdir()
    if with_usb:
        dev = sysfs / "5-2"
        dev.mkdir()
        (dev / "idVendor").write_text("2a39\n")
        (dev / "idProduct").write_text("3fd9\n")
    return proc_root, sysfs


def test_the_installed_session_runs_from_the_installed_tree(tmp_path):
    home, env, _ = make_fake_home(tmp_path)
    assert run("install.sh", ["--no-build", "--no-udev"], env).returncode == 0

    # The package has to be where the shim looks for it: ~/.local/bin is
    # next to ~/.local/lib/oscmix-desk, not next to a src/.
    lib = home / ".local" / "lib" / "oscmix-desk" / "oscmix_desk"
    assert (lib / "__init__.py").is_file()
    assert (lib / "launcher.py").is_file(), \
        "the launcher moved into the package but install.sh did not follow"

    proc_root, sysfs = fake_proc_and_sysfs(tmp_path, with_usb=True)
    config = tmp_path / "routing.conf"
    config.write_text("[route:main]\nplayback = 1/2\noutput = 5/6\n")

    run_env = dict(env)
    run_env.update({
        "OSCMIX_PROC_ROOT": str(proc_root),
        "OSCMIX_SYSFS_USB": str(sysfs),
    })
    # Deliberately from a directory that is not the checkout: the shim
    # must resolve its package from its own path, not from the cwd.
    result = subprocess.run(
        [str(home / ".local" / "bin" / "oscmix-session"),
         "--config", str(config), "--dry-run"],
        env=run_env, capture_output=True, text=True, timeout=60,
        cwd=str(tmp_path),
    )
    assert result.returncode == 0, result.stderr
    assert "would send: /output/5/stereo ,i 1" in result.stdout
    assert "would send: /mix/5/playback/1" in result.stdout
    # Same order rule as everywhere else: links before the mix matrix.
    assert (result.stdout.index("/output/5/stereo")
            < result.stdout.index("/mix/5/playback/1"))


def test_the_installed_launcher_resolves_its_package(tmp_path):
    # oscmix-launch was a standalone script until this release and is a
    # shim now; if install.sh missed launcher.py the shim would die with
    # ImportError on a desktop double-click, with no notification.
    home, env, _ = make_fake_home(tmp_path)
    assert run("install.sh", ["--no-build", "--no-udev"], env).returncode == 0

    proc_root, sysfs = fake_proc_and_sysfs(tmp_path, with_usb=False)
    run_env = dict(env)
    run_env.update({
        "OSCMIX_PROC_ROOT": str(proc_root),
        "OSCMIX_SYSFS_USB": str(sysfs),
        "OSCMIX_NO_NOTIFY": "1",
    })
    result = subprocess.run(
        [str(home / ".local" / "bin" / "oscmix-launch")],
        env=run_env, capture_output=True, text=True, timeout=60,
        cwd=str(tmp_path),
    )
    assert result.returncode == 1
    assert "is not connected" in result.stderr
    assert "Traceback" not in result.stderr
    assert "ImportError" not in result.stderr


def test_the_installed_tree_carries_every_runtime_module(tmp_path):
    # install.sh globs src/oscmix_desk/*.py. A module added in a
    # subdirectory, or one that stops matching the glob, would be missing
    # only at runtime on a user's machine.
    home, env, _ = make_fake_home(tmp_path)
    assert run("install.sh", ["--no-build", "--no-udev"], env).returncode == 0

    source = {path.name for path in
              (PROJECT_ROOT / "src" / "oscmix_desk").glob("*.py")}
    installed = {path.name for path in
                 (home / ".local" / "lib" / "oscmix-desk"
                  / "oscmix_desk").glob("*.py")}
    assert source == installed, "not installed: %s" % sorted(source - installed)


# --------------------------------------------------------------------------
# systemd's user instance is per login session, not per HOME.
# --------------------------------------------------------------------------

def session_home_stub(tmp_path, session_home):
    """A `systemctl` stub that answers show-environment with a HOME.

    The default stub in `make_fake_home` answers nothing, which is the
    "cannot tell" case the scripts treat as "proceed". This one lets a
    test say *whose* session systemd is serving.
    """
    stub = tmp_path / "stub-bin" / "systemctl"
    log = tmp_path / "calls.log"
    stub.write_text(
        '#!/bin/sh\n'
        'echo "systemctl $@" >> "%s"\n'
        'for a in "$@"; do\n'
        '  if [ "$a" = "show-environment" ]; then\n'
        '    echo "HOME=%s"\n'
        '    exit 0\n'
        '  fi\n'
        'done\n'
        'exit 0\n' % (log, session_home))
    stub.chmod(0o755)


def test_uninstall_leaves_another_session_service_alone(tmp_path):
    """The trap this closes, found while running the release checklist.

    `systemctl --user` reads units from the *session's* home whatever
    HOME the script was given, so a scratch-home uninstall stopped and
    disabled the real user's oscmix.service. Nothing was lost, but the
    desk stopped being managed until somebody noticed.
    """
    _home, env, log = make_fake_home(tmp_path)
    run("install.sh", ["--no-build"], env)
    session_home_stub(tmp_path, "/home/somebodyelse")
    log.write_text("")

    result = run("uninstall.sh", ["--purge"], env)

    assert result.returncode == 0
    calls = log.read_text()
    assert "stop oscmix.service" not in calls
    assert "disable" not in calls
    assert "not touching oscmix.service" in result.stderr


def test_install_does_not_arm_another_session_service(tmp_path):
    """The mirror case: installing into a scratch home would otherwise
    enable and restart a service belonging to somebody else's session,
    and then report "backend is running" about it."""
    home, env, log = make_fake_home(tmp_path)
    session_home_stub(tmp_path, "/home/somebodyelse")
    plug_in(tmp_path, env)      # or "no restart" would be true for free

    result = run("install.sh", ["--no-build"], env)

    assert result.returncode == 0
    calls = log.read_text()
    assert "enable" not in calls
    assert "restart oscmix.service" not in calls
    assert "not enabled" in result.stderr
    assert "not restarting the backend" in result.stdout
    # The unit is still installed; only arming it is withheld.
    assert (home / ".config" / "systemd" / "user" / "oscmix.service").is_file()


def test_install_arms_the_service_when_the_session_matches(tmp_path):
    """The guard must not block the ordinary path, which is the whole
    point of comparing rather than refusing outright."""
    home, env, log = make_fake_home(tmp_path)
    session_home_stub(tmp_path, str(home))

    result = run("install.sh", ["--no-build"], env)

    assert result.returncode == 0
    assert "enable --quiet oscmix.service" in log.read_text()


def _fake_system_files(tmp_path, env):
    files = {}
    for name, var in (("udev.rules", "OSCMIX_UDEV_RULE"),
                      ("sleep-hook", "OSCMIX_SLEEP_HOOK"),
                      ("tmpfiles.conf", "OSCMIX_TMPFILES_CONF")):
        path = tmp_path / "system" / name
        path.parent.mkdir(exist_ok=True)
        path.write_text("x")
        env[var] = str(path)
        files[var] = path
    return files


def test_uninstall_from_a_scratch_home_leaves_the_system_files_alone(tmp_path):
    """A scratch-home uninstall removed the real desk's system files.

    Found while running the 0.6.8 release checklist: the uninstall of a
    throwaway home reached for the udev rule, the resume hook and the
    tmpfiles.d entry that the session's own installation depends on,
    and only a sudo that could not ask for a password kept them.
    """
    _home, env, log = make_fake_home(tmp_path)
    files = _fake_system_files(tmp_path, env)
    run("install.sh", ["--no-build", "--no-udev"], env)
    session_home_stub(tmp_path, "/home/somebodyelse")
    log.write_text("")

    result = run("uninstall.sh", [], env)

    assert result.returncode == 0
    assert "sudo rm" not in log.read_text()
    assert "not removing" in result.stderr
    assert all(path.exists() for path in files.values())


def test_uninstall_of_the_session_s_home_removes_the_system_files(tmp_path):
    home, env, log = make_fake_home(tmp_path)
    _fake_system_files(tmp_path, env)
    run("install.sh", ["--no-build", "--no-udev"], env)
    session_home_stub(tmp_path, str(home))
    log.write_text("")

    result = run("uninstall.sh", [], env)

    assert result.returncode == 0
    calls = log.read_text()
    for var in ("OSCMIX_UDEV_RULE", "OSCMIX_SLEEP_HOOK", "OSCMIX_TMPFILES_CONF"):
        assert "sudo rm -f %s" % env[var] in calls


def test_install_warns_a_user_who_is_not_in_audio(tmp_path):
    """Outside `audio` the lock cannot be taken, and every start refuses."""
    home, env, _log = make_fake_home(tmp_path)
    stub = tmp_path / "stub-bin" / "id"
    stub.write_text('#!/bin/sh\n'
                    'case "$1" in\n'
                    '  -un) echo tester ;;\n'
                    '  -nG) echo "tester users" ;;\n'
                    '  -u) echo 1000 ;;\n'
                    '  *) exec /usr/bin/id "$@" ;;\n'
                    'esac\n')
    stub.chmod(0o755)
    session_home_stub(tmp_path, str(home))
    result = run("install.sh", ["--no-build"], env)
    assert result.returncode == 0, result.stderr
    assert "is not in the group audio" in result.stderr
    assert "usermod -aG audio tester" in result.stderr


def test_a_start_that_fails_does_not_end_the_installer(tmp_path):
    """Under set -e a failed restart job ended install.sh before its own
    advice printed (0.6.9)."""
    home, env, _log = make_fake_home(tmp_path)
    session_home_stub(tmp_path, str(home))
    stub = tmp_path / "stub-bin" / "systemctl"
    stub.write_text(stub.read_text().replace(
        "exit 0", 'case "$*" in *restart*) exit 1 ;; *is-active*) exit 3 ;; esac\nexit 0'))
    plug_in(tmp_path, env)
    result = run("install.sh", ["--no-build", "--no-udev"], env)
    assert result.returncode == 0, result.stderr
    assert "backend did not start" in result.stderr


@pytest.mark.parametrize("script", ["install.sh", "uninstall.sh"])
@pytest.mark.parametrize("home", ["", "relative-home"])
def test_a_home_that_is_not_absolute_is_refused_before_anything_is_written(
        tmp_path, script, home):
    """`set -u` catches neither an empty nor a relative HOME, and every
    path hangs off it: /.local, or the working directory's."""
    _home, env, log = make_fake_home(tmp_path)
    env["HOME"] = home
    result = subprocess.run(
        ["bash", str(PROJECT_ROOT / script)]
        + (["--no-build", "--no-udev"] if script == "install.sh" else []),
        env=env, capture_output=True, text=True, timeout=60, cwd=str(tmp_path))
    assert result.returncode == 2
    assert "HOME must be an absolute path" in result.stderr
    assert not log.exists()
    assert not (tmp_path / "relative-home").exists()


def test_nothing_the_scripts_run_as_root_escapes_the_stubs():
    """Run as root, $SUDO is empty: every root command is either stubbed on
    PATH or confined to a path the suite overrides. The tmpfiles step
    was neither, and a root test run created and re-grouped the real
    /run/oscmix-desk (0.6.10)."""
    import re

    for script in ("install.sh", "uninstall.sh"):
        text = "\n".join(line for line in repo_file(script).read_text().splitlines()
                         if not line.lstrip().startswith("#"))
        # One logical command per match: continuation lines joined.
        text = re.sub(r"\\\n\s*", " ", text)
        # $SUDO, ${SUDO} and "$SUDO" alike.
        commands = re.findall(r'"?\$\{?SUDO\}?"?\s+(\S+)([^;&|\n]*)', text)
        assert commands, script
        for tool, rest in commands:
            if tool in STUBBED_TOOLS:
                continue
            operands = [word for word in rest.split()
                        if not word.startswith("-")]
            if tool == "install":
                # install -m MODE SOURCE TARGET: the mode's value is not
                # an option word, the target is the last operand.
                operands = operands[-1:]
            assert tool in ("install", "rm"), (script, tool)
            assert operands, (script, tool, rest)
            for target in operands:
                assert target in OVERRIDDEN_TARGETS, (script, tool, rest)
