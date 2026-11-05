"""`--status` on the playback matrix: each state's complete report, and the edges between them.

test_playback_state.py pins the way into and out of each state. These pin the whole report a
person reads for each -- summary, evidence, detail, the facts beside it and the note --, the
moment a write counts as since the backend started, malformed CC Mix reports, and the backend's
start read from /proc at its exact bounds.
"""

import os

import pytest

from oscmix_desk import Config, Route
from oscmix_desk import playstate as playstate_mod
from oscmix_desk.diagnostics import BackendStatus
from oscmix_desk.journal import stamp
from oscmix_desk.playstate import (
    CHECK_NOTE,
    KNOWN_NOTE,
    REESTABLISHED_NOTE,
    UNKNOWN_NOTE,
    backend_started,
    connect_section,
    interface_sections,
    is_playback,
    playback_state,
)
from oscmix_desk.reconcile import desired
from oscmix_desk.seen import CC_MIX, CC_MODE, Seen, check_record

SERIAL = "24216011"
START = 1000.0
DAW = Route(name="daw", output=(1, 2), playback=(1, 2))
DESK = Config(serial=SERIAL, routes=(DAW,))
WIDE = Config(serial=SERIAL, routes=(DAW, Route(name="fx", output=(3, 4), playback=(3, 4))))
SINCE = stamp(START)


def declared(config=DESK):
    return {entry.path: (entry.tags, entry.args) for entry in desired(config)
            if is_playback(entry.path)}


def wrote(at, config=DESK, **changed):
    journal = {path: (tags, args, stamp(at)) for path, (tags, args) in declared(config).items()}
    journal.update(changed)
    return journal


def saw(at, mix=(0, "TotalMix App"), mode=1, tags="is"):
    reports = {CC_MIX: (tags, mix, stamp(at))}
    if mode is not None:
        reports[CC_MODE] = ("i", (mode,), stamp(at))
    return Seen(reports)


def unknown(detail, **facts):
    return {"state": "unknown", "summary": "unknown", "evidence": None, "detail": detail,
            **facts, "note": UNKNOWN_NOTE}


def test_another_model_is_reported_as_not_applicable_in_full():
    state = playback_state(Config(device_name="Fireface 802", usb_id="2a39:3fcd"), {}, Seen(),
                           START)
    assert state == {"state": "not-applicable", "summary": "not applicable", "evidence": None,
                     "detail": "the playback evidence is for the UCX II", "note": UNKNOWN_NOTE}


def test_without_a_backend_start_the_report_says_why_in_full():
    assert playback_state(DESK, wrote(START + 5), saw(START + 6), None) == unknown(
        "no running backend for the interface, or its start time is unreadable")


def test_a_desk_without_playback_routes_is_reported_in_full():
    inputs = Config(serial=SERIAL, routes=(Route(name="mic", output=(1, 2), input=(5, 6)),))
    assert playback_state(inputs, {}, saw(START + 6), START) == unknown(
        "the desk declares no playback routes, so nothing re-establishes them", since=SINCE)


def test_nothing_written_since_the_start_is_reported_in_full():
    assert playback_state(DESK, {}, saw(START + 6), START) == unknown(
        "2 of 2 declared playback register(s) have no desk write since the interface "
        "appeared: it re-appeared, or nothing was applied yet", since=SINCE)


def test_a_write_at_the_very_moment_the_backend_started_is_not_since_it():
    state = playback_state(DESK, wrote(START), saw(START + 6), START)
    assert state["state"] == "unknown"
    assert state["detail"].startswith("2 of 2 declared playback register(s) have no desk write")


def test_other_writers_are_named_up_to_three_registers():
    changed = {path: (tags, tuple(-20.0 if tag == "f" else 0 for tag in tags), stamp(START + 9))
               for path, (tags, _args) in declared(WIDE).items()}
    assert len(changed) == 4
    state = playback_state(WIDE, wrote(START + 5, WIDE, **changed), saw(START + 6), START)
    assert state == unknown(
        "the latest recorded write of 4 declared playback register(s) is not the desk's value: "
        "another desk writer changed /mix/1/playback/1, /mix/3/playback/3, /playback/1/stereo",
        since=SINCE)


def test_a_recorded_integer_of_another_type_is_not_the_desks_value():
    journal = wrote(START + 5, **{"/playback/1/stereo": ("i", ("1",), stamp(START + 5))})
    state = playback_state(DESK, journal, saw(START + 6), START)
    assert state["detail"].endswith("another desk writer changed /playback/1/stereo")


def test_an_unreported_mode_is_reported_in_full():
    assert playback_state(DESK, wrote(START + 5), Seen(), START) == unknown(
        "CC Mix was not reported since the interface appeared, so the playback mode is not "
        "known", since=SINCE)


@pytest.mark.parametrize("cc", [saw(START + 6, mix=(1, "6ch + phones"), tags="s"),
                                saw(START + 6, mix=(4, "?"))])
def test_a_mode_report_that_names_no_preset_is_no_mode(cc):
    state = playback_state(DESK, wrote(START + 5), cc, START)
    assert state["detail"].startswith("CC Mix was not reported since the interface appeared")


def test_a_re_established_desk_is_reported_in_full():
    journal = wrote(START + 5, **{"/playback/1/stereo": ("i", (1,), stamp(START + 9))})
    assert playback_state(DESK, journal, saw(START + 6), START) == {
        "state": "re-established", "summary": "re-established by the desk",
        "evidence": "write record",
        "detail": "2 declared playback register(s) written by the desk since the interface "
                  "appeared, the last at %s; CC Mix 'TotalMix App'" % stamp(START + 9),
        "declared": 2, "ccmix": "TotalMix App", "since": SINCE, "note": REESTABLISHED_NOTE}


def test_a_preset_without_cc_mode_on_is_reported_in_full():
    state = playback_state(DESK, {}, saw(START + 6, mix=(3, "20ch"), mode=0), START)
    assert state == unknown(
        "CC Mix reports the preset '20ch', but CC Mode was not reported on since the interface "
        "appeared; whether the preset applies is not established", ccmix="20ch", since=SINCE)


def test_a_preset_with_playback_writes_is_reported_in_full():
    state = playback_state(DESK, wrote(START + 5), saw(START + 6, mix=(2, "8ch")), START)
    assert state == unknown(
        "CC Mix reports the preset '8ch', and 2 playback register(s) were written since the "
        "interface appeared; whether such writes act in this mode is not established",
        ccmix="8ch", since=SINCE)


def test_a_known_preset_is_reported_in_full():
    state = playback_state(DESK, {}, saw(START + 6, mix=(3, "20ch")), START)
    assert state == {
        "state": "known", "summary": "known (CC Mix preset 20ch)", "evidence": "static",
        "detail": "CC Mode on and CC Mix '20ch' reported since the interface appeared, and no "
                  "playback write recorded since",
        "ccmix": "20ch", "since": SINCE, "note": KNOWN_NOTE}


NOT_CHECKED = {"state": "not-checked",
               "detail": "no connect check recorded: the desk declares no input routes, or no "
                         "session started since the boot",
               "note": CHECK_NOTE}


def test_the_connect_check_sections_are_reported_in_full():
    assert connect_section(None, START) == NOT_CHECKED
    record = check_record(START + 2, 1, [], [], [])
    assert connect_section(record, START) == {**record, "current": True, "note": CHECK_NOTE}


def test_without_an_interface_both_sections_are_there():
    sections = interface_sections(Config(), None, BackendStatus("absent", "no interface"),
                                  "/nonexistent")
    assert sections == {"playback_matrix": unknown("no interface identified"),
                        "connect_check": NOT_CHECKED}


def stat(proc, pid, ticks, name="oscmix"):
    entry = proc / str(pid)
    entry.mkdir(parents=True, exist_ok=True)
    fields = ["S", "1"] + ["0"] * 17 + [str(ticks), "0", "0"]
    (entry / "stat").write_text("%d (%s) %s\n" % (pid, name, " ".join(fields)))


def test_a_process_name_with_a_parenthesis_inside_is_read_past(tmp_path):
    hz = os.sysconf("SC_CLK_TCK")
    (tmp_path / "uptime").write_text("100.00 400.00\n")
    stat(tmp_path, 7, 20 * hz, name="a) b")
    assert backend_started(7, tmp_path, now=5000.0) == pytest.approx(4920.0)


def test_a_backend_started_at_the_boot_or_just_now_has_a_start(tmp_path):
    hz = os.sysconf("SC_CLK_TCK")
    (tmp_path / "uptime").write_text("100.00 400.00\n")
    stat(tmp_path, 7, 0)
    assert backend_started(7, tmp_path, now=5000.0) == pytest.approx(4900.0)
    stat(tmp_path, 8, 100 * hz)
    assert backend_started(8, tmp_path, now=5000.0) == pytest.approx(5000.0)


def proc_with_backend(tmp_path, pid=101):
    hz = os.sysconf("SC_CLK_TCK")
    (tmp_path / "uptime").write_text("1000.00 4000.00\n")
    stat(tmp_path, pid, 900 * hz)
    return tmp_path


def test_a_backend_that_is_not_ready_has_no_start_even_with_a_pid(tmp_path):
    proc = proc_with_backend(tmp_path)
    starting = BackendStatus("starting", "not ready yet", pid=101)
    state = interface_sections(DESK, None, starting, proc)["playback_matrix"]
    assert state == unknown("no running backend for the interface, or its start time is "
                            "unreadable")


def test_a_desk_without_a_plan_is_unknown_with_the_reason(tmp_path, monkeypatch):
    proc = proc_with_backend(tmp_path)

    def unplannable(_config):
        raise ValueError("route 'x' pan: input channels do not exist")

    monkeypatch.setattr(playstate_mod, "desired", unplannable)
    ready = BackendStatus("ready", "running", pid=101)
    assert interface_sections(DESK, None, ready, proc)["playback_matrix"] == unknown(
        "the desk in effect has no plan: route 'x' pan: input channels do not exist")
