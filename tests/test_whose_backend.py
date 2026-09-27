"""Whose backend, and whose unit: the process that holds the OSC port,
the interface it bridges, the session above it, and the desk the unit
runs -- read from /proc, never assumed (ADR 0021, ADR 0024).
"""

import io
import os
from pathlib import Path

import pytest
from two_boxes import DESK, unit

from oscmix_desk import cli
from oscmix_desk import outcome as outcome_mod
from oscmix_desk.process import control_holder


def test_the_holder_is_followed_to_the_client_it_bridges(endpoint):
    _config, path, proc = endpoint
    holder = control_holder(path, proc)
    assert (holder.oscmix, holder.client, holder.serial) == (True, 24, "24216011")


def test_a_bridge_above_the_holder_is_found_too(endpoint):
    _config, path, proc = endpoint
    (proc / "101/stat").write_text("101 (oscmix) S 102 0 0\n")
    (proc / "102/stat").write_text("102 (alsaseqio) S 1 0 0\n")
    holder = control_holder(path, proc)
    assert (holder.client, holder.serial) == (24, "24216011")


def test_the_bridge_is_found_past_an_unreadable_sibling_and_a_bracket_in_a_name(endpoint):
    _config, path, proc = endpoint
    (proc / "100").mkdir()
    (proc / "100/stat").write_text("100 (gone) S 101 0 0\n")
    (proc / "102/stat").write_text("102 (als)aseqio) S 101 0 0\n")
    holder = control_holder(path, proc)
    assert (holder.client, holder.serial) == (24, "24216011")

def _backend_with_parent(endpoint, parent_argv):
    """A coordinated backend with the given parent; no actual process is signalled."""
    from oscmix_desk.discovery import Device
    from oscmix_desk.process import _cleanup_stale_backend

    config, path, proc = endpoint
    holder = proc / "101"
    parent = proc / "39000"
    (parent / "fd").mkdir(parents=True)
    (parent / "comm").write_text("python3\n")
    (parent / "stat").write_text("39000 (python3) S 1 0 0\n")
    (parent / "cmdline").write_bytes(b"\0".join(a.encode() for a in parent_argv) + b"\0")
    (holder / "stat").write_text("101 (oscmix) S 39000 0 0\n")
    device = Device(config.usb_id, config.serial, 24)
    return path, device, proc, holder, _cleanup_stale_backend

def test_a_backend_of_a_live_session_is_not_stale(endpoint, monkeypatch):
    """A second session by hand terminated the unit's backend (measured)."""
    from oscmix_desk import process

    port, device, proc, _holder, cleanup = _backend_with_parent(
        endpoint, ["python3", "/home/u/.local/bin/oscmix-session"])
    monkeypatch.setattr(process.os, "getuid", os.getuid)
    killed = []
    monkeypatch.setattr(process, "_terminate",
                        lambda pid, still_stale: killed.append(pid) or True)
    assert cleanup(port, device, proc) == 39000
    assert killed == []

def test_a_backend_whose_session_is_gone_is_stale(endpoint, monkeypatch):
    from oscmix_desk import process

    port, device, proc, holder, cleanup = _backend_with_parent(endpoint, ["bash"])
    monkeypatch.setattr(process.os, "getuid", os.getuid)
    monkeypatch.setattr(process, "STALE_BACKEND_SETTLE", 0.0)
    killed = []
    monkeypatch.setattr(process, "_terminate",
                        lambda pid, still_stale: killed.append(pid) or True)
    assert cleanup(port, device, proc) is None
    assert killed == [int(holder.name)]
    # Reparented to init after its session died: stale as well.
    (holder / "stat").write_text("%s (oscmix) S 1 0 0\n" % holder.name)
    killed.clear()
    assert cleanup(port, device, proc) is None
    assert killed == [int(holder.name)]

def test_a_switch_of_another_desk_does_not_reload_the_unit(tmp_path, monkeypatch,
                                                            capsys):
    """The unit re-applied its own routing.conf over the switch (0.6.9)."""
    from oscmix_desk import cli

    reloads = []
    monkeypatch.setattr(cli, "reload_service",
                        lambda: reloads.append(1) or cli.RELOAD_DONE)
    outcome = outcome_mod.Outcome(state=outcome_mod.APPLIED_UNVERIFIED, name="x",
                               reason=outcome_mod.NOT_CHECKED, persisted=True)
    unit_desk = tmp_path / "unit" / "routing.conf"
    unit_desk.parent.mkdir()
    unit_desk.write_text(DESK)
    monkeypatch.setattr(cli, "unit_process", unit())
    monkeypatch.setattr(cli, "discover_config_path", lambda *a: unit_desk)
    assert cli._report_outcome(outcome, tmp_path / "other.conf") == cli.EXIT_OK
    assert reloads == []
    assert cli._report_outcome(outcome, unit_desk) == cli.EXIT_OK
    assert cli._report_outcome(outcome, None) == cli.EXIT_OK
    assert reloads == [1, 1]
    capsys.readouterr()

def test_the_reload_follows_the_unit_s_environment_not_the_shell_s(
        tmp_path, monkeypatch, capsys):
    from oscmix_desk import cli

    unit_desk = tmp_path / "unit" / "routing.conf"
    other = tmp_path / "other" / "routing.conf"
    for path in (unit_desk, other):
        path.parent.mkdir()
        path.write_text(DESK)
    reloads = []
    monkeypatch.setattr(cli, "reload_service",
                        lambda: reloads.append(1) or cli.RELOAD_DONE)
    outcome = outcome_mod.Outcome(state=outcome_mod.APPLIED_UNVERIFIED, name="x",
                               reason=outcome_mod.NOT_CHECKED, persisted=True)
    # The unit names its desk in its own Environment=.
    monkeypatch.setattr(cli, "unit_process",
                        unit({"OSCMIX_CONFIG": str(unit_desk)}))
    # The shell's OSCMIX_CONFIG names another file: no --config, and the
    # switch was still for the other desk (the 0.6.9 bug via the env).
    monkeypatch.setenv("OSCMIX_CONFIG", str(other))
    assert cli._report_outcome(outcome, other) == cli.EXIT_OK
    assert reloads == []
    # A --config naming the unit's own file is the unit's desk.
    assert cli._report_outcome(outcome, unit_desk) == cli.EXIT_OK
    assert reloads == [1]
    # No unit process to read: reload as before.
    monkeypatch.setattr(cli, "unit_process", lambda *a: None)
    assert cli._report_outcome(outcome, other) == cli.EXIT_OK
    assert reloads == [1, 1]
    capsys.readouterr()

def test_the_unit_s_desk_is_resolved_in_the_unit_s_environment(tmp_path,
                                                                monkeypatch):
    """XDG_CONFIG_HOME and HOME of the unit, not of this shell.

    The first cut resolved only OSCMIX_CONFIG from the unit and took
    the XDG fallback from the shell, so `XDG_CONFIG_HOME=/x
    oscmix-session --profile Y` reloaded a unit that runs ~/.config.
    """
    def desk(root):
        (root / "oscmix").mkdir(parents=True)
        (root / "oscmix" / "routing.conf").write_text(DESK)
        return root / "oscmix" / "routing.conf"

    shell = desk(tmp_path / "shell")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(shell.parent.parent))
    monkeypatch.setenv("OSCMIX_CONFIG", str(tmp_path / "elsewhere.conf"))
    by_xdg = desk(tmp_path / "unit-xdg")
    monkeypatch.setattr(cli, "unit_process", unit({
        "XDG_CONFIG_HOME": str(by_xdg.parent.parent)}))
    assert cli._unit_desk() == (True, by_xdg)
    by_home = desk(tmp_path / "unit-home" / ".config")
    monkeypatch.setattr(cli, "unit_process", unit({
        "HOME": str(tmp_path / "unit-home")}))
    assert cli._unit_desk() == (True, by_home)
    # OSCMIX_CONFIG in the unit wins over both, existing or not.
    monkeypatch.setattr(cli, "unit_process", unit({
        "OSCMIX_CONFIG": str(tmp_path / "named.conf"), "HOME": str(tmp_path)}))
    assert cli._unit_desk() == (True, tmp_path / "named.conf")

def test_the_unit_s_desk_is_its_config_argument_before_its_environment(
        tmp_path, monkeypatch):
    """A unit started with `--config /x` runs /x whatever its environment
    says, as reload_mod._config_path reads it; a relative path is against
    the unit's working directory, not this shell's."""
    environ = {"OSCMIX_CONFIG": str(tmp_path / "env.conf")}
    (tmp_path / "shell").mkdir()
    monkeypatch.chdir(tmp_path / "shell")
    for argv in (("oscmix-session", "--config", "/x/routing.conf"),
                 ("oscmix-session", "--config=/x/routing.conf"),
                 ("oscmix-session", "--conf", "/x/routing.conf", "--timeout", "5")):
        monkeypatch.setattr(cli, "unit_process", unit(environ, argv, "/unit"))
        assert cli._unit_desk() == (True, Path("/x/routing.conf")), argv
    monkeypatch.setattr(cli, "unit_process",
                        unit(environ, ("oscmix-session", "--config", "desk/r.conf"),
                              "/unit"))
    assert cli._unit_desk() == (True, Path("/unit/desk/r.conf"))
    monkeypatch.setattr(cli, "unit_process",
                        unit({"OSCMIX_CONFIG": "desk/r.conf"}, cwd="/unit"))
    assert cli._unit_desk() == (True, Path("/unit/desk/r.conf"))
    # A command line this parser cannot read: cannot be told, and the
    # parser's usage text is the unit's, not this switch's stderr.
    monkeypatch.setattr(cli, "unit_process",
                        unit(environ, ("oscmix-session", "--config"), "/unit"))
    monkeypatch.setattr(cli.sys, "stderr", io.StringIO())
    assert cli._unit_desk() == (False, None), "cannot be told"
    assert cli.sys.stderr.getvalue() == ""
    # An empty final argument is an argument, not a terminator: a unit
    # started with `--device ''` runs its --config, not "cannot be told".
    monkeypatch.setattr(cli, "unit_process", unit(
        environ, ("oscmix-session", "--config", "/x/r.conf", "--device", ""),
        "/unit"))
    assert cli._unit_desk() == (True, Path("/x/r.conf"))
    # No desk anywhere: told, and there is none -- an answer, not a guess.
    monkeypatch.setattr(cli, "unit_process", unit({"HOME": str(tmp_path)}))
    assert cli._unit_desk() == (True, None)

def test_the_unit_s_process_is_read_from_proc(tmp_path, monkeypatch):
    """/proc/<MainPID>/{cmdline,environ,cwd}: no quoting to undo, and the
    manager's XDG_CONFIG_HOME and HOME are there, which `systemctl show
    -p Environment` never lists."""
    from oscmix_desk import process

    entry = tmp_path / "4242"
    entry.mkdir()
    (entry / "cmdline").write_bytes(
        b"python3\0oscmix-session\0--config\0/my desk/r.conf\0--device\0\0")
    (entry / "environ").write_bytes(
        b"HOME=/home/x\0OSCMIX_CONFIG=/home/x/my desk/routing.conf\0"
        b"NOEQUALS\0\0EMPTY=\0")
    (entry / "cwd").symlink_to(tmp_path)
    answers = {"MainPID": "4242\n"}
    monkeypatch.setattr(process, "_systemctl_output",
                        lambda *verb: answers.get(verb[2]))
    unit = process.unit_process(tmp_path)
    assert unit == process.UnitProcess(
        argv=("python3", "oscmix-session", "--config", "/my desk/r.conf",
              "--device", ""),
        environ={"HOME": "/home/x", "EMPTY": "",
                 "OSCMIX_CONFIG": "/home/x/my desk/routing.conf"},
        cwd=tmp_path)
    # Exited but not reaped: cmdline and environ read empty. Not told.
    (entry / "cmdline").write_bytes(b"")
    assert process.unit_process(tmp_path) is None
    (entry / "cmdline").write_bytes(b"x\0")
    (entry / "environ").write_bytes(b"")
    assert process.unit_process(tmp_path) is None
    (entry / "environ").write_bytes(b"A=b\0")
    assert process.unit_process(tmp_path) is not None
    (entry / "cwd").unlink()
    assert process.unit_process(tmp_path) is None
    # Not running: MainPID is 0. Unreadable or absent: None as well.
    answers["MainPID"] = "0\n"
    assert process.unit_process(tmp_path) is None
    answers["MainPID"] = "4243\n"
    assert process.unit_process(tmp_path) is None
    answers["MainPID"] = "garbage"
    assert process.unit_process(tmp_path) is None
    answers.clear()
    assert process.unit_process(tmp_path) is None

def test_a_session_named_past_the_interpreter_or_under_init_is_not_one(
        endpoint, monkeypatch):
    """Only argv[0] or argv[1] names the program -- `python3 <script>` or
    the script itself. A later argument that happens to be called
    oscmix-session is an editor's file, and a backend whose parent is
    pid 1 has no session whatever pid 1 runs."""
    from oscmix_desk import process

    port, device, proc, holder, cleanup = _backend_with_parent(
        endpoint, ["vim", "-R", "oscmix-session"])
    monkeypatch.setattr(process.os, "getuid", os.getuid)
    monkeypatch.setattr(process, "STALE_BACKEND_SETTLE", 0.0)
    killed = []
    monkeypatch.setattr(process, "_terminate",
                        lambda pid, still_stale: killed.append(pid) or True)
    assert cleanup(port, device, proc) is None
    assert killed == [int(holder.name)]
    init = proc / "1"
    init.mkdir()
    (init / "cmdline").write_bytes(b"oscmix-session\0")
    (holder / "stat").write_text("%s (oscmix) S 1 0 0\n" % holder.name)
    assert process._supervising_session(holder, proc) is None
    # An argv that is not UTF-8 is read, not raised on.
    (proc / "39000" / "cmdline").write_bytes(
        b"python3\0/opt/\xff/oscmix-session\0")
    (holder / "stat").write_text("%s (oscmix) S 39000 0 0\n" % holder.name)
    assert process._supervising_session(holder, proc) == 39000

def test_the_unit_s_main_pid_is_asked_for_exactly(tmp_path, monkeypatch):
    """`systemctl --user show -p MainPID --value oscmix.service`: without
    --value the answer is `MainPID=4242`, which is no pid."""
    from oscmix_desk import process

    asked = []
    monkeypatch.setattr(process, "_systemctl_output",
                        lambda *verb: asked.append(verb) or "0\n")
    assert process.unit_process(tmp_path) is None
    assert asked == [("show", "-p", "MainPID", "--value", "oscmix.service")]
    # "0" is not running even where a /proc/0 would answer.
    zero = tmp_path / "0"
    zero.mkdir()
    (zero / "cmdline").write_bytes(b"x\0")
    (zero / "environ").write_bytes(b"A=b\0")
    (zero / "cwd").symlink_to(tmp_path)
    assert process.unit_process(tmp_path) is None
    # A value may contain "=": only the first one separates the name.
    entry = tmp_path / "4242"
    entry.mkdir()
    (entry / "cmdline").write_bytes(b"x\0")
    (entry / "environ").write_bytes(b"OSCMIX_CONFIG=/a=b/routing.conf\0")
    (entry / "cwd").symlink_to(tmp_path)
    monkeypatch.setattr(process, "_systemctl_output", lambda *verb: "4242\n")
    assert process.unit_process(tmp_path).environ == {
        "OSCMIX_CONFIG": "/a=b/routing.conf"}

@pytest.mark.parametrize('override', [False, True])
def test_the_unit_desk_uses_the_selected_proc_and_compares_files_safely(
        tmp_path, monkeypatch, override):
    roots = []
    if override:
        monkeypatch.setenv('OSCMIX_PROC_ROOT', str(tmp_path / 'selected-proc'))
    else:
        monkeypatch.delenv("OSCMIX_PROC_ROOT", raising=False)
    monkeypatch.setattr(cli, "unit_process", lambda root: roots.append(root))
    assert cli._unit_desk() == (False, None)
    assert roots == [tmp_path / 'selected-proc' if override else Path('/proc')]
    assert cli._same_file(tmp_path / "a", None) is False

    def unreadable(self, *a, **k):
        raise OSError("loop")

    monkeypatch.setattr(cli.Path, "resolve", unreadable)
    assert cli._same_file(tmp_path / "a", tmp_path / "a") is False
