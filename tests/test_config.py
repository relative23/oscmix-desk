"""routing.conf parsing: valid configs, defaults, and error reporting."""


import oracle
import pytest
from support import repo_file, routing_conf

from oscmix_desk import paths as paths_mod


def test_defaults_without_file(session_mod):
    config = session_mod.load_config(None)
    assert config.device_name == "Fireface UCX II"
    assert config.usb_id == "2a39:3fd9"
    assert config.osc_port == 7222
    assert config.osc_recv_port == 8222
    assert config.routes == []


def test_recv_port_option(session_mod, tmp_path):
    path = routing_conf(tmp_path, "[osc]\nport = 9000\nrecv-port = 9001\n")
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
    path = routing_conf(tmp_path, """
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
    path = routing_conf(tmp_path, """
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
    path = routing_conf(tmp_path, snippet)
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

    home = tmp_path / "home"
    (home / ".config" / "oscmix").mkdir(parents=True)
    expected = home / ".config" / "oscmix" / "routing.conf"
    expected.write_text("[route:x]\nplayback = 1/2\noutput = 1/2\n")
    monkeypatch.setenv("OSCMIX_CONFIG", str(tmp_path / "shell.conf"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "shell-xdg"))
    monkeypatch.setenv("HOME", str(tmp_path / "shell-home"))
    assert session_mod.discover_config_path({"HOME": str(home)}) == expected
    monkeypatch.setattr(paths_mod.pwd, "getpwuid",
                        lambda uid: type("pw", (), {"pw_dir": str(home)})()
                        if uid == paths_mod.os.getuid() else None)
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
    monkeypatch.setattr(paths_mod.pwd, "getpwuid", no_entry)
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
