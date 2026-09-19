"""A config is held to the device its file names.

Channels that device has, sections its register table declares, and a
device without a table saying so rather than swallowing sections.
"""



import pytest
from support import repo_file, routing_conf

from oscmix_desk import model as model_mod
from oscmix_desk import notices as notices_mod


def test_a_channel_the_device_does_not_have_is_rejected(session_mod, tmp_path):
    path = routing_conf(tmp_path, "[device]\nname = Fireface UCX II\n\n"
                           "[route:x]\nplayback = 1/2\noutput = 40/41\n")
    with pytest.raises(session_mod.ConfigError) as excinfo:
        session_mod.load_config(path)
    message = str(excinfo.value)
    assert "40" in message
    assert "Fireface UCX II" in message
    assert "1..20" in message, "the error should say what the device has"

def test_the_device_may_appear_after_the_routes(session_mod, tmp_path):
    # configparser hands sections back in file order, so the check has to
    # be a separate pass -- a route parsed before [device] cannot know
    # which device it is for.
    path = routing_conf(tmp_path, "[route:x]\nplayback = 1/2\noutput = 40/41\n\n"
                           "[device]\nname = Fireface UCX II\n")
    with pytest.raises(session_mod.ConfigError, match="40"):
        session_mod.load_config(path)

def test_an_unmodelled_device_keeps_working_exactly_as_before(session_mod,
                                                              tmp_path):
    # No opinion, not an error. A model that rejected channels on
    # hardware nobody here can test would be guessing.
    path = routing_conf(tmp_path, "[device]\nname = Fireface UFX III\n\n"
                           "[route:x]\nplayback = 1/2\noutput = 40/41\n")
    config = session_mod.load_config(path)
    assert config.routes[0].output == (40, 41)

def test_routes_on_an_unmodelled_device_have_a_warning_for_the_caller(
        session_mod, tmp_path, caplog):
    """Still no opinion (ADR 0006) -- but a channel section on such a device
    has warned since 0.6.2, while its routes were written to hardware
    nobody modelled without a word. The parser stays quiet: it runs on
    every load, and the first cut of this warned two to four times per
    start and about the wrong file while a profile was active. The paths
    that write or show a desk ask about the desk they have in hand
    (`log_desk_notices`, 0.6.11)."""

    with caplog.at_level("WARNING"):
        unmodelled = session_mod.load_config(routing_conf(
            tmp_path, "[device]\nname = Fireface UFX III\n\n"
                      "[route:x]\nplayback = 1/2\noutput = 40/41\n"
                      "[route:y]\nplayback = 3/4\noutput = 3/4\n"))
    assert caplog.text == "", "the parser itself says nothing"
    assert notices_mod.unchecked_routes_warning(unmodelled) == (
        "no register model for 'Fireface UFX III': its 2 route(s) are "
        "written as given, with no check that the device has those "
        "channels (modelled: Fireface UCX II)")
    # A modelled device, the 802 whose channels upstream declares, and a
    # desk with no routes have nothing to be warned about.
    route = "[route:x]\nplayback = 1/2\noutput = %s\n"
    for text in (route % "1/2",
                 "[device]\nname = Fireface 802\n\n" + route % "29/30",
                 "[device]\nname = Fireface UFX III\n"):
        assert notices_mod.unchecked_routes_warning(
            session_mod.load_config(routing_conf(tmp_path, text))) is None, text

@pytest.mark.parametrize("name", ["", "   ", "  # the box"])
def test_an_empty_device_name_is_a_config_error(session_mod, tmp_path, name):
    """`[device] name` is a substring match, and the empty string is a
    substring of every name. Measured on the start path: with one
    MIDI-capable card it selected that card, with a second one -- a USB
    keyboard beside the interface -- the start refused as ambiguous and
    pointed at `serial`; and either way the desk had no model, so nothing
    in it was checked. It worked by accident (0.6.11, ADR 0006)."""
    from oscmix_desk import discovery
    from oscmix_desk.errors import DeviceAmbiguous

    clients = ('Client  24 : "Fireface UCX II (24216011)" [Kernel]\n'
               'Client  28 : "UMC404HD 192k" [Kernel]\n')
    alone = ["HDA Intel PCH", "Fireface UCX II (24216011)"]
    assert discovery.select_seq_client(clients, "", "", alone) == 24
    with pytest.raises(DeviceAmbiguous, match="2 interfaces match ''"):
        discovery.select_seq_client(clients, "", "",
                                    [*alone, "UMC404HD 192k"])
    path = routing_conf(tmp_path, "[device]\nname =%s\n\n"
                           "[route:x]\nplayback = 1/2\noutput = 1/2\n" % name)
    with pytest.raises(session_mod.ConfigError,
                       match="an empty name matches every card that has a "
                             "MIDI port"):
        session_mod.load_config(path)

def test_a_config_records_what_its_file_said_and_what_the_command_line_did(
        session_mod, tmp_path):
    """`loaded` is what the file said, `overrides` what the command line put
    over it, and the five settings are the second over the first. A
    running session resolves a file it reads again with `overrides` and
    holds it against those (ADR 0024, ADR 0026)."""
    from oscmix_desk import CommandLine

    assert session_mod.Config().loaded is None, "not loaded, no record"
    path = routing_conf(
        tmp_path, "[device]\nname = Fireface 802\nserial = 11223344\n\n"
                  "[osc]\nport = 9001\n")
    named = session_mod.load_config(path)
    file = model_mod.Machine("Fireface 802", "2a39:3fd9", "11223344", 9001,
                             8222)
    assert named.loaded == file
    assert named.overrides == CommandLine()
    assert named.loaded.differs_from(model_mod.Machine(
        "Fireface 802", "2a39:3fd9", "5", 9001, 9)) == (
        "serial '11223344' (not '5'), osc recv port 8222 (not 9)")
    said = CommandLine(device_name="Fireface UCX II", osc_port=9000)
    under = session_mod.load_config(path, said=said)
    assert (under.loaded, under.overrides) == (file, said)
    assert (under.device_name, under.osc_port, under.serial) == (
        "Fireface UCX II", 9000, "11223344")


def test_a_desk_is_validated_for_the_device_the_command_line_names(
        session_mod, tmp_path):
    """`--device` arrived after the file had been checked for the device it
    names, so outputs 29/30 of an 802 desk reached a UCX II, which has
    twenty, with a warning at most (0.6.11). The parser knows the command
    line since 0.7.0: the routes, the channel sections and the pins are
    held to the interface the desk goes to."""
    from oscmix_desk import CommandLine

    ucx2 = CommandLine(device_name="Fireface UCX II")
    wide = routing_conf(tmp_path, "[device]\nname = Fireface 802\n"
                                  "[route:x]\nplayback = 1/2\noutput = 29/30\n")
    assert session_mod.load_config(wide).routes
    with pytest.raises(session_mod.ConfigError,
                       match="channel 29 does not exist on a Fireface UCX II"):
        session_mod.load_config(wide, said=ucx2)
    # The other way round: a section the file's own device has no model
    # for is read, not ignored, once the command line names one that has.
    gain = routing_conf(tmp_path, "[device]\nname = Some Box\n"
                                  "[input:3]\ngain = 12.0\n")
    assert session_mod.load_config(gain).channels == ()
    assert [c.option for c in
            session_mod.load_config(gain, said=ucx2).channels] == ["gain"]


def test_a_desk_is_elsewhere_when_a_restart_would_take_it_somewhere_else():
    """A re-read file is resolved as a restart would resolve it -- the
    command line over it -- and then held against what the session runs.
    Comparing the bare file refused the session's own desk three times
    over: the pinned box once routing.conf named it, the port given on the
    command line once the file named that, and any other port under
    `--osc-port`, with advice to restart that a restart would not have
    followed (all found by review)."""
    from oscmix_desk import CommandLine
    from oscmix_desk.model import Machine

    file = Machine("Fireface UCX II", "2a39:3fd9", "", 7222, 8222)
    said = CommandLine(device_name="Some Box", osc_port=9000)
    live = Machine("Some Box", "2a39:3fd9", "24216011", 9000, 8222)
    assert file.under(CommandLine()) == file
    assert file.under(said) == live._replace(serial="")
    assert file.under(CommandLine(osc_port=9000)).device_name == file.device_name
    for same in (file,
                 file._replace(serial="24216011"),          # the pinned box
                 file._replace(osc_port=9500),              # under --osc-port
                 file._replace(device_name="Fireface 802")):    # under --device
        assert same.under(said).elsewhere(live) == "", same
    assert file._replace(serial="99887766").under(said).elsewhere(live) == \
        "serial '99887766' (not '24216011')"
    assert file._replace(usb_id="1234:5678", osc_recv_port=9).under(said) \
        .elsewhere(live) == ("usb id '1234:5678' (not '2a39:3fd9'), "
                             "osc recv port 9 (not 8222)")
    # And with nothing on the command line, the file is all there is.
    assert file._replace(osc_port=9500).elsewhere(
        live._replace(osc_port=7222, device_name=file.device_name)) == \
        "osc port 9500 (not 7222)"

def test_no_file_resolves_to_the_defaults_and_that_is_a_record(session_mod):
    from oscmix_desk.model import Machine

    assert session_mod.load_config(None).loaded == Machine(
        "Fireface UCX II", "2a39:3fd9", "", 7222, 8222)

def test_an_untested_device_constrains_only_what_upstream_declares(
        session_mod, tmp_path):
    """This test used to assert the opposite, and the change is the point.

    While the 802 declared no channels, "being listed must not become a
    constraint" was the promise, and `output = 30/31` was accepted. Its
    channel map now comes from upstream's `device_ff802.c`, so 31 is
    refused -- a channel that does not exist on the hardware, caught by
    a table that was read rather than invented.

    The promise it replaces is narrower and truer: being listed
    constrains a config by exactly what upstream's own table says, and
    never by a guess. A device with no table still gets no opinion.
    """
    path = routing_conf(tmp_path, "[device]\nname = Fireface 802\n\n"
                           "[route:x]\nplayback = 1/2\noutput = 30/31\n")
    with pytest.raises(session_mod.ConfigError) as excinfo:
        session_mod.load_config(path)
    assert "channel 31 does not exist on a Fireface 802" in str(excinfo.value)
    assert "output 1..30" in str(excinfo.value)

def test_the_802_still_accepts_what_it_does_have(session_mod, tmp_path):
    path = routing_conf(tmp_path, "[device]\nname = Fireface 802\n\n"
                           "[route:x]\nplayback = 1/2\noutput = 29/30\n")
    assert session_mod.load_config(path).routes[0].output == (29, 30)

def test_a_device_with_no_table_at_all_still_gets_no_opinion(session_mod,
                                                             tmp_path):
    """The half that did not change: an unmodelled name is unconstrained."""
    path = routing_conf(tmp_path, "[device]\nname = Some Other Interface\n\n"
                           "[route:x]\nplayback = 1/2\noutput = 63/64\n")
    assert session_mod.load_config(path).routes[0].output == (63, 64)

def test_the_channels_a_valid_config_uses_are_still_accepted(session_mod):
    # The shipped example and the syntax range both still pass.
    config = session_mod.load_config(repo_file("config", "routing.conf.example"))
    assert config.routes[0].output == (1, 2)

def _warnings(caplog):
    return [r.getMessage() for r in caplog.records if r.levelname == "WARNING"]

@pytest.mark.parametrize("name", ["Fireface 802", "Some Other Interface"])
def test_a_channel_section_for_a_device_without_registers_is_named_ignored(
        session_mod, tmp_path, caplog, name):
    """`[input:3]` on the 802 used to parse to nothing, in silence.

    The 802 lists channels and no registers; an unknown name lists
    nothing at all. Both get no opinion on routes, which is right, and
    both returned an empty list for a channel section, which is the
    0.6.1 route defect one file over: parsed, shown in nothing, delivered
    nowhere, and looking exactly like a section that worked. The route
    still loads; the section is dropped with a warning that names the
    device and the cause.
    """
    path = routing_conf(tmp_path, "[device]\nname = %s\n\n"
                           "[route:x]\nplayback = 1/2\noutput = 1/2\n\n"
                           "[input:3]\ngain = 12.0\n" % name)
    with caplog.at_level("WARNING"):
        config = session_mod.load_config(path)
    assert len(config.routes) == 1
    assert config.channels == ()
    messages = _warnings(caplog)
    assert len(messages) == 1
    assert "[input:3]" in messages[0]
    assert name in messages[0]
    assert "Fireface UCX II" in messages[0], "the warning names what is modelled"
    assert "newer version" not in messages[0]

@pytest.mark.parametrize("section", ["eq:input:3", "clock"])
def test_a_nested_or_global_section_on_the_802_is_not_blamed_on_a_newer_version(
        session_mod, tmp_path, caplog, section):
    # The dispatcher's fallback used to give every unknown section the
    # "newer version of oscmix-desk" text. On the 802 that was the wrong
    # cause: nothing is newer, the model has no rows for the device.
    path = routing_conf(tmp_path, "[device]\nname = Fireface 802\n\n"
                           "[%s]\nsource = Internal\n" % section)
    with caplog.at_level("WARNING"):
        config = session_mod.load_config(path)
    assert config.channels == ()
    assert config.globals == ()
    (message,) = _warnings(caplog)
    assert "[%s]" % section in message
    assert "Fireface 802" in message
    assert "Fireface UCX II" in message, "the warning names what is modelled"
    assert "newer version" not in message

def test_an_unknown_section_on_a_modelled_device_still_suggests_a_newer_version(
        session_mod, tmp_path, caplog):
    # The other branch keeps its meaning: on the UCX II an unknown
    # section really may come from a newer version (ADR 0006).
    path = routing_conf(tmp_path, "[device]\nname = Fireface UCX II\n\n"
                           "[frobnicate]\nlevel = 1\n")
    with caplog.at_level("WARNING"):
        session_mod.load_config(path)
    (message,) = _warnings(caplog)
    assert "newer version" in message
    assert "[frobnicate]" in message
    assert "[route:<name>]" in message, "it lists what this version knows"

def test_invalid_utf8_is_a_configuration_error(session_mod, tmp_path):
    path = tmp_path / "routing.conf"
    path.write_bytes(b"[route:monit\xf6rs]\nplayback=1/2\noutput=1/2\n")
    with pytest.raises(session_mod.ConfigError, match="cannot read"):
        session_mod.load_config(path)

def test_the_device_section_is_read_first_wherever_it_stands(session_mod, tmp_path):
    """A section above `[device]` was checked against the default model.

    `[pin] output.volume = pin` before `[device] name = Fireface 802`
    was accepted and after it refused; `[clock]` was refused with the
    wrong reason. The outcome of a config must not depend on the order
    of its sections (0.6.10).
    """
    from oscmix_desk.errors import ConfigError

    below = tmp_path / "below.conf"
    below.write_text("[device]\nname = Fireface 802\n[pin]\noutput.volume = pin\n"
                     "[route:a]\nplayback = 1/2\noutput = 1/2\n")
    above = tmp_path / "above.conf"
    above.write_text("[pin]\noutput.volume = pin\n[device]\nname = Fireface 802\n"
                     "[route:a]\nplayback = 1/2\noutput = 1/2\n")
    outcomes = []
    for path in (below, above):
        try:
            session_mod.load_config(path)
            outcomes.append("accepted")
        except ConfigError as exc:
            outcomes.append("refused: " + str(exc).split(":")[0])
    assert outcomes[0] == outcomes[1], outcomes
