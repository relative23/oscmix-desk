"""`--dump-config` as the command, not just the inference.

The round trip is covered in tests/test_dump_config.py against pure
functions. This is the part that opens a socket: what it does when the
port is held, when nobody answers, and what it prints when the device
does.

It exists because the coverage ratchet caught the gap the moment the
feature landed -- 70% to 53% on cli.py -- which is the ratchet doing
exactly its job. The fix is covering the path, not lowering the gate.
"""

import socket
import threading
import time

from support import free_udp_port, osc_bundle

from oscmix_desk import cli
from oscmix_desk import reads as reads_mod


class FakeBackend(threading.Thread):
    """Answers /refresh with a canned register state, like oscmix does."""

    def __init__(self, session_mod, send_port, recv_port, dump):
        super().__init__(daemon=True)
        self.session_mod = session_mod
        self.recv_port = recv_port
        self.dump = dump
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind(("127.0.0.1", send_port))
        self.sock.settimeout(0.2)
        self.stopping = threading.Event()

    def stop(self):
        self.stopping.set()

    def run(self):
        while not self.stopping.is_set():
            try:
                data, _ = self.sock.recvfrom(65536)
            except (socket.timeout, OSError):
                if self.stopping.is_set():
                    return
                continue
            for message in self.session_mod.iter_osc_messages(data):
                try:
                    path, _t, _a = self.session_mod.decode_osc(message)
                except ValueError:
                    continue
                if path == "/refresh":
                    self.sock.sendto(osc_bundle(self.dump),
                                     ("127.0.0.1", self.recv_port))


def dump_of(session_mod, registers):
    return [session_mod.encode_osc(p, t, *a) for p, t, a in registers]


def run_dump(session_mod, capsys, registers, *, hold_port=False):
    send_port, recv_port = free_udp_port(), free_udp_port()
    config = session_mod.Config(device_name="Fireface UCX II",
                                osc_port=send_port, osc_recv_port=recv_port)
    blocker = None
    if hold_port:
        blocker = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        blocker.bind(("127.0.0.1", recv_port))
    backend = FakeBackend(session_mod, send_port, recv_port,
                          dump_of(session_mod, registers))
    backend.start()
    try:
        code = cli._dump_config(config)
    finally:
        backend.stop()
        backend.join(timeout=3)
        backend.sock.close()
        if blocker is not None:
            blocker.close()
    return code, capsys.readouterr().out


MONITOR = [
    ("/input/1/stereo", "i", (1,)),
    ("/output/5/stereo", "i", (1,)),
    ("/mix/5/input/1", "fi", (-6.0, 0)),
]


def test_it_prints_a_config_the_parser_accepts(session_mod, capsys, tmp_path):
    code, out = run_dump(session_mod, capsys, MONITOR)
    assert code == session_mod.EXIT_OK
    assert "[route:in1-2-out5-6]" in out
    assert "input = 1/2" in out
    assert "level = -6.0" in out

    # The output is a config, not a report about one.
    path = tmp_path / "routing.conf"
    path.write_text(out)
    route, = session_mod.load_config(path).routes
    assert route.source == ("input", (1, 2))
    assert route.output == (5, 6)


def test_it_says_what_it_could_not_read(session_mod, capsys):
    _code, out = run_dump(session_mod, capsys, MONITOR)
    assert "does not report" in out
    assert "/mix/{out}/playback/{pb}" in out
    assert "Merge, do not replace" in out


def test_a_device_with_no_monitoring_is_not_an_error(session_mod, capsys):
    code, out = run_dump(session_mod, capsys, [
        ("/output/5/stereo", "i", (1,)),
        ("/mix/5/input/1", "fi", (float("-inf"), 0)),
    ])
    assert code == session_mod.EXIT_OK
    assert "No input routing was reported" in out
    assert "[route:" not in out


def test_a_held_receive_port_is_refused_rather_than_half_read(session_mod,
                                                              capsys, caplog):
    # Two readers split the device's replies. Half an answer rendered as
    # a config looks authoritative, which is worse than an error.
    with caplog.at_level("ERROR"):
        code, out = run_dump(session_mod, capsys, MONITOR, hold_port=True)
    assert code == session_mod.EXIT_FAILURE
    assert out == ""
    assert "in use" in caplog.text
    assert "split" in caplog.text


def test_silence_is_an_error_not_an_empty_config(session_mod, capsys, caplog,
                                                 monkeypatch):
    """"Nobody answered" and "you have no routing" call for opposite
    responses, and both would render as a file with no routes."""
    monkeypatch.setattr(reads_mod, "DUMP_READ_SECONDS", 0.6)
    send_port, recv_port = free_udp_port(), free_udp_port()
    config = session_mod.Config(device_name="Fireface UCX II",
                                osc_port=send_port, osc_recv_port=recv_port)
    with caplog.at_level("ERROR"):
        code = cli._dump_config(config)
    assert code == session_mod.EXIT_FAILURE
    assert capsys.readouterr().out == ""
    assert "no reply" in caplog.text


def test_it_stops_when_the_dump_goes_quiet(session_mod, capsys):
    # Waiting out the whole window regardless would make the command
    # take DUMP_READ_SECONDS every time; the dump is over in ~2 s on a
    # UCX II. Measured here rather than assumed.
    started = time.monotonic()
    code, _out = run_dump(session_mod, capsys, MONITOR)
    elapsed = time.monotonic() - started
    assert code == session_mod.EXIT_OK
    assert elapsed < reads_mod.DUMP_READ_SECONDS, (
        "the read waited out the full window (%.1fs) instead of stopping "
        "when the dump went quiet" % elapsed)


def test_the_read_settles_before_it_asks_for_the_dump(session_mod, tmp_path):
    """The wait `DUMP_LISTEN_SETTLE` documents, actually taken here.

    Its own docstring in tests/test_pin_remember.py says the cost is
    paid by "every verification, every profile switch and every
    --dump-config". The verifier took it; this path never did, for as
    long as `--dump-config` has existed.

    The casualty is the bundle `setrefresh()` flushes first -- all
    twenty `/playback/<n>/stereo`, sent from oscmix's own memory before
    the device's dump reaches the wire. Measured against the real
    device: without the wait, 4 of 8 reads came back with 1982 registers
    and no playback stereo at all; with it, 11 of 11 read 2002.

    Asserted on when the request reaches the backend rather than on a
    patched `sleep`, so it stays true if the wait is implemented some
    other way.
    """
    send_port, recv_port = free_udp_port(), free_udp_port()
    config = session_mod.Config(device_name="Fireface UCX II",
                                osc_port=send_port, osc_recv_port=recv_port)
    backend = TimingBackend(session_mod, send_port, recv_port,
                            dump_of(session_mod, MONITOR))
    backend.start()
    started = time.monotonic()
    try:
        reads_mod._read_device(config)
    finally:
        backend.stop()
        backend.join(timeout=3)
        backend.sock.close()

    assert backend.asked_at is not None, "the backend never saw /refresh"
    waited = backend.asked_at - started
    assert waited >= reads_mod.DUMP_LISTEN_SETTLE, (
        "asked for the dump %.3f s after opening the socket, before the "
        "%.3f s settle -- the playback stereo burst is what gets lost"
        % (waited, reads_mod.DUMP_LISTEN_SETTLE))


class TimingBackend(FakeBackend):
    """FakeBackend that records when `/refresh` arrived."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.asked_at = None

    def run(self):
        while not self.stopping.is_set():
            try:
                data, _ = self.sock.recvfrom(65536)
            except (socket.timeout, OSError):
                if self.stopping.is_set():
                    return
                continue
            for message in self.session_mod.iter_osc_messages(data):
                try:
                    path, _t, _a = self.session_mod.decode_osc(message)
                except ValueError:
                    continue
                if path == "/refresh":
                    if self.asked_at is None:
                        self.asked_at = time.monotonic()
                    for register in self.dump:
                        self.sock.sendto(register, ("127.0.0.1", self.recv_port))
