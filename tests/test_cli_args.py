"""Command-line overrides are bounded like the file they override."""


def test_an_out_of_range_osc_port_on_the_command_line_is_a_config_error(
        session_mod, tmp_path):
    # `[osc] port` refuses 0 and 70000; the override used to accept both
    # and hand them to the backend, whose bind failure was the first sign.
    from oscmix_desk import cli

    path = tmp_path / "routing.conf"
    path.write_text("[route:x]\nplayback = 1/2\noutput = 1/2\n")
    for port in ("70000", "0", "-1"):
        assert cli.main(["--config", str(path), "--osc-port", port]) \
            == session_mod.EXIT_CONFIG, port


# --------------------------------------------------------------------------
# --pipewire-sinks as a command. The generator has its own tests; this is
# the CLI around it, which had none in process until the 0.6.3 mutation
# run reported every mutant of the extracted function as unreached.
# --------------------------------------------------------------------------

STEREO = "[route:monitors]\nplayback = 1/2\noutput = 1/2\n"
PRO_LAYOUT = ["AUX%d" % n for n in range(20)]


def _run(tmp_path, capsys, monkeypatch, text, info, *extra):
    from oscmix_desk import cli

    path = tmp_path / "routing.conf"
    path.write_text(text)
    monkeypatch.setattr(cli, "pw_sink_info", lambda name, target=None: info)
    code = cli.main(["--config", str(path), "--pipewire-sinks", *extra])
    return code, capsys.readouterr().out


def test_pipewire_sinks_uses_the_detected_sink_and_its_layout(
        tmp_path, capsys, monkeypatch, caplog, session_mod):
    with caplog.at_level("INFO"):
        code, out = _run(tmp_path, capsys, monkeypatch, STEREO,
                         ("alsa_output.fireface.pro-output-0", PRO_LAYOUT))
    assert code == session_mod.EXIT_OK
    assert 'target.object = "alsa_output.fireface.pro-output-0"' in out
    assert "AUX0 AUX1" in out
    assert "20-channel channel layout" in caplog.text
    assert "FIXME" not in out


def test_pipewire_sinks_without_detection_leaves_a_fixme_and_warns(
        tmp_path, capsys, monkeypatch, caplog, session_mod):
    with caplog.at_level("WARNING"):
        code, out = _run(tmp_path, capsys, monkeypatch, STEREO, None)
    assert code == session_mod.EXIT_OK
    assert "FIXME" in out
    assert "could not auto-detect" in caplog.text


def test_pipewire_sinks_keeps_an_explicit_target_when_nothing_is_detected(
        tmp_path, capsys, monkeypatch, caplog, session_mod):
    with caplog.at_level("WARNING"):
        code, out = _run(tmp_path, capsys, monkeypatch, STEREO, None,
                         "--pipewire-target", "my.sink")
    assert code == session_mod.EXIT_OK
    assert 'target.object = "my.sink"' in out
    assert "could not auto-detect" not in caplog.text, \
        "a named target is not a detection failure"


def test_pipewire_sinks_with_no_stereo_route_is_a_config_error(
        tmp_path, capsys, monkeypatch, session_mod):
    code, out = _run(tmp_path, capsys, monkeypatch,
                     "[route:mono]\nplayback = 1\noutput = 1\n", None)
    assert code == session_mod.EXIT_CONFIG
    assert out == ""
