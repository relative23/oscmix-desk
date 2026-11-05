"""The connect check at its exact bounds, and every line it writes to the journal in full.

test_connect_check.py pins the tolerances in the middle of their range and parts of the log
lines. These pin the full-scale pan ends, the level tolerance at its exact edge, the window's
deadline and receive slice, and the complete text of each warning and information line.
"""

import logging
from types import SimpleNamespace

import pytest

from oscmix_desk import connectcheck as check_mod
from oscmix_desk.connectcheck import (
    LEVEL_TOLERANCE,
    ROUNDING,
    Cell,
    ConnectCheck,
    agrees,
    check_at_connect,
    log_check,
)
from oscmix_desk.constants import VERIFY_TIMEOUT
from oscmix_desk.model import Config, Route

MIC = Route(name="mic", output=(1, 2), input=(5, 6), level=-6.0)
LOGGER = "oscmix-session"


def lines(caplog):
    return [(record.levelname, record.getMessage()) for record in caplog.records
            if record.name == LOGGER]


@pytest.mark.parametrize("pan", [-100, 100])
def test_a_pan_at_full_scale_is_a_value_to_compare(pan):
    assert agrees(Cell("mic", -6.0, pan), "fi", (-6.0, pan)) is True
    assert agrees(Cell("mic", -6.0, 0), "fi", (-6.0, pan)) is False


def test_a_level_exactly_at_the_tolerance_agrees():
    edge = LEVEL_TOLERANCE + ROUNDING
    assert agrees(Cell("mic", 0.0, 0), "fi", (edge, 0)) is True
    assert agrees(Cell("mic", 0.0, 0), "fi", (edge + 1e-6, 0)) is False


def test_a_difference_is_one_warning_and_one_line_per_cell(caplog):
    caplog.set_level(logging.INFO, logger=LOGGER)
    check = ConnectCheck(0.0, 2, (("/mix/1/input/5", Cell("mic", -6.0, 0), (-12.0, 0)),
                                  ("/mix/2/input/6", Cell("mic", -65.0, 0), (-3.5, 10))), (), ())
    log_check(check)
    assert lines(caplog) == [
        ("WARNING", ("connect check: the device's input mix differs from the desk in 2 of 2 "
                     "route cell(s) (TotalMix tolerances: 0.07 dB, pan 0.007); the desk "
                     "overwrites them now")),
        ("WARNING", ("connect check: route 'mic' /mix/1/input/5: device -12.00 dB, pan 0, "
                     "desk -6.00 dB, pan 0")),
        ("WARNING", ("connect check: route 'mic' /mix/2/input/6: device -3.50 dB, pan 10, "
                     "desk silent")),
    ]


def test_a_match_says_how_many_cells_matched(caplog):
    caplog.set_level(logging.INFO, logger=LOGGER)
    log_check(ConnectCheck(0.0, 3, (), (), ()))
    assert lines(caplog) == [
        ("INFO", "connect check: the device's input mix matches the desk's 3 route cell(s)")]


@pytest.mark.parametrize(("unreported", "invalid"), [(("/mix/1/input/5",), ()),
                                                      ((), ("/mix/1/input/5",))])
def test_a_check_with_cells_not_compared_claims_no_match(caplog, unreported, invalid):
    caplog.set_level(logging.INFO, logger=LOGGER)
    log_check(ConnectCheck(0.0, 2, (), unreported, invalid))
    assert lines(caplog) == [
        ("INFO", "connect check: 1 of 2 route cell(s) not compared (%d not reported, %d "
                 "unreadable)" % (len(unreported), len(invalid)))]


def test_cells_not_compared_are_counted_together(caplog):
    caplog.set_level(logging.INFO, logger=LOGGER)
    log_check(ConnectCheck(0.0, 5, (), ("/mix/1/input/5", "/mix/1/input/6"),
                           ("/mix/2/input/5",)))
    assert lines(caplog) == [
        ("INFO", ("connect check: 3 of 5 route cell(s) not compared (2 not reported, 1 "
                  "unreadable)"))]


class Silent:
    """A device that answers nothing; each receive costs its whole timeout."""

    def __init__(self, clock, fail=None):
        self.clock = clock
        self.fail = fail
        self.timeouts = []

    def request_dump(self):
        if self.fail is not None:
            raise self.fail

    def messages(self, timeout):
        self.timeouts.append(timeout)
        self.clock.now += timeout
        return iter(())


@pytest.fixture
def clock(monkeypatch):
    clock = SimpleNamespace(now=0.0)
    monkeypatch.setattr(check_mod, "time", SimpleNamespace(monotonic=lambda: clock.now,
                                                           time=lambda: 1000.0))
    return clock


def test_a_silent_device_is_given_exactly_the_window_in_quarter_seconds(clock):
    device = Silent(clock)
    check, handed = check_at_connect(Config(routes=(MIC,)), device, lambda: False)
    assert check.unreported == ("/mix/1/input/5",)
    assert handed is None
    assert set(device.timeouts) == {0.25}
    assert clock.now == VERIFY_TIMEOUT, "the window ends at its deadline, not a slice later"


def test_a_check_that_cannot_read_says_why_and_that_the_desk_still_applies(clock, caplog):
    device = Silent(clock, fail=OSError(16, "no fresh backend refresh window available"))
    assert check_at_connect(Config(routes=(MIC,)), device, lambda: False) == (None, None)
    assert lines(caplog) == [
        ("WARNING", ("connect check: the device's input mix could not be read ([Errno 16] no "
                     "fresh backend refresh window available); applying the desk without the "
                     "comparison"))]
