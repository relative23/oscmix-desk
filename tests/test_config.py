"""routing.conf parsing: valid configs, defaults, and error reporting."""

import json

import oracle
import pytest
from conftest import repo_file


def write(tmp_path, text):
    path = tmp_path / "routing.conf"
    path.write_text(text)
    return path


def test_defaults_without_file(session_mod):
    config = session_mod.load_config(None)
    assert config.device_name == "Fireface UCX II"
    assert config.usb_id == "2a39:3fd9"
    assert config.osc_port == 7222
    assert config.osc_recv_port == 8222
    assert config.routes == []


def test_recv_port_option(session_mod, tmp_path):
    path = write(tmp_path, "[osc]\nport = 9000\nrecv-port = 9001\n")
    config = session_mod.load_config(path)
    assert config.osc_port == 9000
    assert config.osc_recv_port == 9001


def test_shipped_example_config_parses(session_mod):
    config = session_mod.load_config(repo_file("config", "routing.conf.example"))
    assert config.device_name == "Fireface UCX II"
    assert len(config.routes) == 1
    route = config.routes[0]
    assert route.name == "main-out"
    assert route.playback == (1, 2)
    assert route.output == (1, 2)
    assert route.level == 0.0


def test_full_config(session_mod, tmp_path):
    path = write(tmp_path, """
[device]
name = Fireface 802
usb-id = 2A39:3FC0

[osc]
port = 9000

[route:monitors]
playback = 1/2
output = 5/6
level = -3.0
volume = 0.0

[route:sub]
playback = 3
output = 7
level = -6
stereo = no
""")
    config = session_mod.load_config(path)
    assert config.device_name == "Fireface 802"
    assert config.usb_id == "2a39:3fc0"  # normalized to lowercase
    assert config.osc_port == 9000
    monitors, sub = config.routes
    assert monitors.playback == (1, 2)
    assert monitors.output == (5, 6)
    assert monitors.level == -3.0
    assert monitors.volume == 0.0
    assert monitors.stereo is True
    assert sub.playback == (3,)
    assert sub.output == (7,)
    assert sub.volume is None
    assert sub.stereo is False


def test_inline_comments_are_stripped(session_mod, tmp_path):
    path = write(tmp_path, """
[route:main]
playback = 1/2  # stereo pair
output = 1/2    ; main out
""")
    config = session_mod.load_config(path)
    assert config.routes[0].playback == (1, 2)


@pytest.mark.parametrize(("snippet", "hint"), [
    ("[route:x]\nplayback = 1/2/3\noutput = 1/2\n", "playback"),
    ("[route:x]\nplayback = 1/2\noutput = five/6\n", "channel number"),
    ("[route:x]\nplayback = 1/2\noutput = 5\n", "both"),
    ("[route:x]\nplayback = 1/2\noutput = 0/1\n", "out of range"),
    ("[route:x]\nplayback = 1/2\noutput = 5/6\nlevel = 20\n", "out of range"),
    ("[route:x]\noutput = 5/6\n", "playback"),
    ("[route:x]\nplayback = 1/2\noutput = 5/6\nstereo = maybe\n", "boolean"),
    ("[route:x]\nplayback = 1/2\noutput = 5/6\nlevle = 0\n", "unknown option"),
    ("[device]\nusb-id = fireface\n", "usb-id"),
    ("[osc]\nport = 99999\n", "out of range"),
    ("[osc]\nport = auto\n", "port"),
])
def test_invalid_configs_raise_helpful_errors(session_mod, tmp_path, snippet, hint):
    path = write(tmp_path, snippet)
    with pytest.raises(session_mod.ConfigError) as excinfo:
        session_mod.load_config(path)
    assert hint in str(excinfo.value)


def test_missing_explicit_file_raises(session_mod, tmp_path):
    with pytest.raises(session_mod.ConfigError):
        session_mod.load_config(tmp_path / "nope.conf")


def test_stereo_route_writes_single_pair_register(session_mod):
    # oscmix folds a stereo-linked pair onto its odd channel: one /mix
    # message with pan 0 is the whole route. Hard-panned per-channel
    # messages would overwrite each other (last pan wins -> hard right).
    route = session_mod.Route(
        name="monitors", playback=(1, 2), output=(5, 6),
        level=0.0, volume=0.0, stereo=True,
    )
    assert oracle.route_messages(route) == [
        ("/playback/1/stereo", "i", (1,)),
        ("/output/5/stereo", "i", (1,)),
        ("/mix/5/playback/1", "fi", (0.0, 0)),
        ("/output/5/volume", "f", (0.0,)),
        ("/output/6/volume", "f", (0.0,)),
    ]


def test_unlinked_pair_route_uses_pair_balance(session_mod):
    # The unlink is stated, not assumed: the hard-panned pair below is
    # only correct against an unlinked output pair.
    route = session_mod.Route(
        name="split", playback=(1, 2), output=(5, 6), stereo=False,
    )
    messages = oracle.route_messages(route)
    assert [(path, types) for path, types, _a in messages] == [
        ("/playback/1/stereo", "i"),
        ("/output/5/stereo", "i"),
        ("/mix/5/playback/1", "fi"),
        ("/mix/6/playback/1", "fi"),
    ]
    assert [args for _p, _t, args in messages[:2]] == [(1,), (0,)]
    # The mix requests carry the +6 dB that compensates oscmix's halving
    # on this path; only the pan differs between the two halves.
    for (_path, _types, args), pan in zip(messages[2:], (-100, 100)):
        assert abs(args[0] - 6.0206) < 0.001
        assert args[1] == pan


def test_mono_route_messages(session_mod):
    route = session_mod.Route(name="sub", playback=(3,), output=(7,), level=-6.0)
    assert oracle.route_messages(route) == [
        ("/mix/7/playback/3", "fi", (-6.0, 0)),
    ]


def test_pair_without_volume_sends_no_volume_messages(session_mod):
    route = session_mod.Route(name="m", playback=(1, 2), output=(1, 2))
    paths = [path for path, _, _ in oracle.route_messages(route)]
    assert not any("volume" in path for path in paths)


CONFLICTING_LINK = """\
[route:phones]
playback = 1/2
output = 7/8
stereo = false

[route:phones-direct]
playback = 7/8
output = 7/8
"""


def test_routes_disagreeing_on_the_link_are_rejected(session_mod, tmp_path):
    # The stereo link belongs to the hardware pair, not to a route. Left
    # unchecked the last link message wins while both routes still write
    # their own mix shape, and the mismatched one silently loses an
    # output -- reproduced on a UCX II before this check existed.
    path = tmp_path / "routing.conf"
    path.write_text(CONFLICTING_LINK)
    with pytest.raises(session_mod.ConfigError) as excinfo:
        session_mod.load_config(path)
    message = str(excinfo.value)
    assert "7/8" in message
    assert "phones" in message
    assert "phones-direct" in message


def test_routes_agreeing_on_the_link_are_accepted(session_mod, tmp_path):
    path = tmp_path / "routing.conf"
    path.write_text(CONFLICTING_LINK.replace(
        "[route:phones-direct]\nplayback = 7/8\noutput = 7/8\n",
        "[route:phones-direct]\nplayback = 7/8\noutput = 7/8\nstereo = false\n"))
    assert len(session_mod.load_config(path).routes) == 2


def test_mono_routes_never_conflict(session_mod, tmp_path):
    # A mono route has no pair to link, so it must not trip the check.
    path = tmp_path / "routing.conf"
    path.write_text("[route:a]\nplayback = 3\noutput = 7\n\n"
                    "[route:b]\nplayback = 4\noutput = 7\n")
    assert len(session_mod.load_config(path).routes) == 2


def test_the_documented_defaults_are_the_actual_defaults(session_mod):
    # These are the values the README and routing.conf.example promise.
    # A silent change would move a UDP port or a channel limit under
    # users who never wrote them down.
    assert session_mod.DEFAULT_DEVICE_NAME == "Fireface UCX II"
    assert session_mod.DEFAULT_USB_ID == "2a39:3fd9"
    assert session_mod.DEFAULT_OSC_PORT == 7222
    assert session_mod.DEFAULT_OSC_RECV_PORT == 8222
    assert session_mod.DEFAULT_DEVICE_TIMEOUT == 30.0
    assert (session_mod.LEVEL_MIN, session_mod.LEVEL_MAX) == (-65.0, 6.0)
    assert (session_mod.CHANNEL_MIN, session_mod.CHANNEL_MAX) == (1, 64)
    # The compensation for oscmix halving the gain on the unlinked path.
    assert abs(session_mod.UNLINKED_GAIN_OFFSET - 6.0206) < 0.001
    assert session_mod.__version__.count(".") == 2


def test_config_discovery_prefers_the_environment(session_mod, tmp_path,
                                                  monkeypatch):
    explicit = tmp_path / "explicit.conf"
    explicit.write_text("[route:x]\nplayback = 1/2\noutput = 1/2\n")
    monkeypatch.setenv("OSCMIX_CONFIG", str(explicit))
    assert session_mod.discover_config_path() == explicit


def test_config_discovery_finds_the_xdg_location(session_mod, tmp_path,
                                                 monkeypatch):
    monkeypatch.delenv("OSCMIX_CONFIG", raising=False)
    xdg = tmp_path / "xdg"
    (xdg / "oscmix").mkdir(parents=True)
    expected = xdg / "oscmix" / "routing.conf"
    expected.write_text("[route:x]\nplayback = 1/2\noutput = 1/2\n")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(xdg))
    assert session_mod.discover_config_path() == expected


def test_config_discovery_resolves_in_the_environment_it_is_given(
        session_mod, tmp_path, monkeypatch):
    """The unit's environment, not this process's (0.6.10); with no
    HOME in it, the password database, which is what `~` expands to."""
    from oscmix_desk import config as config_mod

    home = tmp_path / "home"
    (home / ".config" / "oscmix").mkdir(parents=True)
    expected = home / ".config" / "oscmix" / "routing.conf"
    expected.write_text("[route:x]\nplayback = 1/2\noutput = 1/2\n")
    monkeypatch.setenv("OSCMIX_CONFIG", str(tmp_path / "shell.conf"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "shell-xdg"))
    monkeypatch.setenv("HOME", str(tmp_path / "shell-home"))
    assert session_mod.discover_config_path({"HOME": str(home)}) == expected
    monkeypatch.setattr(config_mod.pwd, "getpwuid",
                        lambda uid: type("pw", (), {"pw_dir": str(home)})()
                        if uid == config_mod.os.getuid() else None)
    assert session_mod.discover_config_path({}) == expected
    assert session_mod.discover_config_path(
        {"OSCMIX_CONFIG": str(tmp_path / "named.conf")}) \
        == tmp_path / "named.conf"
    # A relative XDG_CONFIG_HOME is invalid and ignored (the spec), so the
    # answer does not depend on the asking process's cwd.
    assert session_mod.discover_config_path(
        {"HOME": str(home), "XDG_CONFIG_HOME": "rel/xdg"}) == expected
    monkeypatch.setenv("XDG_CONFIG_HOME", "rel/xdg")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("OSCMIX_CONFIG")
    assert session_mod.discover_config_path() == expected
    # A uid without a passwd entry has no home; `~` would stay `~`.
    # Nothing raises (every start and every SIGHUP reconcile passes
    # through here), and the system location is still searched.
    def no_entry(uid):
        raise KeyError(uid)
    monkeypatch.setattr(config_mod.pwd, "getpwuid", no_entry)
    assert session_mod.discover_config_path({}) is None
    system = tmp_path / "etc" / "routing.conf"
    system.parent.mkdir()
    system.write_text("")
    assert session_mod.discover_config_path(
        {"OSCMIX_SYSTEM_CONFIG": str(system)}) == system


def test_config_discovery_returns_none_when_there_is_nothing(session_mod,
                                                             tmp_path,
                                                             monkeypatch):
    # No config is a supported state: the defaults leave the mixer alone.
    from oscmix_desk import config as config_mod

    monkeypatch.delenv("OSCMIX_CONFIG", raising=False)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "empty"))
    monkeypatch.setattr(config_mod.Path, "is_file", lambda self: False)
    assert session_mod.discover_config_path() is None


# --------------------------------------------------------------------------
# What routing.conf promises across versions -- ADR 0006, roadmap item B.
#
# A test per direction, because the promise has two of them and they are
# not symmetric: a file from the future must still route what this
# version understands, and a file from the past must keep meaning what
# it meant.
# --------------------------------------------------------------------------

# Sections a *later* version might add. [input:N] and [output:N] used to
# stand here and no longer can: 0.3.0 made them real, which is the rule
# working rather than the test rotting. Whatever replaces them has to be
# genuinely unknown, or this checks nothing.
FUTURE_CONFIG = """\
[device]
name = Fireface UCX II

[osc]
port = 7222

[route:main]
playback = 1/2
output = 1/2
level = 0.0

[profile:tracking]
routes = main

[workspace:1]
layout = wide

[durec]
autoplay = true
"""


def test_a_config_from_a_newer_version_still_applies_what_we_understand(
        session_mod, tmp_path, caplog):
    # The forward direction. 0.3.0 adds [input:N], [output:N] and
    # profiles, and --dump-config makes the file machine-generated, so it
    # will travel to machines running this version. Refusing it whole
    # would mean no routing at all and no restart (exit 2 is
    # RestartPreventExitStatus), leaving the device in whatever state the
    # last boot left it over a section this version simply does not need.
    path = write(tmp_path, FUTURE_CONFIG)
    with caplog.at_level("WARNING"):
        config = session_mod.load_config(path)

    assert [route.name for route in config.routes] == ["main"]
    assert config.routes[0].output == (1, 2)
    assert config.osc_port == 7222

    # ... and it says so, once per unknown section, naming each.
    warnings = [record.getMessage() for record in caplog.records
                if record.levelname == "WARNING"]
    assert len(warnings) == 3
    # `[durec]` rather than `[clock]`: clock became a real section when
    # the global families landed, and an example of "a section from a
    # newer version" has to be one this version will not grow. The
    # roadmap puts DUREC transport under "never -- interactive", so it
    # will stay unknown.
    # Both examples are things the roadmap puts under "never" -- DUREC
    # transport is interactive, workspaces are GUI. `[eq:output:5]` used
    # to stand here and stopped being unknown the day EQ landed, which
    # is churn this test does not need twice.
    for section in ("profile:tracking", "workspace:1", "durec"):
        assert any("[%s]" % section in text for text in warnings), section
    assert any("newer version" in text for text in warnings)


def test_a_typo_in_a_known_section_is_still_an_error(session_mod, tmp_path):
    # The asymmetry is the whole decision. An unknown *section* is how a
    # newer version adds a feature; an unknown *option* in a section this
    # version owns is a typo, and ignoring it would apply a routing that
    # differs from the file in a way nobody is told about.
    path = write(tmp_path, "[route:x]\nplayback = 1/2\noutput = 5/6\n"
                           "levl = -20\n")
    with pytest.raises(session_mod.ConfigError) as excinfo:
        session_mod.load_config(path)
    assert "unknown option" in str(excinfo.value)
    assert "levl" in str(excinfo.value)


def test_a_misspelled_section_name_is_a_warning_not_a_correction(
        session_mod, tmp_path, caplog):
    # [routes:x] is a typo for [route:x], and this rule cannot tell the
    # two apart from a future section name. It costs a route silently
    # dropped, which is the price of the forward promise -- so the
    # warning has to be loud enough to find in a journal, and the
    # startup log states how many routes were actually loaded.
    path = write(tmp_path, "[routes:x]\nplayback = 1/2\noutput = 5/6\n")
    with caplog.at_level("WARNING"):
        config = session_mod.load_config(path)
    assert config.routes == []
    assert "[routes:x]" in caplog.text


def test_todays_config_keeps_meaning_what_it_means(session_mod, tmp_path):
    # The backward direction. Every option this version defines has to
    # keep its meaning; a future parser may add sections and options but
    # may not redefine these. Pinning the surface here makes a silent
    # redefinition a failing test rather than a changed device state.
    path = write(tmp_path, """
[device]
name = Fireface UCX II
usb-id = 2a39:3fd9

[osc]
port = 7222
recv-port = 8222

[route:main]
playback = 1/2
output = 5/6
level = -6.0
volume = -12.0
stereo = false
""")
    config = session_mod.load_config(path)
    assert config.device_name == "Fireface UCX II"
    assert config.usb_id == "2a39:3fd9"
    assert (config.osc_port, config.osc_recv_port) == (7222, 8222)
    route, = config.routes
    assert (route.playback, route.output) == ((1, 2), (5, 6))
    assert (route.level, route.volume, route.stereo) == (-6.0, -12.0, False)


def test_the_known_surface_is_stated_rather_than_discovered(session_mod):
    """ADR 0006 promises these names keep their meaning.

    Changing the list is the point: it turns a compatibility decision
    into a visible edit rather than a diff nobody reads.

    `input` was added here in 0.3.0, and the consequence belongs where
    the edit happens. Under ADR 0006 an unknown *option in a known
    section* is an error, so a config with an input route is **rejected
    whole** by a 0.2.0 install -- playback routes included, exit 2, no
    restart.

    That is the intended reading, not an oversight. A route the older
    version cannot express is a monitoring path; dropping it with a
    warning would leave a tracking session with no monitoring and one
    line in the journal. Failing loudly is the lesser harm. The
    alternative -- putting input routes in a new *section*, which would
    only warn -- was rejected for exactly that reason.

    `serial` was added here in 0.6.8, and the same reading applies: a
    `routing.conf` that names it is rejected whole by a 0.6.7 install,
    exit 2. It is opt-in and only needed on a machine with two
    interfaces of one model, so the example config ships it commented
    out -- a downgrade keeps working for everyone who never needed it.
    """
    from oscmix_desk import config as config_mod

    assert {
        "device": {"name", "usb-id", "serial"},
        "osc": {"port", "recv-port"},
        "route": {"playback", "input", "output", "level", "volume", "stereo"},
    } == config_mod._KNOWN_OPTIONS


# --------------------------------------------------------------------------
# Channels the device actually has -- the register model as a consumer.
#
# CHANNEL_MIN..CHANNEL_MAX is 1..64 and says nothing about any particular
# interface, so `output = 40/41` parsed cleanly on a 20-channel UCX II,
# was applied, and did nothing. At message level the routing was perfect,
# which is the shape of failure this project exists to prevent.
# --------------------------------------------------------------------------

def test_a_channel_the_device_does_not_have_is_rejected(session_mod, tmp_path):
    path = write(tmp_path, "[device]\nname = Fireface UCX II\n\n"
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
    path = write(tmp_path, "[route:x]\nplayback = 1/2\noutput = 40/41\n\n"
                           "[device]\nname = Fireface UCX II\n")
    with pytest.raises(session_mod.ConfigError, match="40"):
        session_mod.load_config(path)


def test_an_unmodelled_device_keeps_working_exactly_as_before(session_mod,
                                                              tmp_path):
    # No opinion, not an error. A model that rejected channels on
    # hardware nobody here can test would be guessing.
    path = write(tmp_path, "[device]\nname = Fireface UFX III\n\n"
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
    from oscmix_desk import config as config_mod

    with caplog.at_level("WARNING"):
        unmodelled = session_mod.load_config(write(
            tmp_path, "[device]\nname = Fireface UFX III\n\n"
                      "[route:x]\nplayback = 1/2\noutput = 40/41\n"
                      "[route:y]\nplayback = 3/4\noutput = 3/4\n"))
    assert caplog.text == "", "the parser itself says nothing"
    assert config_mod.unchecked_routes_warning(unmodelled) == (
        "no register model for 'Fireface UFX III': its 2 route(s) are "
        "written as given, with no check that the device has those "
        "channels (modelled: Fireface UCX II)")
    # A modelled device, the 802 whose channels upstream declares, and a
    # desk with no routes have nothing to be warned about.
    route = "[route:x]\nplayback = 1/2\noutput = %s\n"
    for text in (route % "1/2",
                 "[device]\nname = Fireface 802\n\n" + route % "29/30",
                 "[device]\nname = Fireface UFX III\n"):
        assert config_mod.unchecked_routes_warning(
            session_mod.load_config(write(tmp_path, text))) is None, text


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
    path = write(tmp_path, "[device]\nname =%s\n\n"
                           "[route:x]\nplayback = 1/2\noutput = 1/2\n" % name)
    with pytest.raises(session_mod.ConfigError,
                       match="an empty name matches every card that has a "
                             "MIDI port"):
        session_mod.load_config(path)


def test_a_config_records_the_machine_its_file_resolved_to(session_mod,
                                                           tmp_path):
    """`loaded` is what the file said, and it stays what the file said: the
    live attributes are replaced by `--device`, `--osc-port`, the serial a
    start pins and a running session's own -- and both notices that
    matter (checked for another interface, a desk for another backend)
    compare against the record, not against those."""
    from oscmix_desk import config as config_mod

    assert session_mod.Config().loaded is None, "not loaded, no record"
    named = session_mod.load_config(write(
        tmp_path, "[device]\nname = Fireface 802\nserial = 11223344\n\n"
                  "[osc]\nport = 9001\n"))
    assert named.loaded == config_mod.Machine(
        "Fireface 802", "2a39:3fd9", "11223344", 9001, 8222)
    named.device_name, named.osc_port, named.serial = "X", 7, "9"
    assert named.loaded.device_name == "Fireface 802"
    assert named.loaded.differs_from(config_mod.Machine(
        "Fireface 802", "2a39:3fd9", "5", 9001, 9)) == (
        "serial '11223344' (not '5'), osc recv port 8222 (not 9)")
    # Without a record there is nothing to compare, and nothing is said.
    routed = session_mod.load_config(write(
        tmp_path, "[route:x]\nplayback = 1/2\noutput = 1/2\n"))
    routed.device_name = "X"
    assert "checked for 'Fireface UCX II' and is used for 'X'" in \
        config_mod.replaced_device_warning(routed)
    routed.loaded = None
    assert config_mod.replaced_device_warning(routed) is None
    # Routes, channel sections, global sections: each alone was checked
    # for a device, and a file with none of them was not.
    for text, checked in (("[input:1]\ngain = 10\n", True),
                          ("[reverb]\nenabled = on\n", True),
                          ("[osc]\nport = 9001\n", False)):
        desk = session_mod.load_config(write(tmp_path, text))
        assert bool(desk.channels or desk.globals) is checked, text
        desk.device_name = "X"
        assert (config_mod.replaced_device_warning(desk) is not None) \
            is checked, text


def test_a_desk_is_elsewhere_when_a_restart_would_take_it_somewhere_else():
    """A re-read file is resolved as a restart would resolve it -- the
    command line over it -- and then held against what the session runs.
    Comparing the bare file refused the session's own desk three times
    over: the pinned box once routing.conf named it, the port given on the
    command line once the file named that, and any other port under
    `--osc-port`, with advice to restart that a restart would not have
    followed (all found by review)."""
    from oscmix_desk import CommandLine
    from oscmix_desk.config import Machine

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
    from oscmix_desk.config import Machine

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
    path = write(tmp_path, "[device]\nname = Fireface 802\n\n"
                           "[route:x]\nplayback = 1/2\noutput = 30/31\n")
    with pytest.raises(session_mod.ConfigError) as excinfo:
        session_mod.load_config(path)
    assert "channel 31 does not exist on a Fireface 802" in str(excinfo.value)
    assert "output 1..30" in str(excinfo.value)


def test_the_802_still_accepts_what_it_does_have(session_mod, tmp_path):
    path = write(tmp_path, "[device]\nname = Fireface 802\n\n"
                           "[route:x]\nplayback = 1/2\noutput = 29/30\n")
    assert session_mod.load_config(path).routes[0].output == (29, 30)


def test_a_device_with_no_table_at_all_still_gets_no_opinion(session_mod,
                                                             tmp_path):
    """The half that did not change: an unmodelled name is unconstrained."""
    path = write(tmp_path, "[device]\nname = Some Other Interface\n\n"
                           "[route:x]\nplayback = 1/2\noutput = 63/64\n")
    assert session_mod.load_config(path).routes[0].output == (63, 64)


def test_the_channels_a_valid_config_uses_are_still_accepted(session_mod):
    # The shipped example and the syntax range both still pass.
    config = session_mod.load_config(repo_file("config", "routing.conf.example"))
    assert config.routes[0].output == (1, 2)


# --------------------------------------------------------------------------
# The shape 0.4.0's nested settings have to take (ADR 0014).
# --------------------------------------------------------------------------

# Sub-families this version does not carry. The property under test is
# that an *unrecognised* one still falls through to the warning rather
# than being claimed by the dispatch and then rejected -- which is the
# failure ADR 0014 measured in 0.3.0. Naming a family that later lands
# would only re-test that it landed.
#
# That warning was written here and then ignored two lines below it:
# the list named `dynamics` and `roomeq`, and when dynamics landed this
# test started asserting that a *known* section warns as unknown, which
# it does not. Synthetic names only, now -- the test is about the
# dispatch, and a real family name adds nothing to it.
FORWARD_COMPATIBLE_SHAPES = (
    "[nosuchthing:input:3]\nband1freq = 80\n",
    "[notafamily:output:5]\ncompthres = -18.0\n",
    "[stillnothing:output:1]\nband1gain = -3.0\n",
)

REFUSED_SHAPES = (
    "[input:3]\ngain = 12.0\neq.band1freq = 80\n",
    "[input:3]\ngain = 12.0\neq/band1freq = 80\n",
    "[input:3.eq]\nband1freq = 80\n",
    "[input:3:eq]\nband1freq = 80\n",
    "[input:3/eq]\nband1freq = 80\n",
)

_WORKING = ("[device]\nname = Fireface UCX II\n\n"
            "[route:main]\nplayback = 1/2\noutput = 1/2\nlevel = 0.0\n\n")


def _section_names(shapes):
    """The `<sub>` of each `[<sub>:<family>:<n>]` header in the shapes."""
    return [shape.split("[", 1)[1].split(":", 1)[0] for shape in shapes]


def test_the_unknown_names_are_ones_that_cannot_ever_land():
    """The guard that would have caught this three times.

    `dynamics`, `roomeq` and `crossfeed` were each used somewhere as an
    example of a name this version does not know, and each one later
    landed -- at which point the test asserted the opposite of what it
    was written to assert, silently, because a passing test says
    nothing.

    A name the *device* never reports cannot land, and that is checkable
    against the recording rather than against anybody's memory of what
    is planned.
    """
    reported = json.loads(
        repo_file("tests", "data", "refresh-dump.json").read_text())["registers"]
    segments = {segment for path in reported for segment in path.split("/")}
    for name in _section_names(FORWARD_COMPATIBLE_SHAPES) + ["nosuchoption"]:
        assert name not in segments, (
            "%r is a real register segment, so this stops testing the "
            "unknown-section path the day it is declared" % name)


@pytest.mark.parametrize("shape", FORWARD_COMPATIBLE_SHAPES)
def test_a_family_first_section_is_skipped_not_refused(session_mod, tmp_path,
                                                       caplog, shape):
    """ADR 0014, and the reason it is family-first rather than nested.

    A sub-family this version does not carry has to warn, be skipped,
    and leave the rest applied. The shape that matters is the dispatch:
    it must not claim `[<anything>:input:3]` on the strength of the
    `input` in the middle and then fail on the part it does not know --
    which is exactly what 0.3.0 does with `[input:3:eq]`, and why the
    format is family-first.
    """
    path = write(tmp_path, _WORKING + shape)
    with caplog.at_level("WARNING"):
        config = session_mod.load_config(path)
    assert [route.name for route in config.routes] == ["main"]
    assert any("ignoring unknown section" in record.getMessage()
               for record in caplog.records)


@pytest.mark.parametrize("shape", REFUSED_SHAPES)
def test_the_shapes_adr_0014_rejected_really_do_refuse_the_file(session_mod,
                                                                tmp_path,
                                                                shape):
    """The measurement the decision rests on, kept executable.

    The roadmap's plan assumed sub-sections like `[input:3.eq]` would
    degrade. They do not: the parser dispatches on the `input:` prefix
    before it reads the rest, so the whole file dies on
    `int("3.eq")`. That is a property of a released version, so the
    format had to move rather than the parser.

    If this test ever passes for a shape above, the constraint behind
    ADR 0014 has changed and the ADR should say so.
    """
    path = write(tmp_path, _WORKING + shape)
    with pytest.raises(session_mod.ConfigError):
        session_mod.load_config(path)


# --------------------------------------------------------------------------
# A device without a register table says so; it does not swallow sections.
# --------------------------------------------------------------------------

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
    path = write(tmp_path, "[device]\nname = %s\n\n"
                           "[route:x]\nplayback = 1/2\noutput = 1/2\n\n"
                           "[input:3]\ngain = 12.0\n" % name)
    with caplog.at_level("WARNING"):
        config = session_mod.load_config(path)
    assert len(config.routes) == 1
    assert config.channels == []
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
    path = write(tmp_path, "[device]\nname = Fireface 802\n\n"
                           "[%s]\nsource = Internal\n" % section)
    with caplog.at_level("WARNING"):
        config = session_mod.load_config(path)
    assert config.channels == []
    assert config.globals == []
    (message,) = _warnings(caplog)
    assert "[%s]" % section in message
    assert "Fireface 802" in message
    assert "Fireface UCX II" in message, "the warning names what is modelled"
    assert "newer version" not in message


def test_an_unknown_section_on_a_modelled_device_still_suggests_a_newer_version(
        session_mod, tmp_path, caplog):
    # The other branch keeps its meaning: on the UCX II an unknown
    # section really may come from a newer version (ADR 0006).
    path = write(tmp_path, "[device]\nname = Fireface UCX II\n\n"
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
