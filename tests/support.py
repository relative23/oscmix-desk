"""Plain helpers for the tests: where the repository is, a free port, a
fake /proc, a config written to disk.

Unit tests import the ``oscmix_desk`` package directly; the thin
executables in bin/ are covered end to end by the integration tests,
which run them as real subprocesses. ``load_executable`` remains for the
bin/ shims themselves: they have no .py extension, so they need
SourceFileLoader, and their ``if __name__ == "__main__"`` guard keeps the
import side-effect free.
"""

import collections
import importlib.machinery
import importlib.util
import socket
import struct
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent


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

def write_config(path, text):
    """Write a config, creating the directory. Returns the path."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path

def device_key(path):
    """The device key the code under test derives for this config.

    From the same resolution the code uses, against the /proc the suite
    points it at (ADR 0024) -- not against the machine's own card list.
    """
    import os
    from pathlib import Path

    from oscmix_desk.discovery import resolve_device
    from oscmix_desk.profiles import load_config

    config = load_config(path)
    return resolve_device(config.usb_id, config.device_name, config.serial,
                          Path(os.environ["OSCMIX_PROC_ROOT"])).key
