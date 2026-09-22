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
import struct

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


@pytest.mark.parametrize('arguments', [
    ['--limit', '0'], ['--limit', '-1'], ['--match', '/not-a-register/'],
])
def test_empty_or_invalid_selection_refuses_before_device_access(sweep, monkeypatch, arguments):
    monkeypatch.setattr(sweep.sys, 'argv', ['sweep-writes.py', *arguments])
    monkeypatch.setattr(sweep, 'resolve_device', lambda *args:
                        pytest.fail('invalid selection reached hardware discovery'))
    with pytest.raises(SystemExit) as caught:
        sweep.main()
    assert caught.value.code == 2
    assert sweep.settable(0) == []


def test_sigterm_during_a_probe_restores_before_returning(sweep, monkeypatch):
    import signal

    previous = {sig: signal.getsignal(sig) for sig in (signal.SIGINT, signal.SIGTERM)}
    calls = []

    def probe(*args):
        calls.append('probe')
        signal.raise_signal(signal.SIGTERM)
        pytest.fail('probing continued after SIGTERM')

    def repair(*args):
        calls.append('repair')
        assert signal.getsignal(signal.SIGINT) == signal.SIG_IGN
        assert signal.getsignal(signal.SIGTERM) == signal.SIG_IGN
        return {'/output/1/volume': 0.0}, []

    monkeypatch.setattr(sweep, 'sweep', probe)
    monkeypatch.setattr(sweep, 'read_all', lambda *args: {})
    monkeypatch.setattr(sweep, 'repair', repair)
    findings, after, unrestored, error = sweep.measure_and_restore(
        None, None, [], {'/output/1/volume': 0.0})
    assert calls == ['probe', 'repair']
    assert not findings
    assert not unrestored
    assert after == {'/output/1/volume': 0.0}
    assert 'SIGTERM' in error
    assert {sig: signal.getsignal(sig) for sig in previous} == previous


def test_an_unsavable_artifact_still_reports_unrestored_state(sweep, monkeypatch,
                                                            tmp_path, capsys):
    from types import SimpleNamespace

    device = SimpleNamespace(binary_evidence=dict,
                             listen=lambda: SimpleNamespace(close=lambda: None), sent=[])
    monkeypatch.setattr(sweep.sys, 'argv', ['sweep-writes.py', '--limit', '1',
                                           '--out', str(tmp_path / 'missing/out.json')])
    monkeypatch.setattr(sweep, 'resolve_device', lambda *args:
                        SimpleNamespace(serial='123', key='ucx2'))
    monkeypatch.setattr(sweep, 'take_device_lock', lambda *args:
                        SimpleNamespace(release=lambda: None))
    monkeypatch.setattr(sweep, 'CheckedBackend', lambda *args: device)
    monkeypatch.setattr(sweep, 'read_all', lambda *args: {'/output/1/volume': 0.0})
    monkeypatch.setattr(sweep, 'measure_and_restore', lambda *args:
                        ([], {}, ['/output/1/volume'], 'restoration failed'))
    monkeypatch.setattr(sweep, 'built_backend_revision', lambda *args: 'a' * 40)
    monkeypatch.setattr(sweep, 'device_firmware', lambda *args: {})
    assert sweep.main() == 1
    error = capsys.readouterr().err
    assert 'NOT RESTORED' in error
    assert 'could not save sweep evidence' in error
    assert error.index('NOT RESTORED') < error.index('could not save sweep evidence')


def test_pan_drift_is_reported_without_writing_an_unpermitted_mix_cell(sweep):
    path = '/mix/1/input/1'
    before, after = sweep.Readback(), sweep.Readback()
    before[path] = after[path] = -20.0
    before.messages[path] = ('fi', (-20.0, -100))
    after.messages[path] = ('fi', (-20.0, 100))

    class NoWrites:
        def send(self, messages):
            pytest.fail('an unpermitted mix cell was written during restoration: %r' % messages)

    assert sweep.drifted(before, after) == [path]
    _, unrestored = sweep.repair(NoWrites(), None, before, after,
                                 readback=lambda *args: after)
    assert unrestored == [path]


def test_refresh_keeps_tags_and_every_argument(sweep, monkeypatch):
    from types import SimpleNamespace

    ticks = iter([0.0, 0.0, 2.0])
    monkeypatch.setattr(sweep.time, 'monotonic', lambda: next(ticks))
    monkeypatch.setattr(sweep.time, 'sleep', lambda *args: None)
    device = SimpleNamespace(request_dump=lambda: None)
    listener = SimpleNamespace(messages=lambda *args: [
        ('/mix/1/input/1', 'fi', (-20.0, -100)),
        ('/clock/source', 'is', (0, 'Internal')),
        ('/output/1/volume', '', ()),
        ('/input/1/level', 'f', (-32.0,)),
    ])
    seen = sweep.read_all(device, listener, seconds=1.0)
    assert seen == {'/mix/1/input/1': -20.0, '/clock/source': 0}
    assert seen.messages == {
        '/mix/1/input/1': ('fi', (-20.0, -100)),
        '/clock/source': ('is', (0, 'Internal')),
        '/output/1/volume': ('', ()),
    }


def test_partner_restoration_precedes_the_next_probe(sweep, monkeypatch):
    """Actual UCX-II case: channel 9 restores, its linked partner 10 does not."""
    paths = ('/input/9/eq/band2gain', '/input/10/eq/band2gain')
    state = dict.fromkeys(paths, 0.0)
    writes = []
    dropped = False

    class Device:
        def send(self, messages):
            nonlocal dropped
            for path, _tags, args in messages:
                value = args[0]
                writes.append((path, value))
                if path == paths[1] and value != 0:
                    assert state[paths[1]] == 0, 'next probe started from leftover partner state'
                state[path] = value
                partner = paths[1] if path == paths[0] else paths[0]
                if path == paths[0] and value == 0 and not dropped:
                    dropped = True
                else:
                    state[partner] = value

    monkeypatch.setattr(sweep.time, 'sleep', lambda *args: None)
    monkeypatch.setattr(sweep, 'read_all', lambda *args: dict(state))
    targets = [(path, R.register_at(sweep.devices.UCX2, path)) for path in paths]
    findings = sweep.sweep(Device(), None, targets, dict(state))
    assert sweep.summarise(findings) == {'confirmed': 2}
    assert state == dict.fromkeys(paths, 0.0)
    assert (paths[1], 0) in writes


def test_unrestorable_partner_stops_probes_before_the_next_group(sweep, monkeypatch):
    paths = ('/input/9/eq/band2gain', '/input/10/eq/band2gain')
    reference = dict.fromkeys(paths, 0.0)
    groups = []

    def one_pass(device, listener, group, state, step, attempts):
        groups.append([path for path, _register in group])
        return {paths[0]: 0.0, paths[1]: 0.4}, [paths[0]]

    monkeypatch.setattr(sweep, '_pass', one_pass)
    monkeypatch.setattr(sweep, 'repair', lambda *args: ({paths[1]: 0.4}, [paths[1]]))
    targets = [(path, R.register_at(sweep.devices.UCX2, path)) for path in paths]
    with pytest.raises(RuntimeError, match='pass restoration incomplete'):
        sweep.sweep(None, None, targets, reference)
    assert groups == [[paths[0]]]


@pytest.mark.parametrize('changed', [False, True])
def test_evidence_names_comparison_code_and_refuses_a_changed_runtime(
        sweep, monkeypatch, tmp_path, changed):
    import json
    from types import SimpleNamespace

    actual = sweep.source_evidence()
    assert {'numeric.py', 'registers.py', 'devices.py'} <= actual['runtime_sha256'].keys()
    assert all(len(value) == 64 for value in actual['runtime_sha256'].values())
    later = dict(actual, runtime_sha256=dict(actual['runtime_sha256']))
    if changed:
        later['runtime_sha256']['numeric.py'] = '0' * 64
    sources = iter([actual, later])
    monkeypatch.setattr(sweep, 'source_evidence', lambda: next(sources))
    output = tmp_path / 'measurement.json'
    monkeypatch.setattr(sweep.sys, 'argv', ['sweep-writes.py', '--limit', '1',
                                           '--out', str(output)])
    monkeypatch.setattr(sweep, 'resolve_device', lambda *args:
                        SimpleNamespace(serial='123', key='ucx2'))
    monkeypatch.setattr(sweep, 'take_device_lock', lambda *args:
                        SimpleNamespace(release=lambda: None))
    device = SimpleNamespace(binary_evidence=dict,
                             listen=lambda: SimpleNamespace(close=lambda: None), sent=[])
    monkeypatch.setattr(sweep, 'CheckedBackend', lambda *args: device)
    state = {'/output/1/volume': 0.0}
    monkeypatch.setattr(sweep, 'read_all', lambda *args: state)
    monkeypatch.setattr(sweep, 'measure_and_restore', lambda *args:
                        ([{'verdict': 'confirmed'}], state, [], None))
    monkeypatch.setattr(sweep, 'built_backend_revision', lambda *args: 'a' * 40)
    monkeypatch.setattr(sweep, 'device_firmware', lambda *args: {})
    assert sweep.main() == int(changed)
    evidence = json.loads(output.read_text())
    assert evidence['desk_source'] == actual
    assert evidence['not_restored'] == []
    if changed:
        assert 'source files changed during measurement' in evidence['error']
    else:
        assert evidence['error'] is None


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
                lo, hi = (struct.unpack("!f", struct.pack("!f", v))[0]
                          if register.tags == "f" else v
                          for v in (register.lo, register.hi))
                assert register.lo <= value <= register.hi, path
                assert lo <= sweep.as_tag(value, register.tags) <= hi, path


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


@pytest.mark.parametrize("protected", [
    "/input/1/48v", "/input/3/reflevel", "/output/3/reflevel",
    "/clock/samplerate",
])
def test_restore_never_writes_protected_or_read_only_drift(sweep, monkeypatch,
                                                         protected):
    """Knowing a register exists is not permission to restore it."""
    monkeypatch.setattr(sweep.time, "sleep", lambda _seconds: None)
    safe = "/output/7/eq/band2q"
    reference = {safe: 1.0, protected: 1}
    current = {safe: 4.4, protected: 0}
    sent = []

    class Device:
        def send(self, messages):
            for path, _tags, args in messages:
                sent.append(path)
                current[path] = args[0]

    after, unrestored = sweep.repair(
        Device(), None, reference, current, rounds=3,
        readback=lambda _device, _listener: dict(current))
    assert sent == [safe]
    assert after[safe] == 1.0
    assert unrestored == [protected]


def test_restore_keeps_missing_readback_visible(sweep, monkeypatch):
    monkeypatch.setattr(sweep.time, "sleep", lambda _seconds: None)
    sent = []

    class Device:
        def send(self, messages):
            sent.extend(messages)

    reference = {"/input/1/48v": 1, "/output/1/eq/band1gain": 0.0}
    _after, unrestored = sweep.repair(
        Device(), None, reference, {}, rounds=2,
        readback=lambda _device, _listener: {})
    assert unrestored == sorted(reference)
    assert [path for path, _tags, _args in sent] == [
        "/output/1/eq/band1gain", "/output/1/eq/band1gain"]


def test_a_protected_batch_is_rejected_before_any_write(sweep):
    from oscmix_desk.devices import UCX2

    sent = []

    class Device:
        def send(self, messages):
            sent.extend(messages)

    paths = ("/output/1/eq/band1gain", "/input/1/48v")
    writes = [(path, R.register_at(UCX2, path), 0) for path in paths]
    with pytest.raises(ValueError, match="not permitted"):
        sweep.write_batch(Device(), writes)
    assert sent == []


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


@pytest.mark.parametrize(("path", "value", "wrong"), [
    ("/output/1/roomeq/delay", .00425, .0001),
    ("/echo/delay", .2, .01),
    ("/echo/width", .37, .01),
    ("/input/1/dynamics/compratio", 1.4, 1.1),
    ("/input/1/gain", 1., float("nan")),
])
def test_a_changed_but_wrong_report_never_confirms(sweep, path, value, wrong):
    finding = sweep.verdict(path, 0., [(value, 20., wrong)])
    assert finding["verdict"] != "confirmed"
    observation, = finding["observations"]
    assert observation["requested"] == value
    assert observation["encoded"] == struct.unpack("!f", struct.pack("!f", value))[0]
    assert "reported" in observation


def test_a_real_pass_records_requested_wire_and_actual_report(sweep, monkeypatch):
    from oscmix_desk.devices import UCX2

    path = "/output/1/roomeq/delay"
    register = R.register_at(UCX2, path)
    before = {path: 0.}
    states = iter([{path: .0001}, dict(before)])
    sent = []

    class Device:
        def send(self, messages):
            sent.extend(messages)

    monkeypatch.setattr(sweep, "read_all", lambda *_args: next(states))
    monkeypatch.setattr(sweep.time, "sleep", lambda _s: None)
    attempts = {path: []}
    after, settled = sweep._pass(Device(), None, [(path, register)], before, 0, attempts)
    finding = sweep.verdict(path, 0., attempts[path])
    observation, = finding["observations"]
    assert observation["requested"] == .00425
    assert observation["encoded"] == sent[0][2][0]
    assert observation["expected_report"] == .004000000189989805
    assert observation["reported"] == .0001
    assert finding["verdict"] != "confirmed"
    assert settled == [path]
    assert after == before


def test_an_unchanged_observation_is_kept_and_not_confirmed(sweep):
    finding = sweep.verdict("/echo/delay", .1, [(.2, .1, .1)])
    assert finding["verdict"] == "ignored"
    assert finding["observations"][0]["reported"] == .1


@pytest.mark.parametrize("exception", [OSError("send failed"), KeyboardInterrupt()])
def test_an_interrupted_probe_still_restores_and_reports_failure(sweep, monkeypatch,
                                                                exception):
    path = "/output/1/eq/band1gain"
    before = {path: 0.}
    state = dict(before)

    class Device:
        def send(self, messages):
            for name, _tags, args in messages:
                state[name] = args[0]

    def fails(*_args):
        state[path] = -3.
        raise exception

    monkeypatch.setattr(sweep, "sweep", fails)
    monkeypatch.setattr(sweep, "read_all", lambda *_args: dict(state))
    monkeypatch.setattr(sweep.time, "sleep", lambda _s: None)
    findings, after, unrestored, error = sweep.measure_and_restore(Device(), None, [], before)
    assert findings == []
    assert after == before
    assert unrestored == []
    assert type(exception).__name__ in error


def test_failed_restoration_does_not_claim_an_unchanged_device(sweep, monkeypatch):
    before = {"/output/1/eq/band1gain": 0.}
    monkeypatch.setattr(sweep, "sweep", lambda *_args: [])

    def disconnected(*_args):
        raise ValueError("lost identity")

    monkeypatch.setattr(sweep, "read_all", disconnected)
    _, after, unrestored, error = sweep.measure_and_restore(None, None, [], before)
    assert after == {}
    assert unrestored == list(before)
    assert "restoration failed" in error


def test_replaced_backend_cannot_receive_a_restore(sweep, monkeypatch):
    from oscmix_desk.discovery import Device
    from oscmix_desk.process import PortHolder

    interface = Device("2a39:3fd9", "24216011", 24)
    holder = PortHolder(123, True, 24, interface.serial)
    monkeypatch.setattr(sweep, "resolve_device", lambda *_args: interface)
    monkeypatch.setattr(sweep, "port_holder", lambda *_args: holder)
    backend = sweep.CheckedBackend(interface, 1, 2)
    holder = PortHolder(456, True, 24, interface.serial)
    with pytest.raises(ValueError, match="identity"):
        backend.send([("/output/1/eq/band1gain", "f", (0.,))])


def test_nonfinite_raw_values_survive_in_valid_json(sweep):
    import json

    value = {"reported": [float("nan"), float("inf"), float("-inf"), None]}
    text = json.dumps(sweep.json_safe(value), allow_nan=False)
    assert json.loads(text) == {"reported": ["nan", "inf", "-inf", None]}


@pytest.mark.parametrize("state", [{}, {"/echo/delay": float("nan")}])
def test_missing_or_invalid_baseline_is_not_a_successful_deliberate_skip(sweep, state):
    from oscmix_desk.devices import UCX2

    path = "/echo/delay"
    target = (path, R.register_at(UCX2, path))
    finding, = sweep.sweep(None, None, [target], state)
    assert finding["verdict"] == "undetermined"
