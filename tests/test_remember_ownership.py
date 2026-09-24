"""Later repair/reconcile never regain write authority over REMEMBER."""

import itertools
import logging

import pytest
from backend_doubles import RecordingBackend

from oscmix_desk import config as config_mod
from oscmix_desk import routing, verify
from oscmix_desk.backend import OSCMIX
from oscmix_desk.errors import WriteFailed
from oscmix_desk.model import ChannelSetting, Config, Route
from oscmix_desk.reconcile import ApplyIntent
from oscmix_desk.registers import PIN, REMEMBER


@pytest.fixture(autouse=True)
def observation_clock(monkeypatch):
    ticks = itertools.count(0, .001)
    monkeypatch.setattr(verify.time, "monotonic", lambda: next(ticks))
    monkeypatch.setattr(verify.time, "sleep", lambda _: None)
    monkeypatch.setattr(verify, "VERIFY_TIMEOUT", .1)
    monkeypatch.setattr(routing, "LINK_ECHO_TIMEOUT", .1)


def route_config(**kwargs):
    return Config(routes=[Route(name="phones", playback=(1, 2), output=(5, 6),
                                volume=-6.)], **kwargs)


def recorded(monkeypatch, reports):
    backend = RecordingBackend(reports)
    backend.traits = OSCMIX
    monkeypatch.setattr(routing, "loopback", lambda *_: backend)
    monkeypatch.setattr(verify, "loopback", lambda *_: backend)
    return backend


LINKS = [("/playback/1/stereo", "i", (1,)), ("/output/5/stereo", "i", (1,))]


@pytest.mark.parametrize("value", [None, -30., -6., float("nan")])
def test_reconcile_preserves_every_remember_volume(monkeypatch, value):
    reports = [] if value is None else [("/output/5/volume", "f", (value,))]
    backend = RecordingBackend(lambda _: reports)
    config = Config(channels=[ChannelSetting("output", 5, "volume", -6.)])
    assert verify.reconcile_now(config, "REMEMBER regression", backend=backend)
    assert backend.sent == []


@pytest.mark.parametrize("value", [None, -30., -6., float("nan")])
@pytest.mark.parametrize("intent", [ApplyIntent.REPAIR, ApplyIntent.RECONCILE])
def test_other_pin_repair_cannot_reset_either_route_volume(monkeypatch, value, intent):
    def reports(sent):
        gain = 6. if any(path == "/input/3/gain" for path, _, _ in sent) else 0.
        volumes = [] if value is None else [("/output/5/volume", "f", (value,))]
        return LINKS + volumes + [("/input/3/gain", "f", (gain,))]
    backend = recorded(monkeypatch, reports)
    config = route_config(channels=[ChannelSetting("input", 3, "gain", 6.)])
    if intent is ApplyIntent.REPAIR:
        verify.verify_and_repair(config)
    else:
        assert verify.reconcile_now(config, "reload", backend=backend)
    assert ("/input/3/gain", "f", (6.,)) in backend.sent
    assert any(path.startswith("/mix/") for path, _, _ in backend.sent)
    assert not any(path.endswith("/volume") for path, _, _ in backend.sent)
    assert not any(path.endswith("/stereo") for path, _, _ in backend.sent)


@pytest.mark.parametrize("intent", list(ApplyIntent))
@pytest.mark.parametrize("pinned", [False, True])
def test_starting_values_belong_only_to_initial_or_explicit_apply(monkeypatch, intent, pinned):
    backend = recorded(monkeypatch, lambda _: LINKS)
    config = route_config(policies={("output", "volume"): PIN} if pinned else {})
    routing.apply_routing(config, 9000, backend=backend, intent=intent,
                          confirmed=[path for path, _, _ in LINKS])
    expected = pinned or intent in (ApplyIntent.INITIAL, ApplyIntent.EXPLICIT)
    assert [p for p, _, _ in backend.sent if p.endswith("/volume")] == (
        ["/output/5/volume", "/output/6/volume"] if expected else [])


@pytest.mark.parametrize("pinned", [False, True])
@pytest.mark.parametrize("blind", [False, True])
def test_mix_reapply_obeys_effective_volume_policy(monkeypatch, pinned, blind):
    backend = recorded(monkeypatch, lambda _: LINKS)
    config = route_config(policies={("output", "volume"): PIN} if pinned else {})
    if blind:
        routing.blind_reapply_mix(config)
    else:
        routing.send_mix(config)
    assert backend.sent == [("/mix/5/playback/1", "fi", (0., 0))] + (
        [("/output/5/volume", "f", (-6.,)), ("/output/6/volume", "f", (-6.,))]
        if pinned else [])
    assert backend.dumps == int(blind)


@pytest.mark.parametrize("value", [None, 0, 1, 2])
def test_retained_structural_link_requires_confirmation(tmp_path, monkeypatch, value):
    path = tmp_path / "routing.conf"
    path.write_text("[route:phones]\nplayback=1/2\noutput=5/6\n"
                    "[pin]\noutput.stereo=remember\n")
    config = config_mod.load_config(path)
    assert config.policies[("output", "stereo")] == REMEMBER
    reports = LINKS[:1] + ([] if value is None else [("/output/5/stereo", "i", (value,))])
    backend = recorded(monkeypatch, lambda _: reports)
    if value == 1:
        assert verify.reconcile_now(config, "retained structural link", backend=backend)
        assert backend.sent == [("/mix/5/playback/1", "fi", (0., 0))]
    else:
        with pytest.raises(WriteFailed, match="retained link state") as failure:
            verify.reconcile_now(config, "retained structural link", backend=backend)
        assert failure.value.written == ()
        assert failure.value.unwritten == ("/mix/5/playback/1",)
        assert backend.sent == []


@pytest.mark.parametrize("value", [None, 0, 2])
def test_repairing_a_link_cannot_indirectly_reset_retained_pair(monkeypatch, value):
    reports = LINKS[:1] + ([] if value is None else [("/output/5/stereo", "i", (value,))])
    backend = recorded(monkeypatch, lambda _: reports)
    with pytest.raises(WriteFailed, match="retained partner") as failure:
        verify.reconcile_now(route_config(), "link needs repair", backend=backend)
    assert failure.value.written == ()
    assert failure.value.unwritten == ("/output/5/stereo", "/mix/5/playback/1")
    assert backend.sent == []


@pytest.mark.parametrize("unlinked", [False, True])
def test_scalar_write_cannot_change_a_retained_stereo_partner(monkeypatch, unlinked):
    backend = recorded(monkeypatch, lambda _: [])
    route = (Route(name="mono", playback=(1,), output=(5,)) if unlinked else
             Route(name="stereo", playback=(1, 2), output=(5, 6)))
    config = Config(routes=[route], policies={("output", "volume"): PIN},
                    channels=[ChannelSetting("output", 5, "volume", -6.),
                              ChannelSetting("output", 6, "volume", -6.)])
    confirmed = ["/playback/1/stereo", "/output/5/stereo"]
    if unlinked:
        routing.apply_routing(config, 9000, backend=backend,
                              leave_alone=["/output/6/volume"],
                              intent=ApplyIntent.RECONCILE, confirmed=confirmed)
        assert ("/output/5/volume", "f", (-6.,)) in backend.sent
    else:
        with pytest.raises(WriteFailed, match="retained stereo partner"):
            routing.apply_routing(config, 9000, backend=backend,
                                  leave_alone=["/output/6/volume"],
                                  intent=ApplyIntent.RECONCILE, confirmed=confirmed)
        assert backend.sent == []


@pytest.mark.parametrize(("value", "meaning"), [
    (None, "1 REMEMBER retained without feedback"),
    (float("nan"), "1 REMEMBER invalid feedback"),
    (-30., "1 kept by REMEMBER"),
    (-6., "1 confirmed"),
])
def test_retention_summary_does_not_invent_confirmation(monkeypatch, caplog, value, meaning):
    reports = [] if value is None else [("/output/5/volume", "f", (value,))]
    backend = recorded(monkeypatch, lambda _: reports)
    config = Config(channels=[ChannelSetting("output", 5, "volume", -6.)])
    with caplog.at_level(logging.INFO):
        assert verify.reconcile_now(config, "summary", backend=backend)
    summary = next(record.message for record in caplog.records if "confirmed;" in record.message)
    assert meaning in summary
    assert "0 differing PIN" in summary
    if value != -6.:
        assert "0 confirmed" in summary
    assert backend.sent == []


@pytest.mark.parametrize("reports", [[], [("/output/5/volume", "f", (-30.,))]])
def test_explicit_profile_remains_strict_and_persists_after_unverified_apply(
        tmp_path, monkeypatch, reports):
    from oscmix_desk import outcome, profiles

    target = tmp_path / "profiles" / "quiet.conf"
    target.parent.mkdir()
    target.write_text("[output:5]\nvolume=-6\n")
    backend = recorded(monkeypatch, lambda _: reports)
    result = profiles.switch_profile("quiet", config_path=tmp_path / "routing.conf",
                                     backend=backend)
    assert result.state == outcome.APPLIED_UNVERIFIED
    assert result.unverified == ["/output/5/volume"]
    assert result.persisted
    assert result.read_back
    assert (tmp_path / "active-profile").read_text() == "quiet\n"
    assert backend.sent == [("/output/5/volume", "f", (-6.,))]


def test_full_restart_reasserts_starting_value_after_selective_reconcile(monkeypatch):
    backend = recorded(monkeypatch, lambda _: [("/output/5/volume", "f", (-30.,))])
    config = Config(channels=[ChannelSetting("output", 5, "volume", -6.)])
    routing.apply_routing(config, 9000, backend=backend)
    assert verify.reconcile_now(config, "reload", backend=backend)
    assert backend.sent == [("/output/5/volume", "f", (-6.,))]
    routing.apply_routing(config, 9000, backend=backend)
    assert backend.sent == [("/output/5/volume", "f", (-6.,))] * 2


def test_delayed_volume_feedback_stays_remembered(monkeypatch):
    backend = recorded(monkeypatch, lambda _: [])

    def listen():
        batches = iter([LINKS, [("/output/5/volume", "f", (-30.,))]])

        class Listener:
            def messages(self, _timeout):
                yield from next(batches, [])

            def close(self):
                pass

        return Listener()

    monkeypatch.setattr(backend, "listen", listen)
    assert verify.reconcile_now(route_config(), "delayed feedback", backend=backend)
    assert backend.sent == [("/mix/5/playback/1", "fi", (0., 0))]


def test_reload_waits_for_startup_verification_without_resetting_missing_remember(
        tmp_path, monkeypatch):
    import argparse

    from profile_desk import shared_lock_dir

    from oscmix_desk import reload

    shared_lock_dir(tmp_path, monkeypatch)
    path = tmp_path / "routing.conf"
    path.write_text("[output:5]\nvolume=-6\n")
    config = config_mod.load_config(path)
    backend = recorded(monkeypatch, lambda _: [])
    statuses = []
    monkeypatch.setattr(reload, "sd_notify", statuses.append)
    routing.apply_routing(config, 9000, backend=backend)

    class StartupVerifier:
        alive = True

        def is_alive(self):
            return self.alive

        def join(self, timeout):
            assert timeout > 0
            verify.verify_and_repair(config)
            path.write_text("[output:5]\nvolume=-12\n")
            self.alive = False

    reload._reconcile(argparse.Namespace(config=path), config, {"stop": False},
                      StartupVerifier())
    assert backend.dumps == 2
    assert backend.sent == [("/output/5/volume", "f", (-6.,))]
    assert statuses[-1].startswith("STATUS=running; reconciled")
