"""Command-line overrides are bounded like the file they override."""

from itertools import permutations

import pytest


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


# --------------------------------------------------------------------------
# 0.6.10: one action per invocation, and arguments that cannot be waited on.
# --------------------------------------------------------------------------

_ACTION_FLAGS = ["--profile x", "--no-profile", "--diff", "--dump-config",
                 "--snapshot", "--pipewire-sinks", "--list-profiles"]


@pytest.mark.parametrize(("first", "second"), list(permutations(_ACTION_FLAGS, 2)))
def test_two_actions_in_one_command_are_refused_before_anything_runs(
        first, second, monkeypatch, capsys):
    """`--no-profile --diff` restored the desk and never diffed (0.6.9)."""
    from oscmix_desk import cli

    touched = []
    def record(name):
        return lambda *a, **k: touched.append(name) or 0

    for name in ("run_session", "restore_main", "switch_profile", "_snapshot",
                 "_diff", "_dump_config", "_pipewire_sinks"):
        if hasattr(cli, name):
            monkeypatch.setattr(cli, name, record(name))
    with pytest.raises(SystemExit) as raised:
        cli.main([*first.split(), *second.split()])
    assert raised.value.code == 2
    assert touched == []
    assert "cannot be combined" in capsys.readouterr().err


@pytest.mark.parametrize("flag", ["--diff", "--snapshot", "--dump-config",
                                  "--pipewire-sinks", "--list-profiles"])
def test_dry_run_goes_only_with_a_start_a_switch_or_a_restore(flag, capsys):
    from oscmix_desk import cli

    with pytest.raises(SystemExit) as raised:
        cli.main(["--dry-run", flag])
    assert raised.value.code == 2
    assert "--dry-run cannot be combined" in capsys.readouterr().err


@pytest.mark.parametrize("value", ["nan", "inf", "-1"])
def test_a_timeout_that_cannot_expire_is_refused(value, capsys):
    """`--timeout nan` never timed out: the deadline compare is always false."""
    from oscmix_desk import cli

    with pytest.raises(SystemExit) as raised:
        cli.main(["--timeout", value, "--dry-run"])
    assert raised.value.code == 2
    assert "--timeout must be a finite" in capsys.readouterr().err


def test_an_interrupt_is_an_exit_code_not_a_traceback(monkeypatch):
    from oscmix_desk import cli

    def interrupted(*_a, **_k):
        raise KeyboardInterrupt

    monkeypatch.setattr(cli, "_main", interrupted)
    assert cli.main([]) == 130
