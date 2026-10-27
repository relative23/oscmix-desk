"""A result cannot claim mutually incompatible facts or change after return."""

import json
from dataclasses import FrozenInstanceError, replace

import pytest

from oscmix_desk import Outcome, OutcomeState
from oscmix_desk.outcome import (
    APPLIED_MISMATCHED,
    APPLIED_UNVERIFIED,
    APPLIED_VERIFIED,
    NOT_CHECKED,
    REFUSED,
    WRITTEN_IN_PART,
)


@pytest.mark.parametrize(('member', 'value'), [
    (APPLIED_VERIFIED, 'applied-verified'),
    (APPLIED_UNVERIFIED, 'applied-unverified'),
    (APPLIED_MISMATCHED, 'applied-mismatched'),
    (REFUSED, 'refused'),
    (WRITTEN_IN_PART, 'written-in-part'),
])
def test_states_keep_their_string_and_serialization_contract(member, value):
    assert isinstance(member, OutcomeState)
    assert member == value
    assert str(member) == value
    assert json.loads(json.dumps(member)) == value
    assert OutcomeState(value) is member


def test_a_result_owns_immutable_paths_even_when_the_caller_supplies_lists():
    fields = dict(unverified=['/mix/1/playback/1', '/output/1/volume'],
                  unverifiable=['/mix/1/playback/1'], unobserved=['/mix/1/playback/1'],
                  mismatched=['/output/1/volume'], invalid=['/output/1/volume'])
    expected = {name: tuple(paths) for name, paths in fields.items()}
    result = Outcome(APPLIED_MISMATCHED, 'tracking', read_back=True, **fields)
    copied = replace(result, persisted=True)
    for paths in fields.values():
        paths.clear()
    for name, paths in expected.items():
        assert getattr(result, name) == getattr(copied, name) == paths
    with pytest.raises(FrozenInstanceError):
        result.unverified += ('/output/2/volume',)


@pytest.mark.parametrize(('fields', 'why'), [
    (dict(state=REFUSED), 'needs a reason'),
    (dict(state=REFUSED, reason='absent', persisted=True), 'cannot be read back or persisted'),
    (dict(state=REFUSED, reason='absent', read_back=True), 'cannot be read back or persisted'),
    (dict(state=REFUSED, reason='absent', reload_failed=True), 'persisted or partial'),
    (dict(state=REFUSED, reason='absent', unverified=('/input/1/mute',)), 'no unverified writes'),
    (dict(state=REFUSED, reason='absent', written=('/input/1/mute',)), 'only to WRITTEN_IN_PART'),
    (dict(state=APPLIED_VERIFIED), 'complete, matching read-back'),
    (dict(state=APPLIED_VERIFIED, read_back=True,
          unverified=('/input/1/mute',)), 'complete, matching read-back'),
    (dict(state=APPLIED_VERIFIED, read_back=True,
          unwritten=('/input/1/mute',)), 'only to WRITTEN_IN_PART'),
    (dict(state=APPLIED_UNVERIFIED, read_back=True), 'missing or differing paths'),
    (dict(state=APPLIED_UNVERIFIED), 'needs a reason'),
    (dict(state=APPLIED_UNVERIFIED, reason=NOT_CHECKED, read_back=True), 'not requested'),
    (dict(state=APPLIED_VERIFIED, reason=NOT_CHECKED, read_back=True), 'not requested'),
    (dict(state=APPLIED_UNVERIFIED, read_back=True, unverified=('/input/1/mute',),
          unverifiable=('/input/2/mute',)), 'subset'),
    (dict(state=APPLIED_UNVERIFIED, reason='disconnected', unverified=('/input/1/mute',),
          unverifiable=('/input/1/mute',)), 'incomplete read-back'),
    (dict(state=APPLIED_UNVERIFIED, reason=NOT_CHECKED, durable=True), 'must be in effect'),
    (dict(state=APPLIED_UNVERIFIED, reason=NOT_CHECKED, reload_failed=True),
     'persisted or partial'),
    (dict(state=APPLIED_MISMATCHED), 'completed, differing read-back'),
    (dict(state=APPLIED_MISMATCHED, read_back=True), 'completed, differing read-back'),
    (dict(state=APPLIED_UNVERIFIED, reason=NOT_CHECKED,
          mismatched=('/input/1/mute',)), 'incomplete read-back'),
    (dict(state=APPLIED_UNVERIFIED, read_back=True, unverified=('/input/1/mute',),
          mismatched=('/input/1/mute',)), 'known mismatches require'),
    (dict(state=APPLIED_MISMATCHED, read_back=True, mismatched=('/input/1/mute',),
          invalid=('/input/2/mute',)), 'invalid must be a subset'),
    (dict(state=APPLIED_MISMATCHED, read_back=True, mismatched=('/input/1/mute',),
          unobserved=('/input/1/mute',)), 'must be disjoint'),
    (dict(state=APPLIED_MISMATCHED, read_back=True, mismatched=('/input/1/mute',)),
     'include exactly mismatched and unobserved'),
    (dict(state=APPLIED_MISMATCHED, read_back=True, mismatched=('/input/1/mute',),
          unverified=('/input/1/mute',), unverifiable=('/input/1/mute',)),
     'unverifiable must be a subset of unobserved'),
    (dict(state=APPLIED_UNVERIFIED, read_back=True, unverified=('/input/1/mute',)),
     'include exactly mismatched and unobserved'),
    (dict(state=WRITTEN_IN_PART, reason='disconnected'), 'must have submitted a write'),
    (dict(state=WRITTEN_IN_PART, written=('/input/1/mute',),
          unverified=('/input/1/mute',)), 'needs a reason'),
    (dict(state=WRITTEN_IN_PART, reason='disconnected', written=('/input/1/mute',)),
     'every submitted or pending path'),
    (dict(state=WRITTEN_IN_PART, reason='disconnected', written=('/input/1/mute',),
          unwritten=('/input/2/mute',), unverified=('/input/1/mute',)),
     'every submitted or pending path'),
    (dict(state=WRITTEN_IN_PART, reason='disconnected', written=('/input/1/mute',),
          unverified=('/input/1/mute',), persisted=True), 'cannot be read back or persisted'),
    (dict(state=WRITTEN_IN_PART, reason='disconnected', written=('/input/1/mute',),
          unverified=('/input/1/mute',), read_back=True), 'cannot be read back or persisted'),
])
def test_contradictory_results_are_rejected(fields, why):
    with pytest.raises(ValueError, match=why):
        Outcome(name='tracking', **fields)


@pytest.mark.parametrize(('fields', 'why'), [
    (dict(state='refused', reason='absent'), 'OutcomeState'),
    (dict(state='invented'), 'OutcomeState'),
    (dict(state=APPLIED_UNVERIFIED, reason=NOT_CHECKED, persisted=1), 'must be bools'),
    (dict(state=APPLIED_UNVERIFIED, reason=NOT_CHECKED, reload_failed=1), 'must be bools'),
    (dict(state=APPLIED_UNVERIFIED, reason=NOT_CHECKED, unverified='/input/1/mute'), 'paths'),
    (dict(state=APPLIED_UNVERIFIED, reason=NOT_CHECKED, unverified=(1,)), 'paths'),
    (dict(state=APPLIED_UNVERIFIED, reason=NOT_CHECKED, mismatched='/input/1/mute'), 'paths'),
    (dict(state=APPLIED_UNVERIFIED, reason=NOT_CHECKED, unobserved=(1,)), 'paths'),
    (dict(state=APPLIED_UNVERIFIED, reason=NOT_CHECKED, invalid=(1,)), 'paths'),
])
def test_mistyped_results_are_rejected(fields, why):
    with pytest.raises(TypeError, match=why):
        Outcome(name='tracking', **fields)


def test_replace_checks_the_new_combination():
    result = Outcome(APPLIED_VERIFIED, 'tracking', read_back=True)
    with pytest.raises(ValueError, match='complete, matching read-back'):
        replace(result, read_back=False)
    differing = Outcome(APPLIED_MISMATCHED, 'tracking', read_back=True,
                        mismatched=('/output/5/volume',), unverified=('/output/5/volume',))
    with pytest.raises(ValueError, match='known mismatches require'):
        replace(differing, state=APPLIED_UNVERIFIED)


def test_an_unchecked_empty_desk_and_a_lost_last_ack_are_representable():
    unchecked = Outcome(APPLIED_UNVERIFIED, 'empty', reason=NOT_CHECKED)
    assert unchecked.applied
    assert not unchecked.read_back
    partial = Outcome(WRITTEN_IN_PART, 'tracking', reason='ack lost',
                      written=('/input/1/mute',), unverified=('/input/1/mute',))
    assert partial.applied
    assert partial.unwritten == ()


def test_a_refusal_does_not_claim_a_change_to_the_remembered_desk():
    result = Outcome(REFUSED, 'tracking', reason='backend absent')
    assert not result.applied
    assert not result.persisted
    assert not result.read_back
    assert result.describe() == "refused 'tracking', nothing written: backend absent"


@pytest.mark.parametrize('partial', [False, True])
def test_a_reload_failure_preserves_device_and_marker_facts(partial):
    result = (Outcome(WRITTEN_IN_PART, 'tracking', reason='ack lost',
                      written=('/input/1/mute',), unverified=('/input/1/mute',))
              if partial else Outcome(APPLIED_VERIFIED, 'tracking', read_back=True,
                                      persisted=True, durable=True))
    failed = replace(result, reload_failed=True)
    assert failed.state is result.state
    assert failed.applied
    assert failed.persisted is result.persisted
    assert failed.read_back is result.read_back
    assert failed.describe().startswith(result.describe())
    assert 'service refused the reload' in failed.describe()
