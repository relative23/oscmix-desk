"""Typed prerequisites are observations, never invented config targets."""

import itertools
import os

import pytest
from control_peer import DEVICE, OBSERVATION, REFRESH, ScriptedControl

from oscmix_desk import osc, verify
from oscmix_desk.backend import Control
from oscmix_desk.devices import UCX2
from oscmix_desk.errors import ReceivePortError
from oscmix_desk.observation import Observation, boolean_dependency

GAIN = "/input/1/gain"
AUTOSET = "/input/1/autoset"
PARTNER = "/input/2/autoset"


class WindowBackend:
    """One fresh scripted request per window; one decoded batch per receive."""

    def __init__(self, windows):
        self.windows = iter(windows)
        self.batches = iter(())
        self.dumps = 0
        self.receives = 0

    def request_dump(self):
        self.dumps += 1
        self.batches = iter(next(self.windows))

    def messages(self, _timeout):
        self.receives += 1
        for item in next(self.batches, []):
            if isinstance(item, Exception):
                raise item
            yield item


@pytest.fixture
def clock(monkeypatch):
    ticks = itertools.count(0, .01)
    monkeypatch.setattr(verify.time, "monotonic", lambda: next(ticks))


def read(backend, registers=None, dependencies=(AUTOSET,), **kwargs):
    return verify.verify_routing(
        {GAIN: ("f", (0.,))} if registers is None else registers,
        backend, timeout=.3, device_model=UCX2,
        dependency_paths=dependencies, **kwargs)


@pytest.mark.parametrize("value", [0, 1])
def test_boolean_dependency_accepts_only_typed_boolean_values(value):
    assert boolean_dependency(("i", (value,))) == value


@pytest.mark.parametrize("report", [
    None, ("f", (0.,)), ("s", ("0",)), ("", (0,)), ("ii", (0, 0)),
    ("i", ()), ("i", (0, 0)), ("i", (False,)), ("i", (True,)),
    ("i", (0.,)), ("i", (1.,)), ("i", ("0",)), ("i", (-1,)),
    ("i", (2,)), ("i", (float("nan"),)), ("i", (float("inf"),)),
])
def test_boolean_dependency_rejects_missing_or_malformed_last_report(report):
    assert boolean_dependency(report) is None


def test_requested_dependencies_are_retained_without_result_classification(clock):
    backend = WindowBackend([[
        [(GAIN, "f", (0.,)), (AUTOSET, "i", (1,)),
         ("/input/3/autoset", "i", (0,))],
    ]])
    result = read(backend)
    assert result.confirmed == [GAIN]
    assert result.mismatched == result.invalid == result.unobserved == []
    assert result.latest == {GAIN: ("f", (0.,)), AUTOSET: ("i", (1,))}
    assert backend.dumps == backend.receives == 1


def test_no_expected_targets_still_waits_for_dependencies(clock):
    backend = WindowBackend([[[], [(AUTOSET, "i", (0,))]]])
    result = read(backend, registers={})
    assert result.confirmed == result.mismatched == result.unobserved == result.invalid == []
    assert result.latest == {AUTOSET: ("i", (0,))}
    assert backend.receives == 2


def test_no_early_close_after_expected_value_before_dependency(clock):
    backend = WindowBackend([[[(GAIN, "f", (0.,))], [(AUTOSET, "i", (0,))]]])
    result = read(backend)
    assert result.latest[AUTOSET] == ("i", (0,))
    assert backend.receives == 2


def test_reportable_subset_closes_only_after_dependencies(clock):
    write_only = "/output/5/loopback"
    backend = WindowBackend([[
        [(GAIN, "f", (0.,))], [(AUTOSET, "i", (0,))],
        [AssertionError("window should have closed")],
    ]])
    result = read(backend, {GAIN: ("f", (0.,)), write_only: ("i", (1,))})
    assert result.confirmed == [GAIN]
    assert result.unobserved == [write_only]
    assert backend.receives == 2


def test_missing_dependency_does_not_become_an_unobserved_config_target(clock):
    backend = WindowBackend([[[(GAIN, "f", (0.,))]]])
    result = read(backend)
    assert result.confirmed == [GAIN]
    assert result.unobserved == result.mismatched == result.invalid == []
    assert result.latest == {GAIN: ("f", (0.,))}
    assert backend.receives > 1


@pytest.mark.parametrize("last", [
    ("f", (0.,)), ("i", (False,)), ("i", ()), ("i", (0, 0)), ("i", (2,)),
])
def test_whole_delivery_last_malformed_dependency_revokes_old_valid(clock, last):
    backend = WindowBackend([[
        [(GAIN, "f", (0.,)), (AUTOSET, "i", (0,)), (AUTOSET, *last)],
    ]])
    result = read(backend)
    assert result.latest[AUTOSET] == last
    assert boolean_dependency(result.latest[AUTOSET]) is None
    assert result.confirmed == [GAIN]
    assert result.mismatched == result.invalid == result.unobserved == []
    assert backend.receives > 1


def test_invalid_dependency_can_be_corrected_by_a_later_delivery(clock):
    backend = WindowBackend([[
        [(GAIN, "f", (0.,)), (AUTOSET, "i", (True,))],
        [(AUTOSET, "i", (0,))],
    ]])
    result = read(backend)
    assert result.latest[AUTOSET] == ("i", (0,))
    assert backend.receives == 2


@pytest.mark.parametrize("batched", [True, False])
@pytest.mark.parametrize("last", [0, 1])
def test_dependency_last_valid_change_is_retained_until_all_prerequisites(clock, batched, last):
    reports = [(GAIN, "f", (0.,)), (AUTOSET, "i", (1 - last,)),
               (AUTOSET, "i", (last,)), (PARTNER, "i", (0,))]
    batches = [reports] if batched else [[report] for report in reports]
    result = read(WindowBackend([batches]), dependencies=(AUTOSET, PARTNER))
    assert result.latest[AUTOSET] == ("i", (last,))
    assert result.latest[PARTNER] == ("i", (0,))


def test_full_delivery_contradiction_also_revokes_expected_confirmation(clock):
    backend = WindowBackend([[
        [(GAIN, "f", (0.,)), (AUTOSET, "i", (0,)), (GAIN, "f", (6.,))],
    ]])
    result = read(backend)
    assert result.confirmed == []
    assert result.mismatched == [GAIN]
    assert result.latest[GAIN] == ("f", (6.,))
    assert backend.receives > 1


def test_autoset_expected_classification_rejects_wrong_tags_and_extra_values():
    observed = Observation({AUTOSET: ("i", (1,))}, dependency_paths=(AUTOSET,))
    observed.absorb((AUTOSET, "f", (True, 99)))
    assert observed.confirmed == set()
    assert observed.invalid == observed.mismatched == {AUTOSET}
    assert observed.latest[AUTOSET] == ("f", (True, 99))
    assert not observed.dependencies_complete


def test_expected_enum_can_retain_its_appended_name():
    observed = Observation({"/output/1/reflevel": ("i", (1,))})
    observed.absorb(("/output/1/reflevel", "is", (1, "+13dBu")))
    assert observed.confirmed == {"/output/1/reflevel"}
    assert observed.invalid == observed.mismatched == set()


def test_observation_owns_expected_and_latest_argument_tuples():
    want, reported = [0.], [0]
    observed = Observation({GAIN: ("f", want)}, dependency_paths=(AUTOSET,))
    want[0] = 6.
    observed.absorb((GAIN, "f", (0.,)))
    observed.absorb((AUTOSET, "i", reported))
    reported[0] = 1
    assert observed.expected[GAIN] == ("f", (0.,))
    assert observed.confirmed == {GAIN}
    assert observed.latest[AUTOSET] == ("i", (0,))


def test_callback_cannot_mutate_borrowed_latest_arguments(clock):
    args = [0]
    backend = WindowBackend([[[(GAIN, "f", (0.,)), (AUTOSET, "i", args)]]])

    def callback(path, values):
        if path == AUTOSET:
            values[0] = 1

    result = read(backend, on_observed=callback)
    assert args == [1]
    assert result.latest[AUTOSET] == ("i", (0,))


def test_result_default_latest_is_owned_and_old_comparisons_remain_compatible(clock):
    first = verify.VerifyResult([], [], [])
    second = verify.VerifyResult([], [], [])
    first.latest[AUTOSET] = ("i", (0,))
    assert second.latest == {}
    assert first == second
    result = read(WindowBackend([[[(GAIN, "f", (0.,)), (AUTOSET, "i", (1,))]]]))
    assert result == verify.VerifyResult([GAIN], [], [])


def test_result_constructor_owns_explicit_latest_mapping_and_argument_tuples():
    args = [0]
    latest = {AUTOSET: ("i", args)}
    result = verify.VerifyResult([], [], [], latest=latest)
    args[0] = 1
    latest.clear()
    assert result.latest == {AUTOSET: ("i", (0,))}


def test_cancellation_before_request_returns_no_dependency_evidence(clock):
    backend = WindowBackend([])
    result = read(backend, should_stop=lambda: True)
    assert result.unobserved == [GAIN]
    assert result.latest == {}
    assert backend.dumps == backend.receives == 0


def test_cancellation_after_delivery_preserves_last_typed_observations(clock):
    stops = iter([False, False, True])
    backend = WindowBackend([[
        [(GAIN, "f", (0.,)), (AUTOSET, "i", (0,)), (AUTOSET, "f", (0.,))],
    ]])
    result = read(backend, should_stop=lambda: next(stops))
    assert result.latest[AUTOSET] == ("f", (0.,))
    assert boolean_dependency(result.latest[AUTOSET]) is None
    assert result.confirmed == [GAIN]
    assert backend.receives == 1


def test_new_request_does_not_inherit_a_previous_dependency(clock):
    backend = WindowBackend([
        [[(GAIN, "f", (0.,)), (AUTOSET, "i", (0,))]],
        [[(GAIN, "f", (0.,))]],
    ])
    first, second = read(backend), read(backend)
    assert first.latest[AUTOSET] == ("i", (0,))
    assert AUTOSET not in second.latest
    assert backend.dumps == 2


def test_receive_failure_propagates_and_does_not_supply_a_partial_result(clock):
    backend = WindowBackend([
        [[(GAIN, "f", (0.,)), (AUTOSET, "i", (0,))],
         [ReceivePortError(5, "dependency delivery lost")]],
        [[(GAIN, "f", (0.,)), (AUTOSET, "i", (1,)), (PARTNER, "i", (0,))]],
    ])
    with pytest.raises(ReceivePortError, match="dependency delivery lost"):
        read(backend, dependencies=(AUTOSET, PARTNER))
    result = read(backend, dependencies=(AUTOSET, PARTNER))
    assert result.latest[AUTOSET] == ("i", (1,))
    assert backend.dumps == 2


def test_request_failure_propagates_without_reading_or_returning_dependencies(clock):
    backend = WindowBackend([])

    def request():
        raise ReceivePortError(5, "refresh refused")

    backend.request_dump = request
    with pytest.raises(ReceivePortError, match="refresh refused"):
        read(backend)
    assert backend.receives == 0


@pytest.mark.parametrize("fresh", [False, True])
def test_real_control_refresh_discards_old_queue_and_excludes_derived_dependencies(
        tmp_path, fresh):
    reports = [osc.encode_osc(GAIN, "f", 0.)]
    if fresh:
        reports.append(osc.encode_osc(AUTOSET, "i", 1))
    server = ScriptedControl(tmp_path / "control.sock", reports=reports,
                             derived=[osc.encode_osc(AUTOSET, "i", 0)])
    client = Control(server.path, os.getpid(), reader=True)
    try:
        server.sequence += 1
        server.emit(OBSERVATION, code=DEVICE, sequence=server.sequence,
                    payload=osc.encode_osc(AUTOSET, "i", 0))
        client.wait(.01)
        assert client._queue  # stale DEVICE zero would falsely grant a later gain write
        result = verify.verify_routing(
            {GAIN: ("f", (0.,))}, client, timeout=.05,
            device_model=UCX2, dependency_paths=(AUTOSET,))
        assert result.confirmed == [GAIN]
        assert result.mismatched == result.unobserved == result.invalid == []
        assert result.latest.get(AUTOSET) == (("i", (1,)) if fresh else None)
        assert server.requests.count(REFRESH) == 1
        assert server.writes == []
    finally:
        client.close()
        server.close()
