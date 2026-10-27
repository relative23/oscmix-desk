"""AutoSet owns live gain; a declared starting value stays a real expectation."""

import errno
import itertools
from collections import deque
from types import SimpleNamespace

import pytest
from backend_doubles import RecordingBackend

from oscmix_desk import config as config_mod
from oscmix_desk import desk as desk_mod
from oscmix_desk import profiles, routing, verify
from oscmix_desk.backend import OSCMIX
from oscmix_desk.devices import UCX2
from oscmix_desk.errors import ConfigError, ReceivePortError, WriteFailed
from oscmix_desk.model import ChannelSetting, Config, Route
from oscmix_desk.outcome import APPLIED_MISMATCHED
from oscmix_desk.reconcile import (
    ApplyIntent,
    application_plan,
    desired,
    remembered_paths,
    retention_problem,
)
from oscmix_desk.registers import PIN


def path(channel, option):
    return "/input/%d/%s" % (channel, option)


def pair(channel):
    left = channel - (channel - 1) % 2
    return left, left + 1


def latest(channel, own=0, partner=0, stereo=1):
    left, right = pair(channel)
    other = right if channel == left else left
    return {path(channel, "autoset"): ("i", (own,)),
            path(other, "autoset"): ("i", (partner,)),
            path(left, "stereo"): ("i", (stereo,))}


def reports(values):
    return [(name, tags, args) for name, (tags, args) in values.items()]


def configured(channel, *, flag=None, partner_gain=False, routed=False, unrelated=False):
    left, right = pair(channel)
    channels = [ChannelSetting("input", channel, "gain", 6.)]
    if partner_gain:
        channels.append(ChannelSetting("input", right if channel == left else left, "gain", 9.))
    if flag is not None:
        channels.append(ChannelSetting("input", channel, "autoset", flag))
    if unrelated:
        channels.append(ChannelSetting("input", 5, "gain", 12.))
    routes = [Route("monitor", input=(left, right), output=(5, 6), level=-24.)] if routed else []
    return Config(channels=channels, routes=routes)


@pytest.fixture(autouse=True)
def bounded_clock(monkeypatch):
    ticks = itertools.count(0, .001)
    monkeypatch.setattr(routing.time, "monotonic", lambda: next(ticks))
    monkeypatch.setattr(routing.time, "sleep", lambda _: None)
    monkeypatch.setattr(routing, "VERIFY_TIMEOUT", .03)
    monkeypatch.setattr(routing, "LINK_ECHO_TIMEOUT", .03)
    monkeypatch.setattr(verify, "VERIFY_TIMEOUT", .03)
    monkeypatch.setattr(verify, "VERIFY_SETTLE", 0.)


class GainBackend(RecordingBackend):
    """Explicit DEVICE windows; gain/AutoSet writes do not invent feedback.

    Link echoes model only the requested link writes. Each fresh window is
    supplied by its test, including whether a linked AutoSet-off actually
    propagated. A missing flag remains missing.
    """

    def __init__(self, windows=(), *, fail_send=None, stop_after_off=None):
        super().__init__()
        self.traits = OSCMIX
        self.windows = iter(windows)
        self.active = deque()
        self.timeline = []
        self.fail_send = fail_send
        self.send_count = 0
        self.stop_after_off = stop_after_off

    def request_dump(self):
        self.dumps += 1
        self.timeline.append(("refresh", self.dumps))
        window = next(self.windows, [])
        if isinstance(window, Exception):
            raise window
        self.active = deque(window)

    def messages(self, _timeout):
        for item in self.active.popleft() if self.active else ():
            if isinstance(item, Exception):
                raise item
            self.timeline.append(("report", item))
            yield item

    def send(self, messages):
        burst = list(messages)
        self.send_count += 1
        self.timeline.append(("send", tuple(burst)))
        if self.fail_send is not None and self.send_count == self.fail_send[0]:
            prefix = self.fail_send[1]
            super().send(burst[:prefix])
            raise WriteFailed(OSError(errno.EIO, "injected gain transport failure"),
                              [p for p, _t, _a in burst[:prefix]],
                              [p for p, _t, _a in burst[prefix:]])
        super().send(burst)
        echoed = [message for message in burst if message[0].endswith("/stereo")]
        if echoed:
            self.active.append(echoed)
        if self.stop_after_off is not None and any(
                p.endswith("/autoset") and args == (0,) for p, _t, args in burst):
            self.stop_after_off["stop"] = True

    def wait(self, _seconds):
        pass


@pytest.mark.parametrize("intent", [ApplyIntent.REPAIR, ApplyIntent.RECONCILE])
@pytest.mark.parametrize("channel", [1, 2, 3, 4])
@pytest.mark.parametrize("declared", [False, True])
def test_later_plan_retains_linked_gains_without_claiming_they_match(intent, channel, declared):
    config = configured(channel, flag=True if declared else None,
                        partner_gain=True, unrelated=True)
    state = latest(channel, own=0 if declared else 1, partner=0 if declared else 1)
    kept = remembered_paths(config, state)
    assert {path(number, "gain") for number in pair(channel)} <= set(kept)
    selected = application_plan(config, intent, observed=state)
    assert selected.messages() == (("/input/5/gain", "f", (12.,)),)
    assert selected.confirmed == ()
    assert {entry.path for entry in desired(config)} >= {path(channel, "gain")}
    wanted = {entry.path: entry.args for entry in desired(config)}
    assert wanted[path(channel, "gain")] == (6.,)


@pytest.mark.parametrize("channel", [1, 2, 3, 4])
@pytest.mark.parametrize("bad", [None, ("f", (0.,)), ("i", (False,)),
                                  ("i", ()), ("i", (0, 0)), ("i", (2,))])
def test_unknown_or_malformed_partner_preserves_both_linked_gains(channel, bad):
    config = configured(channel, partner_gain=True)
    state = latest(channel)
    left, right = pair(channel)
    other = right if channel == left else left
    if bad is None:
        del state[path(other, "autoset")]
    else:
        state[path(other, "autoset")] = bad
    assert {path(left, "gain"), path(right, "gain")} <= set(remembered_paths(config, state))
    assert application_plan(config, ApplyIntent.REPAIR, observed=state).writes == ()


@pytest.mark.parametrize("channel", [1, 2, 3, 4])
@pytest.mark.parametrize("intent", list(ApplyIntent))
def test_actual_mono_allows_off_channel_gain_without_touching_live_on_partner(channel, intent):
    config = configured(channel)
    state = latest(channel, own=0, partner=1, stereo=0)
    backend = GainBackend()
    routing.apply_routing(config, backend, intent=intent, observed=state)
    assert backend.sent == [(path(channel, "gain"), "f", (6.,))]
    assert backend.dumps == 0


@pytest.mark.parametrize("channel", [1, 2, 3, 4])
def test_planned_join_overrides_actual_mono_and_cannot_change_retained_gain(channel):
    config = configured(channel, routed=True)
    state = latest(channel, own=0, partner=1, stereo=0)
    left, right = pair(channel)
    selected = application_plan(config, ApplyIntent.REPAIR, observed=state)
    assert path(channel, "gain") not in [w.path for w in selected.writes]
    kept = remembered_paths(config, state, linked=(path(left, "stereo"),))
    assert {path(left, "gain"), path(right, "gain")} <= set(kept)
    assert retention_problem(config, selected.writes, kept, (), observed=state)
    backend = GainBackend()
    with pytest.raises(WriteFailed, match="retained partner") as refused:
        routing.apply_routing(config, backend, intent=ApplyIntent.REPAIR, observed=state)
    assert refused.value.written == ()
    assert backend.sent == []


@pytest.mark.parametrize("channel", [1, 2, 3, 4])
@pytest.mark.parametrize("mono", [False, True])
def test_retention_problem_uses_live_mono_for_direct_partner_effects(channel, mono):
    config = configured(channel)
    left, right = pair(channel)
    other = right if channel == left else left
    state = latest(channel, own=0, partner=1, stereo=0 if mono else 1)
    selected = application_plan(config, ApplyIntent.EXPLICIT, observed=state)
    problem = retention_problem(config, selected.writes, [path(other, "gain")], (), observed=state)
    assert (problem is None) is mono


@pytest.mark.parametrize("channel", [5, 6, 7, 8])
@pytest.mark.parametrize("intent", list(ApplyIntent))
def test_other_input_gain_pin_acquires_no_autoset_dependency(channel, intent):
    config = Config(channels=[ChannelSetting("input", channel, "gain", 12.)])
    backend = GainBackend()
    routing.apply_routing(config, backend, intent=intent)
    assert backend.sent == [(path(channel, "gain"), "f", (12.,))]
    assert backend.dumps == 0
    assert path(channel, "gain") not in remembered_paths(config)


@pytest.mark.parametrize("intent", [ApplyIntent.INITIAL, ApplyIntent.EXPLICIT])
@pytest.mark.parametrize("channel", [1, 2, 3, 4])
def test_starting_gain_uses_fresh_complete_off_feedback_without_toggling_omitted_flag(
        intent, channel):
    backend = GainBackend([[reports(latest(channel))]])
    routing.apply_routing(configured(channel), backend, intent=intent)
    assert backend.dumps == 1
    assert backend.sent == [(path(channel, "gain"), "f", (6.,))]
    assert backend.timeline[0] == ("refresh", 1)
    assert backend.timeline[-1] == ("send", ((path(channel, "gain"), "f", (6.,)),))


@pytest.mark.parametrize("intent", [ApplyIntent.INITIAL, ApplyIntent.EXPLICIT])
@pytest.mark.parametrize("channel", [1, 2, 3, 4])
@pytest.mark.parametrize("own_on", [False, True])
def test_live_on_without_explicit_off_refuses_all_writes(intent, channel, own_on):
    state = latest(channel, own=int(own_on), partner=int(not own_on))
    backend = GainBackend([[reports(state)]])
    config = configured(channel, unrelated=True)
    with pytest.raises(WriteFailed, match="AutoSet") as refused:
        routing.apply_routing(config, backend, intent=intent)
    assert refused.value.written == ()
    assert refused.value.unwritten == (path(channel, "gain"), "/input/5/gain")
    assert backend.sent == []
    assert backend.dumps == 1


@pytest.mark.parametrize("last", [("f", (0.,)), ("i", (False,)), ("i", (0, 0))])
def test_last_malformed_complete_delivery_does_not_grant_a_starting_gain(last):
    state = latest(1)
    delivery = reports(state) + [(path(1, "autoset"), *last)]
    backend = GainBackend([[delivery]])
    with pytest.raises(WriteFailed, match="AutoSet") as refused:
        routing.apply_routing(configured(1), backend)
    assert refused.value.written == ()
    assert backend.sent == []


@pytest.mark.parametrize("channel", [1, 2, 3, 4])
def test_explicit_off_precedes_links_and_mix_then_fresh_off_precedes_gain(channel):
    left, _right = pair(channel)
    config = configured(channel, flag=False, routed=True)
    state = latest(channel, own=1, partner=0, stereo=0)
    backend = GainBackend([[reports(latest(channel, stereo=0))], [reports(latest(channel))]])
    routing.apply_routing(config, backend, intent=ApplyIntent.EXPLICIT, observed=state)
    expected = [(path(channel, "autoset"), "i", (0,)),
                (path(left, "stereo"), "i", (1,)), ("/output/5/stereo", "i", (1,)),
                ("/mix/5/input/%d" % left, "fi", (-24., 0)),
                (path(channel, "gain"), "f", (6.,))]
    assert backend.sent == expected
    assert backend.dumps == 2
    send_indices = [n for n, event in enumerate(backend.timeline) if event[0] == "send"]
    refresh_indices = [n for n, event in enumerate(backend.timeline) if event[0] == "refresh"]
    assert send_indices[0] < refresh_indices[0] < send_indices[1]
    assert send_indices[1] < refresh_indices[1] < send_indices[2]


@pytest.mark.parametrize("channel", [1, 2, 3, 4])
def test_declared_on_is_last_after_starting_gain_and_uses_fresh_post_link_off(channel):
    left, _right = pair(channel)
    backend = GainBackend([[reports(latest(channel))]])
    routing.apply_routing(configured(channel, flag=True, routed=True), backend,
                          intent=ApplyIntent.INITIAL, observed=latest(channel))
    assert backend.sent == [
        (path(left, "stereo"), "i", (1,)), ("/output/5/stereo", "i", (1,)),
        ("/mix/5/input/%d" % left, "fi", (-24., 0)),
        (path(channel, "gain"), "f", (6.,)), (path(channel, "autoset"), "i", (1,)),
    ]
    assert backend.dumps == 1


@pytest.mark.parametrize("channel", [1, 2, 3, 4])
@pytest.mark.parametrize("enabled", [False, True])
def test_plan_and_actual_wire_order_autoset_by_dependency_instead_of_config_order(
        channel, enabled):
    left, _right = pair(channel)
    gain = ChannelSetting("input", channel, "gain", 6.)
    flag = ChannelSetting("input", channel, "autoset", enabled)
    # Deliberately put ON before Gain, and OFF after Gain in the file order.
    settings = (flag, gain) if enabled else (gain, flag)
    config = Config(channels=settings, routes=[
        Route("monitor", input=(left, left + 1), output=(5, 6), level=-24.),
    ])
    state = latest(channel)
    plan = application_plan(config, ApplyIntent.INITIAL, observed=state)
    middle = [(path(left, "stereo"), "i", (1,)), ("/output/5/stereo", "i", (1,)),
              ("/mix/5/input/%d" % left, "fi", (-24., 0)),
              (path(channel, "gain"), "f", (6.,))]
    flag_message = (path(channel, "autoset"), "i", (int(enabled),))
    expected = middle + [flag_message] if enabled else [flag_message] + middle
    assert plan.messages() == tuple(expected)
    assert [write.message() for write in plan.channel()] == (
        [middle[-1], flag_message] if enabled else [flag_message, middle[-1]])
    fresh_windows = [[reports(state)]] if enabled else [[reports(state)], [reports(state)]]
    backend = GainBackend(fresh_windows)
    routing.apply_routing(config, backend, observed=state)
    assert backend.sent == expected
    assert config.channels == tuple(settings)


@pytest.mark.parametrize("failure", ["missing", "still-on", "malformed", "request", "receive"])
def test_failed_fresh_off_confirmation_reports_off_written_and_exact_remaining(failure):
    state = latest(1, stereo=0)
    if failure == "missing":
        del state[path(2, "autoset")]
    elif failure == "still-on":
        state[path(1, "autoset")] = ("i", (1,))
    elif failure == "malformed":
        state[path(2, "autoset")] = ("i", (False,))
    if failure == "request":
        window = ReceivePortError(errno.EIO, "fresh off request lost")
    elif failure == "receive":
        window = [[ReceivePortError(errno.EIO, "fresh off delivery lost")]]
    else:
        window = [reports(state)]
    backend = GainBackend([window])
    with pytest.raises(WriteFailed) as failed:
        routing.apply_routing(configured(1, flag=False, routed=True), backend,
                              observed=latest(1, own=1, partner=0, stereo=0))
    assert failed.value.written == ("/input/1/autoset",)
    assert failed.value.unwritten == ("/input/1/stereo", "/output/5/stereo",
                                       "/mix/5/input/1", "/input/1/gain")
    assert backend.sent == [("/input/1/autoset", "i", (0,))]
    assert failed.value.refused is (failure not in ("request", "receive"))


def test_cancel_after_off_reports_partial_write_without_links_or_gain():
    stopped = {"stop": False}
    backend = GainBackend(stop_after_off=stopped)
    with pytest.raises(WriteFailed, match="stop requested") as cancelled:
        routing.apply_routing(configured(1, flag=False, routed=True), backend,
                              observed=latest(1, own=1, partner=0, stereo=0),
                              should_stop=lambda: stopped["stop"])
    assert cancelled.value.written == ("/input/1/autoset",)
    assert cancelled.value.unwritten == ("/input/1/stereo", "/output/5/stereo",
                                          "/mix/5/input/1", "/input/1/gain")
    assert backend.dumps == 0
    assert backend.sent == [("/input/1/autoset", "i", (0,))]


def test_cancel_during_fresh_prerequisite_read_sends_nothing():
    stopped = {"stop": False}
    backend = GainBackend([[reports(latest(1))]])
    original = backend.messages

    def messages(timeout):
        yield from original(timeout)
        stopped["stop"] = True

    backend.messages = messages
    with pytest.raises(OSError, match="stop requested"):
        routing.apply_routing(configured(1), backend, should_stop=lambda: stopped["stop"])
    assert backend.sent == []


def test_cancel_during_fresh_off_read_retains_off_as_written_and_refuses_every_pending_phase():
    stopped = {"stop": False}
    backend = GainBackend([[reports(latest(1, stereo=0))]])
    original = backend.messages

    def messages(timeout):
        yield from original(timeout)
        stopped["stop"] = True

    backend.messages = messages
    with pytest.raises(WriteFailed, match="stop requested") as cancelled:
        routing.apply_routing(configured(1, flag=False, routed=True), backend,
                              observed=latest(1, own=1, partner=0, stereo=0),
                              should_stop=lambda: stopped["stop"])
    assert cancelled.value.written == ("/input/1/autoset",)
    assert cancelled.value.unwritten == ("/input/1/stereo", "/output/5/stereo",
                                          "/mix/5/input/1", "/input/1/gain")
    assert backend.dumps == 1
    assert backend.sent == [("/input/1/autoset", "i", (0,))]


def test_partial_gain_transport_failure_keeps_off_and_sent_gain_in_exact_order():
    backend = GainBackend([[reports(latest(1))], [reports(latest(1))]], fail_send=(2, 1))
    config = configured(1, flag=False, partner_gain=True, unrelated=True)
    with pytest.raises(WriteFailed, match="transport failure") as failed:
        routing.apply_routing(config, backend, observed=latest(1, own=1, partner=1))
    assert failed.value.written == ("/input/1/autoset", "/input/1/gain")
    assert failed.value.unwritten == ("/input/2/gain", "/input/5/gain")
    assert not failed.value.refused
    assert backend.sent == [("/input/1/autoset", "i", (0,)), ("/input/1/gain", "f", (6.,))]


@pytest.mark.parametrize("channel", [1, 2, 3, 4])
@pytest.mark.parametrize("intent", [ApplyIntent.REPAIR, ApplyIntent.RECONCILE])
@pytest.mark.parametrize("bad_last", [False, True])
def test_unrelated_pin_repair_cannot_reset_autoset_owned_gain(channel, intent, bad_last):
    state = latest(channel, own=1, partner=1)
    if bad_last:
        state = latest(channel)
    observed = reports(state)
    if bad_last:
        observed.append((path(channel, "autoset"), "i", (False,)))

    def feedback(sent):
        applied = ("/input/5/gain", "f", (12.,)) in sent
        return observed + [(path(channel, "gain"), "f", (0.,)),
                           ("/input/5/gain", "f", (12. if applied else 0.,))]

    backend = RecordingBackend(feedback)
    config = configured(channel, unrelated=True)
    if intent is ApplyIntent.REPAIR:
        assert verify.verify_and_repair(config, backend)
    else:
        assert verify.reconcile_now(config, "unrelated PIN repair", backend)
    assert backend.sent == [("/input/5/gain", "f", (12.,))]


def test_retention_preserves_true_wanted_gain_mismatch_in_profile_outcome():
    config = configured(1, flag=True)
    backend = RecordingBackend(lambda _sent: reports(latest(1, own=1, partner=1))
                               + [("/input/1/gain", "f", (2.,))])
    result = verify.verify_routing(verify.expected_registers(config), backend,
                                   timeout=.03, device_model=UCX2,
                                   dependency_paths=("/input/1/autoset", "/input/2/autoset",
                                                     "/input/1/stereo"))
    assert result.confirmed == ["/input/1/autoset"]
    assert result.mismatched == ["/input/1/gain"]
    assert application_plan(config, ApplyIntent.REPAIR, observed=result.latest).writes == ()
    outcome = profiles._check("autoset", SimpleNamespace(config=config, verify=lambda: result))
    assert outcome.state == APPLIED_MISMATCHED
    assert outcome.mismatched == outcome.unverified == ("/input/1/gain",)
    assert outcome.read_back
    assert not outcome.persisted


@pytest.mark.parametrize("channel", [1, 2, 3, 4])
@pytest.mark.parametrize("partner", [False, True])
def test_explicit_gain_pin_declared_on_conflict_precedes_desk_identity_lock_and_connect(
        monkeypatch, channel, partner):
    left, right = pair(channel)
    flag_channel = (right if channel == left else left) if partner else channel
    config = Config(channels=[ChannelSetting("input", channel, "gain", 6.),
                              ChannelSetting("input", flag_channel, "autoset", True)],
                    policies={("input", "gain"): PIN})
    calls = []

    def forbidden(*_args, **_kwargs):
        calls.append("external operation")
        raise AssertionError("static conflict must precede identity/lock/connect/BEGIN")

    monkeypatch.setattr(desk_mod, "_target", forbidden)
    monkeypatch.setattr(desk_mod, "device_lock", forbidden)
    monkeypatch.setattr(desk_mod, "connect_backend", forbidden)
    with pytest.raises(ConfigError, match="AutoSet"), desk_mod.open_desk(config):
        pytest.fail("invalid declared ownership must not open a Desk")
    assert calls == []


@pytest.mark.parametrize("channel", [1, 2, 3, 4])
@pytest.mark.parametrize("partner", [False, True])
def test_parser_rejects_the_same_explicit_gain_pin_declared_on_conflict(
        tmp_path, channel, partner):
    left, right = pair(channel)
    flag_channel = (right if channel == left else left) if partner else channel
    sections = {channel: ["gain = 6"]}
    sections.setdefault(flag_channel, []).append("autoset = true")
    text = "[device]\nname = Fireface UCX II\n"
    text += "".join("[input:%d]\n%s\n" % (number, "\n".join(settings))
                    for number, settings in sections.items())
    text += "[pin]\ninput.gain = pin\n"
    filename = tmp_path / "routing.conf"
    filename.write_text(text)
    with pytest.raises(ConfigError, match="AutoSet"):
        config_mod.load_config(filename)


@pytest.mark.parametrize("fresh_link", ["linked", "malformed", "missing"])
def test_fresh_off_link_feedback_revokes_older_confirmed_mono_before_mix_and_retained_ms(
        fresh_link):
    config = Config(routes=[Route("mono", input=(1,), output=(1,), level=-24.)], channels=[
        ChannelSetting("input", 1, "autoset", False),
        ChannelSetting("input", 1, "msproc", True),
        ChannelSetting("input", 2, "msproc", False),
    ], policies={("input", "autoset"): PIN, ("input", "msproc"): PIN})
    fresh = latest(1, stereo=1)
    if fresh_link == "malformed":
        fresh["/input/1/stereo"] = ("i", (False,))
    elif fresh_link == "missing":
        del fresh["/input/1/stereo"]
    backend = GainBackend([[reports(fresh)], [reports(fresh)]])
    with pytest.raises(WriteFailed) as refused:
        routing.apply_routing(config, backend, intent=ApplyIntent.REPAIR,
                              leave_alone=("/input/2/msproc",),
                              confirmed=("/input/1/stereo", "/output/1/stereo"),
                              observed=latest(1, own=1, partner=0, stereo=0))
    assert refused.value.written == ("/input/1/autoset",)
    assert refused.value.unwritten == ("/mix/1/input/1", "/input/1/msproc")
    assert backend.sent == [("/input/1/autoset", "i", (0,))]


@pytest.mark.parametrize("fresh_link", ["mono", "malformed", "missing"])
def test_selected_input_join_requires_fresh_matching_link_before_mix_and_pin_gain(fresh_link):
    config = configured(1, routed=True)
    fresh = latest(1, stereo=0)
    if fresh_link == "malformed":
        fresh["/input/1/stereo"] = ("i", (False,))
    elif fresh_link == "missing":
        del fresh["/input/1/stereo"]
    backend = GainBackend([[reports(fresh)]])
    with pytest.raises(WriteFailed) as refused:
        routing.apply_routing(config, backend, intent=ApplyIntent.INITIAL,
                              observed=latest(1, stereo=0))
    assert refused.value.written == ("/input/1/stereo", "/output/5/stereo")
    assert refused.value.unwritten == ("/mix/5/input/1", "/input/1/gain")
    assert backend.sent == [("/input/1/stereo", "i", (1,)),
                            ("/output/5/stereo", "i", (1,))]
    assert backend.dumps == 1


def test_selected_input_join_with_fresh_off_and_matching_link_keeps_gain_pin_without_flag_write():
    config = configured(1, routed=True)
    backend = GainBackend([[reports(latest(1, stereo=1))]])
    routing.apply_routing(config, backend, intent=ApplyIntent.INITIAL,
                          observed=latest(1, stereo=0))
    assert backend.sent == [("/input/1/stereo", "i", (1,)),
                            ("/output/5/stereo", "i", (1,)),
                            ("/mix/5/input/1", "fi", (-24., 0)),
                            ("/input/1/gain", "f", (6.,))]
    assert backend.dumps == 1
