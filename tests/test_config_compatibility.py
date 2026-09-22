"""What routing.conf promises across versions (ADR 0006, ADR 0014).

Two directions, and they are not symmetric: a file from the future must
still route what this version understands, and a file from the past must
keep meaning what it meant.
"""

import json

import pytest
from support import repo_file, routing_conf

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
    path = routing_conf(tmp_path, FUTURE_CONFIG)
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
    path = routing_conf(tmp_path, "[route:x]\nplayback = 1/2\noutput = 5/6\n"
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
    path = routing_conf(tmp_path, "[routes:x]\nplayback = 1/2\noutput = 5/6\n")
    with caplog.at_level("WARNING"):
        config = session_mod.load_config(path)
    assert config.routes == ()
    assert "[routes:x]" in caplog.text

def test_todays_config_keeps_meaning_what_it_means(session_mod, tmp_path):
    # The backward direction. Every option this version defines has to
    # keep its meaning; a future parser may add sections and options but
    # may not redefine these. Pinning the surface here makes a silent
    # redefinition a failing test rather than a changed device state.
    path = routing_conf(tmp_path, """
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
    path = routing_conf(tmp_path, _WORKING + shape)
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
    path = routing_conf(tmp_path, _WORKING + shape)
    with pytest.raises(session_mod.ConfigError):
        session_mod.load_config(path)
