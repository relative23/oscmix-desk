"""A desk read again by a running session: the lines it logs and the edges of its waits.

test_reload.py pins what a reload applies. These pin what the journal says when a reload waits
for the start-up verifier, gives up on it or is stopped, how a reconcile that ended early is
reported, the complete refusals for an unusable or retargeted desk, and the watcher's reads
staying quiet and strict when asked to be.
"""

import logging
from types import SimpleNamespace

import pytest

from oscmix_desk import reload as reload_mod
from oscmix_desk.config import load_config
from oscmix_desk.constants import RECONCILE_WAIT_FOR_VERIFIER, SERVICE_UNIT
from oscmix_desk.errors import ConfigError, WriteFailed
from oscmix_desk.model import Config

DESK = "[device]\nname = Fireface UCX II\n\n[route:main]\nplayback = 1/2\noutput = 1/2\n"
LOGGER = "oscmix-session"


def lines(caplog):
    return [(record.levelname, record.getMessage()) for record in caplog.records
            if record.name == LOGGER]


class Verifier:
    """A start-up verifier that ends after ``joins`` waits, or never."""

    def __init__(self, joins=None):
        self.joins_left = joins
        self.joined = 0
        self.timeouts = []

    def is_alive(self):
        return self.joins_left is None or self.joined < self.joins_left

    def join(self, timeout=None):
        self.timeouts.append(timeout)
        self.joined += 1


@pytest.fixture
def clock(monkeypatch):
    def install(*times, step=10.0):
        given = list(times)
        state = {"now": 0.0}

        def monotonic():
            if given:
                state["now"] = given.pop(0)
            else:
                state["now"] += step
            return state["now"]

        monkeypatch.setattr(reload_mod, "time", SimpleNamespace(monotonic=monotonic))
    return install


def test_a_reconcile_waits_for_the_verifier_and_says_so(clock, caplog):
    caplog.set_level(logging.INFO, logger=LOGGER)
    clock()
    assert reload_mod._verifier_finished(Verifier(joins=1), {"stop": False}, "watch") is True
    assert lines(caplog) == [
        ("INFO", "watch: waiting for the start-up verifier before reconciling")]


def test_a_verifier_that_outlives_the_wait_skips_the_reconcile_and_says_how_long(clock, caplog):
    caplog.set_level(logging.INFO, logger=LOGGER)
    clock()
    assert reload_mod._verifier_finished(Verifier(), {"stop": False}, "watch") is False
    assert lines(caplog)[-1] == (
        "WARNING", "watch: verifier still running after %.0fs; reconcile skipped -- send the "
                   "reload again" % RECONCILE_WAIT_FOR_VERIFIER)


def test_the_wait_ends_exactly_at_its_deadline(clock):
    clock(0.0, RECONCILE_WAIT_FOR_VERIFIER)
    verifier = Verifier(joins=1)
    assert reload_mod._verifier_finished(verifier, {"stop": False}, "watch") is False
    assert verifier.joined == 0, "not one more wait once the time is up"


def test_a_stop_during_the_wait_skips_the_reconcile(clock):
    clock()
    assert reload_mod._verifier_finished(Verifier(), {"stop": True}, "watch") is False


ERROR = OSError(5, "Input/output error")


def test_a_stopped_reconcile_is_information_with_its_outcome(caplog):
    caplog.set_level(logging.INFO, logger=LOGGER)
    assert reload_mod._stood_down(ERROR, False, True, "watch") == "reconcile skipped"
    assert lines(caplog) == [
        ("INFO", "watch: stop requested; reconcile skipped ([Errno 5] Input/output error)")]


def test_a_reconcile_that_wrote_before_it_failed_is_incomplete(caplog):
    partial = WriteFailed(ERROR, ["/mix/1/playback/1"], ["/playback/1/stereo"])
    assert reload_mod._stood_down(partial, False, False, "watch") == "reconcile incomplete"
    assert lines(caplog) == [("ERROR", "watch: %s; reconcile incomplete" % partial)]


def test_a_reconcile_that_failed_before_writing_says_nothing_was_written(caplog):
    assert reload_mod._stood_down(ERROR, False, False, "watch") == "reconcile skipped"
    assert lines(caplog) == [
        ("ERROR", "watch: [Errno 5] Input/output error; reconcile skipped, nothing written")]


@pytest.fixture
def broken(tmp_path):
    path = tmp_path / "routing.conf"
    path.write_text(DESK)
    running = load_config(path)
    path.write_text(DESK + "\n[route:bad]\noutput = 99\nplayback = 1\n")
    with pytest.raises(ConfigError) as reason:
        load_config(path)
    return running, path, str(reason.value)


def test_a_reload_of_an_unusable_desk_keeps_the_running_one_and_says_why(broken, caplog):
    running, path, reason = broken
    with pytest.raises(ConfigError, match="not usable") as refused:
        reload_mod._reload_config(running, path, reason="watch")
    assert str(refused.value) == "the reloaded desk is not usable"
    assert lines(caplog) == [
        ("ERROR", "watch: %s is not usable (%s); keeping the running configuration"
                  % (path, reason))]


def test_a_strict_start_on_an_unusable_desk_writes_nothing_and_says_why(broken, caplog):
    running, path, reason = broken
    with pytest.raises(ConfigError, match="strict start refused") as refused:
        reload_mod._desk_under_the_lock(path, running, strict=True)
    assert str(refused.value) == ("the desk under the device lock is not usable; strict start "
                                  "refused")
    assert lines(caplog) == [
        ("ERROR", "%s is no longer usable (%s); strict start refused, nothing written"
                  % (path, reason))]


def test_a_reload_names_what_it_read(tmp_path, caplog):
    caplog.set_level(logging.INFO, logger=LOGGER)
    path = tmp_path / "routing.conf"
    path.write_text(DESK)
    running = load_config(path)
    assert reload_mod._reload_config(running, path, reason="watch") == running
    assert ("INFO", "watch: reloaded %s (1 route(s), 0 channel setting(s))" % path) in \
        lines(caplog)


def test_a_desk_moved_to_another_interface_is_not_applied_and_says_how_to_follow(tmp_path,
                                                                                 caplog):
    path = tmp_path / "routing.conf"
    path.write_text(DESK)
    running = load_config(path)
    path.write_text(DESK.replace("[device]\n", "[device]\nserial = 87654321\n"))
    moved = reload_mod._moved(load_config(path), running)
    assert moved
    assert reload_mod._kept_for_this_process(load_config(path), running) is None
    assert lines(caplog) == [
        ("ERROR", "the desk now in effect is for another backend or interface -- %s -- and a "
                  "running session keeps the one it was started for, so it was not applied; "
                  "restart the session to follow it (systemctl --user restart %s)"
                  % (moved, SERVICE_UNIT))]


def test_a_desk_no_file_declared_is_for_this_process():
    running = Config()
    assert reload_mod._moved(Config(), running) == ""
    assert reload_mod._kept_for_this_process(Config(), running) is not None


IGNORED = DESK + "\n[someday]\nvalue = 1\n"


def test_a_strict_watcher_does_not_accept_a_desk_with_ignored_sections(tmp_path):
    path = tmp_path / "routing.conf"
    path.write_text(DESK)
    running = load_config(path)
    path.write_text(IGNORED)
    assert reload_mod.desk_in_effect(running, path) is not None
    assert reload_mod.desk_in_effect(running, path, strict=True) is None
    assert reload_mod.watched_desk(running, path, strict=True)() is None


def test_the_watcher_does_not_announce_the_active_profile(tmp_path, caplog):
    caplog.set_level(logging.INFO, logger=LOGGER)
    path = tmp_path / "routing.conf"
    path.write_text(DESK)
    (tmp_path / "profiles").mkdir()
    (tmp_path / "profiles" / "quiet.conf").write_text("[route:m]\nplayback = 3/4\noutput = 1/2\n")
    (tmp_path / "active-profile").write_text("quiet\n")
    running = load_config(path)
    desk = reload_mod.desk_in_effect(running, path)
    assert [route.name for route in desk.routes] == ["m"]
    assert lines(caplog) == [], "the reconcile it may lead to says which desk it is"
