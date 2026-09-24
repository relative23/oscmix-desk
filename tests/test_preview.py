"""Profile previews compare declarations and never invent a full matrix."""

from support import write_config

from oscmix_desk import cli
from oscmix_desk.model import Config, Route
from oscmix_desk.preview import transition_lines


def desk(output=(5, 6), level=-12, stereo=True):
    return Config(routes=[Route(name="r", playback=(1, 2), output=output,
                                level=level, stereo=stereo, volume=-30)])


def test_omitting_a_route_is_not_a_mute():
    text = "\n".join(transition_lines(desk(), desk((7, 8))))
    assert "not overwritten or explicitly muted by target: /mix/5/playback/1" in text
    assert "/output/5/volume" not in text
    assert "not a complete hardware snapshot" in text
    assert "Unknown playback routes" in text


def test_an_explicit_mute_or_replacement_is_not_an_omission():
    for level in (-65, -20):
        text = "\n".join(transition_lines(desk(), desk(level=level)))
        assert "no previously declared route crosspoints omitted" in text
        assert "not overwritten" not in text


def test_changed_links_name_the_possible_partner_effects():
    text = "\n".join(transition_lines(desk(), desk(stereo=False)))
    assert "target link declarations change: /output/5/stereo" in text
    assert "partner channels/crosspoints" in text
    assert "not overwritten" not in text


def test_unchanged_links_need_no_partner_warning():
    assert not any("partner" in line for line in transition_lines(desk(), desk()))


def test_empty_target_names_every_omitted_crosspoint():
    previous = Config(routes=[*desk().routes, *desk((7, 8)).routes])
    lines = transition_lines(previous, Config())
    assert sum("not overwritten" in line for line in lines) == 2


def test_cli_names_both_files_and_preserves_the_active_marker(tmp_path, monkeypatch, capsys):
    path = tmp_path / "routing.conf"
    write_config(path, "[route:main]\nplayback=1/2\noutput=1/2\n")
    write_config(tmp_path / "profiles/a.conf", "[route:a]\nplayback=1/2\noutput=5/6\n")
    write_config(tmp_path / "profiles/b.conf", "[route:b]\nplayback=1/2\noutput=7/8\n")
    marker = tmp_path / "active-profile"
    marker.write_text("a\n")
    calls = []
    monkeypatch.setattr(cli, "run_session", lambda args, config:
                        calls.append((args.dry_run, config.routes[0].output)) or 0)
    assert cli.main(["--config", str(path), "--profile", "b", "--dry-run"]) == 0
    printed = capsys.readouterr().out
    assert "a.conf -> " in printed
    assert "b.conf" in printed
    assert "not overwritten or explicitly muted by target: /mix/5/playback/1" in printed
    assert calls == [(True, (7, 8))]
    assert marker.read_text() == "a\n"
