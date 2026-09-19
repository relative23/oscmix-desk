"""Backend process handling: stale cleanup and stop escalation.

``_cleanup_stale_backend`` sends SIGTERM to PIDs it selected itself. Code
that signals processes deserves direct tests for its *refusals* more than
for its successes, so most of what follows checks that it does nothing.
"""

import os
import signal

import pytest


@pytest.fixture
def process_mod():
    from oscmix_desk import process

    return process


def fake_proc(tmp_path, entries, listening_port=None, owner=None):
    """A /proc tree: {pid: (comm, argv0)}, an optional net/udp entry.

    ``owner`` is the pid whose ``fd`` directory links to the socket the
    udp row names. Name and user do not make a process the holder of a
    port, and since 0.6.6 the cleanup insists on the link.
    """
    root = tmp_path / "proc"
    (root / "net").mkdir(parents=True)
    header = ("  sl  local_address rem_address   st tx_queue rx_queue tr "
              "tm->when retrnsmt   uid  timeout inode\n")
    rows = header
    if listening_port is not None:
        rows += ("  100: 0100007F:%04X 00000000:0000 07 00000000:00000000 "
                 "00:00000000 00000000  1000        0 1\n" % listening_port)
    (root / "net" / "udp").write_text(rows)
    for pid, (comm, argv0) in entries.items():
        entry = root / str(pid)
        entry.mkdir()
        (entry / "comm").write_text(comm + "\n")
        (entry / "cmdline").write_bytes(argv0.encode() + b"\0")
        handles = entry / "fd"
        handles.mkdir()
        if owner is not None and str(pid) == str(owner):
            os.symlink("socket:[1]", handles / "3")
    return root


def test_nothing_is_signalled_when_the_port_is_free(process_mod, tmp_path,
                                                    monkeypatch):
    # No stale backend can be holding a port nobody is listening on.
    killed = []
    monkeypatch.setattr(process_mod.os, "kill",
                        lambda pid, sig: killed.append(pid))
    proc = fake_proc(tmp_path, {"200": ("oscmix", "/usr/bin/oscmix")})
    process_mod._cleanup_stale_backend(7222, proc)
    assert killed == []


def test_an_unknown_holder_is_reported_not_killed(process_mod, tmp_path,
                                                  monkeypatch, caplog):
    # Someone else has the port. Killing whatever we can find would be
    # worse than failing to bind.
    killed = []
    monkeypatch.setattr(process_mod.os, "kill",
                        lambda pid, sig: killed.append(pid))
    proc = fake_proc(tmp_path, {"200": ("sshd", "/usr/sbin/sshd")},
                     listening_port=7222, owner="200")
    with caplog.at_level("WARNING"):
        process_mod._cleanup_stale_backend(7222, proc)
    assert killed == []
    assert "not an oscmix of this user" in caplog.text


def test_an_oscmix_that_does_not_hold_the_port_is_left_alone(
        process_mod, tmp_path, monkeypatch, caplog):
    """The scenario a name-only match gets wrong.

    Something else holds the port, and a perfectly legitimate oscmix of
    the same user is driving another interface. Until 0.6.6 the cleanup
    terminated that one, and left the actual holder running.
    """
    signalled = []
    monkeypatch.setattr(process_mod, "_terminate",
                        lambda pid: signalled.append(pid) or True)
    proc = fake_proc(tmp_path, {"200": ("sshd", "/usr/sbin/sshd"),
                                "201": ("oscmix", "/home/u/.local/bin/oscmix")},
                     listening_port=7222, owner="200")
    with caplog.at_level("WARNING"):
        process_mod._cleanup_stale_backend(7222, proc)
    assert signalled == [], "the other oscmix never had this port"
    assert "not an oscmix of this user" in caplog.text


def test_a_holder_that_cannot_be_identified_is_left_alone(
        process_mod, tmp_path, monkeypatch, caplog):
    # No fd links to read: nobody can be shown to hold the port, so
    # nobody is signalled, and the start fails on the port wait instead.
    signalled = []
    monkeypatch.setattr(process_mod, "_terminate",
                        lambda pid: signalled.append(pid) or True)
    proc = fake_proc(tmp_path, {"201": ("oscmix", "oscmix")},
                     listening_port=7222)
    with caplog.at_level("WARNING"):
        process_mod._cleanup_stale_backend(7222, proc)
    assert signalled == []
    assert "cannot be identified" in caplog.text


def test_a_stale_backend_is_terminated(process_mod, tmp_path, monkeypatch):
    signalled = []
    monkeypatch.setattr(process_mod, "_terminate",
                        lambda pid: signalled.append((pid, signal.SIGTERM))
                        or True)
    monkeypatch.setattr(process_mod.time, "sleep", lambda _s: None)
    proc = fake_proc(tmp_path, {"201": ("oscmix", "/home/u/.local/bin/oscmix")},
                     listening_port=7222, owner="201")
    assert process_mod._cleanup_stale_backend(7222, proc) is None, \
        "signalled, so the start goes on"
    assert signalled == [(201, signal.SIGTERM)]


def test_a_vanished_process_does_not_raise(process_mod, tmp_path, monkeypatch):
    # Between listing /proc and signalling, the process may exit. That is
    # the normal case, not an error.
    def gone(pid):
        raise ProcessLookupError(pid)

    monkeypatch.setattr(process_mod.os, "pidfd_open", gone, raising=False)
    monkeypatch.setattr(process_mod.os, "kill",
                        lambda pid, sig: (_ for _ in ()).throw(
                            ProcessLookupError(pid)))
    monkeypatch.setattr(process_mod.time, "sleep", lambda _s: None)
    proc = fake_proc(tmp_path, {"202": ("oscmix", "oscmix")},
                     listening_port=7222, owner="202")
    process_mod._cleanup_stale_backend(7222, proc)


def test_termination_uses_a_pidfd_so_pid_reuse_cannot_bite(process_mod,
                                                           monkeypatch):
    # The point of the pidfd: it names the process, not the number. If a
    # pidfd is available, os.kill must not be reached at all.
    opened, signalled, killed = [], [], []
    # raising=False: pidfd_open is Linux-only and some builds lack it,
    # which is exactly why the runtime has a fallback.
    monkeypatch.setattr(process_mod.os, "pidfd_open",
                        lambda pid: opened.append(pid) or 999, raising=False)
    monkeypatch.setattr(process_mod.os, "close", lambda fd: None)
    monkeypatch.setattr(process_mod.signal, "pidfd_send_signal",
                        lambda fd, sig: signalled.append((fd, sig)),
                        raising=False)
    monkeypatch.setattr(process_mod.os, "kill",
                        lambda pid, sig: killed.append(pid))
    process_mod._terminate(4321)
    assert opened == [4321]
    assert signalled == [(999, signal.SIGTERM)]
    assert killed == [], "os.kill must not run when a pidfd was obtained"


def test_without_a_pidfd_nothing_is_signalled_and_the_start_says_so(
        process_mod, tmp_path, monkeypatch, caplog):
    """Blocked by seccomp, or a system without it. It fell back to
    `os.kill` until 0.7.0 -- the race by number that the pidfd exists to
    avoid -- and took that fallback for a process that had merely exited
    too. Nothing is signalled now, the start is refused with exit 2's
    answer (the holder's pid), and the journal says what to do."""
    killed = []

    def refuse(pid):
        raise PermissionError(1, "Operation not permitted")

    monkeypatch.setattr(process_mod.os, "pidfd_open", refuse, raising=False)
    monkeypatch.setattr(process_mod.os, "kill",
                        lambda pid, sig: killed.append((pid, sig)))
    monkeypatch.setattr(process_mod.time, "sleep", killed.append)
    with caplog.at_level("ERROR"):
        assert process_mod._terminate(4321) is False
    assert "the stale oscmix (pid 4321) was not signalled" in caplog.text
    assert "Operation not permitted" in caplog.text
    proc = fake_proc(tmp_path, {"202": ("oscmix", "oscmix")},
                     listening_port=7222, owner="202")
    assert process_mod._cleanup_stale_backend(7222, proc) == 202
    assert killed == [], "no kill by number, and no settle for nothing"


def test_a_process_that_exited_needs_no_signal(process_mod, monkeypatch):
    def gone(pid):
        raise ProcessLookupError(3, "No such process")

    killed = []
    monkeypatch.setattr(process_mod.os, "pidfd_open", gone, raising=False)
    monkeypatch.setattr(process_mod.os, "kill",
                        lambda pid, sig: killed.append(pid))
    assert process_mod._terminate(4321) is True
    assert killed == []


class Child:
    """A backend that ignores SIGTERM for a while, then exits."""

    def __init__(self, ignore_terminate=False, exit_after=0):
        self.ignore_terminate = ignore_terminate
        self.exit_after = exit_after
        self.waits = 0
        self.terminated = self.killed = False

    def wait(self, timeout=None):
        self.waits += 1
        if self.killed or (self.terminated and not self.ignore_terminate):
            return 0
        if self.exit_after and self.waits >= self.exit_after:
            return 0
        if timeout is None:
            return 0
        import subprocess

        raise subprocess.TimeoutExpired("backend", timeout)

    def terminate(self):
        self.terminated = True

    def kill(self):
        self.killed = True


def test_supervise_returns_when_the_backend_exits(process_mod):
    child = Child(exit_after=2)
    assert process_mod.supervise(child, {"stop": False}) == 0
    assert not child.killed


def test_supervise_escalates_to_sigkill_after_the_grace(process_mod,
                                                        monkeypatch):
    # The unit's TimeoutStopSec is 10 s; a backend that ignores SIGTERM
    # has to be killed inside that, or systemd kills the session instead
    # and the shutdown looks like a failure.
    monkeypatch.setattr(process_mod, "CHILD_STOP_GRACE", 0.0)
    child = Child(ignore_terminate=True)
    assert process_mod.supervise(child, {"stop": True}) == 0
    assert child.killed


def test_supervise_does_not_kill_while_no_stop_was_requested(process_mod,
                                                             monkeypatch):
    monkeypatch.setattr(process_mod, "CHILD_STOP_GRACE", 0.0)
    child = Child(exit_after=3)
    process_mod.supervise(child, {"stop": False})
    assert not child.killed


def test_resolve_binary_prefers_the_environment_override(process_mod,
                                                         tmp_path,
                                                         monkeypatch):
    from oscmix_desk import discovery

    binary = tmp_path / "oscmix"
    binary.write_text("#!/bin/sh\n")
    binary.chmod(0o755)
    monkeypatch.setenv("OSCMIX_BIN_BACKEND", str(binary))
    assert discovery.resolve_binary("oscmix", "OSCMIX_BIN_BACKEND") == str(binary)


def test_a_broken_override_fails_loudly_instead_of_falling_back(tmp_path,
                                                                monkeypatch,
                                                                caplog):
    # An override names a specific binary. Quietly using a different one
    # would start something the operator did not ask for; refusing sends
    # the session down the "run install.sh first" path with a reason.
    from oscmix_desk import discovery

    monkeypatch.setenv("OSCMIX_BIN_BACKEND", str(tmp_path / "missing"))
    monkeypatch.setattr(discovery.shutil, "which",
                        lambda name: "/usr/bin/" + name)
    with caplog.at_level("ERROR"):
        assert discovery.resolve_binary("oscmix", "OSCMIX_BIN_BACKEND") is None
    assert "not an executable file" in caplog.text


def test_resolve_binary_falls_back_to_the_standard_locations(monkeypatch):
    # The systemd user manager's PATH need not contain ~/.local/bin,
    # which is exactly where install.sh puts the backend.
    from oscmix_desk import discovery

    home_bin = os.path.expanduser("~/.local/bin/oscmix")
    monkeypatch.delenv("OSCMIX_BIN_BACKEND", raising=False)
    monkeypatch.setattr(discovery.shutil, "which", lambda name: None)
    monkeypatch.setattr(discovery.os, "access",
                        lambda path, mode: path == home_bin)
    assert discovery.resolve_binary("oscmix", "OSCMIX_BIN_BACKEND") == home_bin


def test_a_stale_path_copy_does_not_shadow_the_pinned_install(monkeypatch):
    # Measured 2026-08-26: the first hotplug start after boot ran with
    # the user manager's default PATH, which lacks ~/.local/bin but has
    # /usr/local/bin -- and a stale February build there won the PATH
    # lookup over the pinned install for six hours. The pinned location
    # is consulted before PATH ever is.
    from oscmix_desk import discovery

    home_bin = os.path.expanduser("~/.local/bin/oscmix")
    monkeypatch.delenv("OSCMIX_BIN_BACKEND", raising=False)
    monkeypatch.setattr(discovery.shutil, "which",
                        lambda name: "/usr/local/bin/" + name)
    monkeypatch.setattr(discovery.os, "access",
                        lambda path, mode: path in (home_bin,
                                                    "/usr/local/bin/oscmix"))
    assert discovery.resolve_binary("oscmix", "OSCMIX_BIN_BACKEND") == home_bin


def test_without_a_pinned_install_the_path_lookup_still_serves(monkeypatch):
    # No ~/.local/bin install (a from-source user, say): whatever PATH
    # names is used, exactly as before the ordering fix.
    from oscmix_desk import discovery

    monkeypatch.delenv("OSCMIX_BIN_BACKEND", raising=False)
    monkeypatch.setattr(discovery.shutil, "which",
                        lambda name: "/opt/audio/bin/" + name)
    monkeypatch.setattr(discovery.os, "access", lambda path, mode: False)
    assert (discovery.resolve_binary("oscmix", "OSCMIX_BIN_BACKEND")
            == "/opt/audio/bin/oscmix")


def test_resolve_binary_returns_none_when_nothing_is_found(monkeypatch):
    from oscmix_desk import discovery

    monkeypatch.delenv("OSCMIX_BIN_BACKEND", raising=False)
    monkeypatch.setattr(discovery.shutil, "which", lambda name: None)
    monkeypatch.setattr(discovery.os, "access", lambda path, mode: False)
    assert discovery.resolve_binary("oscmix", "OSCMIX_BIN_BACKEND") is None


# --------------------------------------------------------------------------
# Reloading the unit after a switch (ADR 0018)
# --------------------------------------------------------------------------

def test_reload_service_says_when_the_unit_is_not_running(monkeypatch):
    from oscmix_desk import process

    verbs = []
    monkeypatch.setattr(process, "_systemctl",
                        lambda *verb: verbs.append(verb) or 3)
    assert process.reload_service() == process.RELOAD_NOT_RUNNING
    assert verbs == [("is-active", "--quiet", "oscmix.service")]


def test_reload_service_reloads_a_running_unit(monkeypatch):
    from oscmix_desk import process

    verbs = []
    monkeypatch.setattr(process, "_systemctl",
                        lambda *verb: verbs.append(verb) or 0)
    assert process.reload_service() == process.RELOAD_DONE
    assert verbs == [("is-active", "--quiet", "oscmix.service"),
                     ("reload", "oscmix.service")]


def test_a_running_unit_that_refuses_the_reload_is_its_own_answer(monkeypatch):
    """Three outcomes, not two.

    "Not running" is fine: nothing can revert the switch. A refusal is
    not: the unit is up, acting on a desk it has not re-read, and until
    0.6.6 the caller was told it was not running at all.
    """
    from oscmix_desk import process

    monkeypatch.setattr(process, "_systemctl",
                        lambda *verb: 0 if verb[0] == "is-active" else 1)
    assert process.reload_service() == process.RELOAD_FAILED


def test_systemctl_returns_the_exit_status_and_one_without_the_binary(
        monkeypatch, real_systemctl):
    # Every other test sees the autouse stub; this one checks the
    # function behind it, with subprocess replaced so nothing runs.
    from oscmix_desk import process

    seen = []

    class Done:
        returncode = 4

    def run(argv, **kw):
        # Never raise on a non-zero status, and never let systemctl's
        # own output onto the CLI's stdout, which a script may parse.
        assert kw["check"] is False
        assert kw["stdout"] is process.subprocess.DEVNULL
        assert kw["stderr"] is process.subprocess.DEVNULL
        seen.append(argv)
        return Done()

    monkeypatch.setattr(process.subprocess, "run", run)
    assert real_systemctl("reload", "x.service") == 4
    assert seen == [["systemctl", "--user", "reload", "x.service"]]

    def missing(argv, **kw):
        raise OSError("no systemctl")

    monkeypatch.setattr(process.subprocess, "run", missing)
    assert real_systemctl("is-active", "x.service") == 1


def test_systemctl_output_is_stdout_on_success_and_none_otherwise(
        monkeypatch, real_systemctl_output):
    """The body behind the second stub. As text: unit_process compares
    the MainPID it returns with str.isdigit and joins it onto a path."""
    from oscmix_desk import process

    seen = []

    def answer(code, out):
        class Done:
            returncode = code
            stdout = out
        return Done()

    def run(argv, **kw):
        assert kw["check"] is False
        assert kw["capture_output"] is True
        assert kw["text"] is True
        seen.append(argv)
        return answer(0, "4242\n")

    monkeypatch.setattr(process.subprocess, "run", run)
    assert real_systemctl_output("show", "-p", "MainPID") == "4242\n"
    assert seen == [["systemctl", "--user", "show", "-p", "MainPID"]]
    monkeypatch.setattr(process.subprocess, "run",
                        lambda argv, **kw: answer(1, "partial"))
    assert real_systemctl_output("show") is None

    def missing(argv, **kw):
        raise OSError("no systemctl")

    monkeypatch.setattr(process.subprocess, "run", missing)
    assert real_systemctl_output("show") is None


# --------------------------------------------------------------------------
# Resolving the holder of the port (ADR 0021).
# --------------------------------------------------------------------------

def test_the_owner_is_unknown_when_no_process_holds_the_inode(process_mod,
                                                              tmp_path):
    # The port is listed, but no fd anywhere points at its socket. That
    # is "cannot be told", not "nobody", and the caller must not guess.
    proc = fake_proc(tmp_path, {"201": ("oscmix", "oscmix")},
                     listening_port=7222)
    assert process_mod.socket_owner(7222, proc) is None


def test_the_owner_is_unknown_when_the_port_is_not_listed(process_mod,
                                                          tmp_path):
    proc = fake_proc(tmp_path, {"201": ("oscmix", "oscmix")}, owner="201")
    assert process_mod.socket_owner(7222, proc) is None


def test_a_process_whose_handles_cannot_be_read_is_skipped(process_mod,
                                                           tmp_path):
    """Another user's process, or one that exited during the scan.

    Both raise on the fd directory, and both mean "not this one" rather
    than "give up": the holder may still be further down the list.
    """
    proc = fake_proc(tmp_path, {"200": ("sshd", "/usr/sbin/sshd"),
                                "201": ("oscmix", "oscmix")},
                     listening_port=7222, owner="201")
    (proc / "200" / "fd").chmod(0o000)
    try:
        assert process_mod.socket_owner(7222, proc) == 201
    finally:
        (proc / "200" / "fd").chmod(0o755)


def test_a_handle_that_cannot_be_read_is_skipped(process_mod, tmp_path,
                                                 monkeypatch):
    proc = fake_proc(tmp_path, {"201": ("oscmix", "oscmix")},
                     listening_port=7222, owner="201")

    def refuse(_path):
        raise OSError("gone")

    monkeypatch.setattr(process_mod.os, "readlink", refuse)
    assert process_mod.socket_owner(7222, proc) is None


def test_a_link_to_another_socket_is_not_ownership(process_mod, tmp_path):
    """The prefix and the inode have to hold together.

    With either half alone, the first process that has any socket open
    is named as the holder of the port -- and this function decides who
    gets SIGTERM.
    """
    proc = fake_proc(tmp_path, {"201": ("oscmix", "oscmix")},
                     listening_port=7222)
    os.symlink("socket:[999]", proc / "201" / "fd" / "4")
    assert process_mod.socket_owner(7222, proc) is None
