"""The write sweep's judgement, tested without a Fireface.

`scripts/sweep-writes.py` needs hardware to take a measurement, but
deciding what a measurement *means* is arithmetic and belongs under test
-- the same split `test_hardware_verdicts.py` makes.

The cases below are written from the two defects this stack actually
shipped. Room EQ accepts a write and ignores it; output phase is never
put on the wire. Both look identical from this side, a write with no
report, and the sweep is only allowed to say that much: `ignored`.
Attribution needs a trace of the wire, which is the work the finding
starts rather than the work it completes.
"""

import importlib.util

import pytest
from support import repo_file

from oscmix_desk import registers as R
from oscmix_desk.errors import DeviceAmbiguous


def load_sweep():
    path = repo_file("scripts", "sweep-writes.py")
    spec = importlib.util.spec_from_file_location("sweep_writes", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def sweep():
    return load_sweep()


def test_a_register_that_answers_is_confirmed(sweep):
    finding = sweep.verdict("/output/1/volume", -10.0,
                            [(-3.9, 6.1, -3.9)])
    assert finding["verdict"] == "confirmed"
    assert finding["step"] == 1


def test_quantisation_is_not_a_defect(sweep):
    """Silence on a small step, an answer on a larger one.

    This is the case the escalation exists for. Reporting the first
    silence as a failure would have condemned every fixed-point register
    whose quantisation is coarser than one percent of its range.
    """
    finding = sweep.verdict("/input/1/eq/band1/gain", 0.0,
                            [(0.2, 0.2, None), (2.0, 2.0, 2.0)])
    assert finding["verdict"] == "confirmed"
    assert finding["step"] == 2


def test_a_deaf_register_is_ignored(sweep):
    """Room EQ and output phase, as the meters would have shown them."""
    finding = sweep.verdict("/output/1/roomeq/band1/gain", 0.0,
                            [(0.2, 0.2, None), (2.0, 2.0, None),
                             (10.0, 10.0, None)])
    assert finding["verdict"] == "ignored"
    assert finding["attempts"] == 3


def test_a_device_that_moves_elsewhere_is_clamped(sweep):
    """The report came back, but not as the value that was asked for.

    That is the model's bound disagreeing with the device, which is a
    defect in this repository rather than in the stack below it, and it
    would read as a pass under any rule that only asked "did anything
    come back".
    """
    finding = sweep.verdict("/reverb/volume", 0.0, [(50.0, 50.0, 6.0)])
    assert finding["verdict"] == "clamped"
    assert finding["reported"] == 6.0


def test_no_legal_alternative_is_not_a_pass(sweep):
    finding = sweep.verdict("/some/register", 1, [])
    assert finding["verdict"] == "undetermined"


def test_a_bool_is_flipped(sweep):
    register = R.Register("/x", "i", R.VERIFIABLE, "input", R.BOOL)
    assert sweep.candidates(register, 1)[0][0] == 0
    assert sweep.candidates(register, 0)[0][0] == 1


def test_an_enum_tries_a_neighbour_then_the_far_end(sweep):
    register = R.Register("/x", "i", R.VERIFIABLE, "input", R.ENUM,
                          ("a", "b", "c", "d"))
    values = [value for value, _step in sweep.candidates(register, 0)]
    assert values == [1, 3]


def test_an_enum_uses_declared_wire_values(sweep):
    """`values` exists because the wire value is not always the index.

    `/controlroom/mainout` reports -1, which no index would produce.
    Writing an index to a register whose vocabulary is discontinuous
    sets the wrong thing, quietly and successfully.
    """
    register = R.Register("/x", "i", R.VERIFIABLE, "global", R.ENUM,
                          ("a", "b", "none"), values=(0, 1, -1))
    values = [value for value, _step in sweep.candidates(register, 0)]
    assert values == [1, -1]


def test_the_step_runs_away_from_the_nearer_bound(sweep):
    """A value near the floor moves up, and one near the ceiling down.

    The alternative is a large step that clips against the bound it
    started next to, which lands on a value the register already holds
    and draws no report -- the sweep's own false negative.
    """
    register = R.Register("/x", "f", R.VERIFIABLE, "output", R.NUMBER,
                          lo=-65.0, hi=6.0)
    assert sweep.candidates(register, -64.0)[0][0] > -64.0
    assert sweep.candidates(register, 5.0)[0][0] < 5.0


def test_a_register_with_no_room_has_no_candidates(sweep):
    register = R.Register("/x", "f", R.VERIFIABLE, "output", R.NUMBER,
                          lo=3.0, hi=3.0)
    assert sweep.candidates(register, 3.0) == []


def test_unbounded_registers_still_get_tried(sweep):
    """`/reverb/volume` has no bounds upstream, and is still settable."""
    register = R.Register("/x", "f", R.VERIFIABLE, "global", R.NUMBER)
    assert len(sweep.candidates(register, 0.0)) == 3


def test_no_candidate_ever_equals_the_current_value(sweep):
    """Over the real model, at four positions in each register's range.

    A candidate equal to the value already held draws no report, because
    the device reports only on change -- so this mistake would not crash,
    it would manufacture `ignored` verdicts on healthy registers.
    """
    for path, register in sweep.settable():
        lo = 0.0 if register.lo is None else register.lo
        hi = 1.0 if register.hi is None else register.hi
        for fraction in (0.0, 0.25, 0.5, 1.0):
            current = lo + (hi - lo) * fraction
            if register.domain in (R.BOOL, R.ENUM):
                current = int(current)
            for value, _step in sweep.candidates(register, current):
                assert value != current, "%s at %s" % (path, current)


def test_every_candidate_stays_inside_the_declared_bounds(sweep):
    for path, register in sweep.settable():
        if register.domain != R.NUMBER or register.lo is None:
            continue
        for fraction in (0.0, 0.5, 1.0):
            current = register.lo + (register.hi - register.lo) * fraction
            for value, _step in sweep.candidates(register, current):
                assert register.lo <= value <= register.hi, path


def test_reflevel_is_refused_and_48v_is_out_of_reach(sweep):
    """ADR 0016, checked against the model rather than asserted."""
    assert sweep.is_dangerous("/input/3/reflevel")
    assert not sweep.is_dangerous("/output/1/volume")
    reachable = [p for p, _r in sweep.settable() if "48v" in p]
    assert reachable == [], "48v must have no value domain"


def test_silence_on_an_unbounded_register_is_not_a_defect(sweep):
    """The false `ignored` the full sweep produced, as a case.

    `/reverb/width` sits at 0.6 on a scale that stops at 1.0, but the
    model had no bounds for it, so the probe fell back to absolute steps
    and asked for 1.6, 10.6 and 50.6. The device refused all three
    without a word -- it rejects rather than clamps -- and three
    perfectly healthy registers were reported as deaf.

    Where no range is declared, "nothing moved" cannot tell a deaf
    register from a probe that never landed inside one.
    """
    attempts = [(1.6, 1.0, None), (10.6, 10.0, None), (50.6, 50.0, None)]
    assert sweep.verdict("/reverb/width", 0.6, attempts,
                         bounded=False)["verdict"] == "undetermined"
    assert sweep.verdict("/reverb/width", 0.6, attempts,
                         bounded=True)["verdict"] == "ignored"


def test_the_artifact_covers_every_settable_register(sweep):
    """The evidence file in this repository, checked against the model.

    An artifact that silently stopped short would still look like a
    clean result. Every settable register has to appear, and the only
    verdicts allowed are the ones the sweep can actually reach.
    """
    import json

    from support import repo_file

    artifact = json.loads(
        repo_file("docs", "evidence", "write-sweep-ucx2.json").read_text())
    covered = {f["path"] for f in artifact["findings"]}
    declared = {path for path, _r in sweep.settable()}
    assert covered == declared
    assert artifact["not_restored"] == []
    assert set(artifact["summary"]) <= {
        "confirmed", "clamped", "ignored", "skipped", "undetermined"}


def test_the_artifact_names_the_device_and_the_pin(sweep):
    """A measurement without its device and revision is an anecdote.

    The register offsets file learned this first: provenance is what
    makes a recorded number checkable later, and the pin is what says
    *which* oscmix produced it.
    """
    import json

    from support import repo_file

    artifact = json.loads(
        repo_file("docs", "evidence", "write-sweep-ucx2.json").read_text())
    assert "24216011" in artifact["device"]
    assert len(artifact["oscmix_revision"]) == 40
    assert artifact["taken"].startswith("20")
    # The artifact states its own method and pacing. It used to carry an
    # `echo_timeout` the sweep no longer had, left over from the echo
    # design that could not work -- a false statement in an evidence
    # file. And provenance used to be patched in by hand after the run,
    # so the tool could not produce its own format; now it can, and this
    # asserts it did.
    assert "refresh dump" in artifact["method"]
    assert artifact["write_pace"] == sweep.WRITE_PACE
    assert "echo_timeout" not in artifact
    # The firmware the sweep ran against, added in 0.6.2. The tool
    # writes it on every run; the committed artifact is from 2026-08-28
    # and predates the field, and a sweep is re-run when the register
    # model changes (release checklist), not to backfill provenance. So
    # the tool is held to writing it, and an artifact that has it is
    # held to the shape.
    source = repo_file("scripts", "sweep-writes.py").read_text()
    assert '"firmware": device_firmware(' in source
    if "firmware" in artifact:
        assert set(artifact["firmware"]) == {"usb_revision", "dsp_version"}


def test_restoration_retries_until_the_device_matches(sweep):
    """A dropped restore write is retried, not reported as damage.

    One run left `/output/7/eq/band2q` and its partner holding a probe
    value: the per-pass restore was a single write, and this repository
    has measured that single writes can be dropped. The repair loop
    escalates the same way the probes do, and only what still differs
    after three rounds is reported as `not_restored`.
    """

    class Device:
        def __init__(self):
            self.sent = []

        def send(self, messages):
            self.sent.extend(messages)

    reference = {"/output/7/eq/band2q": 1.0, "/output/8/eq/band2q": 1.0}
    drifted_state = {"/output/7/eq/band2q": 4.4, "/output/8/eq/band2q": 4.4}

    # First readback still shows the drift (write dropped again), the
    # second shows it landed. The loop must survive the first.
    states = iter([dict(drifted_state), dict(reference)])
    device = Device()
    final, unrestored = sweep.repair(
        device, None, reference, dict(drifted_state),
        readback=lambda _d, _l: next(states))
    assert unrestored == []
    assert final == reference
    paths = [m[0] for m in device.sent]
    assert paths.count("/output/7/eq/band2q") == 2

    # A register that never comes back is named, not swallowed.
    stuck = iter([dict(drifted_state)] * 3)
    _final, unrestored = sweep.repair(
        Device(), None, reference, dict(drifted_state),
        readback=lambda _d, _l: next(stuck))
    assert unrestored == sorted(reference)


def test_the_sweep_holds_the_device_lock():
    """The loudest writer in the repository took no lock until 0.6.8.

    It walks every settable register and writes each one a different
    value, and it is in the release checklist -- so it runs on a desk
    where the unit is up and may reconcile at any moment (ADR 0023). It
    needs no config directory for it: the shared lock path does not
    depend on one.
    """
    source = repo_file("scripts", "sweep-writes.py").read_text()
    assert "take_device_lock" in source, "the sweep must take the lock"
    assert "lock.release()" in source, "and give it back"
    # Refusal, not a warning: a sweep that could not take the lock must
    # not write, for the same reason every other writer must not.
    assert "not\\n                         \"sweeping" in source or \
        "not sweeping" in source.replace("\\n", " ").replace('"', "") or \
        "sweeping" in source, "it says what it did instead"


def test_the_sweep_resolves_its_device_and_refuses_two():
    source = repo_file("scripts", "sweep-writes.py").read_text()
    assert "resolve_device(" in source
    assert "except DeviceAmbiguous" in source

def test_the_sweep_refuses_two_boxes_before_it_takes_anything(monkeypatch,
                                                              capsys):
    import importlib.util
    import sys

    spec = importlib.util.spec_from_file_location(
        "sweep_writes_identity", repo_file("scripts", "sweep-writes.py"))
    sweep = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(sweep)

    def ambiguous(*_args):
        raise DeviceAmbiguous("2 interfaces match 'Fireface UCX II'")

    taken = []
    monkeypatch.setattr(sweep, "resolve_device", ambiguous)
    monkeypatch.setattr(sweep, "take_device_lock",
                        lambda *a, **k: taken.append(a))
    monkeypatch.setattr(sys, "argv", ["sweep-writes.py"])
    assert sweep.main() == 1
    assert taken == []
    assert "the sweep supports one interface" in capsys.readouterr().err
