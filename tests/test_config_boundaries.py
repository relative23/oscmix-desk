"""The edges of accepted configuration syntax: ports, channels, names, text."""

import pytest
from support import routing_conf

from oscmix_desk import paths as paths_mod
from oscmix_desk.config import load_config
from oscmix_desk.errors import ConfigError


@pytest.mark.parametrize("option", ["port", "recv-port"])
@pytest.mark.parametrize("port", [1, 65535])
def test_the_whole_port_range_is_accepted(tmp_path, option, port):
    config = load_config(routing_conf(tmp_path, "[osc]\n%s = %d\n" % (option, port)))
    assert getattr(config, "osc_port" if option == "port" else "osc_recv_port") == port


@pytest.mark.parametrize("option", ["port", "recv-port"])
@pytest.mark.parametrize("port", [0, 65536])
def test_a_port_outside_the_range_is_refused(tmp_path, option, port):
    with pytest.raises(ConfigError, match="port"):
        load_config(routing_conf(tmp_path, "[osc]\n%s = %d\n" % (option, port)))


def test_a_percent_sign_is_ordinary_text(tmp_path):
    config = load_config(routing_conf(tmp_path, "[device]\nname = Desk 100%\n"))
    assert config.device_name == "Desk 100%"


def test_more_than_a_pair_of_channels_is_refused(tmp_path):
    with pytest.raises(ConfigError, match="expected a channel"):
        load_config(routing_conf(
            tmp_path, "[route:three]\nplayback = 1/2/3\noutput = 1/2/3\n"))


def test_a_route_name_may_contain_a_colon(tmp_path):
    config = load_config(routing_conf(
        tmp_path, "[route:front:left]\nplayback = 1\noutput = 5\nstereo = no\n"))
    assert [(route.name, route.output) for route in config.routes] == [("front:left", (5,))]


@pytest.mark.parametrize("name", ["Tracking", "mix.2", "A_b-c"])
def test_a_profile_name_may_use_either_letter_case(tmp_path, name):
    path = paths_mod.profile_path(name, tmp_path / "routing.conf")
    assert path == tmp_path / "profiles" / (name + ".conf")


@pytest.mark.parametrize("name", ["", "../routing", "a/b", ".hidden", "-x", "tab\tname"])
def test_a_profile_name_that_is_not_a_plain_identifier_is_refused(tmp_path, name):
    (tmp_path / "routing.conf").write_text("")
    with pytest.raises(ConfigError, match="not a profile name"):
        paths_mod.profile_path(name, tmp_path / "routing.conf")
