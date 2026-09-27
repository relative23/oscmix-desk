"""Routing verification across the production coordinated client."""

import os
import tempfile
from pathlib import Path

import oracle
import pytest
from backend_doubles import RecordingBackend
from control_peer import REFRESH, ScriptedControl
from support import repo_file

from oscmix_desk import osc, verify
from oscmix_desk.backend import Control
from oscmix_desk.devices import UCX2
from oscmix_desk.errors import ReceivePortError
from oscmix_desk.observation import Observation


def make_route(session_mod, **kwargs):
    defaults = dict(name="monitors", playback=(1, 2), output=(5, 6),
                    level=0.0, volume=0.0, stereo=True)
    defaults.update(kwargs)
    return session_mod.Route(**defaults)


def test_expected_registers_keyed_by_path(session_mod):
    config = session_mod.Config(routes=[make_route(session_mod)])
    registers = session_mod.expected_registers(config)
    assert registers["/mix/5/playback/1"] == ("fi", (0.0, 0))
    assert registers["/output/5/stereo"] == ("i", (1,))
    assert len(registers) == 5


def test_prompt_reporting_hint(session_mod):
    # The playback mix matrix is not dumped at all, and the /playback/*
    # section streams so late in the multi-second dump that it cannot be
    # awaited. The audible /output/* path arrives early.
    prompt = verify.register_promptly_reported
    assert prompt("/mix/5/playback/1") is False
    # Reported, and promptly: it is in the recording, at 0.0 s. This
    # line asserted False for two releases and is why the rule drifted
    # -- a test written from the same wrong belief as the code cannot
    # catch it. test_never_reported_agrees_with_the_recorded_dump
    # asserts against the dump instead, which cannot hold a belief.
    assert prompt("/playback/1/stereo") is True
    assert prompt("/mix/5/input/3") is True
    assert prompt("/output/5/volume") is True
    assert prompt("/output/5/stereo") is True


@pytest.mark.parametrize('path', ['/echo/delay', '/unmodelled/parameter'])
def test_channel_cold_plug_hint_does_not_suppress_global_or_unknown_reports(path):
    assert verify.register_promptly_reported(path, UCX2) is True


def test_register_matches_with_float_tolerance():
    # Expected values are deliberately non-zero: with want = 0.0 a sign
    # error in the comparison (want - got vs want + got) is invisible,
    # which is exactly what a surviving mutant showed.
    def match(tags, want, got):
        observed = Observation({"/mix/5/input/1": (tags, want)}, UCX2)
        observed.absorb(("/mix/5/input/1", tags, got))
        return observed.complete
    assert match("fi", (-6.0, 0), (-5.7, 0)) is True     # quantization
    assert match("fi", (-6.0, 0), (-5.4, 0)) is False    # real deviation
    assert match("fi", (-6.0, 0), (6.0, 0)) is False     # sign flipped
    assert match("f", (3.0,), (-3.0,)) is False          # sign flipped
    assert match("fi", (-6.0, 0), (-6.0, 5)) is False    # int mismatch
    assert match("fi", (-6.0, 0), (-6.0,)) is False      # too short
    assert match("fi", (-6.0, 0), (-6.0, 0, 99)) is True  # extra args ignored
    assert match("i", (1,), ("x",)) is False             # unparseable
    assert match("i", (1,), (-1,)) is False              # sign, integer path


def run_verify(session_mod, registers, state, timeout=3.0, **kwargs):
    with tempfile.TemporaryDirectory(prefix="verify-") as directory:
        server = ScriptedControl(Path(directory) / "control.sock", reports=state)
        client = Control(server.path, os.getpid(), reader=True)
        try:
            return session_mod.verify_routing(registers, client, timeout=timeout, **kwargs)
        finally:
            client.close()
            server.close()


def test_verify_confirms_matching_state(session_mod):
    route = make_route(session_mod)
    registers = session_mod.expected_registers(session_mod.Config(routes=[route]))
    state = [osc.encode_osc(path, types, *args)
             for path, types, args in oracle.route_messages(route)]
    result = run_verify(session_mod, registers, state)
    # Every register was replayed verbatim -- including the ones the
    # real device would not dump -- so all of them count as confirmed.
    assert result.mismatched == []
    assert result.unobserved == []
    assert sorted(registers) == result.confirmed


def test_cancel_before_read_keeps_all_expectations_unobserved():
    backend = RecordingBackend()
    result = verify.verify_routing(
        {"/output/5/volume": ("f", (-6.,)), "/output/1/volume": ("f", (-12.,))},
        backend, should_stop=lambda: True, device_model=UCX2)
    assert result == verify.VerifyResult([], [], ["/output/1/volume", "/output/5/volume"])
    assert backend.dumps == 0
    assert backend.sent == []


def test_read_window_closes_after_every_reportable_path_even_with_write_only_left(monkeypatch):
    backend = RecordingBackend()
    deliveries = []

    def receive(_timeout):
        deliveries.append(1)
        assert len(deliveries) == 1, "all reportable expectations were already confirmed"
        yield "/output/5/volume", "f", (-6.,)

    monkeypatch.setattr(backend, "messages", receive)
    result = verify.verify_routing(
        {"/output/5/volume": ("f", (-6.,)), "/output/5/loopback": ("i", (1,))},
        backend, device_model=UCX2)
    assert result == verify.VerifyResult(["/output/5/volume"], [], ["/output/5/loopback"])
    assert backend.dumps == 1
    assert deliveries == [1]


def test_verify_classifies_wrong_value_as_mismatch(session_mod):
    route = make_route(session_mod)
    registers = session_mod.expected_registers(session_mod.Config(routes=[route]))
    state = []
    for path, types, args in oracle.route_messages(route):
        if path == "/output/5/volume":
            # Corrupt the register: -20 dB instead of 0 dB.
            state.append(osc.encode_osc(path, "f", -20.0))
        else:
            state.append(osc.encode_osc(path, types, *args))
    result = run_verify(session_mod, registers, state, timeout=0.5)
    assert result.mismatched == ["/output/5/volume"]
    assert result.unobserved == []


def test_verify_classifies_missing_register_as_unobserved(session_mod):
    route = make_route(session_mod)
    registers = session_mod.expected_registers(session_mod.Config(routes=[route]))
    state = [osc.encode_osc(path, types, *args)
             for path, types, args in oracle.route_messages(route)
             if path != "/mix/5/playback/1"]
    result = run_verify(session_mod, registers, state, timeout=0.5)
    assert result.mismatched == []
    assert result.unobserved == ["/mix/5/playback/1"]


def test_hint_excluded_register_is_still_compared_when_reported(session_mod):
    # Self-healing: if a future oscmix starts dumping the playback mix
    # matrix, a wrong value must surface as a mismatch even though the
    # hint says the register is not promptly reported.
    registers = {"/mix/5/playback/1": ("fi", (0.0, 0))}
    state = [osc.encode_osc("/mix/5/playback/1", "fi", -30.0, 0)]
    result = run_verify(session_mod, registers, state, timeout=0.5)
    assert result.mismatched == ["/mix/5/playback/1"]


def test_later_matching_report_overrides_mismatch(session_mod):
    # During settling the device may first echo a stale value; a later
    # matching report must win.
    registers = {"/output/5/volume": ("f", (0.0,))}
    state = [osc.encode_osc("/output/5/volume", "f", -20.0),
             osc.encode_osc("/output/5/volume", "f", 0.0)]
    result = run_verify(session_mod, registers, state)
    assert result.confirmed == ["/output/5/volume"]
    assert result.mismatched == []


def test_verify_propagates_a_lost_refresh_acknowledgement(session_mod, tmp_path):
    server = ScriptedControl(tmp_path / "control.sock", lose_reply=REFRESH)
    client = Control(server.path, os.getpid(), reader=True)
    try:
        with pytest.raises(ReceivePortError, match="disconnected"):
            session_mod.verify_routing({"/output/5/volume": ("f", (0.,))}, client, timeout=.1)
        assert server.writes == []
    finally:
        client.close()
        server.close()


def test_a_mismatch_keeps_the_window_open(session_mod):
    # The early exit is guarded by `not mismatched`: while anything is
    # wrong the loop must keep listening, because the device may still
    # send a corrected value. Dropping that guard made the read-back
    # return the first, stale answer.
    registers = {"/output/5/volume": ("f", (0.0,)),
                 "/output/5/stereo": ("i", (1,))}
    state = [osc.encode_osc("/output/5/volume", "f", -20.0),
             osc.encode_osc("/output/5/stereo", "i", 1),
             osc.encode_osc("/output/5/volume", "f", 0.0)]
    result = run_verify(session_mod, registers, state)
    assert result.mismatched == []
    assert sorted(result.confirmed) == ["/output/5/stereo",
                                        "/output/5/volume"]


def test_exiting_early_needs_every_prompt_register_not_merely_some(session_mod):
    # The condition is `prompt <= confirmed`, a subset test. A proper
    # subset would exit while one promptly-reported register was still
    # missing, and report it as unobserved.
    registers = {"/output/5/volume": ("f", (0.0,)),
                 "/output/6/volume": ("f", (0.0,)),
                 "/output/5/stereo": ("i", (1,))}
    state = [osc.encode_osc(path, types, *args)
             for path, (types, args) in registers.items()]
    result = run_verify(session_mod, registers, state)
    assert result.unobserved == []
    assert len(result.confirmed) == 3


def test_registers_outside_the_expectation_are_ignored(session_mod):
    # The dump carries thousands of registers we never asked about. The
    # Ignore unknown paths even when other expected reports are pending.
    registers = {"/output/5/volume": ("f", (0.0,))}
    state = [osc.encode_osc("/input/3/gain", "f", 12.0),
             osc.encode_osc("/hardware/ccmix", "i", 0),
             osc.encode_osc("/output/5/volume", "f", 0.0)]
    result = run_verify(session_mod, registers, state)
    assert result.confirmed == ["/output/5/volume"]
    assert result.mismatched == []


def test_a_corrupt_delivery_refuses_the_dump(session_mod):
    # The reliable control path must not turn malformed feedback into
    # silence and allow a dependent repair based on a partial delivery.
    registers = {"/output/5/volume": ("f", (0.0,))}
    state = [b"\x01\x02\x03\x04",                      # no NUL: undecodable
             osc.encode_osc("/output/5/volume", "f", 0.0)]
    with pytest.raises(ReceivePortError, match="invalid OSC delivery"):
        run_verify(session_mod, registers, state)


def test_observer_receives_the_path_and_its_arguments(session_mod):
    # verify_and_repair hangs the mix re-apply off these arguments; a
    # caller handed None for either would never fire it.
    seen = []
    registers = {"/output/5/stereo": ("i", (1,))}
    state = [osc.encode_osc("/output/5/stereo", "i", 1)]
    run_verify(session_mod, registers, state,
               on_observed=lambda path, args: seen.append((path, tuple(args))))
    assert ("/output/5/stereo", (1,)) in seen


# --------------------------------------------------------------------------
# The "never reported" rule, held against the recording it describes.
# --------------------------------------------------------------------------

def test_never_reported_agrees_with_the_recorded_dump(session_mod):
    """Every register the device actually sent must count as reportable.

    The rule was written from memory and drifted: it excluded everything
    under `/playback/`, while the recording carries 42 registers there,
    every `/playback/<n>/stereo` among them. Those are the input-side
    link flags. Calling them unreportable meant a lost link write was
    never a problem and never retried -- on the one register family the
    whole two-phase apply exists for.

    Asserting against the recording rather than against a list keeps the
    rule and the evidence in one place.
    """
    import json

    from oscmix_desk.devices import device_for_name
    from oscmix_desk.verify import register_ever_reported

    dump = json.loads(repo_file("tests", "data", "refresh-dump.json"
                                ).read_text())
    device = device_for_name("Fireface UCX II")
    denied = [path for path in dump["registers"]
              if not register_ever_reported(path, device)]
    assert denied == [], (
        "the device reported these, but the rule says it never does: %s"
        % denied[:10])


def test_the_playback_mix_matrix_is_the_family_that_is_absent(session_mod):
    # The other direction: the one exclusion that the recording supports.
    import json

    from oscmix_desk.verify import register_ever_reported

    dump = json.loads(repo_file("tests", "data", "refresh-dump.json"
                                ).read_text())
    assert [p for p in dump["registers"]
            if p.startswith("/mix/") and "/playback/" in p] == []
    assert register_ever_reported("/mix/1/playback/1") is False


def test_the_register_table_decides_what_is_ever_reported(verify_mod):
    """One source for one fact (0.6.3).

    The playback matrix used to be excluded by a string rule beside a
    table that already classed it REESTABLISHED. With a model, the
    table decides for every class; the rule is only for a device
    without one.
    """
    from oscmix_desk import devices

    ucx2 = devices.UCX2
    assert verify_mod.register_ever_reported("/mix/5/playback/1", ucx2) is False
    assert verify_mod.register_ever_reported("/output/1/loopback", ucx2) is False
    assert verify_mod.register_ever_reported("/output/1/volume", ucx2) is True
    assert verify_mod.register_ever_reported("/mix/5/input/1", ucx2) is True
    # A path the table does not know falls through to the rule.
    assert verify_mod.register_ever_reported("/nothing/like/it", ucx2) is True
    assert verify_mod.register_ever_reported("/mix/5/playback/1", None) is False
    assert verify_mod.register_ever_reported("/output/1/loopback", None) is True
