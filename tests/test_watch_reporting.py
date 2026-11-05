"""`enforce = watch`: what the watcher judges, how it reconnects, and what it tells the journal.

The watcher's own decisions are pinned in test_watch.py. These pin the edges around them: the
reader connection it asks for, the registers that are never a reason (playback cells the device
cannot report, malformed pans), the remembered-path limit, the repeat window's boundary, and the
complete log lines a person reads in the journal when watching reconciles, gives up or ends.
"""

import errno
import logging
from dataclasses import replace

import pytest

from oscmix_desk import watch as watch_mod
from oscmix_desk.config import load_config
from oscmix_desk.constants import (
    WATCH_CONNECTIONS,
    WATCH_LONGEST,
    WATCH_REPEAT_WINDOW,
    WATCH_REPEATS,
    WATCH_SETTLE,
)
from oscmix_desk.errors import BackendControlError
from oscmix_desk.protocol import Source
from oscmix_desk.watch import KNOWN_LIMIT, SLICE, Repeats, Watch

DESK = """[device]
name = Fireface UCX II

[desk]
enforce = watch

[route:guitar]
input = 3/4
output = 1/2
level = -12.0

[route:keys]
input = 5/6
output = 1/2
level = -9.0

[route:music]
playback = 1/2
output = 1/2
level = -6.0
"""
CELL = "/mix/1/input/3"
KEYS = "/mix/1/input/5"
PLAYBACK = "/mix/1/playback/1"
LOGGER = "oscmix-session"


@pytest.fixture
def desk(tmp_path):
    path = tmp_path / "routing.conf"
    path.write_text(DESK)
    return load_config(path)


class Clock:
    def __init__(self, now=100.0):
        self.now = now

    def __call__(self):
        return self.now


class Reader:
    """Replays steps (lease held, reports, seconds that pass first); records each timeout."""

    def __init__(self, steps, clock, error="backend disconnected"):
        self.steps = list(steps)
        self.clock = clock
        self.error = error
        self.lease_held = False
        self.closed = False
        self.timeouts = []

    def messages(self, timeout):
        self.timeouts.append(timeout)
        if not self.steps:
            raise BackendControlError(errno.ECONNRESET, self.error)
        held, reports, seconds = self.steps.pop(0)
        self.clock.now += seconds
        self.lease_held = held
        return iter(reports)

    def close(self):
        self.closed = True


def outside(*reports, after=0.0):
    return (False, tuple(reports), after)


def rest(seconds=WATCH_SETTLE):
    return (False, (), seconds)


def watching(desk, steps, desks=None, clock=None):
    clock = clock or Clock()
    reader = Reader(steps, clock)
    asked = []
    supply = iter(desks) if desks is not None else None
    watcher = Watch(lambda _stop: reader,
                    (lambda: next(supply)) if supply is not None else (lambda: desk),
                    asked.append, lambda: not reader.steps, clock)
    watcher.run()
    return asked, reader, watcher


def messages(caplog, level):
    return [record.getMessage() for record in caplog.records
            if record.name == LOGGER and record.levelno == level]


def test_the_reader_subscribes_to_device_reports_of_the_started_backend(desk, monkeypatch,
                                                                        tmp_path):
    calls = []

    def connect_backend(*args, **kwargs):
        calls.append((args, kwargs))
        return "connection"

    monkeypatch.setattr(watch_mod, "connect_backend", connect_backend)
    path = tmp_path / "routing.conf"

    def stop():
        return False

    assert watch_mod.reader(desk, path, 4242)(stop) == "connection"
    assert calls == [((desk, path), {"reader": True, "sources": Source.DEVICE,
                                     "should_stop": stop, "expected_pid": 4242})]


def test_each_connection_attempt_can_be_stopped(desk):
    given = []

    def connect(stop):
        given.append(stop)
        raise OSError(errno.ENOTCONN, "backend not ready")

    def should_stop():
        return False

    Watch(connect, lambda: desk, lambda _paths: None, should_stop).run()
    assert given == [should_stop] * WATCH_CONNECTIONS


def test_each_receive_waits_one_slice_so_a_stop_is_seen_promptly(desk):
    _asked, reader, _ = watching(desk, [outside(), rest()])
    assert reader.timeouts
    assert set(reader.timeouts) == {SLICE}


def test_a_playback_cell_the_device_cannot_report_is_never_a_reason(desk):
    asked, _, _ = watching(desk, [outside((PLAYBACK, "fi", (-30.0, 0))), rest()])
    assert asked == [], "only registers the device reports back (VERIFIABLE) are judged"


@pytest.mark.parametrize("pan", [200, -101])
def test_a_mix_report_with_a_pan_out_of_range_is_malformed_not_a_contradiction(desk, pan):
    asked, _, _ = watching(desk, [outside((CELL, "fi", (-20.0, pan))), rest()])
    assert asked == []


def test_the_reconcile_names_every_contradicted_pin_in_the_journal(desk, caplog):
    caplog.set_level(logging.INFO, logger=LOGGER)
    asked, _, _ = watching(desk, [outside((CELL, "fi", (-20.0, 0)), (KEYS, "fi", (-30.0, 0))),
                                  rest()])
    assert asked == [(CELL, KEYS)]
    assert ("watch: the device changed %s, %s, which the desk pins; reconciling" % (CELL, KEYS)
            in messages(caplog, logging.INFO))


def test_a_desk_that_drops_watch_says_watching_ends(desk, caplog):
    caplog.set_level(logging.INFO, logger=LOGGER)
    watching(desk, [outside((CELL, "fi", (-20.0, 0))), rest()],
             desks=[replace(desk, enforce="apply")])
    assert messages(caplog, logging.INFO) == [
        "watch: the desk in effect no longer asks for enforce = watch; watching ends"]


def test_a_register_left_to_the_device_is_named_with_the_count_and_window(desk, caplog):
    repeats = Repeats()
    for second in range(WATCH_REPEATS + 1):
        repeats.admit(desk, [CELL], float(second))
    assert messages(caplog, logging.WARNING) == [
        "watch: the device set %s back %d times within %.0fs; leaving it to the device until "
        "the desk changes or a reload" % (CELL, WATCH_REPEATS, WATCH_REPEAT_WINDOW)]


def test_a_register_left_to_the_device_does_not_hold_back_the_others(desk):
    repeats = Repeats()
    for second in range(WATCH_REPEATS):
        assert repeats.admit(desk, [CELL], float(second)) == (CELL,)
    later = float(WATCH_REPEATS)
    assert repeats.admit(desk, [CELL, KEYS], later) == (KEYS,), "the one set back now is left"
    assert repeats.admit(desk, [CELL, KEYS], later + 1) == (KEYS,), "and stays left"


def test_a_repeat_exactly_one_window_old_no_longer_counts(desk):
    repeats = Repeats()
    for second in range(WATCH_REPEATS):
        assert repeats.admit(desk, [CELL], float(second)) == (CELL,)
    assert repeats.admit(desk, [CELL], WATCH_REPEAT_WINDOW) == (CELL,), (
        "the first re-assertion is a whole window old and has dropped out of the count")


def test_a_changed_desk_watches_again_what_was_left_to_the_device(desk):
    other = replace(desk, base="clear-submixes")
    steps = []
    for _ in range(WATCH_REPEATS + 1):
        steps += [outside((CELL, "fi", (-20.0, 0))), rest()]
    steps += [outside((CELL, "fi", (-20.0, 0))), rest()]
    desks = [desk] * (WATCH_REPEATS + 1) + [other]
    asked, _, _ = watching(desk, steps, desks=desks)
    assert asked == [(CELL,)] * (WATCH_REPEATS + 1), "the new desk starts the count again"


def test_known_paths_stop_growing_at_the_limit_but_keep_their_latest_value(desk):
    clock = Clock()
    reader = Reader([outside(("/input/1/gain", "f", (1.0,)), ("/input/9/gain", "f", (9.0,))),
                     rest()], clock)
    watcher = Watch(lambda _stop: reader, lambda: desk, lambda _paths: None,
                    lambda: not reader.steps, clock)
    watcher.known.update(("/filler/%d" % index, ("i", (0,))) for index in range(KNOWN_LIMIT - 1))
    watcher.known["/input/1/gain"] = ("f", (0.0,))
    assert len(watcher.known) == KNOWN_LIMIT
    watcher.run()
    assert len(watcher.known) == KNOWN_LIMIT
    assert watcher.known["/input/1/gain"] == ("f", (1.0,)), "a known path is updated"
    assert "/input/9/gain" not in watcher.known, "a new one is not remembered past the limit"


def test_reports_that_never_rest_are_judged_exactly_at_the_longest_wait(desk):
    # The source changes every second; at WATCH_LONGEST after the first report the watcher
    # judges what it has (the contradiction), before the device returns to the desk's value.
    steps = [outside((CELL, "fi", (-20.0, 0)))]
    steps += [outside((CELL, "fi", (-20.0, 0)), after=1.0) for _ in range(int(WATCH_LONGEST))]
    steps += [outside((CELL, "fi", (-12.0, 0)), after=1.0), rest()]
    asked, _, _ = watching(desk, steps)
    assert asked == [(CELL,)]


def ended_connections(clock, served):
    """A connect that hands out readers ending after ``served`` seconds each, in turn."""
    readers = []

    def connect(_stop):
        if len(readers) >= 10:
            raise AssertionError("watching should have ended by now")
        seconds = served[len(readers)] if len(readers) < len(served) else 0.0
        readers.append(Reader([rest(seconds)], clock))
        return readers[-1]

    return connect, readers


def test_quick_losses_in_a_row_end_watching_after_the_limit(desk, caplog):
    clock = Clock()
    connect, readers = ended_connections(clock, [])
    Watch(connect, lambda: desk, lambda _paths: None, lambda: len(readers) >= 10, clock).run()
    assert len(readers) == WATCH_CONNECTIONS
    assert all(reader.closed for reader in readers)
    ended = "watch: the reader connection ended ([Errno %d] backend disconnected)" % (
        errno.ECONNRESET)
    assert messages(caplog, logging.WARNING) == [ended] * WATCH_CONNECTIONS + [
        "watch: no reader connection ([Errno %d] backend disconnected); changes on the device "
        "are no longer reconciled until the session restarts" % errno.ECONNRESET]


def test_a_long_served_connection_leaves_room_for_the_full_count_again(desk):
    clock = Clock()
    connect, readers = ended_connections(clock, [WATCH_LONGEST])
    Watch(connect, lambda: desk, lambda _paths: None, lambda: len(readers) >= 10, clock).run()
    assert len(readers) == 1 + (WATCH_CONNECTIONS - 1), (
        "the long one counts as the first failure, then the rest of the count")


def test_giving_up_names_the_last_connection_error(desk, caplog):
    def connect(_stop):
        raise OSError(errno.ENOTCONN, "backend not ready")

    Watch(connect, lambda: desk, lambda _paths: None, lambda: False).run()
    assert messages(caplog, logging.WARNING) == [
        "watch: no reader connection ([Errno %d] backend not ready); changes on the device are "
        "no longer reconciled until the session restarts" % errno.ENOTCONN]


def test_an_error_without_a_message_is_named_unknown(desk, caplog):
    def connect(_stop):
        raise OSError()

    Watch(connect, lambda: desk, lambda _paths: None, lambda: False).run()
    assert messages(caplog, logging.WARNING) == [(
        "watch: no reader connection (unknown); changes on the device are no longer "
        "reconciled until the session restarts")]
