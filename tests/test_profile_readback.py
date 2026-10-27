"""A submitted profile can differ, lack feedback, or fail to finish checking."""

import pytest
from backend_doubles import RecordingBackend, echo_link_flags_only
from profile_desk import desk

from oscmix_desk import (
    APPLIED_MISMATCHED,
    APPLIED_UNVERIFIED,
    APPLIED_VERIFIED,
    active_profile,
    restore_main,
    switch_profile,
)
from oscmix_desk import desk as desk_mod
from oscmix_desk.backend import OSCMIX

LEVELS = '[output:5]\nvolume = -6\n[output:7]\nvolume = -6\n'
BLIND_ROUTE = '[route:main]\noutput = 1\nplayback = 1\nlevel = -6\n'


@pytest.mark.parametrize('selection', ['switch', 'restore'])
@pytest.mark.parametrize('feedback', ['correct', 'different', 'missing', 'invalid'])
@pytest.mark.parametrize('blind_route', [False, True])
def test_public_selection_preserves_distinct_feedback_and_intended_marker(
        tmp_path, monkeypatch, selection, feedback, blind_route):
    text = LEVELS + (BLIND_ROUTE if blind_route else '')
    path = desk(tmp_path, main=text, tracking=text)
    reports = [('/output/7/volume', 'f', (-6.,))]
    if feedback != 'missing':
        value = {'correct': -6., 'different': -30., 'invalid': float('nan')}[feedback]
        reports.append(('/output/5/volume', 'f', (value,)))
    backend = RecordingBackend(lambda sent: echo_link_flags_only(sent) + reports)
    backend.traits = OSCMIX
    monkeypatch.setattr(desk_mod, 'VERIFY_TIMEOUT', .003)
    (tmp_path / 'active-profile').write_text('previous\n')

    result = (switch_profile('tracking', path, backend=backend) if selection == 'switch'
              else restore_main(path, backend=backend))

    differing = feedback in ('different', 'invalid')
    missing = ['/mix/1/playback/1'] if blind_route else []
    if feedback == 'missing':
        missing.append('/output/5/volume')
    expected_state = (APPLIED_MISMATCHED if differing else
                      APPLIED_UNVERIFIED if missing else APPLIED_VERIFIED)
    assert result.state is expected_state
    assert result.mismatched == (('/output/5/volume',) if differing else ())
    assert result.unobserved == tuple(missing)
    assert result.invalid == (('/output/5/volume',) if feedback == 'invalid' else ())
    assert result.unverifiable == (('/mix/1/playback/1',) if blind_route else ())
    assert result.unverified == tuple(sorted(result.mismatched + result.unobserved))
    assert result.applied
    assert result.read_back
    assert result.persisted
    assert result.durable
    assert active_profile(path) == ('tracking' if selection == 'switch' else None)
    assert backend.operations == ['begin', 'finish', 'close']
    if differing:
        assert 'read-back mismatched' in result.describe()
        assert '/output/5/volume' in result.describe()
    if feedback == 'invalid':
        assert 'invalid reports' in result.describe()
    if differing and blind_route:
        assert '1 not observed' in result.describe()
        assert '1 this backend cannot report' in result.describe()


@pytest.mark.parametrize('failure', ['read', 'finish', 'close'])
def test_failed_observation_or_completion_discards_earlier_mismatch_classification(
        tmp_path, monkeypatch, failure):
    path = desk(tmp_path, main=LEVELS, tracking=LEVELS)
    marker = tmp_path / 'active-profile'
    marker.write_text('previous\n')
    monkeypatch.setattr(desk_mod, 'VERIFY_TIMEOUT', .003)

    class FailedCompletion(RecordingBackend):
        def messages(self, timeout):
            if failure == 'read':
                yield '/output/5/volume', 'f', (-30.,)
                raise OSError('read failed after a differing report')
            yield from super().messages(timeout)

        def finish(self):
            super().finish()
            if failure == 'finish':
                raise OSError('END not acknowledged')

        def close(self):
            super().close()
            if failure == 'close':
                raise OSError('close failed')

    backend = FailedCompletion(lambda _: [('/output/5/volume', 'f', (-30.,)),
                                         ('/output/7/volume', 'f', (-6.,))])
    backend.traits = OSCMIX
    result = switch_profile('tracking', path, backend=backend)

    assert result.state is APPLIED_UNVERIFIED
    assert result.applied
    assert not result.read_back
    assert result.mismatched == result.unobserved == result.invalid == result.unverifiable == ()
    assert result.unverified == ('/output/5/volume', '/output/7/volume')
    assert result.persisted is (failure == 'read')
    assert marker.read_text() == ('tracking\n' if failure == 'read' else 'previous\n')
