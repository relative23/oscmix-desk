"""`--capture NAME`: the desk the device holds now, kept as a new profile.

A capture is a dump that means it: every value active, the remembered ones as
starting values a switch writes, the playback routes desk last wrote taken
from its journal, every other input cell silent through its base and every
unknown playback cell left alone, gains AutoSet owns left to it, device and
ports left to routing.conf. It never replaces a file,
and no profile appears before it has loaded.
"""

import errno
import os

import pytest

from oscmix_desk import cli
from oscmix_desk import profiles as profiles_mod
from oscmix_desk.constants import EXIT_CONFIG, EXIT_FAILURE, EXIT_OK
from oscmix_desk.devices import UCX2
from oscmix_desk.discovery import lock_key
from oscmix_desk.dump import render_config
from oscmix_desk.errors import ConfigError
from oscmix_desk.journal import journal_path, record_written
from oscmix_desk.model import ChannelSetting, Config, Route
from oscmix_desk.profiles import load_profile, save_capture

MAIN = "[device]\nname = Fireface UCX II\n"
DESK = "[route:pb]\noutput = 1/2\nplayback = 1/2\nlevel = -6.0\n"


@pytest.fixture
def config(tmp_path):
    path = tmp_path / "routing.conf"
    path.write_text(MAIN)
    return path


def test_a_capture_becomes_a_loadable_profile(config):
    path = save_capture("evening", DESK, config)
    assert path == config.parent / "profiles/evening.conf"
    assert path.read_text() == DESK
    assert oct(path.stat().st_mode & 0o777) == oct(0o644)
    route, = load_profile("evening", config).routes
    assert route.source == ("playback", (1, 2))
    assert [p.name for p in path.parent.iterdir()] == ["evening.conf"], "no temporary left"


def test_an_existing_profile_is_never_replaced(config):
    save_capture("evening", DESK, config)
    with pytest.raises(ConfigError, match="already exists"):
        save_capture("evening", "[route:other]\noutput = 3\ninput = 3\n", config)
    assert (config.parent / "profiles/evening.conf").read_text() == DESK


def test_a_profile_appearing_meanwhile_is_not_replaced(config, monkeypatch):
    def taken(source, target):
        raise FileExistsError(errno.EEXIST, "File exists", str(target))

    monkeypatch.setattr(profiles_mod.os, "link", taken)
    with pytest.raises(ConfigError, match="appeared while capturing"):
        save_capture("evening", DESK, config)
    assert list((config.parent / "profiles").iterdir()) == []


def test_a_capture_that_would_not_load_is_removed(config):
    with pytest.raises(ConfigError, match=r"would not load as profile 'broken'.*nothing kept"):
        save_capture("broken", "[route:x]\noutput = 99\ninput = 1\n", config)
    assert list((config.parent / "profiles").iterdir()) == []


def test_a_name_that_is_not_a_profile_name_is_refused(config):
    with pytest.raises(ConfigError, match="not a profile name"):
        save_capture("../escape", DESK, config)


def test_the_capture_rendering_declares_everything_and_no_machine():
    desk = Config(device_name=UCX2.name,
                  routes=(Route("in", input=(1, 2), output=(3, 4), level=-6.0),),
                  channels=(ChannelSetting("output", 1, "volume", -10.0),))
    text = render_config(desk, UCX2, capture=True)
    assert text.startswith("# Captured by oscmix-session --capture.")
    assert "[device]" not in text
    assert "[osc]" not in text
    assert "\nvolume = -10.0\n" in text, "a remembered value is a starting value"
    assert "# volume = -10.0" in render_config(desk, UCX2), "a dump keeps it commented"


def test_the_command_keeps_the_device_desk_and_the_written_playback(config, capsys, read_peer):
    journal = journal_path(config, lock_key("2a39:3fd9", "00000000"))
    journal.parent.mkdir(parents=True, exist_ok=True)
    record_written(journal, {"/mix/1/playback/1": ("fi", (-6.0, 0)),
                             "/playback/1/stereo": ("i", (1,)),
                             "/playback/2/stereo": ("i", (1,))})
    reported = [("/output/1/stereo", "i", (1,)), ("/output/2/stereo", "i", (1,)),
                ("/input/1/stereo", "i", (1,)), ("/output/5/stereo", "i", (1,)),
                ("/output/6/stereo", "i", (1,)), ("/mix/5/input/1", "fi", (-12.0, 0))]
    with read_peer(reported):
        assert cli.main(["--config", str(config), "--capture", "evening"]) == EXIT_OK
    assert "captured profile 'evening'" in capsys.readouterr().out
    routes = {route.name: route for route in load_profile("evening", config).routes}
    assert routes["pb1-2-out1-2"].source == ("playback", (1, 2))
    assert routes["in1-2-out5-6"].level == -12.0
    with read_peer(reported):
        assert cli.main(["--config", str(config), "--capture", "evening"]) == EXIT_CONFIG


def test_an_unreadable_device_captures_nothing(config, read_peer):
    with read_peer([]):
        assert cli.main(["--config", str(config), "--capture", "evening"]) == EXIT_FAILURE
    assert not os.path.exists(config.parent / "profiles/evening.conf")


@pytest.mark.parametrize("extra", [["--dump-config"], ["--diff"], ["--dry-run"],
                                   ["--include-written"]])
def test_capture_is_one_action(config, extra):
    with pytest.raises(SystemExit) as refused:
        cli.main(["--config", str(config), "--capture", "evening", *extra])
    assert refused.value.code == 2


def test_a_capture_is_checked_before_it_appears(config, monkeypatch):
    seen = []

    def link(source, target):
        seen.append(load_profile.__module__)
        raise AssertionError("linked before the check")

    monkeypatch.setattr(profiles_mod.os, "link", link)
    with pytest.raises(ConfigError, match="would not load"):
        save_capture("broken", "[route:x]\noutput = 99\ninput = 1\n", config)
    assert seen == [], "a profile that does not load never had a name"


def test_a_main_config_with_an_ignored_section_does_not_stop_a_capture(config):
    config.write_text(MAIN + "\n[future-section]\nvalue = 1\n")
    assert save_capture("evening", DESK, config).exists()


def test_a_directory_that_cannot_be_written_is_a_refusal_not_a_traceback(config, monkeypatch):
    def full(*_args, **_kwargs):
        raise OSError(errno.ENOSPC, "No space left on device")

    monkeypatch.setattr(profiles_mod.tempfile, "mkstemp", full)
    with pytest.raises(ConfigError, match=r"cannot store profile 'evening'.*No space left"):
        save_capture("evening", DESK, config)


def test_a_capture_silences_what_it_does_not_route_and_leaves_autoset_its_gains():
    desk = Config(device_name=UCX2.name,
                  channels=(ChannelSetting("input", 1, "autoset", True),
                            ChannelSetting("input", 1, "gain", 30.0),
                            ChannelSetting("input", 3, "gain", 12.0)))
    text = render_config(desk, UCX2, capture=True, autoset={"/input/1/gain"})
    assert "\n[desk]\nbase = clear-inputs\n" in text
    assert "\n# gain = 30.0   # AutoSet owns it" in text
    assert "\ngain = 12.0\n" in text


def test_a_captured_desk_with_autoset_on_switches_without_a_gain_write(config, read_peer):
    reported = [("/input/1/autoset", "i", (1,)), ("/input/2/autoset", "i", (0,)),
                ("/input/3/autoset", "i", (0,)), ("/input/4/autoset", "i", (0,)),
                ("/input/1/stereo", "i", (1,)), ("/input/1/gain", "f", (30.0,)),
                ("/input/2/gain", "f", (30.0,)), ("/input/3/gain", "f", (12.0,)),
                ("/output/1/stereo", "i", (1,))]
    with read_peer(reported):
        assert cli.main(["--config", str(config), "--capture", "evening"]) == EXIT_OK
    profile = load_profile("evening", config)
    gains = {s.channel: s.value for s in profile.channels if s.option == "gain"}
    assert 1 not in gains, "AutoSet's"
    assert 2 not in gains, "its linked partner's"
    assert gains[3] == 12.0, "AutoSet reported off"
    assert profile.base == "clear-inputs", "never silences playback it does not know"


def test_a_capture_without_known_playback_says_so(config, read_peer, caplog):
    with read_peer([("/output/1/stereo", "i", (1,)), ("/mix/1/input/1", "fi", (-6.0, 0))]):
        assert cli.main(["--config", str(config), "--capture", "evening"]) == EXIT_OK
    assert "the capture knows no playback routing" in caplog.text
    profile = load_profile("evening", config)
    assert not [route for route in profile.routes if route.playback]
    from oscmix_desk.reconcile import desired
    assert not [entry for entry in desired(profile) if "/playback/" in entry.path], (
        "switching to it writes no playback cell at all")
