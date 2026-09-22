"""Whose backend, and whose unit: the process that holds the OSC port,
the interface it bridges, the session above it, and the desk the unit
runs -- read from /proc, never assumed (ADR 0021, ADR 0024).
"""

import io
import os
from pathlib import Path

from support import fake_proc, free_udp_port
from two_boxes import DESK, B, unit

from oscmix_desk import cli
from oscmix_desk import outcome as outcome_mod
from oscmix_desk.process import port_holder


def test_the_holder_is_followed_to_the_client_it_bridges(tmp_path):
    port = free_udp_port()
    proc = fake_proc(tmp_path, boxes=[B], bound=[(port, "oscmix", B[0])])
    holder = port_holder(port, proc)
    assert holder is not None
    assert (holder.oscmix, holder.client, holder.serial) == (True, B[0], B[1])

def test_a_bridge_above_the_holder_is_found_too(tmp_path):
    """alsaseqio forks and oscmix is the parent on a real desk; not always."""
    port = free_udp_port()
    proc = fake_proc(tmp_path, boxes=[B], bound=[(port, "oscmix", None)])
    holder_entry = next(p for p in proc.iterdir() if p.name.isdigit())
    parent = proc / "39000"
    (parent / "fd").mkdir(parents=True)
    (parent / "comm").write_text("alsaseqio\n")
    (parent / "stat").write_text("39000 (alsaseqio) S 1 0 0\n")
    (parent / "cmdline").write_bytes(b"alsaseqio\x0028:1\x00oscmix\x00")
    (holder_entry / "stat").write_text("%s (oscmix) S 39000 0 0\n"
                                       % holder_entry.name)
    holder = port_holder(port, proc)
    assert (holder.client, holder.serial) == (B[0], B[1])

def test_a_holder_that_is_not_oscmix_and_one_without_a_bridge(tmp_path):
    port, other = free_udp_port(), free_udp_port()
    proc = fake_proc(tmp_path, boxes=[B],
                     bound=[(port, "python3", None), (other, "oscmix", None)])
    stranger = port_holder(port, proc)
    assert stranger is not None
    assert stranger.oscmix is False
    lone = port_holder(other, proc)
    assert (lone.oscmix, lone.client, lone.serial) == (True, None, None)
    assert port_holder(free_udp_port(), proc) is None

def test_another_user_s_oscmix_is_not_this_desk_s_backend(tmp_path, monkeypatch):
    """Name is not ownership; for root, any user's oscmix is still oscmix."""
    from oscmix_desk import process

    port = free_udp_port()
    proc = fake_proc(tmp_path, boxes=[B], bound=[(port, "oscmix", B[0])])
    someone_else = os.getuid() + 1       # read before os.getuid is replaced
    monkeypatch.setattr(process.os, "getuid", lambda: someone_else)
    assert port_holder(port, proc).oscmix is False
    monkeypatch.setattr(process.os, "getuid", lambda: 0)
    assert port_holder(port, proc).oscmix is True

def _holder(tmp_path, comm, argv, children=(), stat_comm=None):
    """A /proc with one UDP holder whose comm, argv and children we choose."""
    port = free_udp_port()
    proc = fake_proc(tmp_path, boxes=[B], bound=[(port, "oscmix", None)])
    entry = next(p for p in proc.iterdir() if p.name.isdigit())
    (entry / "comm").write_bytes(comm)
    (entry / "cmdline").write_bytes(argv)
    for pid, child_stat, child_argv in children:
        child = proc / str(pid)
        (child / "fd").mkdir(parents=True)
        if child_stat is not None:
            (child / "stat").write_bytes(child_stat % entry.name.encode())
        if child_argv is not None:
            (child / "cmdline").write_bytes(child_argv)
    return port, proc

def test_an_oscmix_is_known_by_its_name_or_by_its_program(tmp_path):
    by_program = _holder(tmp_path / "a", b"osc-renamed\n",
                         b"/home/u/.local/bin/oscmix\x00-r\x00udp\x00")
    by_name = _holder(tmp_path / "b", b"oscmix\n", b"/usr/bin/python3\x00x\x00")
    neither = _holder(tmp_path / "c", b"python3\n", b"/usr/bin/python3\x00x\x00")
    assert port_holder(*by_program).oscmix is True
    assert port_holder(*by_name).oscmix is True
    assert port_holder(*neither).oscmix is False

def test_an_unreadable_or_undecodable_holder_is_not_an_oscmix(tmp_path):
    port, proc = _holder(tmp_path / "a", b"oscmix\n", b"oscmix\x00")
    entry = next(p for p in proc.iterdir() if p.name.isdigit())
    (entry / "comm").unlink()
    assert port_holder(port, proc).oscmix is False
    odd = _holder(tmp_path / "b", b"\xff\xfe\n", b"/x/\xffoscmix\x00")
    assert port_holder(*odd).oscmix is False

def test_the_bridge_is_found_past_an_unreadable_sibling_and_a_bracket_in_a_name(
        tmp_path):
    port, proc = _holder(
        tmp_path, b"oscmix\n", b"oscmix\x00",
        children=[
            (39999, b"39999 (gone) S %s 0 0\n", None),         # no cmdline
            (40005, b"40005 (als)aseqio) S %s 0 0\n",
             b"alsaseqio\x0028:1\x00oscmix\x00"),
        ])
    holder = port_holder(port, proc)
    assert (holder.client, holder.serial) == (B[0], B[1])

def _backend_with_parent(tmp_path, parent_argv):
    """A /proc where an oscmix holds a port and its parent is ``parent_argv``."""
    from oscmix_desk.process import _cleanup_stale_backend

    port = free_udp_port()
    proc = fake_proc(tmp_path, bound=[(port, "oscmix", None)])
    holder = next(p for p in proc.iterdir() if p.name.isdigit())
    parent = proc / "39000"
    (parent / "fd").mkdir(parents=True)
    (parent / "comm").write_text("python3\n")
    (parent / "stat").write_text("39000 (python3) S 1 0 0\n")
    (parent / "cmdline").write_bytes(b"\0".join(a.encode() for a in parent_argv) + b"\0")
    (holder / "stat").write_text("%s (oscmix) S 39000 0 0\n" % holder.name)
    return port, proc, holder, _cleanup_stale_backend

def test_a_backend_of_a_live_session_is_not_stale(tmp_path, monkeypatch):
    """A second session by hand terminated the unit's backend (measured)."""
    from oscmix_desk import process

    port, proc, _holder, cleanup = _backend_with_parent(
        tmp_path, ["python3", "/home/u/.local/bin/oscmix-session"])
    monkeypatch.setattr(process.os, "getuid", os.getuid)
    killed = []
    monkeypatch.setattr(process, "_terminate",
                        lambda pid, still_stale: killed.append(pid) or True)
    assert cleanup(port, proc) == 39000
    assert killed == []

def test_a_backend_whose_session_is_gone_is_stale(tmp_path, monkeypatch):
    from oscmix_desk import process

    port, proc, holder, cleanup = _backend_with_parent(tmp_path, ["bash"])
    monkeypatch.setattr(process.os, "getuid", os.getuid)
    monkeypatch.setattr(process, "STALE_BACKEND_SETTLE", 0.0)
    killed = []
    monkeypatch.setattr(process, "_terminate",
                        lambda pid, still_stale: killed.append(pid) or True)
    assert cleanup(port, proc) is None
    assert killed == [int(holder.name)]
    # Reparented to init after its session died: stale as well.
    (holder / "stat").write_text("%s (oscmix) S 1 0 0\n" % holder.name)
    killed.clear()
    assert cleanup(port, proc) is None
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
        tmp_path, monkeypatch):
    """Only argv[0] or argv[1] names the program -- `python3 <script>` or
    the script itself. A later argument that happens to be called
    oscmix-session is an editor's file, and a backend whose parent is
    pid 1 has no session whatever pid 1 runs."""
    from oscmix_desk import process

    port, proc, holder, cleanup = _backend_with_parent(
        tmp_path, ["vim", "-R", "oscmix-session"])
    monkeypatch.setattr(process.os, "getuid", os.getuid)
    monkeypatch.setattr(process, "STALE_BACKEND_SETTLE", 0.0)
    killed = []
    monkeypatch.setattr(process, "_terminate",
                        lambda pid, still_stale: killed.append(pid) or True)
    assert cleanup(port, proc) is None
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

def test_the_unit_desk_reads_the_real_proc_and_compares_files_safely(
        tmp_path, monkeypatch):
    roots = []
    monkeypatch.delenv("OSCMIX_PROC_ROOT", raising=False)
    monkeypatch.setattr(cli, "unit_process", lambda root: roots.append(root))
    assert cli._unit_desk() == (False, None)
    assert roots == [Path("/proc")]
    assert cli._same_file(tmp_path / "a", None) is False

    def unreadable(self, *a, **k):
        raise OSError("loop")

    monkeypatch.setattr(cli.Path, "resolve", unreadable)
    assert cli._same_file(tmp_path / "a", tmp_path / "a") is False
