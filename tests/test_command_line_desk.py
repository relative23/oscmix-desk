"""A desk re-read by a running session is validated for its command line.

`--device` selects the interface the desk is checked against. The 802's
thirty outputs make a route to output 25 valid only under that override:
each path that reads the desk again must keep the command line, or a desk
the session runs would be refused or replaced by the start's copy.
"""

import argparse

import pytest
from support import started_with, write_config
from two_boxes import lock_dir

from oscmix_desk import CommandLine, profiles
from oscmix_desk import reload as reload_mod
from oscmix_desk.config import load_config
from oscmix_desk.errors import ConfigError
from oscmix_desk.marker import remember_active_profile
from oscmix_desk.model import Config

WIDE = "[route:wide]\nplayback = 1/2\noutput = 25/26\n"
NARROW = "[route:main]\nplayback = 1/2\noutput = 1/2\n"
SAID = CommandLine(device_name="Fireface 802")


def test_the_file_alone_refuses_an_output_only_the_override_has(tmp_path):
    path = write_config(tmp_path / "routing.conf", WIDE)
    with pytest.raises(ConfigError, match="does not exist"):
        load_config(path)
    assert load_config(path, said=SAID).routes[0].output == (25, 26)


def test_an_active_profile_is_validated_for_the_command_line(tmp_path):
    path = write_config(tmp_path / "routing.conf", NARROW)
    write_config(tmp_path / "profiles" / "wide.conf", WIDE)
    assert remember_active_profile("wide", path).in_effect
    desk, active = profiles.effective_config(path, SAID)
    assert active == "wide"
    assert [route.output for route in desk.routes] == [(25, 26)]
    assert desk.device_name == "Fireface 802"
    assert desk.overrides == SAID


def test_a_profile_without_a_main_desk_is_validated_for_the_command_line(tmp_path):
    write_config(tmp_path / "profiles" / "wide.conf", WIDE)
    desk = profiles.load_profile("wide", tmp_path / "routing.conf", SAID)
    assert (desk.device_name, desk.routes[0].output) == ("Fireface 802", (25, 26))


def test_a_profile_is_read_onto_the_main_files_own_machine_settings(tmp_path):
    path = write_config(tmp_path / "routing.conf", WIDE + "[osc]\nport = 9000\n")
    write_config(tmp_path / "profiles" / "wide.conf", WIDE)
    desk = profiles.load_profile("wide", path, SAID)
    assert (desk.device_name, desk.osc_port) == ("Fireface 802", 9000)
    # What the files say stays apart from what the command line says.
    assert desk.loaded.device_name == "Fireface UCX II"
    assert desk.overrides == SAID


def test_kept_machine_settings_carry_the_command_line_they_came_from(tmp_path):
    running = started_with(Config(), device="Fireface 802", osc_port=9100)
    kept = profiles.keep_machine_settings(Config(), running)
    assert kept.overrides == running.overrides == CommandLine("Fireface 802", 9100)


def test_a_desk_read_under_the_lock_is_validated_for_the_running_command_line(tmp_path):
    path = write_config(tmp_path / "routing.conf", NARROW)
    running = started_with(load_config(path), device="Fireface 802")
    write_config(path, WIDE)
    fresh = reload_mod._desk_under_the_lock(path, running)
    assert fresh is not running
    assert [route.output for route in fresh.routes] == [(25, 26)]


# ---------------------------------------------------------------------------
# One SIGHUP reconcile: its connection, its failures and its stop.

@pytest.fixture
def sighup(tmp_path, monkeypatch, recording_backend):
    lock_dir(tmp_path, monkeypatch)
    path = write_config(tmp_path / "routing.conf", NARROW)
    connected = []

    def connect(config, config_path, **options):
        connected.append((config_path, options))
        return recording_backend

    monkeypatch.setattr(reload_mod, "connect_backend", connect)
    monkeypatch.setattr(reload_mod, "sd_notify", lambda _text: None)
    stop = {"stop": False}

    def run():
        return reload_mod._reconcile_once(argparse.Namespace(config=path),
                                          load_config(path), stop, None)

    return run, path, connected, stop


def test_a_reconcile_connects_for_its_desk_and_can_be_stopped_while_connecting(
        sighup, monkeypatch, recording_backend):
    run, path, connected, stop = sighup
    monkeypatch.setattr(reload_mod, "reconcile_now", lambda *_a: True)
    assert run() == "reconciled"
    [(config_path, options)] = connected
    assert config_path == path
    assert set(options) == {"should_stop"}
    assert options["should_stop"]() is False
    stop["stop"] = True
    assert options["should_stop"]() is True
    assert recording_backend.operations == ["begin", "finish", "close"]


def test_a_reconcile_whose_connection_fails_is_skipped_and_releases_the_lock(
        sighup, monkeypatch):
    run, _path, _connected, _stop = sighup

    def refused(*_a, **_k):
        raise OSError(111, "Connection refused")

    monkeypatch.setattr(reload_mod, "connect_backend", refused)
    # Nothing was submitted; "incomplete" until 0.8.1.
    assert run() == "reconcile skipped"
    assert run() == "reconcile skipped", "the device lock was released"


def test_a_stop_during_the_reconcile_is_reported_incomplete_without_finishing(
        sighup, monkeypatch, recording_backend):
    run, _path, _connected, stop = sighup

    def stopped(*_a):
        stop["stop"] = True
        return True

    monkeypatch.setattr(reload_mod, "reconcile_now", stopped)
    assert run() == "reconcile incomplete"
    assert recording_backend.operations == ["begin", "close"]
