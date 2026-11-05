"""`[desk] peak-safe`: what the journal says when outputs go down, and when they cannot come back.

test_peak_safe.py pins the order of the writes. These pin the two lines a person reads -- which
outputs went down, and which stay at the explicit zero after a failed give-back --, that a plan
the device gave no volume for is sent unchanged, that an unmodelled interface is never quieted,
and that one unusable volume report does not hide the others.
"""

import errno
import logging

import pytest
from backend_doubles import RecordingBackend

from oscmix_desk.config import load_config
from oscmix_desk.errors import WriteFailed
from oscmix_desk.peaksafe import reported_volumes, send_quietly
from oscmix_desk.reconcile import ApplyIntent, application_plan

DESK = """[device]
name = Fireface UCX II

[desk]
peak-safe = true

[route:main]
playback = 1/2
output = 1/2
level = -6.0
volume = -10.0

[route:phones]
playback = 7/8
output = 7/8
"""
SEEN = {"/output/1/volume": ("f", (-20.0,)), "/output/2/volume": ("f", (-20.0,)),
        "/output/7/volume": ("f", (-30.0,)), "/output/8/volume": ("f", (-30.0,))}
LOGGER = "oscmix-session"


@pytest.fixture
def desk(tmp_path):
    path = tmp_path / "routing.conf"
    path.write_text(DESK)
    return load_config(path)


def lines(caplog):
    return [(record.levelname, record.getMessage()) for record in caplog.records
            if record.name == LOGGER]


def test_the_outputs_going_down_are_named(desk, caplog):
    caplog.set_level(logging.INFO, logger=LOGGER)
    backend = RecordingBackend()
    plan = application_plan(desk, ApplyIntent.EXPLICIT)
    send_quietly(desk, plan, backend, ApplyIntent.EXPLICIT, SEEN, lambda _paths: {},
                 lambda rest: backend.send(write.message() for write in rest.writes))
    assert lines(caplog) == [("INFO", "peak-safe: outputs 1, 2, 7, 8 down for the switch")]


class GiveBackFails(RecordingBackend):
    """Takes the quieting writes, then loses the connection before the volumes go back."""

    def send(self, messages):
        if self.sent:
            raise OSError(errno.EIO, "backend gone")
        super().send(messages)


def test_volumes_that_cannot_come_back_are_named_and_the_switch_failure_is_raised(desk, caplog):
    backend = GiveBackFails()
    failure = WriteFailed(OSError(errno.EIO, "lost"), [], [])

    def send(_rest):
        raise failure

    with pytest.raises(WriteFailed) as raised:
        send_quietly(desk, application_plan(desk, ApplyIntent.EXPLICIT), backend,
                     ApplyIntent.EXPLICIT, SEEN, lambda _paths: {}, send)
    assert raised.value is failure, "the switch's own failure, not the give-back's"
    assert lines(caplog) == [
        ("ERROR", ("peak-safe: outputs 1, 2, 7, 8 stay at -65 dB until the next apply "
                   "([Errno 5] backend gone)"))]


def test_a_switch_whose_outputs_report_no_volume_is_sent_as_planned(desk):
    plan = application_plan(desk, ApplyIntent.EXPLICIT)
    sent, reads = [], []

    def read(paths):
        reads.append(tuple(paths))
        return {}

    send_quietly(desk, plan, RecordingBackend(), ApplyIntent.EXPLICIT, {}, read, sent.append)
    assert sent == [plan]
    assert reads == [tuple("/output/%d/volume" % ch for ch in (1, 2, 7, 8))]


def test_an_unmodelled_interface_is_never_quieted(desk):
    plan = application_plan(desk, ApplyIntent.EXPLICIT)
    other = desk.__class__(device_name="Some Other Interface", peak_safe=True)
    backend, sent = RecordingBackend(), []
    send_quietly(other, plan, backend, ApplyIntent.EXPLICIT, SEEN,
                 lambda _paths: pytest.fail("nothing to read"), sent.append)
    assert sent == [plan]
    assert backend.sent == []


def test_one_unusable_volume_report_does_not_hide_the_others():
    observed = {"/output/1/volume": ("f", ()), "/output/3/volume": ("f", (-12.0,)),
                "/output/4/volume": ("f", ("loud",)), "/output/5/volume": ("f", (-3.0,))}
    assert reported_volumes([1, 2, 3, 4, 5], observed) == {3: -12.0, 5: -3.0}
