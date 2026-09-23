"""ALSA accepting a mode is not evidence that every channel carries audio."""

import json
import logging
from dataclasses import replace
from pathlib import Path

import pytest
from backend_doubles import RecordingBackend, echo_link_flags_only
from profile_desk import GOOD, TRACKING, desk
from support import repo_file

from oscmix_desk import marker, profiles, routing, streams
from oscmix_desk.backend import OSCMIX
from oscmix_desk.errors import WriteFailed
from oscmix_desk.model import Config, Route
from oscmix_desk.outcome import REFUSED, WRITTEN_IN_PART

RECORDED = json.loads(repo_file("tests", "data", "ucx2-playback-modes.json").read_text())["cases"]


def recorded(rate=48000, channels=20):
    return next(row for row in RECORDED if (row["rate"], row["channels"]) == (rate, channels))


def write_card(proc, row, number=2, serial="24216011"):
    card = proc / "asound" / ("card%d" % number)
    params = card / "pcm0p/sub0/hw_params"
    params.parent.mkdir(parents=True, exist_ok=True)
    (card / "usbid").write_text("2a39:3fd9\n")
    (card / "stream0").write_text(row["stream0"].replace("24216011", serial))
    params.write_text(row["hw_params"])
    return card


def routed(channel=1):
    return Config(routes=(Route("main", output=(5, 6), playback=(channel, channel + 1)),))


@pytest.fixture
def proc(tmp_path, monkeypatch):
    root = tmp_path / "proc"
    root.mkdir()
    monkeypatch.setenv("OSCMIX_PROC_ROOT", str(root))
    return root


@pytest.mark.parametrize("row", RECORDED, ids=lambda row: "%d-%d" % (row["rate"], row["channels"]))
def test_measured_modes_keep_their_actual_pcm_parameters_and_known_limits(row):
    mode = streams.parse_playback(row["stream0"], row["hw_params"])
    assert mode.rate == row["rate"]
    assert mode.channels == row["channels"]
    assert mode.altset == {20: 1, 16: 2, 14: 3, 8: 4}[row["channels"]]
    problem = streams.playback_problem(routed(row["channels"] - 1), mode)
    failed = (row["rate"], row["channels"]) in {
        (88200, 20), (96000, 20), (176400, 16), (176400, 20), (192000, 16), (192000, 20),
    }
    assert (problem is not None) is failed


@pytest.mark.parametrize("rate", [88200, 96000])
def test_double_speed_20_channel_stream_does_not_invent_a_half_sized_register_map(rate):
    mode = streams.PlaybackMode(rate, 20, 1)
    assert streams.playback_problem(routed(15), mode) is None
    assert "playback 17/18" in streams.playback_problem(routed(17), mode)
    # A USB stream capacity is not the physical output/address capacity.
    to_digital = replace(routed(), routes=(Route("digital", output=(19, 20), playback=(1, 2)),))
    assert streams.playback_problem(to_digital, mode) is None


def test_a_shorter_usb_stream_has_no_playback_channels_beyond_its_end():
    assert "playback 9/10" in streams.playback_problem(
        routed(9), streams.PlaybackMode(48000, 8, 4))
    assert "not a measured" in streams.playback_problem(
        routed(), streams.PlaybackMode(64000, 8, 4))
    assert "unmeasured USB alternate-setting map" in streams.playback_problem(
        routed(), streams.PlaybackMode(48000, 8, 1))


def test_inactive_pcm_does_not_adopt_an_advertised_rate_or_a_previous_hw_params():
    row = recorded()
    idle = row["stream0"].replace("Status: Running", "Status: Stop")
    assert streams.parse_playback(idle, row["hw_params"]) is None


@pytest.mark.parametrize("kind", [
    "no-playback", "no-status", "duplicate-status", "no-altset", "wrong-interface",
    "duplicate-altset",
    "missing-descriptor", "duplicate-descriptor", "channels", "format", "pcm-format",
    "missing-rate", "duplicate-rate", "zero-rate", "fractional-rate", "zero-denominator",
    "unknown-map",
])
def test_inconsistent_active_data_is_never_a_valid_mode(kind):
    row = recorded()
    stream, params = row["stream0"], row["hw_params"]
    if kind == "no-playback":
        stream = stream.split("Playback:")[0]
    elif kind == "no-status":
        stream = stream.replace("Status: Running", "Status: Unknown")
    elif kind == "duplicate-status":
        stream = stream.replace("  Status: Running", "  Status: Running\n  Status: Stop")
    elif kind == "no-altset":
        stream = stream.replace("    Altset = 1\n", "")
    elif kind == "wrong-interface":
        stream = stream.replace("    Interface = 1\n", "    Interface = 9\n")
    elif kind == "duplicate-altset":
        stream = stream.replace("    Altset = 1\n", "    Altset = 1\n    Altset = 1\n")
    elif kind == "missing-descriptor":
        stream = stream.replace("  Interface 1\n    Altset 1", "  Interface 1\n    Altset 9")
    elif kind == "duplicate-descriptor":
        stream = stream.replace("  Interface 1\n    Altset 2", "  Interface 1\n    Altset 1")
    elif kind == "channels":
        params = params.replace("channels: 20", "channels: 16")
    elif kind == "format":
        stream = stream.replace("Format: S24_3LE", "Format: S16_LE")
    elif kind == "pcm-format":
        params = params.replace("format: S24_3LE", "format: S16_LE")
    elif kind == "missing-rate":
        params = "\n".join(line for line in params.splitlines() if not line.startswith("rate:"))
    elif kind == "duplicate-rate":
        params += "rate: 48000 (48000/1)\n"
    elif kind == "zero-rate":
        params = params.replace("rate: 48000 (48000/1)", "rate: 0 (0/1)")
    elif kind == "fractional-rate":
        params = params.replace("48000/1", "96001/2")
    elif kind == "zero-denominator":
        params = params.replace("48000/1", "48000/0")
    elif kind == "unknown-map":
        stream = stream.replace("Channels: 20", "Channels: 18")
        params = params.replace("channels: 20", "channels: 18")
    with pytest.raises(ValueError, match=r"USB|ALSA"):
        streams.parse_playback(stream, params)


def test_only_the_configured_serials_card_is_read(proc):
    write_card(proc, recorded(192000, 20), number=2, serial="11111111")
    write_card(proc, recorded(), number=12)
    state = streams.read_playback(replace(routed(), serial="24216011"), proc)
    assert (state.card, state.serial, state.mode.rate) == (12, "24216011", 48000)
    with pytest.raises(OSError, match="more than one"):
        streams.read_playback(routed(), proc)
    assert streams.read_playback(replace(routed(), serial="absent"), proc).mode is None


@pytest.mark.parametrize("usbid", [None, "ffff:ffff\n"])
def test_a_matching_name_does_not_replace_the_usb_identity(proc, usbid):
    card = write_card(proc, recorded())
    if usbid is None:
        (card / "usbid").unlink()
    else:
        (card / "usbid").write_text(usbid)
    with pytest.raises(OSError, match=r"identify|identity"):
        streams.read_playback(routed(), proc)


def test_an_unreadable_ucx_stream_is_not_treated_as_idle(proc, monkeypatch):
    card = write_card(proc, recorded())
    actual_read = Path.read_text

    def unreadable(path, *args, **kwargs):
        if path == card / "stream0":
            raise PermissionError("denied")
        return actual_read(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", unreadable)
    with pytest.raises(OSError, match="cannot read"):
        streams.read_playback(routed(), proc)


def test_an_unreadable_unrelated_card_does_not_hide_the_ucx(proc, monkeypatch):
    other = write_card(proc, recorded(), number=1)
    (other / "usbid").write_text("ffff:ffff\n")
    write_card(proc, recorded(), number=2)
    actual_read = Path.read_text

    def unreadable(path, *args, **kwargs):
        if path == other / "stream0":
            raise PermissionError("unrelated card")
        return actual_read(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", unreadable)
    assert streams.read_playback(routed(), proc).card == 2
    (other / "usbid").unlink()
    assert streams.read_playback(routed(), proc).card == 2


@pytest.mark.parametrize("usbid", [None, "ffff:ffff", "2a39:3fd9"])
def test_a_missing_header_is_a_problem_for_the_ucx_not_for_unrelated_cards(proc, usbid):
    card = write_card(proc, recorded())
    (card / "stream0").write_text("")
    if usbid is None:
        (card / "usbid").unlink()
    else:
        (card / "usbid").write_text(usbid)
    if usbid == "2a39:3fd9":
        with pytest.raises(OSError, match="identity is missing"):
            streams.read_playback(routed(), proc)
    else:
        assert streams.read_playback(routed(), proc).card is None


@pytest.mark.parametrize("change", ["identity", "mode", "params", "unreadable", "empty"])
def test_mid_read_changes_are_refused(proc, monkeypatch, change):
    card = write_card(proc, recorded())
    actual_read = Path.read_text
    counts = {}

    def changing_read(path, *args, **kwargs):
        text = actual_read(path, *args, **kwargs)
        counts[path] = counts.get(path, 0) + 1
        if path == card / "stream0" and counts[path] == 2:
            if change == "identity":
                return text.replace("24216011", "11111111")
            if change == "mode":
                return text.replace("Status: Running", "Status: Stop")
            if change == "unreadable":
                raise OSError("removed")
            if change == "empty":
                return ""
        if path == card / "pcm0p/sub0/hw_params" and counts[path] == 2 and change == "params":
            return text.replace("48000", "44100")
        return text

    monkeypatch.setattr(Path, "read_text", changing_read)
    with pytest.raises(OSError, match="cannot validate"):
        streams.read_playback(routed(), proc)


def test_idle_start_is_explicitly_unknown_and_not_continuously_rewarned(proc, caplog):
    card = write_card(proc, recorded())
    stream = card / "stream0"
    stream.write_text(stream.read_text().replace("Status: Running", "Status: Stop"))
    guard = streams.PlaybackGuard(routed())
    guard.check()
    guard.check()
    assert caplog.text.count("live USB mode is not validated") == 1
    assert guard.before.mode is None


def test_active_log_names_the_observed_card_and_not_the_requested_desktop_rate(proc, caplog):
    write_card(proc, recorded(96000, 14), number=12)
    caplog.set_level(logging.INFO, logger="oscmix-session")
    guard = streams.PlaybackGuard(routed())
    guard.check()
    guard.check()
    assert caplog.text.count("active USB playback on ALSA card 12 (serial 24216011)") == 1
    assert "96000 Hz, 14 channels, alternate setting 3" in caplog.text


@pytest.mark.parametrize("config", [
    Config(), Config(device_name="another interface", routes=routed().routes),
    Config(usb_id="ffff:ffff", routes=routed().routes),
    Config(routes=(Route("monitor", output=(5,), input=(1,)),)),
])
def test_other_devices_and_input_only_routes_do_not_claim_playback_validation(config, monkeypatch):
    monkeypatch.setattr(streams, "read_playback",
                        lambda *_args: pytest.fail("unexpected PCM access"))
    streams.PlaybackGuard(config).check()


def test_known_bad_mode_refuses_before_any_phase_is_sent(proc):
    write_card(proc, recorded(192000, 20))
    device = RecordingBackend(reports=echo_link_flags_only)
    device.traits = OSCMIX
    with pytest.raises(WriteFailed) as caught:
        routing.apply_routing(routed(), 7222, backend=device)
    assert not device.sent
    assert not caught.value.written
    assert caught.value.unwritten == (
        "/playback/1/stereo", "/output/5/stereo", "/mix/5/playback/1",
    )


@pytest.mark.parametrize(("before", "after"), [(48000, 44100), (48000, None), (None, 48000)])
def test_changes_between_phases_report_exact_partial_writes(proc, before, after):
    def install(rate):
        row = dict(recorded(rate or 48000))
        if rate is None:
            row["stream0"] = row["stream0"].replace("Status: Running", "Status: Stop")
        write_card(proc, row)

    install(before)

    class ChangingBackend(RecordingBackend):
        traits = OSCMIX

        def send(self, messages):
            super().send(messages)
            install(after)

    device = ChangingBackend(reports=echo_link_flags_only)
    with pytest.raises(WriteFailed, match="changed during the apply") as caught:
        routing.apply_routing(routed(), 7222, backend=device)
    assert caught.value.written == ("/playback/1/stereo", "/output/5/stereo")
    assert caught.value.unwritten == ("/mix/5/playback/1",)
    assert all(path.endswith("/stereo") for path, _tags, _args in device.sent)


def test_a_change_after_the_last_write_does_not_invent_a_partial_apply(proc):
    write_card(proc, recorded())

    class ChangingBackend(RecordingBackend):
        traits = OSCMIX

        def send(self, messages):
            super().send(messages)
            if any(path.startswith("/mix/") for path, _tags, _args in self.sent):
                write_card(proc, recorded(192000, 20))

    device = ChangingBackend(reports=echo_link_flags_only)
    routing.apply_routing(routed(), 7222, backend=device)
    assert len(device.sent) == 3


@pytest.mark.parametrize("partial", [False, True])
def test_profile_marker_does_not_advance_after_a_mode_refusal(proc, tmp_path, partial):
    write_card(proc, recorded() if partial else recorded(192000, 20))
    path = desk(tmp_path, main=GOOD, tracking=TRACKING)

    class ChangingBackend(RecordingBackend):
        traits = OSCMIX

        def send(self, messages):
            super().send(messages)
            write_card(proc, recorded(192000, 20))

    result = profiles.switch_profile(
        "tracking", path, backend=ChangingBackend(reports=echo_link_flags_only), verify=False)
    assert result.state == (WRITTEN_IN_PART if partial else REFUSED)
    assert marker.active_profile(path) is None
    assert result.applied is partial
    if partial:
        assert not result.persisted


def test_verifiers_mix_reapply_also_checks_the_live_mode(proc, monkeypatch):
    write_card(proc, recorded(192000, 20))
    monkeypatch.setattr(routing, "loopback", lambda *_args: pytest.fail("backend reached"))
    with pytest.raises(OSError, match="failed the measured"):
        routing.send_mix(routed())
