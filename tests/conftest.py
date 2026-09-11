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
def _no_real_systemctl(monkeypatch):
    """No test reaches the machine's own user manager.

    process._systemctl is the one place the package runs systemctl
    outside the launcher (whose tests stub their own). Every call answers
    "not active" here; a test of the real function patches subprocess.
    The integration suite once started the developer's oscmix.service
    for real (0.6.1); an in-process test must not be able to either.
    """
    from oscmix_desk import process

    global _REAL_SYSTEMCTL, _REAL_SYSTEMCTL_OUTPUT
    if _REAL_SYSTEMCTL is None:
        _REAL_SYSTEMCTL = process._systemctl
        _REAL_SYSTEMCTL_OUTPUT = process._systemctl_output
    monkeypatch.setattr(process, "_systemctl", lambda *verb: 1)
    monkeypatch.setattr(process, "_systemctl_output", lambda *verb: "")


_REAL_SYSTEMCTL = None
_REAL_SYSTEMCTL_OUTPUT = None


@pytest.fixture
def real_systemctl():
    """The unstubbed function, for the one test that checks it."""
    return _REAL_SYSTEMCTL


@pytest.fixture
def real_systemctl_output():
    return _REAL_SYSTEMCTL_OUTPUT


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
