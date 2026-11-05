"""The reads an apply makes before it writes, and the refusals they produce.

`prechecks` opens one fresh DEVICE window per check. These tests pin what a person reads when a
stop interrupts such a read (which observation, and that writes were refused), when the window
closes, and the complete refusal texts for AutoSet ownership, input links and the DSP limits.
"""

import itertools
import logging
from types import SimpleNamespace

import pytest
from backend_doubles import RecordingBackend

from oscmix_desk import prechecks, routing
from oscmix_desk.backend import OSCMIX
from oscmix_desk.constants import VERIFY_TIMEOUT
from oscmix_desk.model import ChannelSetting, Config, Route
from oscmix_desk.prechecks import (
    dsp_limit_problem,
    gain_problem,
    input_link_expectations,
    read_dependencies,
    require_gain_ownership,
    volume_reader,
)
from oscmix_desk.reconcile import (
    PHASE_CHANNEL,
    PHASE_LINK,
    PHASE_MIX,
    ApplyIntent,
    Write,
    WriteReason,
)
from oscmix_desk.registers import Policy

FLAGS = ("/input/1/autoset", "/input/2/autoset", "/input/1/stereo")
VOLUMES = ["/output/1/volume", "/output/2/volume"]


class Window:
    """Reports in batches, one batch per receive; each receive costs ``slice`` seconds."""

    def __init__(self, batches, clock):
        self.batches = list(batches)
        self.clock = clock
        self.dumps = 0
        self.timeouts = []

    def request_dump(self):
        self.dumps += 1

    def messages(self, timeout):
        self.timeouts.append(timeout)
        self.clock.now += timeout
        return iter(self.batches.pop(0) if self.batches else ())


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


@pytest.fixture
def clock(monkeypatch):
    clock = Clock()
    monkeypatch.setattr(prechecks, "time", SimpleNamespace(monotonic=clock))
    return clock


def stops(*answers):
    """A stop check answering in turn, then False."""
    answers = iter(answers)
    return lambda: next(answers, False)


def write(path, *args, phase=PHASE_CHANNEL):
    tags = "fi" if path.startswith("/mix/") else ("f" if path.endswith("/gain") else "i")
    return Write(path, tags, args, phase, WriteReason.MISMATCHED)


def flags(own=1, partner=0, stereo=0):
    return [("/input/1/autoset", "i", (own,)), ("/input/2/autoset", "i", (partner,)),
            ("/input/1/stereo", "i", (stereo,))]


def test_a_stop_before_the_read_refuses_the_writes_and_requests_nothing(clock):
    backend = Window([], clock)
    with pytest.raises(OSError, match="writes refused") as refused:
        read_dependencies(backend, FLAGS, lambda: True)
    assert str(refused.value) == "stop requested before AutoSet observation; writes refused"
    assert backend.dumps == 0


def test_a_stop_during_the_read_names_the_observation(clock):
    backend = Window([], clock)
    with pytest.raises(OSError, match="writes refused") as refused:
        read_dependencies(backend, FLAGS, stops(False, True), purpose="DSP limit")
    assert str(refused.value) == "stop requested during DSP limit observation; writes refused"
    assert backend.dumps == 1


def test_a_stop_after_a_complete_read_still_refuses(clock):
    backend = Window([flags()], clock)
    with pytest.raises(OSError, match="writes refused") as refused:
        read_dependencies(backend, FLAGS, stops(False, False, True))
    assert str(refused.value) == "stop requested after AutoSet observation; writes refused"


def test_the_window_closes_once_every_flag_and_expected_link_reported(clock):
    backend = Window([flags(stereo=1)], clock)
    seen = read_dependencies(backend, FLAGS, lambda: False, links={"/input/1/stereo": 1})
    assert seen == {path: (tags, args) for path, tags, args in flags(stereo=1)}
    assert len(backend.timeouts) == 1, "no second receive once everything reported"
    assert all(timeout <= 0.25 for timeout in backend.timeouts), "a stop is seen promptly"


def test_a_window_whose_time_is_up_reads_nothing_more(monkeypatch):
    times = iter([0.0, VERIFY_TIMEOUT, VERIFY_TIMEOUT])
    monkeypatch.setattr(prechecks, "time", SimpleNamespace(monotonic=lambda: next(times)))
    backend = Window([flags()], Clock())
    assert read_dependencies(backend, FLAGS, lambda: False) == {}
    assert backend.timeouts == []


def test_a_peak_safe_read_returns_the_volumes_once_each_reported(clock):
    backend = Window([[("/output/1/volume", "f", (-10.0,)), ("/output/2/volume", "f", (-12.5,))]],
                     clock)
    assert volume_reader(backend, lambda: False)(VOLUMES) == {
        "/output/1/volume": ("f", (-10.0,)), "/output/2/volume": ("f", (-12.5,))}
    assert len(backend.timeouts) == 1, "a volume is a value, not a flag: any report settles it"


def test_a_peak_safe_read_waits_for_every_volume(clock):
    backend = Window([[("/output/1/volume", "f", (-10.0,))], [],
                      [("/output/2/volume", "f", (-12.5,))]], clock)
    assert set(volume_reader(backend, lambda: False)(VOLUMES)) == set(VOLUMES)
    assert len(backend.timeouts) == 3


def test_a_stop_before_a_peak_safe_read_names_it(clock):
    with pytest.raises(OSError, match="writes refused") as refused:
        volume_reader(Window([], clock), lambda: True)(VOLUMES)
    assert str(refused.value) == "stop requested before peak-safe observation; writes refused"


def roomeq(*channels):
    return Config(channels=tuple(ChannelSetting("output", ch, "roomeq", 1) for ch in channels),
                  policies={("output", "roomeq"): Policy.PIN})


def test_a_stop_before_the_dsp_limit_read_names_it(clock):
    selected = [write("/output/17/roomeq", 1)]
    with pytest.raises(OSError, match="writes refused") as refused:
        dsp_limit_problem(roomeq(17), selected, {}, Window([], clock), lambda: True)
    assert str(refused.value) == "stop requested before DSP limit observation; writes refused"


class DeviceWindow(RecordingBackend):
    """One DEVICE window per refresh, as the test supplies it."""

    def __init__(self, window):
        super().__init__()
        self.traits = OSCMIX
        self.window = window

    def request_dump(self):
        self.dumps += 1

    def messages(self, _timeout):
        yield from ((path, tags, args) for path, (tags, args) in self.window.items())


@pytest.fixture
def bounded_clock(monkeypatch):
    ticks = itertools.count(0, .001)
    monkeypatch.setattr(routing.time, "monotonic", lambda: next(ticks))
    monkeypatch.setattr(routing.time, "sleep", lambda _: None)
    monkeypatch.setattr(prechecks, "VERIFY_TIMEOUT", .03)
    monkeypatch.setattr(routing, "LINK_ECHO_TIMEOUT", .03)


@pytest.mark.usefixtures("bounded_clock")
def test_a_start_over_the_room_eq_limit_says_what_it_leaves_off(caplog):
    window = {"/output/%d/roomeq" % ch: ("i", (1 if ch <= 15 else 0,)) for ch in range(1, 21)}
    window.update({"/output/%d/crossfeed" % ch: ("i", (0,)) for ch in range(1, 21)})
    window.update({"/output/%d/stereo" % ch: ("i", (0,)) for ch in range(1, 21, 2)})
    backend = DeviceWindow(window)
    routing.apply_routing(roomeq(17, 18), backend, intent=ApplyIntent.INITIAL)
    assert backend.sent == []
    channels = ", ".join(map(str, [*range(1, 16), 17, 18]))
    assert [record.getMessage() for record in caplog.records
            if record.levelno == logging.WARNING] == [
        "this apply would leave Room EQ on 17 output channels (%s); the device runs it on at "
        "most 16; not switching on /output/17/roomeq, /output/18/roomeq now, so the rest of "
        "the desk applies" % channels]


def test_gains_autoset_owns_are_named_in_the_refusal():
    config = Config(channels=(ChannelSetting("input", 1, "gain", 6.0),
                              ChannelSetting("input", 2, "gain", 9.0)))
    latest = {path: (tags, args) for path, tags, args in flags(own=1, partner=1, stereo=1)}
    selected = [write("/input/1/gain", 6.0), write("/input/2/gain", 9.0)]
    problem = ("AutoSet is active or not confirmed off for gain writes: /input/1/gain, "
               "/input/2/gain")
    assert gain_problem(config, selected, latest, ApplyIntent.EXPLICIT, (), ()) == problem
    with pytest.raises(OSError, match="AutoSet is active") as refused:
        require_gain_ownership(config, selected, latest, ApplyIntent.EXPLICIT, (), ())
    assert str(refused.value) == problem


def partner_gain(stereo_write):
    config = Config(channels=(ChannelSetting("input", 2, "gain", 9.0),))
    latest = {path: (tags, args) for path, tags, args in flags(own=1, partner=0, stereo=0)}
    selected = [write("/input/1/stereo", stereo_write, phase=PHASE_LINK),
                write("/input/2/gain", 9.0)]
    return gain_problem(config, selected, latest, ApplyIntent.EXPLICIT, (), ())


def test_a_link_the_plan_writes_hands_autoset_the_partner_gain():
    assert partner_gain(1) == ("AutoSet is active or not confirmed off for gain writes: "
                               "/input/2/gain"), "linked, input 1's AutoSet owns both gains"


def test_an_unlink_the_plan_writes_leaves_the_partner_gain_free():
    # Input 2's gain is not AutoSet's; what remains is the unlink touching input 1's own.
    assert partner_gain(0) == ("link write /input/1/stereo could change retained partner "
                               "settings: /input/1/gain")


def test_a_reconcile_keeps_a_declared_autoset_even_when_the_device_reports_it_off():
    config = Config(channels=(ChannelSetting("input", 1, "gain", 6.0),
                              ChannelSetting("input", 1, "autoset", 1)))
    latest = {path: (tags, args) for path, tags, args in flags(own=0, partner=0, stereo=0)}
    selected = [write("/input/1/gain", 6.0)]
    assert gain_problem(config, selected, latest, ApplyIntent.RECONCILE, (), ()) == (
        "AutoSet is active or not confirmed off for gain writes: /input/1/gain")
    assert gain_problem(config, selected, latest, ApplyIntent.EXPLICIT, (), ()) is None, (
        "an explicit apply may set the starting value before AutoSet is switched on")


MUSIC = Route("music", playback=(1, 2), output=(1, 2))
MIC = Route("mic", input=(1, 2), output=(3, 4), level=-12.0)
TALK = Route("talk", input=(3,), output=(5, 6), level=-12.0)


def test_only_the_routes_the_plan_writes_need_fresh_input_links():
    config = Config(routes=(MUSIC, TALK, MIC))
    selected = [write("/mix/3/input/1", -12.0, 0, phase=PHASE_MIX)]
    assert input_link_expectations(config, selected) == {"/input/1/stereo": 1}
    both = [*selected, write("/mix/5/input/3", -12.0, 0, phase=PHASE_MIX)]
    assert input_link_expectations(config, both) == {"/input/1/stereo": 1, "/input/3/stereo": 0}


def test_unconfirmed_input_links_are_named_in_the_refusal():
    config = Config(routes=(MUSIC, MIC, TALK))
    selected = [write("/mix/3/input/1", -12.0, 0, phase=PHASE_MIX),
                write("/mix/5/input/3", -12.0, 0, phase=PHASE_MIX)]
    latest = {"/input/1/autoset": ("i", (0,)), "/input/2/autoset": ("i", (0,)),
              "/input/3/autoset": ("i", (0,)), "/input/4/autoset": ("i", (0,))}
    assert gain_problem(config, selected, latest, ApplyIntent.EXPLICIT, (), ()) == (
        "fresh input link state is not confirmed for dependent mix writes: /input/1/stereo, "
        "/input/3/stereo")
