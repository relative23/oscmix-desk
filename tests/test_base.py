"""`[desk] base`: what the matrix is beneath the routes.

TotalMix resets a mix before one is built (Options, Reset Mix; DAW mode). A
base does that for a desk, so a profile is a whole matrix and an omitted route
is a silent one. A base never decides a link the desk does not declare: it
silences every register of a block whose links nobody declares, which the
backend folds onto a linked pair, and declares straight playback as routes of
its own that link only pairs the desk sets alike. Everything it writes goes out
on an explicit start or switch and is then REMEMBERed.
"""

import pytest

from oscmix_desk.config import load_config
from oscmix_desk.errors import ConfigError
from oscmix_desk.pipewire import generate_pipewire_conf
from oscmix_desk.profiles import compare_desks
from oscmix_desk.reconcile import ApplyIntent, application_plan, desired, remembered_paths

DEVICE = "[device]\nname = Fireface UCX II\n"
MUSIC = "\n[route:music]\nplayback = 1/2\noutput = 1/2\nlevel = -6.0\n"


def load(tmp_path, body, name="routing.conf"):
    path = tmp_path / name
    path.write_text(DEVICE + body)
    return load_config(path)


def base_of(config):
    return {entry.path: entry.args for entry in desired(config) if entry.base}


def test_keep_is_the_default_and_adds_nothing(tmp_path):
    config = load(tmp_path, MUSIC)
    assert config.base == "keep"
    assert base_of(config) == {}
    assert base_of(load(tmp_path, "\n[desk]\nbase = keep\n" + MUSIC)) == {}


def test_clear_submixes_silences_every_cell_no_route_writes(tmp_path):
    entries = base_of(load(tmp_path, "\n[desk]\nbase = clear-submixes\n" + MUSIC))
    assert set(entries.values()) == {(-65.0, 0)}
    assert not [path for path in entries if path.endswith("/stereo")], "a base links nothing"
    assert "/mix/1/playback/1" not in entries, "the route wins"
    inputs = [path for path in entries if "/input/" in path]
    playbacks = [path for path in entries if "/playback/" in path]
    # Output 1/2 is a declared pair (one row); the other outputs and every input pair are
    # not, so they are silenced per channel, whatever their link on the device.
    assert len(inputs) == 10 * 2 + 9 * 10 * 4
    assert len(playbacks) == 9 * 2 + 9 * 2 + 9 * 9 * 4
    assert {"/mix/1/input/1", "/mix/1/input/2", "/mix/3/input/1", "/mix/4/input/2"} <= set(inputs)
    assert "/mix/2/input/1" not in inputs, "the declared output pair has one register"
    assert {"/mix/3/playback/1", "/mix/4/playback/1"} <= set(playbacks)
    assert "/mix/3/playback/2" not in playbacks, "the declared playback pair has one register"


def test_a_base_never_touches_an_input_link(tmp_path):
    # Linking an input pair hands AutoSet's gain of one channel to its partner.
    body = "\n[desk]\nbase = daw\n\n[input:1]\nautoset = true\n"
    entries = desired(load(tmp_path, body))
    assert not [entry.path for entry in entries if entry.path.startswith("/input/")
                and entry.path.endswith("/stereo")]


def test_a_declared_mono_route_is_addressed_per_channel(tmp_path):
    body = ("\n[desk]\nbase = clear-submixes\n"
            "\n[route:talk]\ninput = 5\noutput = 7\nlevel = -3.0\nstereo = false\n")
    cells = base_of(load(tmp_path, body))
    assert {"/mix/7/input/6", "/mix/8/input/5", "/mix/8/input/6"} <= set(cells)
    assert "/mix/7/input/5" not in cells, "the route's own cell"


def test_straight_playback_declares_each_pair_on_its_outputs(tmp_path):
    body = "\n[desk]\nbase = straight-playback\n\n[route:x]\nplayback = 1/2\noutput = 3/4\n"
    config = load(tmp_path, body)
    based = {route.name: route for route in config.routes if route.base}
    assert len(based) == 10
    assert (based["[desk] base 1/2"].playback, based["[desk] base 1/2"].output) == ((1, 2), (1, 2))
    cells = base_of(config)
    assert cells["/mix/1/playback/1"] == (0.0, 0)
    assert cells["/mix/3/playback/3"] == (0.0, 0)
    assert cells["/playback/5/stereo"] == (1,), "the base owns the links of its own routes"
    assert "/playback/1/stereo" not in cells, "the desk declares that one"
    assert cells["/mix/5/playback/1"] == (-65.0, 0)
    assert "/mix/3/playback/1" not in cells, "the desk's route"
    assert not any("/input/" in path for path in cells), "inputs are left alone"


def test_a_route_of_the_desk_wins_over_the_base(tmp_path):
    body = ("\n[desk]\nbase = straight-playback\n" + MUSIC)
    config = load(tmp_path, body)
    assert "[desk] base 1/2" not in {route.name for route in config.routes}
    entries = {entry.path: entry for entry in desired(config)}
    assert entries["/mix/1/playback/1"].args == (-6.0, 0)
    assert not entries["/mix/1/playback/1"].base
    assert not entries["/output/1/stereo"].base, "a link both declare is the desk's"


def test_straight_playback_follows_declared_mono_pairs(tmp_path):
    body = ("\n[desk]\nbase = straight-playback\n"
            "\n[route:a]\nplayback = 3\noutput = 9\nstereo = false\n")
    config = load(tmp_path, body)
    based = {route.name: route for route in config.routes if route.base}
    # Playback 3/4 is declared two channels: two mono routes, and output 3/4 unlinked.
    assert based["[desk] base 3"].output == (3,)
    assert based["[desk] base 4"].playback == (4,)
    # Output 9/10 is declared two channels: the pair plays on it split, playback linked.
    assert based["[desk] base 9/10"].stereo is False
    cells = base_of(config)
    assert cells["/mix/3/playback/3"] == (0.0, 0)
    assert cells["/mix/4/playback/4"] == (0.0, 0)
    assert cells["/output/3/stereo"] == (0,)
    assert cells["/mix/9/playback/9"][1] == -100
    assert cells["/mix/10/playback/9"][1] == 100


def test_straight_playback_refuses_a_mono_playback_pair_on_a_linked_output(tmp_path):
    body = ("\n[desk]\nbase = straight-playback\n"
            "\n[route:a]\nplayback = 3\noutput = 9\nstereo = false\n"
            "\n[route:b]\nplayback = 5/6\noutput = 3/4\n")
    with pytest.raises(ConfigError, match="playback 3/4 is two mono channels and output 3/4"):
        load(tmp_path, body)


def test_the_base_links_no_pair_the_desk_sets_apart(tmp_path):
    body = "\n[output:5]\nvolume = -10.0\n\n[output:6]\nvolume = -20.0\n"
    with pytest.raises(ConfigError, match="output 5 and 6 set volume differently"):
        load(tmp_path, "\n[desk]\nbase = straight-playback\n" + body)
    cleared = base_of(load(tmp_path, "\n[desk]\nbase = clear-submixes\n" + body))
    assert "/output/5/stereo" not in cleared, "silence needs no link"
    alike = "\n[output:5]\nvolume = -10.0\n\n[output:6]\nvolume = -10.0\n"
    assert base_of(load(tmp_path, "\n[desk]\nbase = daw\n" + alike))["/output/5/stereo"] == (1,)
    split = body + "\n[route:own]\nplayback = 5/6\noutput = 5/6\nstereo = false\n"
    assert load(tmp_path, "\n[desk]\nbase = daw\n" + split).base == "daw"


def test_a_mirror_sends_its_sources_base_routes(tmp_path):
    body = "\n[desk]\nbase = straight-playback\n\n[output:3]\nmirror = 1/2\n"
    config = load(tmp_path, body)
    assert "[desk] base 3/4" not in {route.name for route in config.routes}
    cells = base_of(config)
    assert cells["/mix/3/playback/1"] == (0.0, 0), "what 1/2 sends"
    assert cells["/mix/3/playback/3"] == (-65.0, 0), "not a 1:1 of its own"


def test_daw_also_silences_hardware_monitoring(tmp_path):
    cells = base_of(load(tmp_path, "\n[desk]\nbase = daw\n"))
    assert cells["/mix/1/input/1"] == (-65.0, 0)
    assert cells["/mix/5/playback/5"] == (0.0, 0)


@pytest.mark.parametrize(("body", "reason"), [
    ("\n[desk]\nbase = total-reset\n", "expected one of keep, clear-inputs, clear-submixes"),
    ("\n[desk]\nbased = daw\n", "unknown option"),
])
def test_refusals(tmp_path, body, reason):
    with pytest.raises(ConfigError, match=reason):
        load(tmp_path, body)


def test_the_base_is_written_explicitly_and_remembered_afterwards(tmp_path):
    config = load(tmp_path, "\n[desk]\nbase = daw\n" + MUSIC)
    base = set(base_of(config))
    explicit = {write.path for write in application_plan(config, ApplyIntent.EXPLICIT).writes}
    assert base <= explicit
    assert base <= set(remembered_paths(config))
    for intent in (ApplyIntent.REPAIR, ApplyIntent.RECONCILE):
        later = {write.path for write in application_plan(config, intent).writes}
        assert not base & later, "a later change in TotalMix stays until the next switch"
    assert "/mix/1/playback/1" in explicit


def test_links_go_before_any_cell(tmp_path):
    entries = desired(load(tmp_path, "\n[desk]\nbase = daw\n" + MUSIC))
    phases = [entry.phase for entry in entries]
    assert phases == sorted(phases)
    last_link = max(i for i, entry in enumerate(entries) if entry.path.endswith("/stereo"))
    first_cell = min(i for i, entry in enumerate(entries) if entry.path.startswith("/mix/"))
    assert last_link < first_cell


def test_a_base_gets_no_desktop_sinks(tmp_path):
    text = generate_pipewire_conf(load(tmp_path, "\n[desk]\nbase = daw\n" + MUSIC),
                                  "target", None)
    assert "music" in text
    assert "[desk] base" not in text


def test_a_profile_brings_its_own_base_and_compare_shows_it(tmp_path):
    main = tmp_path / "routing.conf"
    main.write_text(DEVICE + MUSIC)
    (tmp_path / "profiles").mkdir()
    (tmp_path / "profiles/clean.conf").write_text("[desk]\nbase = clear-submixes\n" + MUSIC)
    lines = compare_desks(":main", "clean", main)
    assert "/mix/1/input/1: - -> -65.0 0" in lines
    assert all("policy" not in line for line in lines if line.startswith("/mix/1/input/1:"))


def test_a_pinned_playback_unmute_owns_its_columns_without_the_base(tmp_path):
    # The base's 1:1 route is REMEMBERed: a repair would release the mute over cells it no
    # longer writes. So the base cannot complete the columns an unmute owns.
    columns = "".join("\n[route:c%d]\nplayback = 1/2\noutput = %d/%d\nlevel = -65\n"
                      % (out, out, out + 1) for out in range(3, 21, 2))
    body = ("\n[desk]\nbase = straight-playback\n\n[pin]\nplayback.mute = pin\n"
            "\n[playback:1]\nmute = false\n\n[playback:2]\nmute = false\n" + columns)
    with pytest.raises(ConfigError, match="base's routes do not count"):
        load(tmp_path, body)
    own = body + "\n[route:main]\nplayback = 1/2\noutput = 1/2\n"
    config = load(tmp_path, own)
    for intent in (ApplyIntent.REPAIR, ApplyIntent.RECONCILE):
        application_plan(config, intent)


def test_silence_that_may_fold_is_written_but_not_compared(tmp_path):
    config = load(tmp_path, "\n[desk]\nbase = clear-submixes\n" + MUSIC)
    entries = {entry.path: entry for entry in desired(config) if entry.base}
    assert entries["/mix/3/input/1"].compared, "the odd/odd register is always reported"
    assert not entries["/mix/4/input/1"].compared, "folds onto 3/4 when the device links it"
    assert not entries["/mix/3/input/2"].compared
    assert entries["/mix/1/input/1"].compared, "output 1/2 is a declared pair"
    assert not entries["/mix/1/input/2"].compared
    from oscmix_desk.verify import expected_registers
    expected = expected_registers(config)
    assert "/mix/3/input/1" in expected
    assert "/mix/4/input/1" not in expected
    seen = {path: (float("-inf"), 0) for path in expected if path.startswith("/mix/")}
    from oscmix_desk.devices import device_for_name
    from oscmix_desk.reconcile import MISMATCHED, MISSING, plan
    result = plan(desired(config), seen, device_for_name(config.device_name))
    assert not [w for w in result.writes
                if w.reason in (MISMATCHED, MISSING) and w.path.startswith("/mix/")
                and "/input/" in w.path], "--diff after a clean apply: nothing differs"
    seen["/mix/4/input/1"] = (0.0, 0)
    loud = plan(desired(config), seen, device_for_name(config.device_name))
    assert [w.path for w in loud.writes if w.reason == MISMATCHED] == ["/mix/4/input/1"], (
        "an unlinked device reports the cell, so it is compared")


def test_clear_inputs_leaves_playback_alone(tmp_path):
    cells = base_of(load(tmp_path, "\n[desk]\nbase = clear-inputs\n" + MUSIC))
    assert cells
    assert all("/input/" in path for path in cells)


MONO_1_2 = ("\n[route:l]\nplayback = 1\noutput = 1\n"
            "\n[route:r]\nplayback = 2\noutput = 2\n")


@pytest.mark.parametrize(("body", "reason"), [
    ("\n[output:3]\nmirror = 1\n", "output 1/2, but one of those outputs is mirrored alone"),
    (MONO_1_2 + "\n[output:4]\nmirror = 1\n",
     "output 3/4, but output 3/4 is half a mirror target"),
    (MONO_1_2 + "\n[output:7]\nmirror = 1\n",
     "output 7/8, but output 7/8 is half a mirror target"),
    ("\n[route:a]\nplayback = 3\noutput = 9\nstereo = false\n\n[output:5]\nmirror = 3/4\n",
     "output 3/4, but it plays there as two mono channels and the pair is mirrored whole"),
])
def test_a_mirror_the_base_cannot_copy_is_refused_plainly(tmp_path, body, reason):
    with pytest.raises(ConfigError, match=reason):
        load(tmp_path, "\n[desk]\nbase = straight-playback\n" + body)
