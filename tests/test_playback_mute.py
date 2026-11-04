"""Playback mute: ownership, ordering and honest readback."""

from dataclasses import replace
from types import SimpleNamespace

import pytest
from backend_doubles import RecordingBackend

from oscmix_desk import config as config_mod
from oscmix_desk import desk, dump, profiles, reconcile, routing, session, streams, verify
from oscmix_desk import prechecks as prechecks_mod
from oscmix_desk.devices import FF802, UCX2
from oscmix_desk.errors import ConfigError, WriteFailed
from oscmix_desk.model import ChannelSetting, Config, Route
from oscmix_desk.observation import Observation
from oscmix_desk.outcome import APPLIED_MISMATCHED, APPLIED_UNVERIFIED
from oscmix_desk.playback import retained_pairs, validate_playback_mute
from oscmix_desk.registers import BOOL, PIN, REESTABLISHED, REMEMBER, register_at


def complete_routes(left=1):
    return tuple(
        Route("out%d" % out, playback=(left, left + 1), output=(out, out + 1), level=-65.0)
        for out in range(1, 21, 2)
    )


def mute_config(value=1, left=1, *, pinned=False, routes=None, **changes):
    return Config(
        channels=(
            ChannelSetting("playback", left, "mute", value),
            ChannelSetting("playback", left + 1, "mute", value),
        ),
        routes=complete_routes(left) if value == 0 and routes is None else (routes or ()),
        policies={("playback", "mute"): PIN} if pinned else {},
        **changes,
    )


def config_text(config):
    text = ""
    for route in config.routes:
        text += "[route:%s]\nplayback=%s\noutput=%s\nlevel=%s\n" % (
            route.name,
            "/".join(map(str, route.playback)),
            "/".join(map(str, route.output)),
            route.level,
        )
    for setting in config.channels:
        text += "[%s:%d]\n%s=%s\n" % (
            setting.family,
            setting.channel,
            setting.option,
            setting.value,
        )
    return text


@pytest.mark.parametrize("channel", range(1, 21))
def test_mute_row_has_no_device_or_cold_verification(channel):
    row = register_at(UCX2, "/playback/%d/mute" % channel)
    assert row.domain is BOOL
    assert row.tags == "i"
    assert row.verify is REESTABLISHED
    assert row.policy is REMEMBER
    assert not verify.register_ever_reported(row.path(ch=channel), UCX2)
    assert not verify.register_promptly_reported(row.path(ch=channel), UCX2)
    assert register_at(FF802, row.path(ch=channel)) is None
    assert register_at(UCX2, "/playback/21/mute") is None


@pytest.mark.parametrize("left", range(1, 21, 2))
@pytest.mark.parametrize("value", [0, 1])
def test_parser_and_pin_override_share_pair_validation(tmp_path, left, value):
    path = tmp_path / "desk.conf"
    path.write_text(config_text(mute_config(value, left)) + "[pin]\nplayback.mute=pin\n")
    parsed = config_mod.load_config(path)
    assert parsed.policies[("playback", "mute")] is PIN
    assert [entry.args for entry in reconcile.desired(parsed) if entry.path.endswith("/mute")] == [
        (value,),
        (value,),
    ]


@pytest.mark.parametrize("value", [False, True, 0, 1, 0.0, 1.0])
def test_programmatic_bool_contract_and_final_desired(value):
    config = mute_config(value)
    mutes = [entry for entry in reconcile.desired(config) if entry.path.endswith("/mute")]
    assert [entry.args for entry in mutes] == [(int(value),), (int(value),)]


@pytest.mark.parametrize("value", [2, -1, 0.5, float("nan"), float("inf"), "1", None])
def test_invalid_programmatic_bool_refused_before_any_connection(monkeypatch, value):
    operation = desk.Desk(mute_config(value))
    monkeypatch.setattr(operation, "_lock_device", lambda: pytest.fail("lock acquired"))
    with pytest.raises(ConfigError):
        operation._open()
    assert operation._backend is None


@pytest.mark.parametrize(
    ("name", "usb"),
    [
        ("Fireface 802", "2a39:3fb0"),
        ("Unknown interface", "2a39:3fd9"),
        ("Fireface UCX II", "ffff:ffff"),
    ],
)
def test_unmodelled_or_wrong_device_refused_before_io(name, usb):
    with pytest.raises(ConfigError, match="register model"):
        reconcile.desired(mute_config(device_name=name, usb_id=usb))


@pytest.mark.parametrize("channel", [0, 21, True, 1.0, "1"])
def test_invalid_playback_channel_refused(channel):
    config = Config(channels=(ChannelSetting("playback", channel, "mute", True),))
    with pytest.raises(ConfigError):
        reconcile.desired(config)


@pytest.mark.parametrize(
    ("family", "option"),
    [("playbak", "mute"), ("playback", "volume"), ("playback", "stereo"), ("playback", "unknown")],
)
def test_wrong_channel_family_or_option_refused(family, option):
    config = Config(channels=(ChannelSetting(family, 1, option, True),))
    with pytest.raises(ConfigError):
        reconcile.application_plan(config, reconcile.ApplyIntent.INITIAL)


@pytest.mark.parametrize("channel", [1, 2, 3, 4, 19, 20])
def test_undeclared_partner_is_not_silently_owned(channel):
    config = Config(channels=(ChannelSetting("playback", channel, "mute", 1),))
    with pytest.raises(ConfigError, match="both explicit partners"):
        reconcile.desired(config)


def test_inconsistent_partner_flags_refused_even_with_a_mono_route():
    config = Config(
        channels=(
            ChannelSetting("playback", 1, "mute", 1),
            ChannelSetting("playback", 2, "mute", 0),
        ),
        routes=(Route("mono", playback=(1,), output=(1,)),),
    )
    with pytest.raises(ConfigError, match="must agree"):
        reconcile.desired(config)


@pytest.mark.parametrize("missing_output", range(1, 21, 2))
def test_every_omitted_output_pair_prevents_unmute(missing_output):
    routes = tuple(route for route in complete_routes() if route.output[0] != missing_output)
    with pytest.raises(ConfigError, match="complete explicit matrix"):
        reconcile.desired(mute_config(0, routes=routes))


def test_on_can_attenuate_without_matrix_but_off_cannot():
    assert (
        len(reconcile.application_plan(mute_config(), reconcile.ApplyIntent.INITIAL).writes) == 2
    )
    with pytest.raises(ConfigError, match="complete explicit matrix"):
        reconcile.desired(mute_config(0, routes=()))


def test_only_final_off_enters_readback_and_prerequisite_is_visible():
    config = mute_config(0)
    wanted = reconcile.application_plan(config, reconcile.ApplyIntent.EXPLICIT)
    on = [write for write in wanted.writes if write.phase is reconcile.Phase.PLAYBACK_MUTE_ON]
    off = [write for write in wanted.writes if write.phase is reconcile.Phase.PLAYBACK_MUTE_OFF]
    assert [write.args for write in on] == [(1,), (1,)]
    assert all(write.reason is reconcile.WriteReason.PREREQUISITE for write in on)
    assert [write.args for write in off] == [(0,), (0,)]
    assert wanted.messages()[:2] == tuple(write.message() for write in on)
    assert wanted.messages()[-2:] == tuple(write.message() for write in off)
    expected = verify.expected_registers(config)
    assert expected["/playback/1/mute"] == expected["/playback/2/mute"] == ("i", (0,))


def test_dry_run_prints_exact_application_plan(capsys):
    config = mute_config(0)
    session._print_dry_run(None, config)
    lines = [
        line for line in capsys.readouterr().out.splitlines() if line.startswith("would send:")
    ]
    wanted = reconcile.application_plan(config, reconcile.ApplyIntent.INITIAL).messages()
    assert lines == [
        "would send: %s ,%s %s" % (path, tags, " ".join(map(str, args)))
        for path, tags, args in wanted
    ]


@pytest.mark.parametrize("intent", [reconcile.ApplyIntent.REPAIR, reconcile.ApplyIntent.RECONCILE])
@pytest.mark.parametrize("value", [0, 1])
def test_remember_mute_protects_pair_and_matrix_and_marks_pin_omissions(intent, value):
    config = mute_config(value, routes=complete_routes())
    plan = reconcile.application_plan(config, intent)
    assert plan.writes == ()
    assert plan.confirmed == ()
    assert "/playback/1/mute" in plan.retained
    assert "/playback/2/mute" in plan.retained
    assert "/playback/1/stereo" in plan.retained
    assert "/mix/1/playback/1" in plan.retained
    assert "/output/1/stereo" in plan.retained


def test_unknown_retained_partner_also_protects_a_route_only_config():
    config = Config(routes=complete_routes())
    plan = reconcile.application_plan(
        config, reconcile.ApplyIntent.INITIAL, leave_alone=("/playback/2/mute",)
    )
    assert plan.writes == ()
    assert "/playback/1/mute" in plan.retained
    assert retained_pairs(("/playback/20/mute", "invalid", "/playback/bad/mute")) == {19}


def test_retention_guard_cannot_be_bypassed_with_a_direct_selected_write():
    config = Config(routes=complete_routes())
    selected = reconcile.application_plan(config, reconcile.ApplyIntent.INITIAL).writes
    problem = reconcile.retention_problem(config, selected, ("/playback/2/mute",), ())
    assert "unobservable retained playback mute" in problem


@pytest.mark.parametrize("intent", [reconcile.ApplyIntent.REPAIR, reconcile.ApplyIntent.RECONCILE])
def test_pin_can_reestablish_full_owned_off_sequence(intent):
    config = mute_config(0, pinned=True)
    confirmed = tuple(
        entry.path for entry in reconcile.desired(config) if entry.phase is reconcile.Phase.LINK
    )
    wanted = reconcile.application_plan(config, intent, confirmed=confirmed)
    assert not wanted.links()
    assert len(wanted.mix()) == 10
    assert wanted.messages()[0] == ("/playback/1/mute", "i", (1,))
    assert wanted.messages()[-1] == ("/playback/2/mute", "i", (0,))


def test_omitted_mix_never_receives_a_temporary_or_final_toggle():
    config = mute_config(0, pinned=True)
    with pytest.raises(ValueError, match="cannot bypass retained"):
        reconcile.application_plan(
            config, reconcile.ApplyIntent.INITIAL, leave_alone=("/mix/1/playback/1",)
        )


@pytest.mark.parametrize("flag", [0, 1])
def test_even_a_typed_device_echo_cannot_verify_an_unobservable_flag(flag):
    path = "/playback/1/mute"
    observation = Observation({path: ("i", (flag,))}, UCX2)
    observation.absorb((path, "i", (flag,)))
    assert observation.confirmed == observation.mismatched == set()
    assert observation.unobserved == {path}
    plan = reconcile.plan(
        (reconcile.Entry(path, "i", (flag,), reconcile.Phase.PLAYBACK_MUTE_ON),),
        {path: (flag,)},
        UCX2,
    )
    assert not plan.confirmed
    assert plan.unverifiable == (path,)


def test_dump_names_mute_as_unrecoverable_and_never_invents_a_flag():
    assert "/playback/{ch}/mute" in dump.unrecoverable(UCX2)
    assert dump.channels_from_observed({"/playback/1/mute": (1,)}, UCX2) == ()
    text = dump.render_config(Config(), UCX2)
    assert "/playback/{ch}/mute" in text
    assert "Playback mute remains unverified" in text


@pytest.mark.parametrize("fake_confirmation", [False, True])
def test_actual_readback_keeps_public_outcome_unverified(monkeypatch, fake_confirmation):
    config = mute_config()

    class Backend:
        def request_dump(self):
            pass

        def messages(self, _timeout):
            return iter(
                [
                    (path, tags, args)
                    for path, (tags, args) in verify.expected_registers(config).items()
                ]
                if fake_confirmation
                else ()
            )

    operation = SimpleNamespace(
        config=config,
        verify=lambda: verify.verify_routing(
            verify.expected_registers(config), Backend(), timeout=0.002, device_model=UCX2
        ),
    )
    outcome = profiles._check("mute profile", operation)
    assert outcome.state is APPLIED_UNVERIFIED
    assert outcome.read_back
    assert outcome.unobserved == outcome.unverifiable == ("/playback/1/mute", "/playback/2/mute")
    assert not outcome.mismatched


def test_suppressed_pin_link_mismatch_remains_visible_in_outcome():
    config = mute_config(routes=complete_routes())
    observed = verify.VerifyResult(
        [],
        ["/output/1/stereo"],
        [path for path in verify.expected_registers(config) if path != "/output/1/stereo"],
    )
    operation = SimpleNamespace(config=config, verify=lambda: observed)
    outcome = profiles._check("retained mute", operation)
    assert outcome.state is APPLIED_MISMATCHED
    assert outcome.mismatched == ("/output/1/stereo",)
    assert "/playback/1/mute" in outcome.unverifiable


@pytest.mark.parametrize("operation", ["repair", "reconcile"])
@pytest.mark.parametrize("reported", [None, 0])
def test_retained_output_pin_unknown_or_wrong_cannot_finish_verified(
    monkeypatch, operation, reported
):
    config = mute_config(routes=complete_routes())
    reports = [(path, tags, args) for path, (tags, args)
               in verify.expected_registers(config).items()
               if path != "/output/1/stereo"]
    if reported is not None:
        reports.append(("/output/1/stereo", "i", (reported,)))
    backend = RecordingBackend(lambda _sent: reports)
    monkeypatch.setattr(verify, "VERIFY_TIMEOUT", .002)
    monkeypatch.setattr(verify, "wait_unless_stopped", lambda *args: False)
    completed = (verify.verify_and_repair(config, backend) if operation == "repair"
                 else verify.reconcile_now(config, "retained mute regression", backend))
    assert completed is False
    assert backend.sent == []


@pytest.mark.parametrize(
    ("rate", "channels", "altset", "target", "allowed"),
    [
        (48000, 20, 1, 19, True),
        (96000, 20, 1, 17, False),
        (192000, 14, 3, 13, True),
        (192000, 14, 3, 15, False),
        (192000, 20, 1, 1, False),
    ],
)
def test_channel_only_playback_uses_measured_live_capacity(
    rate, channels, altset, target, allowed
):
    config = mute_config(left=target)
    assert (
        streams.playback_problem(config, streams.PlaybackMode(rate, channels, altset)) is None
    ) is allowed


def test_channel_only_guard_checks_and_refuses_mid_apply_change(monkeypatch):
    states = iter(
        (
            streams.PlaybackState(2, "24216011", None),
            streams.PlaybackState(2, "24216011", streams.PlaybackMode(48000, 20, 1)),
        )
    )
    monkeypatch.setattr(streams, "read_playback", lambda config, proc: next(states))
    guard = streams.PlaybackGuard(mute_config())
    guard.check()
    with pytest.raises(OSError, match="mode or identity changed"):
        guard.check()


def phase_config():
    config = mute_config(0)
    return replace(
        config,
        channels=config.channels
        + (
            ChannelSetting("input", 3, "autoset", 0),
            ChannelSetting("input", 5, "gain", 2.0),
            ChannelSetting("input", 1, "autoset", 1),
        ),
    )


class PhaseBackend:
    def __init__(self, fail_phase=None):
        self.sent = []
        self.bursts = []
        self.fail_phase = fail_phase

    def send(self, messages):
        burst = list(messages)
        self.bursts.append(burst)
        if len(self.bursts) == self.fail_phase:
            self.sent.extend(burst[:1])
            raise WriteFailed(
                OSError("injected phase failure"),
                [w[0] for w in burst[:1]],
                [w[0] for w in burst[1:]],
            )
        self.sent.extend(burst)


def phase_dependencies():
    return {"/input/%d/autoset" % ch: ("i", (0,)) for ch in range(1, 5)} | {
        "/input/1/stereo": ("i", (0,)),
        "/input/3/stereo": ("i", (0,)),
    }


@pytest.fixture
def phase_guards(monkeypatch):
    calls = []
    monkeypatch.setattr(routing.PlaybackGuard, "check", lambda self: calls.append("guard"))
    monkeypatch.setattr(prechecks_mod, "read_dependencies", lambda *_a, **_k: phase_dependencies())
    monkeypatch.setattr(
        routing, "_cross_the_barrier", lambda *args, **kwargs: calls.append("barrier")
    )
    return calls


def test_all_seven_phases_use_the_pure_order_and_preserve_off_confirmation(phase_guards):
    config = phase_config()
    backend = PhaseBackend()
    routing.apply_routing(config, backend, observed=phase_dependencies())
    wanted = reconcile.application_plan(config, reconcile.ApplyIntent.INITIAL)
    assert backend.sent == list(wanted.messages())
    assert len(backend.bursts) == 7
    assert phase_guards.count("guard") == 7
    assert phase_guards.count("barrier") == 1
    assert backend.bursts[0] == [("/playback/1/mute", "i", (1,)), ("/playback/2/mute", "i", (1,))]
    assert backend.bursts[-1] == [("/playback/1/mute", "i", (0,)), ("/playback/2/mute", "i", (0,))]


@pytest.mark.parametrize("failing_phase", range(1, 8))
def test_every_phase_failure_preserves_exact_partial_submission_fields(
    phase_guards, failing_phase
):
    config = phase_config()
    backend = PhaseBackend(failing_phase)
    wanted = reconcile.application_plan(config, reconcile.ApplyIntent.INITIAL)
    phases = list(reconcile.Phase)
    expected_done = [w.path for w in wanted.writes if w.phase < phases[failing_phase - 1]]
    current = [w.path for w in wanted.writes if w.phase == phases[failing_phase - 1]]
    expected_pending = current[1:] + [
        w.path for w in wanted.writes if w.phase > phases[failing_phase - 1]
    ]
    with pytest.raises(WriteFailed, match="injected phase failure") as failure:
        routing.apply_routing(config, backend, observed=phase_dependencies())
    assert failure.value.written == tuple(expected_done + current[:1])
    assert failure.value.unwritten == tuple(expected_pending)
    assert len(backend.bursts) == failing_phase


def test_existing_route_only_config_acquires_no_mute_or_ownership_requirement():
    config = Config(routes=(Route("old partial", playback=(1, 2), output=(1, 2)),))
    assert reconcile.application_plan(config, reconcile.ApplyIntent.INITIAL).messages() == (
        ("/playback/1/stereo", "i", (1,)),
        ("/output/1/stereo", "i", (1,)),
        ("/mix/1/playback/1", "fi", (0.0, 0)),
    )


def test_unknown_legacy_option_remains_ignored_with_a_valid_known_family():
    config = Config(channels=(ChannelSetting("input", 13, "future-option", 1),))
    assert reconcile.desired(config) == ()


def test_valid_pair_parser_refuses_unsupported_device_even_in_tolerant_mode(tmp_path):
    path = tmp_path / "desk.conf"
    path.write_text(
        "[device]\nname=Fireface 802\nusb-id=2a39:3fb0\n"
        "[playback:1]\nmute=1\n[playback:2]\nmute=1\n"
    )
    with pytest.raises(ConfigError, match="no playback register model"):
        config_mod.load_config(path)


def test_duplicate_conflicting_mutes_and_route_link_conflicts_are_refused():
    config = mute_config()
    with pytest.raises(ConfigError, match="conflicting playback"):
        validate_playback_mute(
            replace(config, channels=config.channels + (ChannelSetting("playback", 1, "mute", 0),))
        )
    with pytest.raises(ConfigError, match="routes disagree"):
        validate_playback_mute(
            replace(
                config,
                routes=(
                    Route("mono", playback=(1,), output=(1,)),
                    Route("pair", playback=(1, 2), output=(1, 2)),
                ),
            )
        )


@pytest.mark.parametrize("value", [2, -1, "false", None])
def test_invalid_programmatic_route_link_refuses_before_temporary_mute(value):
    route = Route("bad link", playback=(1, 2), output=(1, 2), stereo=value)
    with pytest.raises(ConfigError, match="stereo must be boolean"):
        reconcile.application_plan(mute_config(routes=(route,)), reconcile.ApplyIntent.INITIAL)


@pytest.mark.parametrize("pinned", [False, True])
@pytest.mark.parametrize("value", [0, 1])
def test_matrix_repair_preserves_owned_mute_phases_and_remember_retention(
    phase_guards, pinned, value
):
    config = mute_config(value, pinned=pinned, routes=complete_routes())
    confirmed = tuple(entry.path for entry in reconcile.desired(config)
                      if entry.phase is reconcile.Phase.LINK)
    wanted = reconcile.matrix_repair_plan(config, confirmed)
    backend = PhaseBackend()
    routing.send_mix(config, backend, confirmed=confirmed)
    assert backend.sent == list(wanted.messages())
    if pinned:
        assert backend.sent[:2] == [("/playback/1/mute", "i", (1,)),
                                   ("/playback/2/mute", "i", (1,))]
        if value == 0:
            assert backend.sent[-2:] == [("/playback/1/mute", "i", (0,)),
                                        ("/playback/2/mute", "i", (0,))]
        else:
            assert not any(path.endswith("/mute") and args == (0,)
                           for path, _tags, args in backend.sent)
    else:
        assert backend.sent == []


@pytest.mark.parametrize("container", [tuple, list, set, frozenset])
def test_retained_mute_ownership_accepts_ordered_and_unordered_collections(container):
    config = mute_config(0)
    retained = container(["/playback/2/mute"])
    protected = reconcile.protected_entries(config, retained)
    assert retained_pairs(retained) == {1}
    assert protected == reconcile.protected_entries(config, ("/playback/2/mute",))
    assert {"/playback/1/mute", "/playback/2/mute", "/playback/1/stereo",
            "/playback/2/stereo", "/mix/1/playback/1"} <= set(protected)
