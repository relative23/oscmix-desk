"""The register model, checked against the recordings it came from.

A hand-written model of a device is knowledge that decays silently:
nothing fails when it goes stale, the device just does something other
than what the config says. These tests are the difference between a
model and a memory -- every claim `devices.py` makes about the UCX II
is held against `tests/data/refresh-dump.json` (a warm `/refresh`) and
`tests/data/cold-plug-timeline.json` (a real USB replug).

Two claims are deliberately *not* checked this way, and both are noted
where they arise: the meter channels, which the model records but 0.2.0
never writes, and the odd-channel-only shape of the input mix matrix,
which is link state rather than a capability.
"""

import json
import re

import pytest
from conftest import repo_file

from oscmix_desk import devices, registers


@pytest.fixture(scope="module")
def warm():
    return json.loads(repo_file("tests", "data", "refresh-dump.json").read_text())


@pytest.fixture(scope="module")
def cold():
    return json.loads(
        repo_file("tests", "data", "cold-plug-timeline.json").read_text())


# --------------------------------------------------------------------------
# The device dimension exists from the first line, not retrofitted.
# --------------------------------------------------------------------------

def test_the_model_is_indexed_by_device():
    assert len(devices.DEVICES) >= 2
    assert devices.UCX2.usb_id != devices.FF802.usb_id
    # `48v` on 1-2 and `hi-z` on 3-4 are UCX II facts, not Fireface facts.
    assert devices.UCX2.channels_for("48v") == (1, 2)
    assert devices.UCX2.channels_for("hi-z") == (3, 4)


def test_an_untested_device_declares_no_registers_rather_than_guesses():
    """"May work" as a property of the data, not a sentence in a README.

    The 802 declares no *registers* because oscmix cannot drive it at
    the pinned revision: `init()` lists only `&ffucxii`, so an 802 exits
    with "unsupported device"; and `ff802` has no `.regtoctl` or
    `.ctltoreg`, which are called unguarded in seven places. A register
    model against that would describe writes that cannot happen.

    It does declare channels, and that is not the same thing. They come
    from upstream's own `device_ff802.c` -- 30 in, 30 out, gain on the
    eight analog inputs, 48V and hi-Z on the four Mic/Inst ones -- so
    they are read rather than guessed. No evidence artifact, so it stays
    unsupported.
    """
    assert devices.FF802.supported is False
    assert devices.FF802.registers == ()
    assert devices.FF802.evidence is None


def test_the_802_channel_map_is_not_a_copy_of_the_ucx2():
    """The point of a device dimension: the two really do differ.

    A capability map that quietly matched the UCX II would look declared
    and mean nothing. These three differences are upstream's, not ours:
    twenty channels against thirty, 48V on 1-2 against 9-12, and the
    802's Mic/Inst channels carrying no gain register at all.
    """
    assert devices.FF802.channels_for("input") != \
        devices.UCX2.channels_for("input")
    assert devices.FF802.channels_for("48v") == (9, 10, 11, 12)
    assert devices.UCX2.channels_for("48v") == (1, 2)
    assert 9 not in devices.FF802.channels_for("input-gain")


def test_a_supported_device_names_its_evidence():
    # The roadmap's bar: register table declared, capabilities recorded,
    # and one hardware evidence artifact.
    for device in devices.DEVICES:
        if device.supported:
            assert device.registers, "%s claims support with no registers" % device.key
            assert device.channels
            assert device.evidence, "%s claims support with no evidence" % device.key


def test_an_unmodelled_device_is_no_opinion_not_an_error():
    # Every caller must treat None as "keep doing what you did".
    assert devices.device_for_name("Fireface UFX III") is None
    assert registers.verify_class(None, "/output/1/volume") is None
    assert registers.cold_plug_complete(None, "/output/1/stereo") is False


def test_the_configured_device_name_resolves():
    assert devices.device_for_name("Fireface UCX II") is devices.UCX2
    assert devices.device_for_name("  fireface ucx ii  ") is devices.UCX2


# --------------------------------------------------------------------------
# Every channel range, against the recording it was read from.
# --------------------------------------------------------------------------

def channels_in(dump, prefix, leaf):
    found = set()
    for path in dump:
        match = re.fullmatch(r"/%s/(\d+)/%s" % (prefix, re.escape(leaf)), path)
        if match:
            found.add(int(match.group(1)))
    return tuple(sorted(found))


@pytest.mark.parametrize(("capability", "prefix", "leaf"), [
    ("output", "output", "stereo"),
    ("output", "output", "volume"),
    ("input", "input", "stereo"),
    ("playback", "playback", "stereo"),
    ("48v", "input", "48v"),
    ("hi-z", "input", "hi-z"),
    ("input-gain", "input", "gain"),
    ("input-reflevel", "input", "reflevel"),
    ("output-reflevel", "output", "reflevel"),
])
def test_every_channel_range_matches_the_recording(warm, capability, prefix, leaf):
    recorded = channels_in(warm["registers"], prefix, leaf)
    assert devices.UCX2.channels_for(capability) == recorded, (
        "the model says /%s/N/%s exists on %s, the device reported %s"
        % (prefix, leaf, devices.UCX2.channels_for(capability), recorded))


def test_the_meters_run_further_than_the_control_registers(warm):
    # The reason a single "channel count" per device would already be
    # wrong: meters go to 22, everything that can be *set* stops at 20.
    meters = channels_in(warm["registers"], "output", "level")
    assert devices.UCX2.channels_for("meter") == meters
    assert max(meters) > max(devices.UCX2.channels_for("output"))


# --------------------------------------------------------------------------
# Verification classes, against what the dump does and does not report.
# --------------------------------------------------------------------------

def test_every_verifiable_register_really_is_reported(warm):
    reported = set(warm["registers"])
    missing = [p for p in registers.declared_paths(devices.UCX2)
               if registers.verify_class(devices.UCX2, p) == registers.VERIFIABLE
               and p not in reported]
    assert missing == [], (
        "declared verifiable but absent from the recorded dump: %s" % missing[:8])


def test_every_write_only_register_really_is_absent(warm):
    reported = set(warm["registers"])
    present = [p for p in registers.declared_paths(devices.UCX2)
               if registers.verify_class(devices.UCX2, p) == registers.WRITE_ONLY
               and p in reported]
    assert present == [], (
        "declared write-only but the device reported it: %s -- if upstream "
        "started dumping these, the verifier may confirm them" % present[:8])


def test_the_playback_matrix_is_the_only_re_established_family(warm):
    reest = [r for r in devices.UCX2.registers
             if r.verify == registers.REESTABLISHED]
    assert [r.template for r in reest] == ["/mix/{out}/playback/{pb}"]
    # ... and it is absent, which is what forces the class.
    assert not [p for p in warm["registers"]
                if p.startswith("/mix/") and "/playback/" in p]


def test_the_input_matrix_is_verifiable_which_0_3_0_depends_on(warm):
    assert registers.verify_class(devices.UCX2, "/mix/5/input/1") == \
        registers.VERIFIABLE
    assert len([p for p in warm["registers"]
                if p.startswith("/mix/") and "/input/" in p]) >= 100


def test_the_class_of_an_unknown_path_is_unknown(warm):
    """Not a default of "verifiable": an unmodelled register must not
    inherit a promise.

    The example is taken from the recording rather than written here.
    Twice now this test named a path that a later release declared --
    `/reverb/type`, then `/input/1/eq/band1gain` -- and each time the
    failure was the test doing its job and the fix was churn. Asking the
    dump for something the model does not carry keeps it honest without
    needing an edit per family.
    """
    device = devices.UCX2
    declared = set(registers.declared_paths(device))
    undeclared = sorted(p for p in warm["registers"]
                        if p not in declared and not p.endswith("/level"))
    assert undeclared, "the model now declares the entire dump -- update this"
    assert registers.verify_class(device, undeclared[0]) is None
    assert registers.verify_class(device, "/no/such/register") is None


def test_every_declared_class_is_one_of_the_three():
    for device in devices.DEVICES:
        for register in device.registers:
            assert register.verify in registers.VERIFY_CLASSES


# --------------------------------------------------------------------------
# The cold-plug dimension, which the warm dump alone cannot tell you.
# --------------------------------------------------------------------------

def test_the_families_called_complete_really_arrive_whole(cold):
    """Every channel, not most of them.

    This is the claim that matters: 0.2.0 works after a hotplug because
    the stereo flags come back for all 20 channels within ~2.3 s. If a
    pin bump made that partial, everything this project applies would be
    verifying against a half-filled cache.
    """
    reported = set(cold["first_report_seconds"])
    for register in devices.UCX2.registers:
        if not register.per_channel:
            continue
        if not registers.cold_plug_complete(devices.UCX2,
                                            register.path(ch=1)):
            continue
        missing = [register.path(ch=c)
                   for c in devices.UCX2.channels_for(register.channels)
                   if register.path(ch=c) not in reported]
        assert missing == [], (
            "%s is declared complete after a cold plug but %d channel(s) "
            "were not reported: %s"
            % (register.template, len(missing), missing[:6]))


def test_everything_else_is_not_claimed_to_be_complete(cold):
    """The honest half, and the reason this is a list of what *is* whole.

    A cold plug delivered 1234 of 1932 non-meter registers, and what was
    missing is ragged rather than lawful: `/output/N/mute` came back for
    channels 1, 2, 3, 8, 9 and 10, and not for 4-7 or 11-20. That is a
    truncated stream, not a rule, and modelling it per family or per
    channel would encode one recording as a device property.

    So the model refuses to answer for anything it did not measure whole
    -- including registers nobody measured at all.
    """
    reported = set(cold["first_report_seconds"])
    partial = []
    for register in devices.UCX2.registers:
        if not register.per_channel:
            continue
        channels = devices.UCX2.channels_for(register.channels)
        if not channels or register.verify != registers.VERIFIABLE:
            continue
        seen = sum(1 for c in channels if register.path(ch=c) in reported)
        if 0 < seen < len(channels):
            partial.append((register.template, seen, len(channels)))
            assert not registers.cold_plug_complete(
                devices.UCX2, register.path(ch=channels[0])), (
                "%s arrived for %d of %d channels but is declared complete"
                % (register.template, seen, len(channels)))
    assert partial, (
        "nothing arrived partially -- if a cold plug is complete now, "
        "re-record and simplify this away")


def test_what_0_2_0_verifies_survives_a_cold_plug(cold):
    # The reason this gap went unnoticed for two releases: everything
    # this release actually checks is in the fast, complete part.
    reported = set(cold["first_report_seconds"])
    for path in ("/output/1/stereo", "/output/5/stereo", "/output/7/stereo",
                 "/playback/1/stereo"):
        assert path in reported, "%s missing from a cold plug" % path
        assert registers.verify_class(devices.UCX2, path) == registers.VERIFIABLE
        assert registers.cold_plug_complete(devices.UCX2, path)


def test_an_unmeasured_register_is_never_called_complete():
    # A verifier must not fail a register into a warning because a
    # hotplug was still filling the cache, and "unknown" must not read
    # as "fine".
    assert not registers.cold_plug_complete(devices.UCX2, "/reverb/type")
    assert not registers.cold_plug_complete(devices.UCX2, "/output/1/mute")
    assert not registers.cold_plug_complete(None, "/output/1/stereo")


# --------------------------------------------------------------------------
# Global registers: the families with no channel dimension.
# --------------------------------------------------------------------------

def test_the_global_registers_are_declared_once_not_per_channel():
    """A register with no `{ch}` is one path, not none.

    `declared_paths` expands every row over a channel list. A row with
    no placeholder expands to nothing there, which would let a family be
    declared in the table and never checked against a recording -- the
    quietest way to be wrong about a device.
    """
    from oscmix_desk.devices import device_for_name
    from oscmix_desk.registers import (
        GLOBAL,
        declared_paths,
    )

    device = device_for_name("Fireface UCX II")
    globals_ = [r for r in device.registers if r.channels == GLOBAL]
    assert globals_, "no global family declared"
    for register in globals_:
        assert not register.per_channel
        assert "{" not in register.template

    paths = declared_paths(device)
    for register in globals_:
        assert paths.count(register.template) == 1, register.template


def test_every_declared_echo_register_is_in_the_recording(warm):
    """The table against the device, not against the datasheet.

    The whole point of the register model is that a claim the device
    does not support is a failing test rather than a surprise on
    somebody's desk. `/echo` is the first family declared without a
    channel, so it is the first chance for that check to pass vacuously.
    """
    from oscmix_desk.devices import device_for_name
    from oscmix_desk.registers import GLOBAL

    device = device_for_name("Fireface UCX II")
    declared = {r.template for r in device.registers
                if r.channels == GLOBAL and r.template.startswith("/echo")}
    assert len(declared) == 7
    missing = sorted(declared - set(warm["registers"]))
    assert missing == [], (
        "declared but never reported by the device: %s" % missing)


def test_the_echo_family_is_complete_against_the_recording(warm):
    """And the other direction: nothing in the dump left undeclared.

    A family half-declared is worse than one not declared at all --
    `--dump-config` would emit the half it knows and silently drop the
    rest, which reads as "the device has no echo settings".
    """
    from oscmix_desk.devices import device_for_name

    device = device_for_name("Fireface UCX II")
    declared = {r.template for r in device.registers}
    reported = {p for p in warm["registers"] if p.startswith("/echo")}
    assert sorted(reported - declared) == []


def test_the_echo_bounds_are_upstreams_and_not_invented():
    """Bounds come from oscmix.c's node table, checked by their arithmetic.

    `delay` is `.scale=0.001, .min=0, .max=2000` -- 0 to 2 seconds.
    `volume` is `.scale=0.1, .min=-650, .max=60`, which is exactly
    LEVEL_MIN..LEVEL_MAX, the range a fader already has.

    `feedback` declares no bounds upstream and declares none here.
    Asserting that is the point: a range invented to look tidy would
    reject values the device accepts.

    `width` used to sit beside it and no longer does. Not because a
    tidier range won the argument, but because the write sweep found the
    device refusing 1.02 outright -- no clamp, no report, a config
    silently ignored -- and the bound was then bracketed at 0.0..1.0
    against the hardware. Measured beats both invented and absent.
    """
    from oscmix_desk.constants import LEVEL_MAX, LEVEL_MIN
    from oscmix_desk.devices import device_for_name

    by_path = {r.template: r for r in device_for_name("Fireface UCX II").registers}
    assert (by_path["/echo/delay"].lo, by_path["/echo/delay"].hi) == (0.0, 2.0)
    assert by_path["/echo/delay"].unit == "s"
    assert (by_path["/echo/volume"].lo, by_path["/echo/volume"].hi) == (
        LEVEL_MIN, LEVEL_MAX)
    assert by_path["/echo/feedback"].lo is None
    assert by_path["/echo/feedback"].hi is None
    assert (by_path["/echo/width"].lo, by_path["/echo/width"].hi) == (0.0, 1.0)


def test_an_unmodelled_device_has_no_opinion_about_options():
    """The `device is None` path, which is a supported state.

    `device_for_name` answers None for hardware this project has no
    register table for, and the whole config layer is built so that such
    a device constrains nothing rather than refusing everything. The
    three lookups added for the multi-row gain split have to agree with
    that, or an unmodelled device would start raising instead of
    shrugging.
    """
    from oscmix_desk.registers import (
        option_channels,
        option_register,
        settable_option_rows,
        settable_options,
    )

    assert settable_option_rows(None, "input") == ()
    assert settable_options(None, "input") == {}
    assert option_register(None, "input", "gain", 1) is None
    assert option_channels(None, "input", "gain") == ()


def test_rows_sharing_a_template_never_overlap():
    """The invariant that makes a multi-row option safe.

    `/input/{ch}/gain` is three rows because the device's limits differ
    by channel. That only works while their capabilities are disjoint:
    two rows claiming the same channel would make `register_at` answer
    by table order, which is not a decision anyone made, and would put
    the same path in `declared_paths` twice.

    Nothing enforced this when the split was written. It held by
    inspection, which is the state a test is for.
    """
    import collections

    from oscmix_desk import registers

    for device in devices.DEVICES:
        if not device.registers:
            continue
        by_template = collections.defaultdict(list)
        for register in device.registers:
            by_template[register.template].append(register)
        for template, rows in by_template.items():
            if len(rows) == 1:
                continue
            seen: set = set()
            for register in rows:
                channels = set(device.channels_for(register.channels))
                assert not (seen & channels), (
                    "%s on %s: rows overlap on %s"
                    % (template, device.key, sorted(seen & channels)))
                seen |= channels
        paths = list(registers.declared_paths(device))
        assert len(paths) == len(set(paths)), "%s declares a path twice" % device.key


def test_the_gain_rows_together_cover_what_the_device_reports():
    """Split by what it accepts, complete against what it reports.

    The three rows have to add up to `input-gain`, the capability the
    recording test checks against the dump. A split that quietly lost a
    channel would leave a register the device reports and the model does
    not describe at all.
    """
    from oscmix_desk.devices import UCX2

    rows = [r for r in UCX2.registers if r.template == "/input/{ch}/gain"]
    covered: set = set()
    for register in rows:
        covered |= set(UCX2.channels_for(register.channels))
    assert covered == set(UCX2.channels_for("input-gain"))
