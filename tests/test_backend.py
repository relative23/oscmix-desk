"""The seam, and the backend traits it declares.

A trait is only worth declaring if it can be checked. Each one below is
held against a recording or against a measurement recorded elsewhere in
this repository, because a table of beliefs about upstream is exactly
the kind of thing that decays without anything failing -- which is how
the "15-20 s dump" and `LINK_SYNC_BLIND_DELAY = 20` survived two
releases.
"""

import json
import socket

import pytest
from support import free_udp_port, repo_file

from oscmix_desk import backend, osc


@pytest.fixture(scope="module")
def warm():
    return json.loads(repo_file("tests", "data", "refresh-dump.json").read_text())


# --------------------------------------------------------------------------
# The traits, against evidence.
# --------------------------------------------------------------------------

def test_the_playback_matrix_trait_matches_the_recorded_dump(warm):
    # False, and this is what forces the whole re-establish path: a /mix
    # write draws no reply and the dump omits the family entirely.
    reported = [p for p in warm["registers"]
                if p.startswith("/mix/") and "/playback/" in p]
    assert backend.OSCMIX.dumps_playback_matrix is (reported != [])
    assert backend.OSCMIX.dumps_playback_matrix is False


def test_the_link_state_trait_is_why_the_barrier_exists():
    """False, measured by instrumenting upstream rather than by reading it.

    `patches/README.md` records it: logging `out->stereo` inside
    `setlevel()` and racing `/output/5/stereo=1` against
    `/mix/5/playback/1` on a UCX II gives `stereo=0` unpatched and
    `stereo=1` with the patch offered as michaelforney/oscmix#31.

    When that lands and the pin moves, this flips to True and the
    barrier goes -- in that order (ADR 0008), and this is the flag that
    says so rather than a search through the control flow.
    """
    from oscmix_desk import constants

    assert backend.OSCMIX.reports_link_state_on_write is False
    # The constants that exist only because of it. If the trait ever
    # flips while these remain, the workaround outlived its reason.
    assert constants.LINK_ECHO_TIMEOUT > 0
    assert constants.LINK_SETTLE > 0
    assert constants.LINK_SYNC_BLIND_DELAY > 0


def test_the_unchanged_register_trait_is_why_the_echo_cannot_be_the_only_barrier():
    # False: writing a value the device already holds produces no
    # report, so a timeout on the echo is normal rather than an error.
    assert backend.OSCMIX.reports_unchanged_registers is False


def test_a_trait_table_is_a_frozen_value():
    # Traits describe a backend; mutating them at runtime would mean the
    # workarounds could change under the code that reads them.
    import dataclasses

    with pytest.raises(dataclasses.FrozenInstanceError):
        backend.OSCMIX.dumps_playback_matrix = True  # type: ignore[misc]


# --------------------------------------------------------------------------
# The seam itself.
# --------------------------------------------------------------------------

def test_a_burst_arrives_in_the_order_it_was_given(session_mod):
    # The order is the caller's. The whole two-phase design is an
    # ordering, so a backend that reordered or coalesced would be
    # silently undoing it.
    port = free_udp_port()
    rx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    rx.bind(("127.0.0.1", port))
    rx.settimeout(2.0)
    try:
        messages = [("/output/%d/stereo" % n, "i", (1,)) for n in range(1, 8)]
        backend.loopback(port, free_udp_port()).send(messages)
        got = []
        for _ in messages:
            datagram, _addr = rx.recvfrom(65536)
            got.append(osc.decode_osc(datagram)[0])
    finally:
        rx.close()
    assert got == [m[0] for m in messages]


def test_a_taken_receive_port_is_none_rather_than_an_error():
    # The normal desktop case: the mixer GUI holds it whenever its
    # window is open. An exception here would turn "the user has the
    # mixer open" into a failed start.
    port = free_udp_port()
    holder = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    holder.bind(("127.0.0.1", port))
    try:
        assert backend.loopback(free_udp_port(), port).listen() is None
    finally:
        holder.close()


def test_a_free_receive_port_yields_a_listener():
    device = backend.loopback(free_udp_port(), free_udp_port())
    listener = device.listen()
    assert listener is not None
    listener.close()


def test_the_listener_releases_the_port_it_held():
    # A listener that leaked its socket would make the *next* verify
    # pass see the port as taken and silently go blind.
    port = free_udp_port()
    device = backend.loopback(free_udp_port(), port)
    first = device.listen()
    assert first is not None
    first.close()
    second = device.listen()
    assert second is not None, "the port was still held after close()"
    second.close()


def test_the_listener_works_as_a_context_manager():
    port = free_udp_port()
    device = backend.loopback(free_udp_port(), port)
    with device.listen() as listener:
        assert listener is not None
    assert device.listen() is not None


def test_a_timeout_yields_nothing_rather_than_raising():
    device = backend.loopback(free_udp_port(), free_udp_port())
    with device.listen() as listener:
        assert list(listener.messages(0.05)) == []


def test_a_malformed_datagram_is_skipped_not_raised(session_mod):
    # This reads off a socket. One bad message must not end a dump that
    # is otherwise confirming registers.
    port = free_udp_port()
    device = backend.loopback(free_udp_port(), port)
    with device.listen() as listener:
        tx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        tx.sendto(b"\xff\xfe not osc at all \x00\x01", ("127.0.0.1", port))
        tx.close()
        assert list(listener.messages(0.5)) == []


def test_a_dump_request_is_a_refresh(session_mod):
    port = free_udp_port()
    rx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    rx.bind(("127.0.0.1", port))
    rx.settimeout(2.0)
    try:
        backend.loopback(port, free_udp_port()).request_dump()
        datagram, _addr = rx.recvfrom(65536)
    finally:
        rx.close()
    assert osc.decode_osc(datagram)[0] == "/refresh"


def test_the_seam_is_the_only_place_that_opens_a_device_socket():
    """The property that makes the seam worth having.

    Six places used to open their own socket and know the address. If a
    seventh appears outside this module, the dependency on oscmix stops
    being visible in one place -- and the own-state-path option the
    roadmap wants to keep open gets more expensive with each one.

    notify.py is excluded: it speaks to systemd over a UNIX socket, not
    to the device.
    """
    import ast

    package = repo_file("src", "oscmix_desk")
    offenders = []
    for path in sorted(package.glob("*.py")):
        if path.name in ("backend.py", "notify.py", "discovery.py"):
            continue
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if (isinstance(node, ast.Attribute) and node.attr == "socket"
                    and isinstance(node.value, ast.Name)
                    and node.value.id == "socket"):
                offenders.append(path.name)
    assert offenders == [], (
        "these open a device socket outside the seam: %s" % sorted(set(offenders)))
