"""The playback matrix in `--status`: known (CC Mix preset), re-established by the desk, unknown.

The device never reports its playback matrix (ADR 0037). Status states what the desk can stand
behind and what each state rests on: a preset is static evidence about the firmware, and
"re-established" is a record of acknowledged writes since the running backend started -- never
a hardware confirmation. Every way into and out of each state is pinned here.
"""

import json
import os
import time

import pytest
from two_boxes import A

from oscmix_desk import Config, Route, desktop, hostservice, status
from oscmix_desk.discovery import lock_key
from oscmix_desk.journal import journal_path, record_written, stamp
from oscmix_desk.playstate import (
    backend_started,
    connect_section,
    interface_sections,
    is_playback,
    playback_state,
)
from oscmix_desk.reconcile import desired
from oscmix_desk.seen import CC_MIX, CC_MODE, Seen, check_record, record_seen

START = 1000.0
DAW = Route(name="daw", output=(1, 2), playback=(1, 2))
DESK = Config(serial=A[1], routes=(DAW,))


def declared(config=DESK):
    return {entry.path: (entry.tags, entry.args) for entry in desired(config)
            if is_playback(entry.path)}


def wrote(at, config=DESK, **changed):
    """The journal after the desk wrote its playback registers at ``at``."""
    journal = {path: (tags, args, stamp(at)) for path, (tags, args) in declared(config).items()}
    journal.update(changed)
    return journal


def saw(at, mix=(0, "TotalMix App"), mode=1):
    reports = {CC_MIX: ("is", mix, stamp(at))}
    if mode is not None:
        reports[CC_MODE] = ("i", (mode,), stamp(at))
    return Seen(reports)


def test_the_playback_registers_are_the_matrix_and_the_flags():
    assert set(declared()) == {"/mix/1/playback/1", "/playback/1/stereo"}
    assert not is_playback("/mix/1/input/1")
    assert not is_playback("/output/1/stereo")
    assert is_playback("/playback/3/mute")


def test_without_a_running_backend_nothing_is_known():
    state = playback_state(DESK, wrote(START + 5), saw(START + 6), None)
    assert (state["state"], state["summary"], state["evidence"]) == ("unknown", "unknown", None)
    assert "no running backend" in state["detail"]


def test_nothing_applied_yet_is_unknown():
    state = playback_state(DESK, {}, saw(START + 1), START)
    assert state["state"] == "unknown"
    assert "2 of 2 declared playback register(s) have no desk write" in state["detail"]
    assert state["since"] == stamp(START)


def test_written_since_the_interface_appeared_in_totalmix_app_mode_is_re_established():
    state = playback_state(DESK, wrote(START + 5), saw(START + 6), START)
    assert state["state"] == "re-established"
    assert state["summary"] == "re-established by the desk"
    assert state["evidence"] == "write record"
    assert state["declared"] == 2
    assert state["ccmix"] == "TotalMix App"
    assert "never a hardware confirmation" in state["note"]
    assert "GTK mixer" in state["note"], "what the record cannot see is said"


def test_a_re_appeared_interface_makes_every_earlier_write_unknown_until_the_next():
    journal, cc = wrote(START + 5), saw(START + 6)
    assert playback_state(DESK, journal, cc, START)["state"] == "re-established"
    again = START + 60
    state = playback_state(DESK, journal, cc, again)
    assert state["state"] == "unknown"
    assert "re-appeared, or nothing was applied yet" in state["detail"]
    state = playback_state(DESK, wrote(again + 2), saw(again + 3), again)
    assert state["state"] == "re-established", "the new session's writes re-establish it"


def test_part_of_the_desk_written_is_unknown():
    journal = wrote(START + 5)
    journal["/playback/1/stereo"] = ("i", (1,), stamp(START - 5))
    state = playback_state(DESK, journal, saw(START + 6), START)
    assert state["state"] == "unknown"
    assert "1 of 2 declared" in state["detail"]


def test_another_writer_with_another_value_makes_it_unknown():
    journal = wrote(START + 5, **{"/mix/1/playback/1": ("fi", (-20.0, 0), stamp(START + 9))})
    state = playback_state(DESK, journal, saw(START + 6), START)
    assert state["state"] == "unknown"
    assert "another desk writer changed /mix/1/playback/1" in state["detail"]
    for written in (("i", (0.0, 0), stamp(START + 9)), ("fi", (0.0,), stamp(START + 9)),
                    ("fi", ("x", 0), stamp(START + 9))):
        state = playback_state(DESK, wrote(START + 5, **{"/mix/1/playback/1": written}),
                               saw(START + 6), START)
        assert state["state"] == "unknown"


def test_the_same_value_from_another_writer_and_writes_elsewhere_keep_it():
    journal = wrote(START + 5, **{"/mix/1/playback/1": ("fi", (0, 0), stamp(START + 9)),
                                  "/mix/3/playback/3": ("fi", (-6.0, 0), stamp(START + 9))})
    state = playback_state(DESK, journal, saw(START + 6), START)
    assert state["state"] == "re-established", "only the declared registers are covered"
    assert "not covered" in state["note"]


@pytest.mark.parametrize("cc", [Seen(), saw(START - 1), saw(START + 6, mix=(9, "?")),
                                saw(START + 6, mix=("0", "TotalMix App")),
                                saw(START + 6, mix=(True, "TotalMix App"))])
def test_an_unreported_mode_is_unknown(cc):
    state = playback_state(DESK, wrote(START + 5), cc, START)
    assert state["state"] == "unknown"
    assert "CC Mix was not reported since the interface appeared" in state["detail"]


def test_a_preset_without_playback_writes_is_known_from_static_evidence():
    state = playback_state(DESK, {}, saw(START + 6, mix=(3, "20ch")), START)
    assert state["state"] == "known"
    assert state["summary"] == "known (CC Mix preset 20ch)"
    assert state["evidence"] == "static"
    assert "not read back and not measured" in state["note"]
    other = playback_state(Config(serial=A[1]), {}, saw(START + 6, mix=(1, "6ch + phones")),
                           START)
    assert other["summary"] == "known (CC Mix preset 6ch + phones)"


def test_a_preset_with_playback_writes_since_is_unknown():
    state = playback_state(DESK, wrote(START + 5), saw(START + 6, mix=(2, "8ch")), START)
    assert state["state"] == "unknown"
    assert "whether such writes act in this mode is not established" in state["detail"]
    assert state["ccmix"] == "8ch"
    before = playback_state(DESK, wrote(START - 5), saw(START + 6, mix=(2, "8ch")), START)
    assert before["state"] == "known", "writes before the interface appeared do not count"


@pytest.mark.parametrize("mode", [None, 0])
def test_a_preset_without_cc_mode_on_is_unknown(mode):
    state = playback_state(DESK, {}, saw(START + 6, mix=(3, "20ch"), mode=mode), START)
    assert state["state"] == "unknown"
    assert "CC Mode was not reported on" in state["detail"]


def test_a_desk_without_playback_routes_re_establishes_nothing():
    inputs = Config(serial=A[1], routes=(Route(name="mic", output=(1, 2), input=(5, 6)),))
    state = playback_state(inputs, {}, saw(START + 6), START)
    assert state["state"] == "unknown"
    assert "declares no playback routes" in state["detail"]


def test_another_model_is_not_applicable():
    state = playback_state(Config(device_name="Fireface 802", usb_id="2a39:3fcd"), {}, Seen(),
                           START)
    assert state["state"] == "not-applicable"


def _stat(proc, pid, ticks, name="oscmix"):
    entry = proc / str(pid)
    entry.mkdir(parents=True, exist_ok=True)
    fields = ["S", "1"] + ["0"] * 17 + [str(ticks), "0", "0"]
    (entry / "stat").write_text("%d (%s) %s\n" % (pid, name, " ".join(fields)))


def test_the_backend_start_comes_from_proc(tmp_path):
    hz = os.sysconf("SC_CLK_TCK")
    (tmp_path / "uptime").write_text("100.00 400.00\n")
    _stat(tmp_path, 7, 20 * hz, name="osc (mix)")
    assert backend_started(7, tmp_path, now=5000.0) == pytest.approx(4920.0)
    _stat(tmp_path, 8, 200 * hz)
    assert backend_started(8, tmp_path, now=5000.0) is None, "younger than the boot is no answer"
    (tmp_path / "9").mkdir()
    (tmp_path / "9" / "stat").write_text("9 (short) S 1\n")
    assert backend_started(9, tmp_path) is None
    assert backend_started(10, tmp_path) is None
    assert backend_started(7, tmp_path / "absent") is None


def test_the_connect_check_says_whether_it_belongs_to_the_running_backend():
    record = check_record(START + 2, 1, [], [], [])
    assert connect_section(record, START)["current"] is True
    assert connect_section(record, START + 10)["current"] is False
    assert connect_section(record, None)["current"] is False
    assert connect_section(None, START)["state"] == "not-checked"


# --------------------------------------------------------------------------
# Through `--status`, which reads the files and /proc and opens nothing.
# --------------------------------------------------------------------------

@pytest.fixture
def world(tmp_path, monkeypatch, endpoint):
    _config, _control, proc = endpoint
    monkeypatch.setenv("OSCMIX_PROC_ROOT", str(proc))
    monkeypatch.setattr(status, "resolve_binary", lambda *_: None)
    monkeypatch.setattr(hostservice, "STATE", tmp_path)
    monkeypatch.setattr(desktop, "resolve_binary", lambda *_: None)
    path = tmp_path / "routing.conf"
    path.write_text("[device]\nserial=%s\n[route:daw]\nplayback=1/2\noutput=1/2\n"
                    "[route:mic]\ninput=5/6\noutput=1/2\nlevel=-6\n" % A[1])
    return path, proc


def _started_now(proc, seconds_ago=30.0):
    hz = os.sysconf("SC_CLK_TCK")
    (proc / "uptime").write_text("1000.00 4000.00\n")
    _stat(proc, 101, int((1000.0 - seconds_ago) * hz))
    return time.time() - seconds_ago


def test_status_reports_a_re_established_desk_and_the_connect_check(world, capsys):
    path, proc = world
    started = _started_now(proc)
    key = lock_key("2a39:3fd9", A[1])
    record_written(journal_path(None, key), declared(), now=started + 5)
    mismatch = {"route": "mic", "path": "/mix/1/input/5",
                "device": {"level_db": -12.0, "pan": 0}, "declared": {"level_db": -6.0, "pan": 0}}
    record_seen(journal_path(None, key, kind="seen"),
                {CC_MIX: ("is", (0, "TotalMix App"), started + 6),
                 CC_MODE: ("i", (1,), started + 6)},
                check_record(started + 4, 2, [mismatch], ["/mix/1/input/7"], []))
    report = status.collect_status(path)
    matrix, check = report["sections"]["playback_matrix"], report["sections"]["connect_check"]
    assert matrix["summary"] == "re-established by the desk"
    assert check["state"] == "mismatched"
    assert check["current"] is True
    assert check["mismatched"] == [mismatch]
    assert report["verification"] == "not-performed", "a write record is not a verification"
    assert status.print_status(path) == 0
    text = capsys.readouterr().out
    assert "summary: re-established by the desk" in text
    assert ("- route mic, path /mix/1/input/5, device level_db -12.0, pan 0, declared "
            "level_db -6.0, pan 0") in text
    assert "    unreported:\n      - /mix/1/input/7\n" in text
    assert "invalid: none" in text
    assert status.print_status(path, as_json=True) == 0
    assert json.loads(capsys.readouterr().out)["sections"]["playback_matrix"]["state"] == \
        "re-established"


def test_status_without_records_says_unknown_and_not_checked(world):
    path, proc = world
    _started_now(proc)
    sections = status.collect_status(path)["sections"]
    assert sections["playback_matrix"]["state"] == "unknown"
    assert "have no desk write" in sections["playback_matrix"]["detail"]
    assert sections["connect_check"]["state"] == "not-checked"


def test_status_without_a_readable_backend_start_says_unknown(world):
    path, _proc = world
    sections = status.collect_status(path)["sections"]
    assert sections["backend"]["state"] == "ready"
    assert "start time is unreadable" in sections["playback_matrix"]["detail"]


def test_status_of_an_invalid_desk_names_no_playback_state(world):
    path, _proc = world
    path.write_text("[device]\nusb-id=bad\n")
    sections = status.collect_status(path)["sections"]
    for name in ("playback_matrix", "connect_check"):
        assert sections[name]["state"] == "unknown"
        assert "invalid configuration" in sections[name]["detail"]


def test_interface_sections_need_an_interface(tmp_path):
    from oscmix_desk.diagnostics import BackendStatus

    absent = BackendStatus("absent", "no interface")
    sections = interface_sections(Config(), None, absent, tmp_path)
    assert sections["playback_matrix"]["detail"] == "no interface identified"
    other = interface_sections(Config(device_name="Fireface 802", usb_id="2a39:3fcd"), None,
                               absent, tmp_path)
    assert other["playback_matrix"]["state"] == "not-applicable"
    unplugged = interface_sections(Config(serial=A[1]), None, absent, tmp_path)
    assert "no running backend" in unplugged["playback_matrix"]["detail"]
    assert unplugged["connect_check"]["state"] == "not-checked"


def test_a_desk_that_cannot_be_planned_is_unknown(world, monkeypatch):
    from oscmix_desk import playstate

    path, proc = world
    _started_now(proc)

    def unplannable(_config):
        raise ValueError("route 'x' pan: input channels do not exist")

    monkeypatch.setattr(playstate, "desired", unplannable)
    state = status.collect_status(path)["sections"]["playback_matrix"]
    assert state["state"] == "unknown"
    assert "has no plan" in state["detail"]


def test_a_playback_desk_records_the_mode_from_its_read_back(coordinated, tmp_path):
    """No input routes, no connect check: the verifier's refresh still shows CC Mix."""
    from support import osc_bundle
    from two_boxes import B

    from oscmix_desk import ApplyIntent, open_desk
    from oscmix_desk.osc import encode_osc
    from oscmix_desk.seen import read_seen

    config = Config(serial=B[1], routes=(DAW,))
    with coordinated() as peer:
        peer.reports = [osc_bundle([encode_osc(CC_MIX, "is", 0, "TotalMix App"),
                                    encode_osc(CC_MODE, "i", 1),
                                    encode_osc("/output/1/stereo", "i", 1)])]
        with open_desk(config, tmp_path / "routing.conf") as operation:
            operation.apply(ApplyIntent.INITIAL)
            assert peer.order[0] != "/refresh", "nothing to compare, nothing read first"
            operation.verify()
    record = read_seen(journal_path(tmp_path / "routing.conf", lock_key("2a39:3fd9", B[1]),
                                    kind="seen"))
    assert record.reports[CC_MIX][:2] == ("is", (0, "TotalMix App"))
    assert record.connect is None
