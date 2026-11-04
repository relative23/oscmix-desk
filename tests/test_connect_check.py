"""The connect check: the device's input mix against the desk, before a start overwrites it.

TotalMix FX compares the input mix at connect, within 0.07 dB and 0.007 of full-scale pan, and
asks on a difference. The desk compares the same way at the start of a session, says so, and
then applies exactly as it does without the check: no prompt, no refusal, the same writes.
"""

import math

import pytest
from support import osc_bundle, write_config
from two_boxes import B

from oscmix_desk import ApplyIntent, Config, Route, open_desk
from oscmix_desk import connectcheck as check_mod
from oscmix_desk.config import load_config
from oscmix_desk.connectcheck import Cell, agrees, check_at_connect, declared_cells, judge
from oscmix_desk.discovery import lock_key
from oscmix_desk.journal import journal_path
from oscmix_desk.osc import encode_osc
from oscmix_desk.seen import CC_MIX, CC_MODE, read_seen

KEY = lock_key("2a39:3fd9", B[1])
INF = float("-inf")


def mic(level=-6.0, **fields):
    return Route(**{"name": "mic", "output": (1, 2), "input": (5, 6), "level": level, **fields})


class Device:
    """Answers one refresh with fixed reports; records what it was asked."""

    def __init__(self, reports=(), fail=None):
        self.reports = list(reports)
        self.dumps = 0
        self.fail = fail
        self._pending = []

    def request_dump(self):
        if self.fail is not None:
            raise self.fail
        self.dumps += 1
        self._pending = list(self.reports)

    def messages(self, _timeout):
        pending, self._pending = self._pending, []
        yield from pending


def test_only_the_desks_input_routes_are_compared(tmp_path):
    path = write_config(tmp_path / "routing.conf", (
        "[desk]\nbase = clear-inputs\n"
        "[route:mic]\ninput = 5/6\noutput = 1/2\nlevel = -6\n"
        "[route:solo]\ninput = 7\noutput = 3\nlevel = -10\n"
        "[route:wide]\ninput = 9/10\noutput = 7/8\nstereo = false\nlevel = -3\n"
        "[route:daw]\nplayback = 1/2\noutput = 1/2\n"
        "[route:again]\ninput = 7\noutput = 3\nlevel = -20\n"))
    cells = declared_cells(load_config(path))
    assert set(cells) == {"/mix/1/input/5", "/mix/3/input/7", "/mix/7/input/9",
                          "/mix/8/input/9"}, "no playback cell, none of the base's silent cells"
    assert cells["/mix/1/input/5"] == Cell("mic", -6.0, 0)
    assert cells["/mix/3/input/7"] == Cell("again", -20.0, 0), "a later route wins, as it writes"
    left, right = cells["/mix/7/input/9"], cells["/mix/8/input/9"]
    assert (left.pan, right.pan) == (-100, 100)
    assert left.level == pytest.approx(-3.0 + 20 * math.log10(2)), "the register as written"


@pytest.mark.parametrize(("report", "verdict"), [
    (("fi", (-6.0, 0)), True),
    (("fi", (-6.07, 0)), True),
    (("fi", (-5.93, 0)), True),
    (("fi", (-6.08, 0)), False),
    (("fi", (-6.1, 0)), False),
    (("fi", (-6.0, 1)), False),
    (("fi", (-6.0, -1)), False),
    (("fi", (INF, 0)), False),
    (("fi", (-6.0, 0, 9)), True),
    (("fi", (-6, 0)), True),
    (("f", (-6.0,)), None),
    (("fi", (-6.0,)), None),
    (("fs", (-6.0, "0")), None),
    (("fi", ("-6", 0)), None),
    (("fi", (float("nan"), 0)), None),
    (("fi", (float("inf"), 0)), None),
    (("fi", (True, 0)), None),
    (("fi", (-6.0, True)), None),
    (("fi", (-6.0, 0.0)), None),
    (("fi", (-6.0, 101)), None),
])
def test_totalmix_tolerances_decide(report, verdict):
    assert agrees(Cell("mic", -6.0, 0), *report) is verdict


def test_a_silent_cell_has_no_balance():
    silent = Cell("off", -65.0, 0)
    assert agrees(silent, "fi", (INF, 0)) is True
    assert agrees(silent, "fi", (INF, 40)) is True, "the backend's pan of silence is not a value"
    assert agrees(silent, "fi", (-70.0, 0)) is True, "below the floor is digital zero too"
    assert agrees(silent, "fi", (-64.0, 0)) is False
    assert agrees(Cell("panned", -6.0, 50), "fi", (-6.0, 50)) is True


def test_every_cell_is_classified_by_its_last_report():
    cells = {"/mix/1/input/1": Cell("a", -6.0, 0), "/mix/1/input/3": Cell("b", 0.0, 0),
             "/mix/1/input/5": Cell("c", 0.0, 0), "/mix/1/input/7": Cell("d", 0.0, 0)}
    latest = {"/mix/1/input/1": ("fi", (-6.0, 0)), "/mix/1/input/3": ("fi", (-12.0, 0)),
              "/mix/1/input/5": ("f", (0.0,))}
    check = judge(cells, latest, 0.0)
    assert check.compared == 4
    assert [path for path, _cell, _args in check.mismatched] == ["/mix/1/input/3"]
    assert check.unreported == ("/mix/1/input/7",)
    assert check.invalid == ("/mix/1/input/5",)
    record = check.record()
    assert record["state"] == "mismatched"
    assert record["mismatched"] == [{"route": "b", "path": "/mix/1/input/3",
                                     "device": {"level_db": -12.0, "pan": 0},
                                     "declared": {"level_db": 0.0, "pan": 0}}]


def test_a_desk_without_input_routes_reads_nothing():
    device = Device()
    config = Config(routes=(Route(name="daw", output=(1, 2), playback=(1, 2)),))
    assert check_at_connect(config, device, lambda: False) == (None, None)
    assert device.dumps == 0


def test_a_difference_is_reported_with_route_device_and_desk_values(caplog):
    device = Device([("/mix/1/input/5", "fi", (-12.0, 0)), (CC_MIX, "is", (0, "TotalMix App"))])
    with caplog.at_level("INFO"):
        check, handed = check_at_connect(Config(routes=(mic(),)), device, lambda: False)
    assert device.dumps == 1
    assert handed is None, "inputs 5/6 have no AutoSet dependency to hand over"
    assert check.record()["state"] == "mismatched"
    assert "differs from the desk in 1 of 1 route cell(s)" in caplog.text
    assert "route 'mic' /mix/1/input/5: device -12.00 dB, pan 0, desk -6.00 dB, pan 0" \
        in caplog.text
    assert "the desk overwrites them now" in caplog.text


def test_a_silent_desk_cell_reads_as_silent(caplog):
    device = Device([("/mix/1/input/5", "fi", (-12.0, 0))])
    with caplog.at_level("INFO"):
        check, _handed = check_at_connect(Config(routes=(mic(-65.0),)), device, lambda: False)
    assert "device -12.00 dB, pan 0, desk silent" in caplog.text
    assert check.record()["mismatched"][0]["declared"] == {"level_db": "-inf", "pan": 0}


def test_a_match_is_information(caplog):
    device = Device([("/mix/1/input/5", "fi", (-6.0000015, 0))])
    with caplog.at_level("INFO"):
        check, _handed = check_at_connect(Config(routes=(mic(),)), device, lambda: False)
    assert check.record()["state"] == "matched"
    assert "matches the desk's 1 route cell(s)" in caplog.text
    assert "WARNING" not in caplog.text


def test_a_silent_device_ends_the_window_at_the_timeout(monkeypatch, caplog):
    monkeypatch.setattr(check_mod, "VERIFY_TIMEOUT", 0.05)
    with caplog.at_level("INFO"):
        check, handed = check_at_connect(Config(routes=(mic(), mic(-3.0, input=(7, 8)))),
                                         Device([("/mix/1/input/7", "fi", (-3.0, 0))]),
                                         lambda: False)
    assert check.unreported == ("/mix/1/input/5",)
    assert check.record()["state"] == "incomplete"
    assert handed is None
    assert "1 of 2 route cell(s) not compared (1 not reported, 0 unreadable)" in caplog.text


def test_a_check_that_cannot_read_only_says_so(caplog):
    failing = Device(fail=OSError(16, "no fresh backend refresh window available"))
    assert check_at_connect(Config(routes=(mic(),)), failing, lambda: False) == (None, None)
    assert "could not be read" in caplog.text
    caplog.clear()
    assert check_at_connect(Config(routes=(mic(),)), failing, lambda: True) == (None, None)
    assert caplog.text == ""


def test_a_stop_reads_nothing_and_hands_nothing_over():
    device = Device([("/mix/1/input/5", "fi", (-6.0, 0))])
    assert check_at_connect(Config(routes=(mic(),)), device, lambda: True) == (None, None)
    assert device.dumps == 0
    asked = iter((False, True))
    assert check_at_connect(Config(routes=(mic(),)), Device([]), lambda: next(asked, True)) == \
        (None, None)


AUTOSET = [("/input/1/autoset", "i", (0,)), ("/input/2/autoset", "i", (0,)),
           ("/input/1/stereo", "i", (1,))]


def test_the_same_refresh_supplies_the_autoset_observation(monkeypatch):
    route = mic(input=(1, 2))
    device = Device([("/mix/1/input/1", "fi", (-6.0, 0)), *AUTOSET])
    check, handed = check_at_connect(Config(routes=(route,)), device, lambda: False)
    assert check.record()["state"] == "matched"
    assert handed == {path: (tags, args) for path, tags, args in AUTOSET}
    monkeypatch.setattr(check_mod, "VERIFY_TIMEOUT", 0.05)
    check, handed = check_at_connect(Config(routes=(route,)),
                                     Device([("/mix/1/input/1", "fi", (-6.0, 0))]), lambda: False)
    assert handed is None, "an unobserved dependency is the apply's to read, as without the check"


def report_bundle(*reports):
    return osc_bundle([encode_osc(path, tags, *args) for path, tags, args in reports])


def started(coordinated, config, intent, reports, tmp_path):
    with coordinated() as peer:
        peer.reports = [report_bundle(*reports)]
        with open_desk(config, tmp_path / "routing.conf") as operation:
            operation.apply(intent)
        return list(peer.order)


DEVICE_STATE = [("/mix/1/input/1", "fi", (-12.0, 0)), *AUTOSET,
                (CC_MIX, "is", (0, "TotalMix App")), (CC_MODE, "i", (1,)),
                ("/output/1/stereo", "i", (1,))]


def test_a_start_writes_what_a_switch_writes_after_one_shared_refresh(coordinated, tmp_path):
    """Inputs 1/2 need a fresh AutoSet observation: the check's refresh is that one."""
    config = Config(serial=B[1], routes=(mic(input=(1, 2)),))
    start = started(coordinated, config, ApplyIntent.INITIAL, DEVICE_STATE, tmp_path)
    switch = started(coordinated, config, ApplyIntent.EXPLICIT, DEVICE_STATE, tmp_path)
    assert start == switch
    assert start[0] == "/refresh"
    assert start[1] != "/refresh", "one refresh before the first write, as before the check"
    assert "/mix/1/input/1" in start, "a difference does not stop the start"


def test_without_autoset_a_start_reads_once_more_than_a_switch(coordinated, tmp_path, caplog):
    config = Config(serial=B[1], routes=(mic(),))
    reports = [("/mix/1/input/5", "fi", (-12.0, 0)), (CC_MIX, "is", (3, "20ch")),
               (CC_MODE, "i", (1,)), ("/output/1/stereo", "i", (1,))]
    switch = started(coordinated, config, ApplyIntent.EXPLICIT, reports, tmp_path)
    with caplog.at_level("INFO"):
        start = started(coordinated, config, ApplyIntent.INITIAL, reports, tmp_path)
    assert start == ["/refresh", *switch]
    assert "route 'mic' /mix/1/input/5: device -12.00 dB" in caplog.text
    record = read_seen(journal_path(tmp_path / "routing.conf", KEY, kind="seen"))
    assert record.connect["state"] == "mismatched"
    assert record.connect["mismatched"][0]["route"] == "mic"
    assert record.reports[CC_MIX][:2] == ("is", (3, "20ch")), "the dump's CC Mix, as seen"
    assert record.reports[CC_MODE][:2] == ("i", (1,))


def test_reloads_and_switches_do_not_check(coordinated, tmp_path):
    config = Config(serial=B[1], routes=(mic(),))
    agreeing = [("/mix/1/input/5", "fi", (-6.0, 0)), ("/input/5/stereo", "i", (1,)),
                ("/output/1/stereo", "i", (1,))]
    for intent in (ApplyIntent.EXPLICIT, ApplyIntent.RECONCILE):
        started(coordinated, config, intent, agreeing, tmp_path)
        assert read_seen(journal_path(tmp_path / "routing.conf", KEY, kind="seen")).connect \
            is None
