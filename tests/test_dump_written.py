"""`--dump-config --include-written`: what desk wrote where the device is silent.

The device reports neither the playback matrix, the playback link flags nor
playback mute. A dump alone therefore cannot reproduce software routing. The
write journal can say what desk itself last sent there -- and the dump says,
just as loudly, that this is desk's record and not the device's.
"""

import pytest

from oscmix_desk import cli
from oscmix_desk.config import load_config
from oscmix_desk.devices import UCX2
from oscmix_desk.discovery import lock_key
from oscmix_desk.dump import WrittenState, render_config, written_state
from oscmix_desk.journal import journal_path, record_written
from oscmix_desk.model import Config

AT = "2026-10-27T10:00:00Z"
LINKED = {"/output/1/stereo": (1,), "/output/2/stereo": (1,)}


def journal(**cells):
    return {path: (tags, args, AT) for path, (tags, args) in cells.items()}


def pair_route():
    return {"/mix/1/playback/1": ("fi", (-6.0, 0)),
            "/playback/1/stereo": ("i", (1,)), "/playback/2/stereo": ("i", (1,))}


def test_a_linked_playback_pair_comes_back_as_its_route():
    state = written_state(journal(**pair_route()), LINKED, UCX2, "j")
    route, = state.routes
    assert route.name == "pb1-2-out1-2"
    assert route.source == ("playback", (1, 2))
    assert (route.output, route.level, route.stereo) == ((1, 2), -6.0, True)


def test_the_unlinked_split_and_a_mono_route_come_back_too():
    cells = {"/mix/3/playback/3": ("fi", (6.0, -100)), "/mix/4/playback/3": ("fi", (6.0, 100)),
             "/playback/3/stereo": ("i", (1,)), "/playback/4/stereo": ("i", (1,)),
             "/mix/7/playback/9": ("fi", (-3.0, 0)),
             "/playback/9/stereo": ("i", (0,)), "/playback/10/stereo": ("i", (0,))}
    reported = {"/output/3/stereo": (0,), "/output/4/stereo": (0,),
                "/output/7/stereo": (0,), "/output/8/stereo": (0,)}
    routes = {route.name: route for route in written_state(journal(**cells), reported,
                                                           UCX2, "j").routes}
    assert routes["pb3-4-out3-4-split"].level == 0.0
    assert routes["pb3-4-out3-4-split"].stereo is False
    assert routes["pb9-out7"].source == ("playback", (9,))


def test_registers_the_device_reports_are_never_taken_from_the_journal():
    cells = dict(pair_route(), **{"/output/1/volume": ("f", (-20.0,)),
                                  "/output/1/stereo": ("i", (0,))})
    state = written_state(journal(**cells), LINKED, UCX2, "j")
    assert [route.name for route in state.routes] == ["pb1-2-out1-2"], \
        "the reported output link wins over a journal entry for it"


def test_mute_flags_come_back_and_a_malformed_one_is_named():
    cells = {"/playback/1/mute": ("i", (1,)), "/playback/2/mute": ("i", (1,)),
             "/playback/3/mute": ("s", ("x",))}
    warnings = []
    state = written_state(journal(**cells), {}, UCX2, "j", warnings)
    assert state.mutes == {1: 1, 2: 1}
    assert any(w.startswith("/playback/3/mute:") for w in warnings)


def _render(state):
    return render_config(Config(device_name=UCX2.name), UCX2, (), state)


def test_the_section_is_marked_unconfirmed_and_mute_stays_commented():
    cells = dict(pair_route(), **{"/playback/1/mute": ("i", (0,)),
                                  "/playback/2/mute": ("i", (0,))})
    out = _render(written_state(journal(**cells), LINKED, UCX2, "/run/x.written.json"))
    assert "WRITTEN BY DESK, NOT REPORTED BY THE DEVICE -- unconfirmed." in out
    assert "From /run/x.written.json" in out
    assert "follows at the end, from its" in out
    assert "Playback routes are not below" not in out
    assert "# written %s\n[route:pb1-2-out1-2]" % AT in out
    assert "# [playback:1]\n# mute = false   # written %s" % AT in out


def test_an_empty_journal_says_so_and_a_plain_dump_is_unchanged():
    out = _render(WrittenState("none", (), {}, {}))
    assert "The journal holds no playback route or mute flag." in out
    plain = render_config(Config(device_name=UCX2.name), UCX2)
    assert "Playback routes are not below" in plain
    assert "WRITTEN BY DESK" not in plain


def test_the_rendered_dump_with_written_routes_loads_as_a_config(tmp_path):
    out = _render(written_state(journal(**pair_route()), LINKED, UCX2, "j"))
    path = tmp_path / "routing.conf"
    path.write_text(out)
    route, = load_config(path).routes
    assert route.source == ("playback", (1, 2))


def test_the_option_belongs_to_a_dump():
    with pytest.raises(SystemExit) as refused:
        cli.main(["--include-written", "--diff"])
    assert refused.value.code == 2


def test_the_command_reads_the_journal_beside_the_lock(capsys, read_peer):
    path = journal_path(None, lock_key("2a39:3fd9", "00000000"))
    path.parent.mkdir(parents=True, exist_ok=True)
    record_written(path, pair_route())
    reported = [("/output/1/stereo", "i", (1,)), ("/output/2/stereo", "i", (1,))]
    with read_peer(reported):
        code = cli._dump_config(Config(device_name="Fireface UCX II"), include_written=True)
    out = capsys.readouterr().out
    assert code == 0
    assert "[route:pb1-2-out1-2]" in out
    assert "From %s" % path in out


def test_without_a_journal_the_section_still_says_so(capsys, read_peer):
    with read_peer([("/output/1/stereo", "i", (1,))]):
        cli._dump_config(Config(device_name="Fireface UCX II"), include_written=True)
    assert "The journal holds no playback route or mute flag." in capsys.readouterr().out


def test_the_command_line_passes_the_option_through(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(cli, "_dump_config",
                        lambda config, path, **options: calls.append(options) or 0)
    path = tmp_path / "routing.conf"
    path.write_text("[device]\nname = Fireface UCX II\n")
    assert cli.main(["--config", str(path), "--dump-config", "--include-written"]) == 0
    assert cli.main(["--config", str(path), "--dump-config"]) == 0
    assert calls == [{"include_written": True}, {}]
