"""What every test runs under: the isolation from the machine, and the
fixtures.

No test may touch the machine's lock directories, its user manager, its
backend, its sequencer or the developer's own desk; the autouse fixtures
below are what holds that, and the patch guard is what keeps them
hooked when code moves. Plain helpers live in ``support``, the stand-ins
for a backend in ``backend_doubles``.
"""

import ast
import os
import socket
import sys
import types
from pathlib import Path

import pytest
from _pytest.monkeypatch import MonkeyPatch
from backend_doubles import (
    RecordingBackend,
    UnbindableBackend,
    echo_link_flags_only,
    echo_within_traits,
)
from support import PROJECT_ROOT

sys.path.insert(0, str(PROJECT_ROOT / "src"))


# --------------------------------------------------------------------------
# A patch nothing reads is a test that tests nothing.
# --------------------------------------------------------------------------

_NAMES_READ = {}


def _names_read(module):
    """Every name the module's own code loads, as far as its source says."""
    path = getattr(module, "__file__", None)
    if path not in _NAMES_READ:
        tree = ast.parse(Path(path).read_text(encoding="utf-8"))
        _NAMES_READ[path] = {
            node.id for node in ast.walk(tree)
            if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load)}
    return _NAMES_READ[path]


def _is_ours(module):
    path = getattr(module, "__file__", None) or ""
    return path.endswith(".py") and any(
        path.startswith(str(PROJECT_ROOT / part) + os.sep)
        for part in ("src", "scripts"))


_setattr = MonkeyPatch.setattr


def _setattr_that_something_reads(self, target, name=None, *args, **kwargs):
    """``monkeypatch.setattr``, refusing a patch on one of this project's
    modules that the module itself never reads.

    The suite isolates itself from the machine, and steers the code under
    test, by replacing module attributes. A function that moves to another
    module takes its reads with it: the patch on the old module still
    succeeds -- the name is still imported there -- and changes nothing.
    That is how tests reached the machine's lock directory and its user
    manager before (0.6.1, 0.6.10), and it is what splitting the large
    modules would have done a hundred times over in silence (0.7.0).
    """
    if isinstance(target, types.ModuleType) and isinstance(name, str) \
            and _is_ours(target) and name not in _names_read(target):
        pytest.fail("patching %s.%s changes nothing: no code in %s reads "
                    "that name -- patch the module that does"
                    % (target.__name__, name, target.__name__),
                    pytrace=False)
    return _setattr(self, target, name, *args, **kwargs)


MonkeyPatch.setattr = _setattr_that_something_reads


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
    from oscmix_desk import locking

    machine = (os.environ.get("XDG_RUNTIME_DIR"), locking.SHARED_LOCK_DIR)
    monkeypatch.setenv("XDG_RUNTIME_DIR",
                       str(tmp_path_factory.mktemp("runtime")))
    absent = str(tmp_path_factory.mktemp("nolockdir") / "absent")
    monkeypatch.setenv("OSCMIX_LOCK_DIR", absent)
    # The default too, not only the variable that overrides it. A mutant
    # that renames the variable falls back to /run/oscmix-desk, and the
    # 0.6.9 mutation run left lock files there -- tests that could have
    # held the running desk's lock while they ran.
    monkeypatch.setattr(locking, "SHARED_LOCK_DIR", absent)
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
                                    locking.SHARED_LOCK_DIR), undone
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
def _no_real_backend(monkeypatch):
    """No test talks to the machine's own backend.

    A desk with no `[osc]` section resolves to UDP 7222 and 8222, where
    the developer's oscmix listens: a test that ran `--dump-config`
    unstubbed read the UCX II through it on every run, and on a machine
    without one waited out the read instead (0.6.11, found by review).
    In-process only: the guard cannot see a subprocess, and those tests
    stay offline by what they run -- a dry run, a desk that declares
    nothing, a stub for a backend. The refusal is raised *and* held
    against the test at teardown, since the code under test is entitled
    to catch what it could not send.
    """
    from oscmix_desk.constants import DEFAULT_OSC_PORT, DEFAULT_OSC_RECV_PORT

    reached = []

    def guarded(name):
        real = getattr(socket.socket, name)

        def call(self, *args):
            address = args[-1] if name == "sendto" else args[0]
            if isinstance(address, tuple) and address[1] in (
                    DEFAULT_OSC_PORT, DEFAULT_OSC_RECV_PORT):
                reached.append("%s %r" % (name, address))
                raise AssertionError("a test reached for the machine's "
                                     "backend: %s" % reached[-1])
            return real(self, *args)

        return call

    for name in ("bind", "connect", "sendto"):
        monkeypatch.setattr(socket.socket, name, guarded(name))
    yield
    assert reached == [], \
        "a test reached for the machine's backend: %s" % reached


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
    from oscmix_desk import launcher
    from oscmix_desk import paths as paths_mod

    empty = tmp_path_factory.mktemp("xdg-config")
    monkeypatch.delenv("OSCMIX_CONFIG", raising=False)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(empty))
    nowhere = empty / "etc-oscmix-routing.conf"
    monkeypatch.setenv("OSCMIX_SYSTEM_CONFIG", str(nowhere))
    monkeypatch.setattr(paths_mod, "SYSTEM_CONFIG", nowhere)
    monkeypatch.setattr(launcher, "SYSTEM_CONFIG", nowhere)


@pytest.fixture(scope="session")
def session_mod():
    """The runtime package: its public surface, as ``__all__`` defines it."""
    import oscmix_desk

    return oscmix_desk


@pytest.fixture(autouse=True)
def _no_real_diagnostic_queries(monkeypatch):
    """Status/GTK tests cannot inspect the actual user's service or GSettings."""
    from oscmix_desk import desktop, diagnostics

    def unavailable(_command):
        raise OSError("host query isolated by the test suite")

    _REAL.setdefault("diagnostic_query", diagnostics.query)
    monkeypatch.setattr(diagnostics, "query", unavailable)
    monkeypatch.setattr(desktop, "query", unavailable)


@pytest.fixture
def diagnostic_query():
    return _REAL["diagnostic_query"]


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


@pytest.fixture
def recording_backend():
    """Records the wire, and answers the link barrier like a device does."""
    from oscmix_desk import backend as backend_mod
    RecordingBackend.traits = backend_mod.OSCMIX
    return RecordingBackend(reports=echo_link_flags_only)


@pytest.fixture
def silent_backend():
    """The desktop case: the receive port is held, so nothing can be read."""
    from oscmix_desk import backend as backend_mod
    RecordingBackend.traits = backend_mod.OSCMIX
    return RecordingBackend(reports=None)


@pytest.fixture
def unbindable_backend():
    """`[osc] recv-port = 80` for an ordinary user: EACCES, not the GUI."""
    from oscmix_desk import backend as backend_mod
    RecordingBackend.traits = backend_mod.OSCMIX
    return UnbindableBackend()


@pytest.fixture
def confirming_backend():
    """A device that echoes back what its traits say it can report."""
    from oscmix_desk import backend as backend_mod
    RecordingBackend.traits = backend_mod.OSCMIX
    return RecordingBackend(reports=echo_within_traits)


@pytest.fixture
def session_module():
    from oscmix_desk import session

    return session


@pytest.fixture
def lifecycle(session_module, monkeypatch):
    """run_session with every outside interaction replaced.

    Returns a helper that records the readiness notifications sent, so a
    test can assert not only *that* READY was sent but how often.
    """
    from session_doubles import (
        FakeChild,
        RunningChild,
        TimeoutExpired,
        make_args,
    )

    notifications = []
    children = []

    def run(*, seq_client=42, usb_present=True, binaries=True,
            returncode=0, stop_requested=False, routes=(), port_ready=True,
            alive=False, config_fields=None, lock_unavailable=False,
            ambiguous=False, session_running=False, backend_unreachable=False,
            **args):
        config_fields = config_fields or {}
        monkeypatch.setattr(session_module, "sd_notify", notifications.append)
        def wait(usb_id, device_name, serial, timeout, proc_root):
            from oscmix_desk.discovery import Device, resolve_device
            from oscmix_desk.errors import DeviceAmbiguous

            if ambiguous:
                raise DeviceAmbiguous("2 interfaces match 'Fireface UCX II'")
            if seq_client is None:
                return None
            # The client is the fixture's; the serial is what the /proc
            # the test points at shows for it, through the real resolution.
            found = resolve_device(usb_id, device_name, serial, proc_root)
            return Device(usb_id=usb_id, serial=found.serial, client=seq_client)

        monkeypatch.setattr(session_module, "wait_for_device", wait)
        monkeypatch.setattr(session_module, "usb_device_present",
                            lambda *a, **k: usb_present)
        monkeypatch.setattr(session_module, "resolve_binary",
                            lambda *a, **k: "/bin/true" if binaries else None)
        monkeypatch.setattr(session_module, "_cleanup_stale_backend",
                            lambda *a, **k: 39000 if session_running else None)
        monkeypatch.setattr(session_module, "_install_stop_handlers",
                            lambda *a, **k: None)
        # True: the port came up. False is the backend that lives but
        # never binds, which since 0.6.6 fails the start (ADR 0021).
        monkeypatch.setattr(session_module, "_await_backend_port",
                            lambda *a, **k: port_ready)
        def apply(*a, **k):
            if lock_unavailable:
                from oscmix_desk.errors import DeviceLockUnavailable
                raise DeviceLockUnavailable("2a39:3fd9")
            if backend_unreachable:
                raise OSError(101, "Network is unreachable")

        monkeypatch.setattr(session_module, "_apply_and_verify", apply)
        def spawn(*a, **k):
            child = RunningChild() if alive else FakeChild(returncode)
            children.append(child)
            return child

        monkeypatch.setattr(session_module, "subprocess",
                            type("S", (), {"Popen": staticmethod(spawn),
                                           "TimeoutExpired": TimeoutExpired})())

        def fake_supervise(child, stop, on_reload=None,
                           reload_requested=None):
            # Signature mirrors the real one, keywords included: a double
            # that accepts **kwargs would have swallowed the reconcile
            # trigger silently instead of failing here.
            stop["stop"] = stop_requested
            return returncode

        monkeypatch.setattr(session_module, "supervise", fake_supervise)

        # A Config is frozen, so the serial a start pins is in the config
        # the session goes on with, not in the one it was given: that is
        # what `run.config` is from the moment there is a backend to start.
        start_backend = session_module._start_backend

        def start(client, running):
            run.config = running
            return start_backend(client, running)

        monkeypatch.setattr(session_module, "_start_backend", start)

        from oscmix_desk import Config
        config = Config(routes=tuple(routes), **config_fields)
        run.config = config
        return session_module.run_session(make_args(**args), config)

    run.notifications = notifications
    run.children = children
    run.config = None
    return run
