"""`[desk] enforce = watch`: a device report that contradicts the desk asks for a reconcile.

The watcher never writes. It judges the reports that arrive outside every
operation's lease, once they rest, against the desk in effect -- only what a
selective reconcile would write and the device can report -- and asks the
session's supervise loop for that reconcile, as SIGHUP does.
"""

import errno
import logging
import time
from dataclasses import replace

import pytest

from oscmix_desk import reload as reload_mod
from oscmix_desk import session as session_mod
from oscmix_desk import watch as watch_mod
from oscmix_desk.config import load_config
from oscmix_desk.constants import WATCH_LONGEST, WATCH_REPEAT_WINDOW, WATCH_REPEATS, WATCH_SETTLE
from oscmix_desk.errors import BackendControlError, ConfigError
from oscmix_desk.watch import Repeats, Watch, drift

DESK = """[device]
name = Fireface UCX II

[desk]
enforce = watch

[route:guitar]
input = 3/4
output = 1/2
level = -12.0

[route:music]
playback = 1/2
output = 1/2
level = -6.0

[output:1]
volume = -10.0

[input:3]
gain = 20.0
"""
CELL = "/mix/1/input/3"
AUTOSET_OFF = (("/input/3/autoset", "i", (0,)), ("/input/4/autoset", "i", (0,)))


@pytest.fixture
def desk(tmp_path):
    path = tmp_path / "routing.conf"
    path.write_text(DESK)
    return load_config(path)


class Clock:
    def __init__(self):
        self.now = 100.0

    def __call__(self):
        return self.now


class Reader:
    """A reader connection replaying steps: (lease held, reports, seconds that pass first)."""

    def __init__(self, steps, clock):
        self.steps = list(steps)
        self.clock = clock
        self.lease_held = False
        self.closed = False

    def messages(self, _timeout):
        if not self.steps:
            raise BackendControlError(errno.ECONNRESET, "backend disconnected")
        held, reports, seconds = self.steps.pop(0)
        self.clock.now += seconds
        self.lease_held = held
        return iter(reports)

    def close(self):
        self.closed = True


def report(path, tags, *args):
    return (path, tags, args)


def outside(*reports, after=0.0):
    return (False, reports, after)


def owned(*reports, after=0.0):
    return (True, reports, after)


def rest(seconds=WATCH_SETTLE):
    return (False, (), seconds)


def watching(desk, steps, desks=None):
    """Run a watcher over ``steps``; what it asked for and the reader it used."""
    clock = Clock()
    reader = Reader(steps, clock)
    asked = []
    supply = iter(desks) if desks is not None else None
    watcher = Watch(lambda _stop: reader,
                    (lambda: next(supply)) if supply is not None else (lambda: desk),
                    asked.append, lambda: not reader.steps, clock)
    watcher.run()
    return asked, reader, watcher


def test_a_contradicted_pin_value_asks_for_one_reconcile(desk):
    asked, reader, _ = watching(desk, [outside(report(CELL, "fi", -20.0, 0)), rest()])
    assert asked == [(CELL,)]
    assert reader.closed


def test_the_echo_of_the_desk_asks_for_nothing(desk):
    asked, _, _ = watching(desk, [outside(report(CELL, "fi", -12.0, 0),
                                          report("/output/1/stereo", "i", 1)), rest()])
    assert asked == []


def test_reports_during_a_lease_are_the_operations_own(desk):
    during = [owned(report(CELL, "fi", -20.0, 0)), rest()]
    before = [outside(report(CELL, "fi", -20.0, 0)), owned(after=1.0), rest()]
    assert watching(desk, during)[0] == []
    assert watching(desk, before)[0] == [], "a change an operation is about to overwrite"


def test_it_waits_for_the_reports_to_rest(desk):
    turning = [outside(report(CELL, "fi", -20.0 - step, 0), after=1.0) for step in range(5)]
    asked, _, _ = watching(desk, [*turning, rest(WATCH_SETTLE - 0.5)])
    assert asked == [], "still within the settle time of the last detent"
    asked, _, _ = watching(desk, [*turning, rest()])
    assert asked == [(CELL,)]


def test_a_source_that_never_rests_is_judged_after_the_longest_wait(desk):
    endless = [outside(report(CELL, "fi", -20.0, 0), after=1.0)
               for _ in range(int(WATCH_LONGEST) + 2)]
    asked, _, _ = watching(desk, endless)
    assert asked == [(CELL,)]


def test_remember_and_autoset_owned_gains_are_never_a_reason(desk):
    fader = report("/output/1/volume", "f", -30.0)
    gain = report("/input/3/gain", "f", 23.5)
    assert watching(desk, [outside(fader, gain), rest()])[0] == [], (
        "a fader is REMEMBER; a gain is AutoSet's until AutoSet is known to be off")
    flags = [report(path, tags, *args) for path, tags, args in AUTOSET_OFF]
    asked, _, _ = watching(desk, [owned(*flags), outside(gain), rest()])
    assert asked == [("/input/3/gain",)], "flags learned from an operation's dump still count"


def test_a_malformed_report_is_not_a_contradiction(desk):
    asked, _, _ = watching(desk, [outside(report(CELL, "s", "loud")), rest()])
    assert asked == []


def test_a_desk_that_no_longer_asks_for_watch_ends_watching(desk, caplog):
    caplog.set_level(logging.INFO)
    steps = [outside(report(CELL, "fi", -20.0, 0)), rest(),
             outside(report(CELL, "fi", -20.0, 0)), rest()]
    asked, reader, _ = watching(desk, steps, desks=[replace(desk, enforce="apply")])
    assert asked == []
    assert len(reader.steps) == 2, "it stopped at the first judgement"
    assert "no longer asks for enforce = watch" in caplog.text


def test_an_unusable_desk_asks_for_nothing_and_watching_goes_on(desk):
    steps = [outside(report(CELL, "fi", -20.0, 0)), rest(),
             outside(report(CELL, "fi", -21.0, 0)), rest()]
    asked, _, _ = watching(desk, steps, desks=[None, desk])
    assert asked == [(CELL,)]


def test_a_register_the_device_keeps_setting_back_is_left_to_it(desk, caplog):
    steps = []
    for _ in range(WATCH_REPEATS + 1):
        steps += [outside(report(CELL, "fi", -20.0, 0)), rest()]
    asked, _, _ = watching(desk, steps)
    assert asked == [(CELL,)] * WATCH_REPEATS
    assert "set %s back %d times" % (CELL, WATCH_REPEATS) in caplog.text


def test_repeats_count_within_their_window_and_per_desk(desk):
    repeats = Repeats()
    for second in range(WATCH_REPEATS):
        assert repeats.admit(desk, [CELL], float(second)) == (CELL,)
    assert repeats.admit(desk, [CELL], float(WATCH_REPEATS)) == ()
    assert repeats.admit(desk, [CELL], WATCH_REPEAT_WINDOW * 10) == (), "left until forgiven"
    repeats.forgive()
    assert repeats.admit(desk, [CELL], 0.0) == (CELL,)
    other = replace(desk, base="clear-submixes")
    for second in range(WATCH_REPEATS):
        repeats.admit(desk, [CELL], float(second))
    assert repeats.admit(other, [CELL], 3.0) == (CELL,), "a new desk starts again"
    slow = Repeats()
    for second in range(WATCH_REPEATS + 2):
        assert slow.admit(desk, [CELL], second * WATCH_REPEAT_WINDOW) == (CELL,)


def test_a_reload_watches_again_what_was_left_to_the_device(desk):
    steps = []
    for _ in range(WATCH_REPEATS + 1):
        steps += [outside(report(CELL, "fi", -20.0, 0)), rest()]
    asked, _, watcher = watching(desk, steps)
    assert len(asked) == WATCH_REPEATS
    watcher.forgive()
    clock = Clock()
    clock.now = 10 * WATCH_REPEAT_WINDOW
    reader = Reader([outside(report(CELL, "fi", -20.0, 0)), rest()], clock)
    watcher._connect, watcher._clock = (lambda _stop: reader), clock
    watcher._should_stop = lambda: not reader.steps
    watcher.run()
    assert len(asked) == WATCH_REPEATS + 1


def test_a_lost_connection_is_replaced_then_given_up(desk, caplog):
    attempts = []

    def connect(_stop):
        attempts.append(1)
        raise OSError(errno.ENOTCONN, "backend not ready")

    Watch(connect, lambda: desk, lambda _paths: None, lambda: False).run()
    assert len(attempts) == 3
    assert "no reader connection (" in caplog.text


def test_drift_needs_a_modelled_device_and_reports(desk):
    assert drift(desk, {}, {}) == ()
    unknown = replace(desk, device_name="Some Other Interface")
    assert drift(unknown, {CELL: ("fi", (-20.0, 0))}, {}) == ()


@pytest.mark.parametrize(("pending", "reason", "forgiven"), [
    ({"reload": True, "watch": True}, "watch", False),
    ({"reload": True, "watch": True, "sighup": True}, "SIGHUP", True),
    ({"reload": True}, None, False),
])
def test_one_reconcile_serves_every_pending_request(monkeypatch, pending, reason, forgiven):
    # Without either flag the request was served already: the watcher raised "reload" again
    # after a SIGHUP's reconcile had taken its "watch" along.
    calls = []
    monkeypatch.setattr(session_mod, "_reconcile",
                        lambda *_a, reason="SIGHUP", **_k: calls.append(reason))

    class Watcher:
        forgiven = False

        def forgive(self):
            self.forgiven = True

    watcher = Watcher()
    session_mod._serve_reload(None, None, {"stop": False}, None, pending, watcher)
    assert calls == ([reason] if reason else [])
    assert watcher.forgiven is forgiven
    assert "watch" not in pending
    assert "sighup" not in pending


def test_only_a_watching_desk_opens_a_reader(desk, monkeypatch, caplog):
    class Child:
        pid = 4242

        def poll(self):
            return None

    connected = []

    def connect_backend(*_args, **kwargs):
        connected.append(kwargs)
        raise OSError(errno.ENOTCONN, "not here")

    monkeypatch.setattr(watch_mod, "connect_backend", connect_backend)
    stop = {"stop": False}
    args = type("Args", (), {"strict": False})()
    assert session_mod._start_watch(Child(), replace(desk, enforce="apply"), None, args,
                                    stop, {"reload": False}) is None
    watcher = session_mod._start_watch(Child(), desk, None, args, stop, {"reload": False})
    assert watcher is not None
    deadline = time.monotonic() + 5.0
    while len(connected) < 3 and time.monotonic() < deadline:
        time.sleep(0.01)
    assert connected
    assert connected[0]["reader"] is True
    assert connected[0]["expected_pid"] == 4242


def test_the_watcher_reads_the_desk_quietly(tmp_path, desk, caplog):
    path = tmp_path / "routing.conf"
    path.write_text(DESK)
    assert reload_mod.desk_in_effect(desk, path) == reload_mod.keep_machine_settings(
        load_config(path), desk)
    path.write_text(DESK + "\n[route:broken]\noutput = 99\nplayback = 1\n")
    caplog.clear()
    assert reload_mod.desk_in_effect(desk, path) is None
    assert "not usable" not in caplog.text
    assert reload_mod.desk_in_effect(desk, None) is desk


@pytest.mark.parametrize(("body", "reason"), [
    ("[desk]\nenforce = always\n", "expected one of apply, watch"),
])
def test_enforce_refusals(tmp_path, body, reason):
    path = tmp_path / "routing.conf"
    path.write_text("[device]\nname = Fireface UCX II\n\n" + body)
    with pytest.raises(ConfigError, match=reason):
        load_config(path)


def test_enforce_defaults_to_apply(tmp_path):
    path = tmp_path / "routing.conf"
    path.write_text("[device]\nname = Fireface UCX II\n")
    assert load_config(path).enforce == "apply"


def test_a_profile_inherits_enforce_and_may_not_set_it(tmp_path):
    from oscmix_desk.profiles import load_profile

    main = tmp_path / "routing.conf"
    main.write_text(DESK)
    (tmp_path / "profiles").mkdir()
    (tmp_path / "profiles/quiet.conf").write_text("[route:m]\nplayback = 1/2\noutput = 1/2\n")
    assert load_profile("quiet", main).enforce == "watch"
    (tmp_path / "profiles/loud.conf").write_text("[desk]\nenforce = apply\n")
    with pytest.raises(ConfigError, match="every profile inherits it"):
        load_profile("loud", main)


def test_a_connection_that_served_a_while_starts_the_count_again(desk):
    clock = Clock()
    readers = []

    def connect(_stop):
        if len(readers) == 6:
            raise OSError(errno.ENOTCONN, "gone")
        readers.append(Reader([rest(WATCH_LONGEST)], clock))
        return readers[-1]

    Watch(connect, lambda: desk, lambda _paths: None, lambda: False, clock).run()
    assert len(readers) == 6, "a loss after long service starts again; failures in a row end it"


def test_the_watcher_reads_the_desk_again_only_after_a_file_changed(tmp_path, desk, caplog):
    path = tmp_path / "routing.conf"
    path.write_text(DESK + "\n[someday]\nvalue = 1\n")
    current = reload_mod.watched_desk(desk, path)
    first = current()
    assert first is not None
    assert current() is first
    assert caplog.text.count("someday") == 1, "warned once, not on every check"
    path.write_text(DESK.replace("level = -12.0", "level = -10.0"))
    assert current() is not first
