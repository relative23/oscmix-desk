"""`mirror =`: an output that sends what another sends (manual 27.3).

TotalMix keeps a mirrored submix equal to its source while the mirror's own
fader, EQ and dynamics stay its own. Desk does the same with what it declares:
each route of the source output is declared again for the mirror, without the
route's output volume, when the config loads -- so every apply keeps the two
equal, and nothing downstream needs to know the word.
"""

import pytest

from oscmix_desk.config import load_config
from oscmix_desk.errors import ConfigError
from oscmix_desk.pipewire import generate_pipewire_conf
from oscmix_desk.reconcile import desired

DEVICE = "[device]\nname = Fireface UCX II\n"
MONITORS = """
[route:music]
playback = 1/2
output = 1/2
level = -6.0
volume = -10.0

[route:guitar]
input = 3/4
output = 1/2
level = -12.0
pan = 20
"""


def load(tmp_path, body):
    path = tmp_path / "routing.conf"
    path.write_text(DEVICE + body)
    return load_config(path)


def test_a_pair_mirror_copies_every_route_without_its_volume(tmp_path):
    config = load(tmp_path, MONITORS + "\n[output:3]\nmirror = 1/2\n")
    mirrored = {route.name: route for route in config.routes if route.mirror_of}
    assert sorted(mirrored) == ["guitar-mirror-3-4", "music-mirror-3-4"]
    music = mirrored["music-mirror-3-4"]
    assert (music.output, music.playback, music.level, music.volume) == ((3, 4), (1, 2), -6.0,
                                                                         None)
    guitar = mirrored["guitar-mirror-3-4"]
    assert (guitar.source, guitar.pan, guitar.mirror_of) == (("input", (3, 4)), 20, (1, 2))


def test_the_mirror_is_written_like_any_route(tmp_path):
    entries = {entry.path: entry.args
               for entry in desired(load(tmp_path, MONITORS + "\n[output:3]\nmirror = 1/2\n"))}
    assert entries["/mix/3/playback/1"] == (-6.0, 0)
    assert entries["/mix/3/input/3"] == (-12.0, 20)
    assert entries["/output/3/stereo"] == (1,)
    assert "/output/3/volume" not in entries
    assert entries["/output/1/volume"] == (-10.0,)


def test_a_mono_mirror_copies_mono_routes(tmp_path):
    body = ("\n[route:talk]\ninput = 5\noutput = 5\nlevel = -3.0\nstereo = false\n"
            "\n[output:7]\nmirror = 5\n")
    config = load(tmp_path, body)
    mirror, = (route for route in config.routes if route.mirror_of)
    assert (mirror.output, mirror.input, mirror.level) == ((7,), (5,), -3.0)


def test_a_mirror_of_an_output_without_routes_adds_nothing(tmp_path):
    config = load(tmp_path, "\n[output:3]\nmirror = 1/2\nvolume = -20.0\n")
    assert config.routes == ()
    assert [c.option for c in config.channels] == ["volume"], "its own options still apply"


@pytest.mark.parametrize(("body", "reason"), [
    ("\n[output:4]\nmirror = 1/2\n", "must name its odd output"),
    ("\n[output:3]\nmirror = 2/3\n", "not an output pair"),
    ("\n[output:3]\nmirror = 3/4\n", "cannot mirror itself"),
    ("\n[output:3]\nmirror = 21\n", "has no output"),
    ("\n[output:3]\nmirror = left\n", "expected an output"),
    ("\n[output:3]\nmirror = 1/2/3\n", "one output or one pair"),
    ("\n[output:3]\nmirror = 1\n", "only partly covers"),
    ("\n[output:3]\nmirror = 1/2\n[output:5]\nmirror = 3/4\n", "itself a mirror"),
    ("\n[output:3]\nmirror = 1/2\n[output:4]\nmirror = 1\n", "target of two mirrors"),
    ("\n[output:3]\nmirror = 1/2\n[route:own]\nplayback = 3/4\noutput = 3/4\n",
     "comes only from its source"),
])
def test_refusals(tmp_path, body, reason):
    with pytest.raises(ConfigError, match=reason):
        load(tmp_path, MONITORS + body)


def test_the_option_is_listed_with_the_output_options(tmp_path):
    with pytest.raises(ConfigError, match=r"valid: .*mirror"):
        load(tmp_path, "\n[output:3]\nmirrors = 1/2\n")


def test_an_unmodelled_device_ignores_it_like_every_output_option(tmp_path, caplog):
    path = tmp_path / "routing.conf"
    path.write_text("[device]\nname = Some Other Interface\n\n[output:3]\nmirror = 1/2\n")
    assert load_config(path).routes == ()


def test_a_mirror_gets_no_desktop_sink_of_its_own(tmp_path):
    text = generate_pipewire_conf(load(tmp_path, MONITORS + "\n[output:3]\nmirror = 1/2\n"),
                                  "target", None)
    assert "music" in text
    assert "mirror" not in text
