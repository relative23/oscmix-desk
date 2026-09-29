"""The seam, and the backend traits it declares.

A trait is only worth declaring if it can be checked. Each one below is
held against a recording or against a measurement recorded elsewhere in
this repository, because a table of beliefs about upstream is exactly
the kind of thing that decays without anything failing -- which is how
the "15-20 s dump" and `LINK_SYNC_BLIND_DELAY = 20` survived two
releases.
"""

import json
import os

import pytest
from control_peer import HELLO, REFRESH, ScriptedControl
from support import repo_file

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
    barrier goes -- in that order, and this is the flag that
    says so rather than a search through the control flow.
    """
    from oscmix_desk import constants

    assert backend.OSCMIX.reports_link_state_on_write is False
    # The constants that exist only because of it. If the trait ever
    # flips while these remain, the workaround outlived its reason.
    assert constants.LINK_ECHO_TIMEOUT > 0


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

def test_a_burst_arrives_in_the_order_it_was_given(tmp_path):
    peer = ScriptedControl(tmp_path / "c.sock")
    device = backend.Control(peer.path, os.getpid())
    try:
        device.begin()
        messages = [("/output/%d/stereo" % n, "i", (1,)) for n in range(1, 8)]
        device.send(messages)
        assert [osc.decode_osc(packet) for packet in peer.writes] == messages
        device.finish()
    finally:
        device.close()
        peer.close()


def test_a_timeout_yields_nothing_rather_than_raising(tmp_path):
    peer = ScriptedControl(tmp_path / "c.sock")
    device = backend.Control(peer.path, os.getpid(), reader=True)
    try:
        assert list(device.messages(0.05)) == []
    finally:
        device.close()
        peer.close()


def test_a_malformed_osc_delivery_invalidates_the_connection(tmp_path):
    valid = osc.encode_osc("/output/5/stereo", "i", 1)
    peer = ScriptedControl(tmp_path / "c.sock", reports=[b"bad!", valid])
    device = backend.Control(peer.path, os.getpid(), reader=True)
    try:
        device.request_dump()
        with pytest.raises(OSError, match="invalid OSC delivery"):
            list(device.messages(0.5))
        with pytest.raises(OSError, match="no longer valid"):
            list(device.messages(0.5))
    finally:
        device.close()
        peer.close()


def test_subscription_precedes_the_control_refresh_request(tmp_path):
    peer = ScriptedControl(tmp_path / "c.sock")
    device = backend.Control(peer.path, os.getpid(), reader=True)
    try:
        device.request_dump()
        assert peer.requests == [HELLO, REFRESH]
        assert peer.writes == []
    finally:
        device.close()
        peer.close()


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
