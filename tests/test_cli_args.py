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


def _run(tmp_path, capsys, monkeypatch, text, info, *extra, dump="[]"):
    from oscmix_desk import cli

    path = tmp_path / "routing.conf"
    path.write_text(text)
    monkeypatch.setattr(cli, "pw_dump_objects",
                        lambda: None if dump is None else [{"id": 1}])
    target = extra[extra.index("--pipewire-target") + 1] \
        if "--pipewire-target" in extra else None

    def find_sink(objects, name, wanted):
        assert (objects, name, wanted) == ([{"id": 1}], "Fireface UCX II",
                                           target)
        return info

    monkeypatch.setattr(cli, "find_sink", find_sink)
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
    # Named and not in pw-dump: the positions below are the surround
    # table, and the output says so rather than looking right (0.6.10).
    assert "no sink named 'my.sink' in pw-dump" in caplog.text
    assert "7.1 surround layout" in caplog.text


def test_pipewire_sinks_does_not_call_a_target_missing_it_could_not_look_up(
        tmp_path, capsys, monkeypatch, caplog, session_mod):
    """pw-dump missing or failing is not "no such sink"."""
    with caplog.at_level("WARNING"):
        code, out = _run(tmp_path, capsys, monkeypatch, STEREO, None,
                         "--pipewire-target", "my.sink", dump=None)
    assert code == session_mod.EXIT_OK
    assert 'target.object = "my.sink"' in out
    assert "pw-dump could not be read, so sink 'my.sink' was not checked" \
        in caplog.text
    assert "no sink named" not in caplog.text


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


def test_a_pair_is_refused_before_the_config_is_read(monkeypatch, capsys,
                                                     tmp_path):
    """It ran after the load until 0.6.10: a broken routing.conf answered
    `--diff --snapshot` with "configuration error", naming neither."""
    from oscmix_desk import cli

    broken = tmp_path / "routing.conf"
    broken.write_text("[route:x]\noutput = 99\n")

    def unread(*_a, **_k):
        raise AssertionError("the config was read before the refusal")

    monkeypatch.setattr(cli, "discover_config_path", unread)
    monkeypatch.setattr(cli, "effective_config", unread)
    for argv in (["--diff", "--snapshot"],
                 ["--config", str(broken), "--diff", "--snapshot"],
                 ["--timeout", "nan"]):
        with pytest.raises(SystemExit) as raised:
            cli.main(argv)
        assert raised.value.code == 2, argv
    err = capsys.readouterr().err
    assert "--diff and --snapshot cannot be combined" in err
    assert "--timeout must be a finite" in err


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


def test_the_refusal_words_and_its_boundaries(capsys):
    """`--timeout 0` is a valid answer -- do not wait -- and the refusals
    are argparse errors in the program's name, not log lines."""
    from oscmix_desk import cli

    parser = cli.build_arg_parser()
    cli._refuse_conflicting_actions(parser, parser.parse_args(["--timeout", "0"]))
    for argv, words in ((["--diff", "--snapshot"],
                         "--diff and --snapshot cannot be combined"),
                        (["--dry-run", "--diff"],
                         "--dry-run cannot be combined with --diff"),
                        (["--timeout", "-1"],
                         ("--timeout must be a finite number of seconds, "
                          "not -1.0"))):
        with pytest.raises(SystemExit):
            cli._refuse_conflicting_actions(parser, parser.parse_args(argv))
        assert capsys.readouterr().err.splitlines()[-1] == \
            "oscmix-session: error: " + words


def _unmodelled_desk(tmp_path):
    """routing.conf with one route, profiles with three, on 'Some Box'."""
    path = tmp_path / "routing.conf"
    path.write_text("[device]\nname = Some Box\n\n"
                    "[route:main]\nplayback = 1/2\noutput = 1/2\n")
    (tmp_path / "profiles").mkdir()
    for name in ("one", "two"):
        (tmp_path / "profiles" / ("%s.conf" % name)).write_text(
            "".join("[route:%s]\nplayback = 1/2\noutput = %d/%d\n"
                    % (tag, n, n + 1) for tag, n in (("a", 1), ("b", 3),
                                                     ("c", 5))))
    return path


def _unchecked(caplog):
    return [r.getMessage() for r in caplog.records
            if "no register model" in r.getMessage()]


def test_a_listing_writes_nothing_and_warns_about_nothing(tmp_path, caplog):
    """From the parser the warning fired N+1 times for N profiles."""
    from oscmix_desk import cli

    path = _unmodelled_desk(tmp_path)
    with caplog.at_level("WARNING"):
        assert cli.main(["--config", str(path), "--list-profiles"]) == 0
    assert _unchecked(caplog) == []


def test_a_switch_and_a_restore_warn_about_the_desk_they_write(
        tmp_path, caplog, recording_backend):
    """The first placement warned about the desk *in effect*: one route
    for `--profile one`, whose three were the ones going out, and three
    for `--no-profile`, which writes routing.conf's one (0.6.11)."""
    from oscmix_desk import profiles

    path = _unmodelled_desk(tmp_path)
    with caplog.at_level("WARNING"):
        profiles.switch_profile("one", config_path=path,
                                backend=recording_backend, verify=False)
    expected = ("no register model for 'Some Box': its 3 route(s) are "
                "written as given, with no check that the device has those "
                "channels (modelled: Fireface UCX II)")
    assert _unchecked(caplog) == [expected]
    caplog.clear()
    with caplog.at_level("WARNING"):
        profiles.restore_main(config_path=path, backend=recording_backend,
                              verify=False)
    assert len(_unchecked(caplog)) == 1
    assert "its 1 route(s)" in _unchecked(caplog)[0]


def test_a_dry_run_warns_about_the_desk_it_shows(tmp_path, caplog):
    from oscmix_desk import cli

    path = _unmodelled_desk(tmp_path)
    with caplog.at_level("WARNING"):
        cli.main(["--config", str(path), "--timeout", "0", "--dry-run",
                  "--profile", "two"])
    assert len(_unchecked(caplog)) == 1
    assert "its 3 route(s)" in _unchecked(caplog)[0]


def test_a_reload_warns_about_the_desk_it_re_read(tmp_path, caplog):
    from oscmix_desk import Config
    from oscmix_desk import session as session_module

    path = _unmodelled_desk(tmp_path)
    (tmp_path / "active-profile").write_text("one\n")
    with caplog.at_level("WARNING"):
        session_module._reloaded_desk(Config(device_name="Some Box"), path)
    assert len(_unchecked(caplog)) == 1
    assert "its 3 route(s)" in _unchecked(caplog)[0]


def test_a_device_override_that_bypasses_the_validation_is_named(
        tmp_path, caplog):
    """`--device` arrives after the file was validated. When it names
    another model, or none, the channel check said nothing about the
    interface the routes now go to."""
    from oscmix_desk import cli

    path = tmp_path / "routing.conf"
    path.write_text("[route:main]\nplayback = 1/2\noutput = 1/2\n")
    with caplog.at_level("WARNING"):
        cli.main(["--config", str(path), "--device", "Some Box",
                  "--list-profiles"])
    assert ("--device replaces [device] name after validation: this config "
            "was checked for 'Fireface UCX II' and is used for 'Some Box'"
            ) in caplog.text
    caplog.clear()
    with caplog.at_level("WARNING"):
        cli.main(["--config", str(path), "--device", "fireface ucx ii",
                  "--list-profiles"])
    assert "--device" not in caplog.text, "the same model, spelled differently"


@pytest.mark.parametrize("action", [["--profile", "one"], ["--no-profile"]])
@pytest.mark.parametrize("override", [["--device", "Some Box"],
                                      ["--osc-port", "9000"]])
@pytest.mark.parametrize("dry", [[], ["--dry-run"]])
def test_an_override_a_switch_never_saw_is_refused(tmp_path, capsys, action,
                                                   override, dry):
    """A switch takes its interface and ports from the config. `--device`
    and `--osc-port` were dropped on that path without a word, while the
    dry run of the same switch honoured them -- and so showed something
    the switch would not do (0.6.11)."""
    from oscmix_desk import cli

    path = _unmodelled_desk(tmp_path)
    with pytest.raises(SystemExit) as refused:
        cli.main(["--config", str(path), *action, *override, *dry])
    assert refused.value.code == 2
    assert ("%s cannot be combined with %s: a switch takes its interface "
            "and ports from routing.conf and the profile"
            % (override[0], action[0])) in capsys.readouterr().err


def test_an_override_still_goes_with_a_start_and_with_a_read(tmp_path,
                                                             monkeypatch):
    from oscmix_desk import cli

    seen = []
    monkeypatch.setattr(cli, "run_session",
                        lambda args, config: seen.append(
                            (config.device_name, config.osc_port)) or 0)
    path = tmp_path / "routing.conf"
    path.write_text("[route:main]\nplayback = 1/2\noutput = 1/2\n")
    assert cli.main(["--config", str(path), "--device", "fireface ucx ii",
                     "--osc-port", "9000"]) == 0
    assert seen == [("fireface ucx ii", 9000)]


def test_a_dry_run_shows_the_profile_for_the_interface_the_profile_names(
        tmp_path, caplog, monkeypatch):
    """The desk in effect's ports and device name were written over the
    profile before it was shown: a profile naming its own interface was
    shown, and warned about, as another one's (0.6.11)."""
    from oscmix_desk import cli

    shown = []
    monkeypatch.setattr(cli, "run_session",
                        lambda args, desk: shown.append(desk) or 0)
    path = tmp_path / "routing.conf"
    path.write_text("[osc]\nport = 9001\n\n"
                    "[route:main]\nplayback = 1/2\noutput = 1/2\n")
    (tmp_path / "profiles").mkdir()
    (tmp_path / "profiles" / "far.conf").write_text(
        "[device]\nname = Some Box\n\n[osc]\nport = 9100\n\n"
        "[route:far]\nplayback = 1/2\noutput = 41/42\n")
    (tmp_path / "profiles" / "near.conf").write_text(
        "[route:near]\nplayback = 1/2\noutput = 3/4\n")
    (tmp_path / "active-profile").write_text("far\n")
    assert cli.main(["--config", str(path), "--dry-run",
                     "--profile", "near"]) == 0
    assert cli.main(["--config", str(path), "--dry-run", "--no-profile"]) == 0
    assert cli.main(["--config", str(path), "--dry-run",
                     "--profile", "far"]) == 0
    assert [(d.device_name, d.osc_port) for d in shown] == [
        ("Fireface UCX II", 9001),      # near inherits routing.conf, not far
        ("Fireface UCX II", 9001),
        ("Some Box", 9100)]
