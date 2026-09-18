"""Shared test fixtures.

Unit tests import the ``oscmix_desk`` package directly; the thin
executables in bin/ are covered end to end by the integration tests,
which run them as real subprocesses.

``load_executable`` remains for the bin/ shims themselves: they have no
.py extension, so they need SourceFileLoader, and their
``if __name__ == "__main__"`` guard keeps the import side-effect free.
"""

import collections
import importlib.machinery
import importlib.util
import os
import socket
import struct
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))


def repo_file(*parts):
    """Locate a file that ships with the repository.

    Not simply ``PROJECT_ROOT / parts``: a mutation run executes a copied
    tree that contains only sources and tests, no shipped data files. That
    copy lives inside the real checkout, so walking up finds the original.
    """
    for base in Path(__file__).resolve().parents:
        candidate = base.joinpath(*parts)
        if candidate.exists():
            return candidate
    raise FileNotFoundError("/".join(parts))


_recent_ports = collections.deque(maxlen=32)


def free_udp_port():
    """An ephemeral UDP port that was free a moment ago.

    The bind-close-return pattern lets the OS hand the same port out
    twice in a row, and callers draw pairs -- send == recv means a test
    backend answering itself and a CLI reading total silence, which is
    how test_a_rewrite_alone_does_not_make_it_differ failed once in
    CI's flake gate (run 33161748663) with empty stdout. Remembering
    the last few draws makes a pair collision impossible.
    """
    while True:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
        sock.close()
        if port not in _recent_ports:
            _recent_ports.append(port)
            return port


def fake_proc(directory, bound=(), boxes=()):
    """A /proc for device resolution and port identity (ADR 0024).

    ``boxes`` are (client, serial) pairs: each becomes a sequencer client
    named ``Fireface UCX II (<serial>)`` and a card, so the resolution
    sees what a machine with those interfaces shows. ``bound`` are
    (port, comm, client) triples: a UDP socket on ``port`` held by a
    process named ``comm`` -- and, when ``client`` is not None, an
    alsaseqio child of it bridging that client, which is how the unit's
    ``alsaseqio <client>:1 oscmix`` looks once alsaseqio has forked.

    Returns the directory to point `OSCMIX_PROC_ROOT` at.
    """
    net = directory / "net"
    net.mkdir(parents=True, exist_ok=True)
    header = ("  sl  local_address rem_address   st tx_queue rx_queue tr"
              " tm->when retrnsmt   uid  timeout inode ref pointer drops")
    rows = [header]
    for index, (port, comm, client) in enumerate(bound):
        inode = 100000 + index
        rows.append("%5d: 0100007F:%04X 00000000:0000 07 00000000:00000000"
                    " 00:00000000 00000000  1000        0 %d 2 0 0"
                    % (index, port, inode))
        holder = 40000 + 10 * index
        _fake_process(directory, holder, comm, 1, [comm, "-r", "udp"],
                      socket_inode=inode)
        if client is not None:
            _fake_process(directory, holder + 1, "alsaseqio", holder,
                          ["alsaseqio", "%d:1" % client, "oscmix"])
    (net / "udp").write_text("\n".join(rows) + "\n")
    seq = directory / "asound" / "seq"
    seq.mkdir(parents=True, exist_ok=True)
    (seq / "clients").write_text("".join(
        'Client %3d : "Fireface UCX II (%s)" [Kernel Legacy]\n'
        % (client, serial) for client, serial in boxes))
    (directory / "asound" / "cards").write_text("".join(
        " %d [II%s ]: USB-Audio - Fireface UCX II (%s)\n"
        "      RME Fireface UCX II (%s) at usb-0000:77:00.0-%d\n"
        % (i + 2, serial, serial, serial, i + 1)
        for i, (_client, serial) in enumerate(boxes)))
    return directory


def _fake_process(directory, pid, comm, ppid, argv, socket_inode=None):
    entry = directory / str(pid)
    (entry / "fd").mkdir(parents=True, exist_ok=True)
    (entry / "comm").write_text(comm + "\n")
    (entry / "stat").write_text("%d (%s) S %d 0 0\n" % (pid, comm, ppid))
    (entry / "cmdline").write_bytes(
        b"\0".join(a.encode() for a in argv) + b"\0")
    if socket_inode is not None:
        (entry / "fd" / "3").symlink_to("socket:[%d]" % socket_inode)


def proc_with_ports(directory, *ports):
    """A /proc in which an oscmix of this user holds each of ``ports``.

    A test that drives the real CLI without a backend still has to get
    past the reachability check -- since 0.6.9 that means a visible
    interface and an oscmix of this user on the port bridging its client,
    not merely a bound port (ADR 0024) -- because
    it is the outcome-to-exit-code translation it is testing, and
    reachability has tests of its own.
    """
    return fake_proc(directory, boxes=[(24, "24216011")],
                     bound=[(port, "oscmix", 24) for port in ports])


def read_until_ready(notify_sock):
    """The first non-STATUS datagram on a notify socket.

    Since 0.6.3 the session reports its phase to systemd with STATUS=
    before READY=1 -- legitimate protocol, and what `systemctl status`
    shows while the unit is still activating. A test that asserts the
    *first* datagram is READY would fail on that; this returns the first
    datagram that is not a STATUS line, and asserts nothing else came
    before it.
    """
    while True:
        datagram = notify_sock.recv(4096)
        if datagram.startswith(b"STATUS="):
            continue
        return datagram


def osc_bundle(messages):
    """Pack OSC messages into one bundle datagram."""
    bundle = bytearray(b"#bundle\x00" + b"\x00" * 8)
    for message in messages:
        bundle.extend(struct.pack(">i", len(message)))
        bundle.extend(message)
    return bytes(bundle)


def load_executable(name):
    path = repo_file("bin", name)
    loader = importlib.machinery.SourceFileLoader(name.replace("-", "_"), str(path))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    # dataclasses (3.14+) resolves annotations via sys.modules[__module__].
    sys.modules[loader.name] = module
    loader.exec_module(module)
    return module


@pytest.fixture(autouse=True)
def _own_runtime_dir(tmp_path_factory, monkeypatch):
    """No test may touch the device lock of the machine it runs on.

    The lock is keyed by the interface and searched for in
    `/run/oscmix-desk` first, `$XDG_RUNTIME_DIR/oscmix-desk` second
    (ADR 0023). Both are real on a developer machine with the service
    running: a test taking that lock would block the real desk, and a
    test that leaves one held would block it for good.

    `OSCMIX_LOCK_DIR` points at a directory that does not exist, so the
    search falls through to the runtime directory below unless a test
    sets it itself.
    """
    from oscmix_desk import profiles

    machine = (os.environ.get("XDG_RUNTIME_DIR"), profiles.SHARED_LOCK_DIR)
    monkeypatch.setenv("XDG_RUNTIME_DIR",
                       str(tmp_path_factory.mktemp("runtime")))
    absent = str(tmp_path_factory.mktemp("nolockdir") / "absent")
    monkeypatch.setenv("OSCMIX_LOCK_DIR", absent)
    # The default too, not only the variable that overrides it. A mutant
    # that renames the variable falls back to /run/oscmix-desk, and the
    # 0.6.9 mutation run left lock files there -- tests that could have
    # held the running desk's lock while they ran.
    monkeypatch.setattr(profiles, "SHARED_LOCK_DIR", absent)
    yield
    # Checked before monkeypatch restores anything, since this fixture
    # asked for it. A test may point either location somewhere of its
    # own; it may not end with the machine's back in effect, which is
    # what `monkeypatch.undo()` in a test body does to every autouse
    # fixture at once. A 0.6.10 test did, and took a real lock file in
    # /run/oscmix-desk for the rest of its run.
    undone = ("a test put the machine's lock directories back in effect "
              "(monkeypatch.undo()?)")
    runtime, shared = machine
    assert shared != os.environ.get("OSCMIX_LOCK_DIR",
                                    profiles.SHARED_LOCK_DIR), undone
    assert runtime is None or runtime != os.environ.get("XDG_RUNTIME_DIR"), \
        undone


@pytest.fixture(autouse=True)
def _device_is_plugged_in(tmp_path_factory, monkeypatch):
    """A sysfs where the interface is present, unless a test says otherwise.

    Since 0.6.8 a switch refuses when the interface is not connected
    (ADR 0023), and the check reads `OSCMIX_SYSFS_USB`. On a machine
    with no Fireface -- CI, a laptop -- every switch test would refuse
    for the wrong reason. A test that wants the device gone points the
    variable at an empty directory itself.
    """
    sysfs = tmp_path_factory.mktemp("sysfs")
    device = sysfs / "5-2"
    device.mkdir()
    (device / "idVendor").write_text("2a39\n")
    (device / "idProduct").write_text("3fd9\n")
    monkeypatch.setenv("OSCMIX_SYSFS_USB", str(sysfs))


@pytest.fixture(autouse=True)
def _no_stray_proc_reads(tmp_path_factory, monkeypatch):
    """An empty /proc, so nothing reads the machine's own.

    Since 0.6.8 a switch that opens its own socket refuses when nothing
    is bound to the OSC port (ADR 0023). Most tests hand in a backend
    and never reach that check; the ones that do not must not decide
    against the ports of the developer's running desk, which a real
    `/proc` would let them do.
    """
    proc = tmp_path_factory.mktemp("proc")
    (proc / "net").mkdir()
    (proc / "net" / "udp").write_text(
        "  sl  local_address rem_address   st tx_queue rx_queue tr"
        " tm->when retrnsmt   uid  timeout inode ref pointer drops\n")
    monkeypatch.setenv("OSCMIX_PROC_ROOT", str(proc))
    # With no sequencer clients file, the device wait opens /dev/snd/seq to
    # make the kernel load snd-seq. Read-only and harmless, and still the
    # machine's: a test that reaches the wait gets a path that is not there.
    monkeypatch.setenv("OSCMIX_SEQ_DEV", str(proc / "no-seq-device"))


@pytest.fixture(autouse=True)
def _no_real_systemctl(monkeypatch):
    """No test reaches the machine's own user manager.

    process._systemctl and process._systemctl_output are the two places
    the package runs systemctl outside the launcher (whose tests stub
    their own). Every call answers "not active" or "no output" here; the
    tests of the real functions (`real_systemctl`,
    `real_systemctl_output`) patch subprocess.
    The integration suite once started the developer's oscmix.service
    for real (0.6.1); an in-process test must not be able to either.
    """
    from oscmix_desk import process

    _REAL.setdefault("_systemctl", process._systemctl)
    _REAL.setdefault("_systemctl_output", process._systemctl_output)
    monkeypatch.setattr(process, "_systemctl", lambda *verb: 1)
    monkeypatch.setattr(process, "_systemctl_output", lambda *verb: None)


#: The functions the autouse stubs replace, as imported before any stub.
_REAL = {}


@pytest.fixture
def real_systemctl():
    """The unstubbed function, for the one test that checks it."""
    return _REAL["_systemctl"]


@pytest.fixture
def real_systemctl_output():
    """The unstubbed function, for the one test that checks it."""
    return _REAL["_systemctl_output"]


@pytest.fixture(autouse=True)
def _no_real_config(tmp_path_factory, monkeypatch):
    """No test reads the developer's routing.conf, profiles or marker.

    `cli.main` without `--config` resolves the desk from OSCMIX_CONFIG,
    XDG_CONFIG_HOME, HOME and /etc; the action-pair tests reached the
    real ~/.config/oscmix/routing.conf that way (0.6.10). An empty,
    absolute XDG_CONFIG_HOME ends the search before HOME, and the system
    location points nowhere -- through the variable, which a session or
    launcher started as a subprocess inherits, and through the module
    defaults, which a mutant renaming the variable falls back to.
    A test that wants a desk sets its own.
    """
    from oscmix_desk import config, launcher

    empty = tmp_path_factory.mktemp("xdg-config")
    monkeypatch.delenv("OSCMIX_CONFIG", raising=False)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(empty))
    nowhere = empty / "etc-oscmix-routing.conf"
    monkeypatch.setenv("OSCMIX_SYSTEM_CONFIG", str(nowhere))
    monkeypatch.setattr(config, "SYSTEM_CONFIG", nowhere)
    monkeypatch.setattr(launcher, "SYSTEM_CONFIG", nowhere)


@pytest.fixture(scope="session")
def session_mod():
    """The runtime package: its public surface, as ``__all__`` defines it."""
    import oscmix_desk

    return oscmix_desk


@pytest.fixture(scope="session")
def routing_mod():
    """Reach into routing for its own knobs.

    Constants are imported by value, so patching them has to target the
    module that reads them -- patching the package re-export would set an
    attribute nobody consults.
    """
    from oscmix_desk import routing

    return routing


@pytest.fixture(scope="session")
def verify_mod():
    from oscmix_desk import verify

    return verify


@pytest.fixture(scope="session")
def pipewire_mod():
    from oscmix_desk import pipewire

    return pipewire


@pytest.fixture(scope="session")
def launch_mod():
    """The launcher, now a package module rather than a standalone script."""
    from oscmix_desk import launcher

    return launcher


@pytest.fixture
def fake_sysfs(tmp_path):
    """A sysfs USB tree containing one Fireface UCX II."""
    root = tmp_path / "sysfs-usb"
    dev = root / "5-2"
    dev.mkdir(parents=True)
    (dev / "idVendor").write_text("2a39\n")
    (dev / "idProduct").write_text("3fd9\n")
    (dev / "bcdDevice").write_text("0301\n")
    # An interface directory without id files, as in real sysfs.
    (root / "5-2:1.0").mkdir()
    return root


@pytest.fixture
def empty_sysfs(tmp_path):
    root = tmp_path / "sysfs-usb-empty"
    root.mkdir()
    hub = root / "usb1"
    hub.mkdir()
    (hub / "idVendor").write_text("1d6b\n")
    (hub / "idProduct").write_text("0002\n")
    return root


def _hypothesis_available():
    try:
        import hypothesis  # noqa: F401
    except ImportError:
        return False
    return True


def pytest_report_header(config):
    """Say up front whether the contract tests are going to run at all."""
    if _hypothesis_available():
        return None
    return ("contract tests: DISABLED -- hypothesis is not installed "
            "(pip install -r requirements-dev.txt)")


def pytest_terminal_summary(terminalreporter, exitstatus, config):
    """Refuse to let a skipped contract suite look like a green run.

    tests/test_contracts.py skips itself without hypothesis. With `-q`
    that is one digit in a summary line, so a checkout missing the dev
    requirements reports "all passed" having checked no contract at all
    -- including the two that exist because of shipped defects. Set
    OSCMIX_REQUIRE_CONTRACTS=1 (CI does) to make it an error instead.
    """
    if _hypothesis_available():
        return
    terminalreporter.write_sep("=", "CONTRACT TESTS DID NOT RUN", red=True,
                               bold=True)
    terminalreporter.write_line(
        "hypothesis is not installed, so tests/test_contracts.py was "
        "skipped in full.")
    terminalreporter.write_line(
        "Nothing checked the OSC codec against hostile input, that a route "
        "writes only")
    terminalreporter.write_line(
        "what it declares, or that config parsing is total. This run proves "
        "less than it says.")
    terminalreporter.write_line("")
    terminalreporter.write_line("    pip install -r requirements-dev.txt")


def write_config(path, text):
    """Write a config, creating the directory. Returns the path."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


class _RecordingBackend:
    """A backend that records the wire and never confirms anything.

    A double rather than a real socket because the property under test is
    *what was sent*, and specifically that a refused config sends
    nothing. A real socket can only show absence by waiting, which is a
    slow test that passes when the code is merely slow.
    """

    traits = None  # filled in below from the real table

    def __init__(self, reports=None):
        self.sent = []
        self.dumps = 0
        self._reports = reports

    def send(self, messages):
        self.sent.extend((p, t, tuple(a)) for p, t, a in messages)

    def request_dump(self):
        self.dumps += 1

    def listen(self):
        if self._reports is None:
            return None          # port taken: the mixer GUI case
        return _ReplayListener(self, self._reports)


class _ReplayListener:
    def __init__(self, backend, reports):
        self._backend = backend
        self._reports = reports
        self._done = False

    def messages(self, _timeout):
        if self._done:
            return
        self._done = True
        yield from self._reports(self._backend.sent)

    def close(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        self.close()


def _echo_link_flags_only(sent):
    """What a device does promptly: report the stereo flags, nothing else.

    Enough to release the link barrier, which is what the real device
    does and what makes these tests take milliseconds instead of the
    1.5 s the barrier waits when nothing answers. Deliberately *not* an
    echo of everything -- confirming the rest is what
    `confirming_backend` is for, and a double that confirms by accident
    is how a test starts asserting a device nobody has.
    """
    return [(path, tags, args) for path, tags, args in sent
            if path.endswith("/stereo")]


@pytest.fixture
def recording_backend():
    """Records the wire, and answers the link barrier like a device does."""
    from oscmix_desk import backend as backend_mod
    _RecordingBackend.traits = backend_mod.OSCMIX
    return _RecordingBackend(reports=_echo_link_flags_only)


@pytest.fixture
def silent_backend():
    """The desktop case: the receive port is held, so nothing can be read."""
    from oscmix_desk import backend as backend_mod
    _RecordingBackend.traits = backend_mod.OSCMIX
    return _RecordingBackend(reports=None)


class _UnbindableBackend(_RecordingBackend):
    """The receive port cannot be bound, and nobody holds it."""

    def listen(self):
        from oscmix_desk.errors import ReceivePortError
        raise ReceivePortError(
            13, "cannot bind the receive port UDP 80: Permission denied")


@pytest.fixture
def unbindable_backend():
    """`[osc] recv-port = 80` for an ordinary user: EACCES, not the GUI."""
    from oscmix_desk import backend as backend_mod
    _RecordingBackend.traits = backend_mod.OSCMIX
    return _UnbindableBackend()


def _echo_within_traits(sent):
    """Echo back what a backend with OSCMIX's traits would report.

    Not everything it was told. The first version of this double echoed
    the lot, including `/mix/<out>/playback/<pb>` -- which upstream never
    reports, measured, and declared as
    `backend.Traits.dumps_playback_matrix = False`. A double more
    capable than the thing it stands in for turns every test that uses
    it into a test of a device nobody has, and hides exactly the
    outcomes that only exist because of the limitation.
    """
    return [(path, tags, args) for path, tags, args in sent
            if not (path.startswith("/mix/") and "/playback/" in path)
            and path != "/refresh"]


@pytest.fixture
def confirming_backend():
    """A device that echoes back what its traits say it can report."""
    from oscmix_desk import backend as backend_mod
    _RecordingBackend.traits = backend_mod.OSCMIX
    return _RecordingBackend(reports=_echo_within_traits)
