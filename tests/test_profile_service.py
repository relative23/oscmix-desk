"""Python selections obey the activation fence and transfer service ownership."""

import pytest
from backend_doubles import RecordingBackend
from profile_desk import desk

from oscmix_desk import (
    active_profile,
    hostservice,
    load_config,
    profiles,
    restore_main,
    switch_profile,
)
from oscmix_desk.discovery import lock_key
from oscmix_desk.errors import WriteFailed
from oscmix_desk.locking import take_device_lock
from oscmix_desk.outcome import APPLIED_UNVERIFIED, REFUSED, WRITTEN_IN_PART
from oscmix_desk.process import RELOAD_DONE, RELOAD_FAILED, RELOAD_NOT_RUNNING

LEVELS = '[input:1]\ngain = 2\n[input:2]\ngain = 4\n'


def manual_gain_reports(_sent):
    """These service tests select independent gains with AutoSet freshly off."""
    return [('/input/1/autoset', 'i', (0,)), ('/input/2/autoset', 'i', (0,)),
            ('/input/1/stereo', 'i', (0,))]


def _select(selection, path, backend):
    if selection == 'switch':
        return switch_profile('tracking', path, backend=backend, verify=False)
    return restore_main(path, backend=backend, verify=False)


@pytest.mark.parametrize('selection', ['switch', 'restore'])
@pytest.mark.parametrize('fence', [name for _, name in hostservice.MAINTENANCE_FILES])
def test_python_selection_refuses_maintenance_before_lock_connection_or_writes(
        tmp_path, monkeypatch, selection, fence):
    path = desk(tmp_path, main=LEVELS, tracking=LEVELS)
    marker = tmp_path / 'active-profile'
    marker.write_text('previous\n')
    hostservice.STATE.mkdir()
    (hostservice.STATE / fence).write_text('incomplete\n')
    backend = RecordingBackend()
    monkeypatch.setattr(profiles, '_operation',
                        lambda *a, **k: pytest.fail('must not open the device'))
    monkeypatch.setattr(profiles, 'reload_service', lambda: pytest.fail('must not reload'))

    result = _select(selection, path, backend)

    assert result.state is REFUSED
    assert 'installation maintenance incomplete' in result.reason
    assert not result.reload_failed
    assert backend.sent == backend.operations == []
    assert marker.read_text() == 'previous\n'


@pytest.mark.parametrize('selection', ['switch', 'restore'])
@pytest.mark.parametrize('service', ['same', 'other', 'stopped', 'failed'])
def test_python_selection_hands_over_only_after_commit_close_and_unlock(
        tmp_path, monkeypatch, selection, service):
    path = desk(tmp_path, main=LEVELS, tracking=LEVELS)
    config = load_config(path)
    backend, reloads = RecordingBackend(manual_gain_reports), []
    unit_path = tmp_path / 'other.conf' if service == 'other' else path
    monkeypatch.setattr(profiles, '_unit_desk', lambda: (True, unit_path))

    def reload():
        assert backend.operations == ['begin', 'finish', 'close']
        assert active_profile(path) == ('tracking' if selection == 'switch' else None)
        lock = take_device_lock(path, lock_key(config.usb_id, config.serial), wait=0)
        assert lock is not None, 'service reconcile must be able to take the writer lock'
        lock.release()
        reloads.append(True)
        return {'same': RELOAD_DONE, 'stopped': RELOAD_NOT_RUNNING,
                'failed': RELOAD_FAILED}[service]

    monkeypatch.setattr(profiles, 'reload_service', reload)
    result = _select(selection, path, backend)

    assert result.state is APPLIED_UNVERIFIED
    assert result.applied
    assert result.persisted
    assert result.durable
    assert result.reload_failed is (service == 'failed')
    assert reloads == ([] if service == 'other' else [True])
    assert backend.sent == [('/input/1/gain', 'f', (2.,)), ('/input/2/gain', 'f', (4.,))]


@pytest.mark.parametrize('selection', ['switch', 'restore'])
@pytest.mark.parametrize('reload_failed', [False, True])
def test_partial_python_selection_keeps_previous_marker_and_reports_reload_failure(
        tmp_path, monkeypatch, selection, reload_failed):
    path = desk(tmp_path, main=LEVELS, tracking=LEVELS)
    marker = tmp_path / 'active-profile'
    marker.write_text('previous\n')
    reloads = []

    class PartialBackend(RecordingBackend):
        def send(self, messages):
            first, *rest = list(messages)
            self.sent.append(first)
            raise WriteFailed(OSError('disconnected'), [first[0]], [m[0] for m in rest])

    backend = PartialBackend(manual_gain_reports)
    monkeypatch.setattr(profiles, '_unit_desk', lambda: (True, path))

    def reload():
        assert backend.operations == ['begin', 'close']
        assert marker.read_text() == 'previous\n'
        reloads.append(True)
        return RELOAD_FAILED if reload_failed else RELOAD_DONE

    monkeypatch.setattr(profiles, 'reload_service', reload)
    result = _select(selection, path, backend)

    assert result.state is WRITTEN_IN_PART
    assert result.written == ('/input/1/gain',)
    assert result.unwritten == ('/input/2/gain',)
    assert not result.persisted
    assert not result.read_back
    assert result.reload_failed is reload_failed
    assert reloads == [True]
