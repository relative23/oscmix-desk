"""Which machine a profile is for (ADR 0026).

A profile is the desk, not the machine: it inherits `[osc]` and
`[device]` from its `routing.conf`, is validated for the device that
names, and is told when it names another one.
"""


import pytest
from conftest import write_config
from profile_desk import GOOD, TRACKING, desk, retargeting_desk

from oscmix_desk import notices as notices_mod
from oscmix_desk import profiles


def test_a_profile_without_an_osc_section_uses_the_main_config_port(tmp_path):
    """Written after this cost a real device its state.

    The profile below states no ``[osc] port``. Before the fix it fell
    back to the compiled-in default, 7222 -- which on the development
    machine was the *live backend*, so a unit test writing a profile
    reached a Fireface and moved a fader on it. It passed, because
    everything it asserted was true.

    The general rule it produced: a profile describes the desk, the main
    config describes the machine. Ports and the device name are the
    machine's.
    """
    write_config(tmp_path / "routing.conf",
                 "[osc]\nport = 9001\nrecv-port = 9002\n"
                 "[device]\nname = Fireface UFX III\n")
    write_config(tmp_path / "profiles" / "tracking.conf", GOOD)

    config = profiles.load_profile("tracking", tmp_path / "routing.conf")
    assert config.osc_port == 9001
    assert config.osc_recv_port == 9002
    assert config.device_name == "Fireface UFX III"

def test_a_profile_that_states_a_port_keeps_its_own(tmp_path):
    # The machine with two backends is exactly the machine whose
    # profiles are per-backend, so stating it has to win.
    write_config(tmp_path / "routing.conf", "[osc]\nport = 9001\n")
    write_config(tmp_path / "profiles" / "other.conf",
                 "[osc]\nport = 9500\n" + GOOD)
    assert profiles.load_profile("other", tmp_path / "routing.conf"
                                 ).osc_port == 9500

#: A value for each machine setting that is neither the default nor main's.
_STATED = {"port": "9500", "recv-port": "9600", "name": "Fireface 802",
           "usb-id": "2a39:3fb0", "serial": "11223344"}

@pytest.mark.parametrize(("section", "option", "attr"),
                         profiles.MACHINE_SETTINGS)
def test_a_profile_states_one_machine_setting_and_inherits_the_other_four(
        tmp_path, section, option, attr):
    """Option by option, not section by section: a profile that states
    `[device] serial` keeps the main config's `usb-id`, which sits in the
    same section. Since 0.6.11 that rests on the parser's fallbacks
    rather than on a second look at the file, so it is held here for
    every row of the table, against a main config whose five values are
    all non-default."""
    path = write_config(tmp_path / "routing.conf",
                        "[device]\nname = Some Box\nusb-id = 1111:2222\n"
                        "serial = 99887766\n\n"
                        "[osc]\nport = 9001\nrecv-port = 9002\n")
    write_config(tmp_path / "profiles" / "one.conf",
                 "[%s]\n%s = %s\n\n[route:x]\nplayback = 1/2\noutput = 1/2\n"
                 % (section, option, _STATED[option]))
    main = profiles.load_config(path)
    profile = profiles.load_profile("one", path)
    stated = int(_STATED[option]) if section == "osc" else _STATED[option]
    assert getattr(profile, attr) == stated
    for _section, _option, other in profiles.MACHINE_SETTINGS:
        if other != attr:
            assert getattr(profile, other) == getattr(main, other), other

def test_stating_the_default_explicitly_still_counts_as_stating_it(tmp_path):
    # "equals the default" cannot distinguish "said 7222" from "said
    # nothing". The parser can: it takes what the file says, and falls
    # back to what it was read onto only when the file says nothing.
    from oscmix_desk.constants import DEFAULT_OSC_PORT

    write_config(tmp_path / "routing.conf", "[osc]\nport = 9001\n")
    write_config(tmp_path / "profiles" / "pinned.conf",
                 "[osc]\nport = %d\n" % DEFAULT_OSC_PORT + GOOD)
    assert profiles.load_profile("pinned", tmp_path / "routing.conf"
                                 ).osc_port == DEFAULT_OSC_PORT

def test_every_machine_level_field_on_config_is_inherited(tmp_path):
    """The table cannot silently miss one.

    A `Config` field is machine-level when no `[route]` and no channel
    section can write it -- those are the desk. Everything else
    describes the box the desk is plugged into, and a profile that
    reverted it to the compiled-in default would be wrong on any machine
    that had set it. `usb-id` was missing from the first version of the
    table for exactly that reason: nothing pointed at it.
    """
    import dataclasses

    from oscmix_desk.model import Config

    # `policies` is the desk's, not the machine's: "should my monitor
    # faders come back after a restart" is a statement about how this
    # set of routing is meant to behave, so a profile brings its own.
    # `globals` likewise -- an echo send is part of a mix, not part of
    # the box it runs on.
    desk = {"routes", "channels", "policies", "globals"}
    # Neither: `loaded` records the machine settings the file resolved to,
    # `main` what a profile's routing.conf resolved to, and `overrides`
    # what the command line replaced -- no file's to state, and carried
    # along by `keep_machine_settings` beside the table (0.6.11).
    record = {"loaded", "main", "overrides"}
    machine = {f.name for f in dataclasses.fields(Config)} - desk - record
    covered = {attr for _section, _option, attr in profiles.MACHINE_SETTINGS}
    assert machine == covered, (
        "not inherited by a profile switch: %s" % sorted(machine - covered))

def test_usb_id_is_inherited_like_the_ports(tmp_path):
    write_config(tmp_path / "routing.conf",
                 "[device]\nname = Fireface UCX II\nusb-id = 2a39:3fd9\n")
    write_config(tmp_path / "profiles" / "tracking.conf", GOOD)
    assert profiles.load_profile("tracking", tmp_path / "routing.conf"
                                 ).usb_id == "2a39:3fd9"

def test_a_profile_is_validated_for_the_desk_s_device_not_the_default(
        tmp_path, caplog):
    """Measured before the fix, on a desk for an interface nobody modelled:
    a profile routing to output 25/26 was refused because "channel 25 does
    not exist on a Fireface UCX II", and a profile's `[output:1] volume`
    was accepted through the UCX II's table and would have been written
    -- while the same section in routing.conf has been ignored with a
    warning since 0.6.2. The profile was parsed while it still named the
    default device, and given the desk's name afterwards (0.6.11)."""
    path = write_config(tmp_path / "routing.conf",
                        "[device]\nname = Some Box\n\n"
                        "[route:main]\nplayback = 1/2\noutput = 1/2\n")
    write_config(tmp_path / "profiles" / "far.conf",
                 "[route:far]\nplayback = 1/2\noutput = 25/26\n")
    write_config(tmp_path / "profiles" / "vol.conf",
                 "[route:main]\nplayback = 1/2\noutput = 1/2\n\n"
                 "[output:1]\nvolume = -10.0\n")
    far = profiles.load_profile("far", path)
    assert far.device_name == "Some Box"
    assert [route.output for route in far.routes] == [(25, 26)]
    with caplog.at_level("WARNING"):
        vol = profiles.load_profile("vol", path)
    assert vol.channels == [], "nothing of it may reach a device nobody modelled"
    assert "ignoring [output:1]: no register model for 'Some Box'" in caplog.text

def test_a_profile_on_an_802_desk_is_held_to_the_802_s_channels(tmp_path):
    path = write_config(tmp_path / "routing.conf",
                        "[device]\nname = Fireface 802\n\n"
                        "[route:main]\nplayback = 1/2\noutput = 1/2\n")
    write_config(tmp_path / "profiles" / "far.conf",
                 "[route:far]\nplayback = 1/2\noutput = 29/30\n")
    write_config(tmp_path / "profiles" / "gone.conf",
                 "[route:gone]\nplayback = 1/2\noutput = 31/32\n")
    assert profiles.load_profile("far", path).routes[0].output == (29, 30)
    with pytest.raises(profiles.ConfigError, match="Fireface 802"):
        profiles.load_profile("gone", path)

def test_a_profile_that_names_its_own_device_is_held_to_that_one(tmp_path):
    """A profile may state a machine setting, and then it wins -- for the
    validation too, which is the half the old order got right by luck."""
    path = write_config(tmp_path / "routing.conf",
                        "[device]\nname = Some Box\n\n"
                        "[route:main]\nplayback = 1/2\noutput = 1/2\n")
    write_config(tmp_path / "profiles" / "ucx.conf",
                 "[device]\nname = Fireface UCX II\n\n"
                 "[route:far]\nplayback = 1/2\noutput = 25/26\n")
    with pytest.raises(profiles.ConfigError, match="Fireface UCX II"):
        profiles.load_profile("ucx", path)

def test_the_serial_is_inherited_like_the_usb_id(tmp_path):
    """A profile that does not state it keeps the running one.

    Without this a switch would hand the next writer an empty serial,
    the key would be recomputed from the card list, and the pinning done
    at start-up would be undone by the first profile change.
    """
    path = desk(tmp_path, tracking=TRACKING)
    path.write_text(path.read_text() + "\n[device]\nserial = 24216011\n")
    profile = profiles.load_profile("tracking", path)
    assert profile.serial == "24216011"

def test_a_profile_that_names_another_machine_is_told_what_0_7_0_does(
        tmp_path, caplog, recording_backend):
    """It still wins in 0.6.x (ADR 0011). ADR 0026 ends that: one persisted
    profile meant three targets, and two such profiles hold two device
    locks over one marker. Said where the desk is written, once.

    A profile that *restates* routing.conf's values is not told anything:
    `--dump-config > profiles/x.conf`, the documented way to make one,
    writes `[device]` and `[osc]` into every profile. The first cut
    warned about the sections and would have refused them all."""

    path = retargeting_desk(tmp_path)
    for name in ("here", "same"):
        assert notices_mod.other_machine_warning(
            profiles.load_profile(name, path)) is None, name
    assert notices_mod.other_machine_warning(profiles.load_config(path)) is None
    there = notices_mod.other_machine_warning(profiles.load_profile("there", path))
    assert there.startswith(
        "this profile names another backend or interface than its "
        "routing.conf -- serial '99887766' (not ''), osc port 9500 (not 9001)")
    assert "from 0.7.0 it is refused (ADR 0026)" in there
    with caplog.at_level("WARNING"):
        profiles.switch_profile("there", config_path=path,
                                backend=recording_backend, verify=False)
    assert caplog.text.count("from 0.7.0 it is refused") == 1

def test_a_dumped_config_makes_a_profile_nobody_is_warned_about(tmp_path):
    from oscmix_desk.dump import render_config

    path = write_config(tmp_path / "routing.conf", GOOD)
    dumped = render_config(profiles.load_config(path))
    assert "[device]" in dumped, "which is why the sections cannot be the rule"
    assert "[osc]" in dumped
    write_config(tmp_path / "profiles" / "dumped.conf", dumped)
    assert notices_mod.other_machine_warning(
        profiles.load_profile("dumped", path)) is None
