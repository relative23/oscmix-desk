"""What one WRITE does in the actual patched C backend, over simulated MIDI only.

Run beside backend_control.py with OSCMIX_CONTROL_BINARY set:
  OSCMIX_CONTROL_BINARY=/absolute/path/oscmix python -m pytest tests/backend_writes.py
Patch 0016 lets the lease owner's WRITE renew the idle time as a KEEPALIVE does; the upstream
pin 89ed835 sends the output's own index in the loopback word; patch 0015 binds GTK's High
Damp control to the node the backend declares. None of these tests opens a hardware device.
"""

import os
import re
import time
from pathlib import Path

import pytest
from control_peer import BEGIN, BUSY, GUI, KEEPALIVE, OK, WRITE, SimulatedMidi, wait_for

from oscmix_desk.osc import encode_osc
from oscmix_desk.protocol import LEASE_IDLE

#: The RME SysEx header of a sub ID 3 message, the one the loopback setter sends.
LOOPBACK_HEADER = b"\xf0\x00\x20\x0d\x10\x03"


@pytest.fixture(scope="module")
def binary():
    path = Path(os.environ["OSCMIX_CONTROL_BINARY"]).resolve()
    assert path.is_file()
    assert os.access(path, os.X_OK)
    return path


@pytest.fixture
def midi(binary, tmp_path):
    target = SimulatedMidi(binary, tmp_path / "w.sock")
    try:
        wait_for(lambda: target.path.exists())
        yield target
    finally:
        target.close()


@pytest.fixture
def desk(midi):
    peer = midi.connect()
    try:
        yield peer
    finally:
        peer.close()


def loopback_words(midi):
    """The 32-bit words of every sub ID 3 message the backend wrote, in order."""
    words = []
    for packet in list(midi.packets):
        if packet.startswith(LOOPBACK_HEADER):
            body = packet[len(LOOPBACK_HEADER):-1]
            words.extend(sum(value << (7 * shift) for shift, value in enumerate(body[i:i + 5]))
                         for i in range(0, len(body), 5))
    return words


def test_an_owner_write_renews_the_idle_time(midi, desk):
    assert desk.request(BEGIN)[2] == OK
    started = time.monotonic()
    command = encode_osc("/output/5/volume", "f", -6)
    while time.monotonic() - started < LEASE_IDLE + 3:
        time.sleep(1.5)
        assert desk.request(WRITE, payload=command)[2] == OK
    # Eight seconds after BEGIN without one KEEPALIVE, and still the owner.
    assert desk.request(KEEPALIVE)[2] == OK
    assert "operation lease expired" not in midi.log()


def test_a_refused_write_renews_nothing(midi, desk):
    gui = midi.connect(GUI)
    try:
        assert desk.request(BEGIN)[2] == OK
        started = time.monotonic()
        command = encode_osc("/output/5/volume", "f", -6)
        stale = desk.token - 1
        while time.monotonic() - started < LEASE_IDLE - 1:
            time.sleep(1)
            assert gui.request(WRITE, payload=command)[2] == BUSY
            assert desk.request(WRITE, payload=command, token=stale)[2] == BUSY
        wait_for(lambda: "operation lease expired" in midi.log(), timeout=3)
        assert LEASE_IDLE - 0.5 <= time.monotonic() - started < LEASE_IDLE + 3
        assert midi.registers() == []
    finally:
        gui.close()


@pytest.mark.parametrize("output", [1, 3, 20])
def test_the_loopback_word_names_the_output(midi, desk, output):
    assert desk.request(BEGIN)[2] == OK
    for on in (1, 0):
        path = "/output/%d/loopback" % output
        assert desk.request(WRITE, payload=encode_osc(path, "i", on))[2] == OK
    wait_for(lambda: len(loopback_words(midi)) == 2)
    assert loopback_words(midi) == [1 << 7 | output - 1, output - 1]


def test_gtk_binds_only_reverb_nodes_the_backend_declares(binary):
    gtk = (binary.parent / "gtk/main.c").read_text()
    backend = (binary.parent / "oscmix.c").read_text()
    bound = re.findall(r'mixer_bind\(self->osc, "/reverb/(\w+)"', gtk)
    assert "highdamp" in bound
    assert "damp" not in bound
    missing = [name for name in bound if '{"%s",' % name not in backend]
    assert missing == []
