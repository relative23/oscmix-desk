"""`--diff-profile A B`: what two desks set differently, without the device.

A desk is a profile, or `:main` for routing.conf, loaded exactly as a switch
loads it. The comparison is over the registers each desk asks for and the
policy it holds them with, so it answers "what would change if I switched"
before anything is connected.
"""

import pytest

from oscmix_desk import cli
from oscmix_desk.constants import EXIT_CONFIG, EXIT_DIFFERS, EXIT_OK
from oscmix_desk.errors import ConfigError
from oscmix_desk.profiles import MAIN_DESK, compare_desks

MAIN = """[device]
name = Fireface UCX II

[route:main]
output = 1/2
playback = 1/2
level = 0.0

[output:1]
volume = -10.0
"""


@pytest.fixture
def desks(tmp_path):
    config = tmp_path / "routing.conf"
    config.write_text(MAIN)
    profiles = tmp_path / "profiles"
    profiles.mkdir()
    (profiles / "tracking.conf").write_text(
        "[route:main]\noutput = 1/2\nplayback = 1/2\nlevel = -6.0\n\n"
        "[output:1]\nvolume = -10.0\n\n[output:3]\nvolume = -20.0\n")
    (profiles / "same.conf").write_text(
        "[route:main]\noutput = 1/2\nplayback = 1/2\nlevel = 0.0\n\n[output:1]\nvolume = -10.0\n")
    (profiles / "pinned.conf").write_text(
        "[route:main]\noutput = 1/2\nplayback = 1/2\nlevel = 0.0\n\n"
        "[output:1]\nvolume = -10.0\n\n[pin]\noutput.volume = pin\n")
    return config


def test_changed_added_and_removed_registers_are_listed_in_order(desks):
    lines = compare_desks(MAIN_DESK, "tracking", desks)
    assert "/mix/1/playback/1: 0.0 0 -> -6.0 0" in lines
    assert "/output/3/volume: - -> -20.0" in lines
    assert all("/output/1/volume" not in line for line in lines)
    paths = [line.split(":")[0] for line in lines]
    assert paths == sorted(paths, key=lambda p: [int(x) if x.isdigit() else x
                                                 for x in p.split("/")])
    assert compare_desks("tracking", MAIN_DESK, desks)[-1] == "/output/3/volume: -20.0 -> -"


def test_identical_desks_have_no_lines(desks):
    assert compare_desks(MAIN_DESK, "same", desks) == []


def test_a_policy_change_alone_is_a_difference(desks):
    lines = compare_desks(MAIN_DESK, "pinned", desks)
    assert lines == ["/output/1/volume: -10.0 -> -10.0 (policy remember -> pin)"]


def test_an_unknown_or_malformed_name_is_a_config_error(desks):
    with pytest.raises(ConfigError, match="no profile 'nope'"):
        compare_desks(MAIN_DESK, "nope", desks)
    with pytest.raises(ConfigError, match="not a profile name"):
        compare_desks(":other", MAIN_DESK, desks)


def test_the_command_exit_codes_follow_diff(desks, capsys):
    assert cli.main(["--config", str(desks), "--diff-profile", ":main", "same"]) == EXIT_OK
    assert "set the same registers" in capsys.readouterr().out
    command = ["--config", str(desks), "--diff-profile", ":main", "tracking"]
    assert cli.main(command) == EXIT_DIFFERS
    out = capsys.readouterr().out
    assert "/output/3/volume: - -> -20.0" in out
    assert "2 register(s) differ; no device access" in out
    assert cli.main(["--config", str(desks), "--diff-profile", ":main", "nope"]) == EXIT_CONFIG


@pytest.mark.parametrize("extra", [["--check"], ["--diff"], ["--profile", "same"],
                                   ["--device", "X"], ["--dry-run"]])
def test_it_is_one_action_and_takes_no_overrides(desks, extra):
    with pytest.raises(SystemExit) as refused:
        cli.main(["--config", str(desks), "--diff-profile", ":main", "same", *extra])
    assert refused.value.code == 2
