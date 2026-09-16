"""PipeWire named-sink generation (--pipewire-sinks)."""

import json

import pytest


def make_config(session_mod, routes):
    config = session_mod.Config()
    config.routes = routes
    return config


def test_position_fallback_mapping(session_mod):
    assert session_mod.pipewire_positions((1, 2)) == ["FL", "FR"]
    assert session_mod.pipewire_positions((5, 6)) == ["FC", "LFE"]
    assert session_mod.pipewire_positions((7, 8)) == ["SL", "SR"]


def test_position_fallback_rejects_channels_above_eight(session_mod):
    with pytest.raises(session_mod.ConfigError):
        session_mod.pipewire_positions((9, 10))


def test_positions_follow_the_sink_layout_when_known(session_mod):
    # Pro-audio/Direct profile: 20 channels labeled AUX0..AUX19.
    aux = ["AUX%d" % i for i in range(20)]
    assert session_mod.pipewire_positions((5, 6), aux) == ["AUX4", "AUX5"]
    assert session_mod.pipewire_positions((19, 20), aux) == ["AUX18", "AUX19"]
    with pytest.raises(session_mod.ConfigError):
        session_mod.pipewire_positions((21,), aux)


def test_parse_positions_from_spa_json_string(pipewire_mod):
    assert pipewire_mod._parse_positions("[ AUX0, AUX1 ]") == ["AUX0", "AUX1"]
    assert pipewire_mod._parse_positions(["FL", "FR"]) == ["FL", "FR"]
    assert pipewire_mod._parse_positions("") is None
    assert pipewire_mod._parse_positions(None) is None


def test_generated_conf_contains_sink_per_pair_route(session_mod):
    routes = [
        session_mod.Route(name="monitors", playback=(5, 6), output=(5, 6)),
        session_mod.Route(name="sub", playback=(3,), output=(7,)),  # mono: skipped
    ]
    conf = session_mod.generate_pipewire_conf(
        make_config(session_mod, routes), target="alsa_output.fireface"
    )
    assert conf.count("libpipewire-module-loopback") == 1
    assert 'node.description = "monitors"' in conf
    assert 'node.name = "oscmix.monitors"' in conf
    assert "audio.position = [ FC LFE ]" in conf
    assert 'target.object = "alsa_output.fireface"' in conf
    assert "media.class = Audio/Sink" in conf


def test_node_names_are_sanitized(session_mod):
    routes = [session_mod.Route(name="krk monitors!", playback=(5, 6),
                                output=(5, 6))]
    conf = session_mod.generate_pipewire_conf(
        make_config(session_mod, routes), target="t"
    )
    assert 'node.name = "oscmix.krk_monitors_"' in conf
    assert 'node.description = "krk monitors!"' in conf


def test_non_identity_route_gets_a_note(session_mod):
    routes = [session_mod.Route(name="krk", playback=(1, 2), output=(5, 6))]
    conf = session_mod.generate_pipewire_conf(
        make_config(session_mod, routes), target="t"
    )
    assert "playback = 5/6" in conf
    assert "output = 5/6" in conf
    assert "NOTE" in conf


def test_identity_route_needs_no_note(session_mod):
    routes = [session_mod.Route(name="krk", playback=(5, 6), output=(5, 6))]
    conf = session_mod.generate_pipewire_conf(
        make_config(session_mod, routes), target="t"
    )
    assert "NOTE" not in conf


def test_missing_target_produces_fixme_placeholder(session_mod):
    routes = [session_mod.Route(name="m", playback=(1, 2), output=(1, 2))]
    conf = session_mod.generate_pipewire_conf(
        make_config(session_mod, routes), target=None
    )
    assert "FIXME" in conf


def test_no_pair_routes_is_a_config_error(session_mod):
    with pytest.raises(session_mod.ConfigError):
        session_mod.generate_pipewire_conf(make_config(session_mod, []), "t")


PW_DUMP = json.dumps([
    {"info": {"props": {"media.class": "Audio/Source",
                        "node.name": "mic"}}},
    {"info": {"props": {"media.class": "Audio/Sink",
                        "node.name": "alsa_output.pci-hdmi",
                        "node.description": "HDMI Audio",
                        "audio.position": "[ FL, FR ]"}}},
    {"info": {"props": {
        "media.class": "Audio/Sink",
        "node.name": "alsa_output.usb-RME_Fireface_UCX_II-00.Direct__sink",
        "node.description": "Fireface UCX II Direct",
        "audio.position": "[ AUX0, AUX1, AUX2, AUX3 ]"}}},
    {"id": 99},  # object without info/props
])


def test_sink_info_finds_fireface_with_positions(session_mod):
    info = session_mod.pw_sink_info("Fireface UCX II", dump_text=PW_DUMP)
    assert info == (
        "alsa_output.usb-RME_Fireface_UCX_II-00.Direct__sink",
        ["AUX0", "AUX1", "AUX2", "AUX3"],
    )


def test_sink_info_looks_up_explicit_target(session_mod):
    info = session_mod.pw_sink_info("whatever", target="alsa_output.pci-hdmi",
                                    dump_text=PW_DUMP)
    assert info == ("alsa_output.pci-hdmi", ["FL", "FR"])


def test_sink_info_returns_none_without_match(session_mod):
    assert session_mod.pw_sink_info(
        "Babyface", dump_text=json.dumps([])) is None
    assert session_mod.pw_sink_info("X", dump_text="not json") is None
    assert session_mod.pw_sink_info("X", target="missing-node",
                                    dump_text=PW_DUMP) is None


def test_generate_uses_sink_layout_for_positions(session_mod):
    routes = [session_mod.Route(name="krk", playback=(5, 6), output=(5, 6))]
    aux = ["AUX%d" % i for i in range(20)]
    conf = session_mod.generate_pipewire_conf(
        make_config(session_mod, routes), target="t", sink_positions=aux
    )
    assert "audio.position = [ AUX4 AUX5 ]" in conf


def test_routes_sharing_an_output_pair_produce_one_sink(session_mod):
    # Typical setup: a default-sink route (1/2 -> 5/6) plus the identity
    # route for the named sink (5/6 -> 5/6). One sink, first name wins,
    # and the identity route silences the NOTE.
    routes = [
        session_mod.Route(name="monitors", playback=(1, 2), output=(5, 6)),
        session_mod.Route(name="monitors-direct", playback=(5, 6),
                          output=(5, 6)),
    ]
    conf = session_mod.generate_pipewire_conf(
        make_config(session_mod, routes), target="t"
    )
    assert conf.count("libpipewire-module-loopback") == 1
    assert 'node.description = "monitors"' in conf
    # The identity route exists, so no missing-identity warning...
    assert "identity route in routing.conf" not in conf
    # ...only the informational note about the folded duplicate route.
    assert "'monitors-direct' also targets outputs 5/6" in conf


def test_colliding_route_names_are_reported_not_silent(session_mod):
    # Two differently named routes onto the same output pair: one sink,
    # but the generated file must say which route was folded away.
    routes = [
        session_mod.Route(name="monitors", playback=(1, 2), output=(5, 6)),
        session_mod.Route(name="studio", playback=(5, 6), output=(5, 6)),
    ]
    conf = session_mod.generate_pipewire_conf(
        make_config(session_mod, routes), target="t"
    )
    assert conf.count("libpipewire-module-loopback") == 1
    assert "'studio' also targets outputs 5/6" in conf
    assert "named after route 'monitors'" in conf


def test_a_quote_in_a_route_name_does_not_break_the_conf(session_mod):
    """routing.conf accepts the name; the conf quoted it raw (0.6.9)."""
    routes = [session_mod.Route(name='the "big" room', playback=(1, 2),
                                output=(1, 2))]
    conf = session_mod.generate_pipewire_conf(
        make_config(session_mod, routes), target="alsa_output.fireface")
    assert 'node.description = "the \\"big\\" room"' in conf
    assert 'node.name = "oscmix.the__big__room"' in conf
