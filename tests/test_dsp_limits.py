"""The manual's cross-channel DSP limits are refused before any write.

The UCX II runs Room EQ on at most 16 output channels (manual 25.3.3) and
Crossfeed on at most 4 stereo outputs (25.3.1). No single register is out
of range when a config breaks either limit, so the count is checked over
the whole state: the config's own declarations when it loads, and the
apply's writes over every other output's observed state before it sends.
"""

import itertools

import pytest
from backend_doubles import RecordingBackend

from oscmix_desk import routing
from oscmix_desk.backend import OSCMIX
from oscmix_desk.config import load_config
from oscmix_desk.errors import ConfigError, WriteFailed
from oscmix_desk.limits import LIMITS, dsp_problem, observation_paths, switches_on
from oscmix_desk.model import ChannelSetting, Config
from oscmix_desk.reconcile import ApplyIntent
from oscmix_desk.registers import Policy


def _conf(tmp_path, body):
    path = tmp_path / "routing.conf"
    path.write_text("[device]\nname = Fireface UCX II\n" + body)
    return path


def _roomeq(channels, value=1):
    return "".join("\n[roomeq:output:%d]\nenabled = %d\n" % (ch, value) for ch in channels)


def _crossfeed(channels, value=3):
    return "".join("\n[output:%d]\ncrossfeed = %d\n" % (ch, value) for ch in channels)


def test_the_ucx_ii_limits_are_the_manuals():
    limits = LIMITS["Fireface UCX II"]
    assert (limits.roomeq_channels, limits.crossfeed_pairs) == (16, 4)


def test_room_eq_on_sixteen_outputs_loads(tmp_path):
    config = load_config(_conf(tmp_path, _roomeq(range(1, 17))))
    assert sum(1 for c in config.channels if c.option == "roomeq") == 16


def test_room_eq_on_seventeen_outputs_is_refused(tmp_path):
    with pytest.raises(ConfigError, match=r"Room EQ on 17 output channels.*at most 16"):
        load_config(_conf(tmp_path, _roomeq(range(1, 18))))


def test_room_eq_switched_off_does_not_count(tmp_path):
    body = _roomeq(range(1, 17)) + _roomeq(range(17, 21), value=0)
    assert len(load_config(_conf(tmp_path, body)).channels) == 20


def test_crossfeed_on_four_stereo_outputs_loads(tmp_path):
    load_config(_conf(tmp_path, _crossfeed([1, 2, 3, 4, 5, 6, 7, 8])))


def test_crossfeed_on_a_fifth_stereo_output_is_refused(tmp_path):
    with pytest.raises(ConfigError, match=r"Crossfeed on 5 stereo outputs .*9/10.*at most 4"):
        load_config(_conf(tmp_path, _crossfeed([1, 3, 5, 7, 10])))


def test_crossfeed_off_does_not_count(tmp_path):
    load_config(_conf(tmp_path, _crossfeed([1, 3, 5, 7]) + _crossfeed([9, 11], value=0)))


def _config(roomeq=(), crossfeed=()):
    return Config(channels=[ChannelSetting("output", ch, "roomeq", 1) for ch in roomeq]
                  + [ChannelSetting("output", ch, "crossfeed", 2) for ch in crossfeed])


def _observed(roomeq=(), crossfeed=()):
    state = {"/output/%d/roomeq" % ch: ("i", (1 if ch in roomeq else 0,)) for ch in range(1, 21)}
    state.update({"/output/%d/crossfeed" % ch: ("i", (3 if ch in crossfeed else 0,))
                  for ch in range(1, 21)})
    return state


def test_an_apply_counts_its_writes_over_the_observed_outputs():
    config = _config(roomeq=[17])
    writes = [("/output/17/roomeq", (1,))]
    assert dsp_problem(config, writes, _observed(roomeq=range(1, 16))) is None
    problem = dsp_problem(config, writes, _observed(roomeq=range(1, 17)))
    assert "would leave Room EQ on 17 output channels" in problem


def test_a_written_off_replaces_the_observed_on():
    config = _config(roomeq=[17])
    writes = [("/output/17/roomeq", (1,)), ("/output/16/roomeq", (0,))]
    assert dsp_problem(config, writes, _observed(roomeq=range(1, 17))) is None


def test_crossfeed_pairs_count_once_and_mix_observed_with_written():
    config = _config(crossfeed=[9])
    writes = [("/output/9/crossfeed", (2,))]
    assert dsp_problem(config, writes, _observed(crossfeed=[1, 2, 3, 6])) is None
    problem = dsp_problem(config, writes, _observed(crossfeed=[1, 3, 5, 8]))
    assert "Crossfeed on 5 stereo outputs (1/2, 3/4, 5/6, 7/8, 9/10)" in problem


@pytest.mark.parametrize("report", [None, ("i", ()), ("f", ("x",)), ("i", (1.5,))])
def test_an_output_that_did_not_report_counts_as_off(report):
    state = _observed(roomeq=range(1, 17))
    if report is None:
        del state["/output/16/roomeq"]
    else:
        state["/output/16/roomeq"] = report
    assert dsp_problem(_config(roomeq=[17]), [("/output/17/roomeq", (1,))], state) is None


def test_a_plan_switching_nothing_on_is_never_refused_or_observed():
    config = _config(roomeq=[1])
    writes = [("/output/1/roomeq", (0,)), ("/output/1/crossfeed", (0,))]
    assert not switches_on(config, writes)
    assert dsp_problem(config, writes, _observed(roomeq=range(1, 21))) is None


def test_a_device_without_limits_is_not_counted():
    config = Config(device_name="Some Other Interface")
    assert observation_paths(config) == ()
    assert not switches_on(config, [("/output/1/roomeq", (1,))])


def test_observation_covers_both_registers_of_every_output_and_every_link():
    paths = observation_paths(_config())
    assert len(paths) == 50
    assert "/output/20/roomeq" in paths
    assert "/output/20/crossfeed" in paths
    assert "/output/19/stereo" in paths


def _links(*pairs):
    return {"/output/%d/stereo" % left: ("i", (1 if left in pairs else 0,))
            for left in range(1, 21, 2)}


def test_room_eq_on_one_channel_of_a_linked_pair_counts_both(tmp_path):
    routes = "".join("\n[route:p%d]\nplayback = %d/%d\noutput = %d/%d\n"
                     % (left, left, left + 1, left, left + 1) for left in range(1, 18, 2))
    with pytest.raises(ConfigError, match="Room EQ on 18 output channels"):
        load_config(_conf(tmp_path, routes + _roomeq(range(1, 18, 2))))
    load_config(_conf(tmp_path, routes + _roomeq(range(1, 16, 2))))
    config = _config(roomeq=[17])
    writes = [("/output/17/roomeq", (1,))]
    unlinked = {**_observed(roomeq=range(1, 16, 2)), **_links()}
    assert dsp_problem(config, writes, unlinked) is None, "eight channels and output 17"
    linked = {**unlinked, **_links(*range(1, 16, 2))}
    assert "Room EQ on 17 output channels" in dsp_problem(config, writes, linked)
    seven = {**unlinked, **_links(*range(1, 14, 2))}
    assert dsp_problem(config, writes, seven) is None, "fourteen, fifteen and seventeen"
    assert "Room EQ on 17" in dsp_problem(config, [*writes, ("/output/17/stereo", (1,))],
                                          seven), "a link the plan writes"


@pytest.fixture
def bounded_clock(monkeypatch):
    ticks = itertools.count(0, .001)
    monkeypatch.setattr(routing.time, "monotonic", lambda: next(ticks))
    monkeypatch.setattr(routing.time, "sleep", lambda _: None)
    monkeypatch.setattr(routing, "VERIFY_TIMEOUT", .03)
    monkeypatch.setattr(routing, "LINK_ECHO_TIMEOUT", .03)


class WindowBackend(RecordingBackend):
    """One DEVICE window per refresh, as the test supplies it."""

    def __init__(self, window):
        super().__init__()
        self.traits = OSCMIX
        self.window = window

    def request_dump(self):
        self.dumps += 1

    def messages(self, _timeout):
        yield from ((path, tags, args) for path, (tags, args) in self.window.items())


@pytest.mark.usefixtures("bounded_clock")
def test_a_switch_reads_the_outputs_and_refuses_before_any_write():
    backend = WindowBackend(_observed(roomeq=range(1, 17)))
    with pytest.raises(WriteFailed, match="Room EQ on 17") as refused:
        routing.apply_routing(_config(roomeq=[17]), backend, intent=ApplyIntent.EXPLICIT)
    assert backend.sent == []
    assert backend.dumps == 1
    assert refused.value.written == ()
    assert refused.value.unwritten == ("/output/17/roomeq",)


@pytest.mark.parametrize("intent", [ApplyIntent.INITIAL, ApplyIntent.REPAIR,
                                    ApplyIntent.RECONCILE])
@pytest.mark.usefixtures("bounded_clock")
def test_a_start_leaves_the_switch_on_to_the_device_and_applies_the_rest(intent, caplog):
    config = Config(channels=[ChannelSetting("output", 17, "roomeq", 1),
                              ChannelSetting("output", 3, "volume", -10.0)],
                    policies={("output", "roomeq"): Policy.PIN, ("output", "volume"): Policy.PIN})
    backend = WindowBackend(_observed(roomeq=range(1, 17)))
    routing.apply_routing(config, backend, intent=intent)
    assert backend.sent == [("/output/3/volume", "f", (-10.0,))]
    assert "not switching on /output/17/roomeq now" in caplog.text


class CountingWindow(WindowBackend):
    def __init__(self, window):
        super().__init__(window)
        self.windows = 0

    def messages(self, timeout):
        self.windows += 1
        yield from super().messages(timeout)


@pytest.mark.usefixtures("bounded_clock")
def test_crossfeed_levels_end_the_observation_at_once(monkeypatch):
    monkeypatch.setattr(routing, "VERIFY_TIMEOUT", 10.0)
    backend = CountingWindow({**_observed(crossfeed=[1, 3, 5]), **_links()})
    routing.apply_routing(_config(crossfeed=[9]), backend)
    assert backend.windows == 1, "a level of 3 is a report, not a malformed flag"


@pytest.mark.usefixtures("bounded_clock")
def test_apply_within_the_limit_writes():
    backend = WindowBackend(_observed(roomeq=range(1, 16)))
    routing.apply_routing(_config(roomeq=[17]), backend)
    assert backend.sent == [("/output/17/roomeq", "i", (1,))]


@pytest.mark.usefixtures("bounded_clock")
def test_a_supplied_observation_is_used_without_another_read():
    backend = WindowBackend({})
    with pytest.raises(WriteFailed, match="Crossfeed on 5"):
        routing.apply_routing(_config(crossfeed=[9]), backend, intent=ApplyIntent.EXPLICIT,
                              observed={**_observed(crossfeed=[1, 3, 5, 7]), **_links()})
    assert backend.dumps == 0
    assert backend.sent == []


@pytest.mark.parametrize(("load", "rate", "warned"), [
    (50, 48000, False), (99, 48000, False), (110, 96000, True), (84, 192000, False),
])
def test_an_overloaded_device_is_named_by_its_load(load, rate, warned):
    # Measured on a UCX II: Room EQ on 16 channels and Crossfeed on 2 pairs are 50 at
    # 48 kHz, 110 at 96 kHz and 84 at 192 kHz, every flag still reading on.
    from oscmix_desk.limits import overload
    note = overload({"/hardware/dspload": ("i", (load,))})
    assert bool(note) is warned, rate
    if warned:
        assert "DSP load of 110%" in note
    assert overload({}) is None
    assert overload({"/hardware/dspload": ("i", ("x",))}) is None


def test_a_read_back_keeps_the_load_beside_the_declared_registers():
    from oscmix_desk.observation import Observation
    seen = Observation({"/output/1/volume": ("f", (-10.0,))}, noted=("/hardware/dspload",))
    seen.absorb(("/hardware/dspload", "i", (120,)))
    seen.absorb(("/hardware/dspvers", "i", (36,)))
    assert seen.latest == {"/hardware/dspload": ("i", (120,))}
    assert seen.unobserved == {"/output/1/volume"}, "noted paths are never expected"
