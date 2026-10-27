"""FX probes preserve observed state and failure evidence without hardware."""

import errno
import importlib.util
import itertools
import json
import os
import signal
import time
from contextlib import closing, contextmanager
from types import SimpleNamespace

import pytest
from control_peer import END, WRITE, ScriptedControl
from support import repo_file, short_directory

from oscmix_desk.backend import Control, Delivery
from oscmix_desk.errors import WriteFailed
from oscmix_desk.model import Config
from oscmix_desk.numeric import float32
from oscmix_desk.osc import decode_osc, encode_osc
from oscmix_desk.protocol import Source


@pytest.fixture
def probe():
    spec = importlib.util.spec_from_file_location('fx_probe', repo_file('scripts/probe-fx.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def initial_state(pair='input:13', linked=1):
    family, first = pair.split(':')
    state = {'/input/1/48v': ('i', (0,)), '/input/2/48v': ('i', (0,)),
             '/mix/1/input/1': ('fi', (-float('inf'), -100))}
    for channel in (int(first), int(first) + 1):
        state['/%s/%d/fx' % (family, channel)] = ('f', (-65.0,))
        state['/%s/%d/stereo' % (family, channel)] = ('i', (linked,))
    return state


def inventory_for(state):
    return {'schema': 2, 'running_backend': {'matches_local_build': True},
            'registers': {path: [tags, .01] for path, (tags, _args) in state.items()}}


class DeviceModel:
    """Model device reports separately from WRITE acknowledgements/cached echoes."""

    def __init__(self, state, *, behavior='follow', reports=True, derived=False, drift=None):
        self.state = dict(state)
        self.before = dict(state)
        self.behavior = behavior
        self.reports = reports
        self.derived = derived
        self.drift = drift
        self.queue = []
        self.writes = []
        self.dumps = 0
        self.sequence = 0
        self.closed = False
        self.epoch = bytes(range(16))

    def request_dump(self):
        self.dumps += 1
        if self.closed:
            raise OSError(errno.ENOTCONN, 'peer is closed')
        if self.dumps == 3 and self.drift:
            self.state[self.drift[0]] = self.drift[1]
        for path, (tags, args) in self.state.items():
            if self.dumps == 3 and not self.reports and path.endswith('/fx'):
                continue
            self.sequence += 1
            origin = Source.DERIVED if self.dumps == 3 and self.derived else Source.DEVICE
            self.queue.append(Delivery(origin, self.sequence, self.epoch,
                                       encode_osc(path, tags, *args)))

    def next_delivery(self, timeout):
        if self.queue:
            return self.queue.pop(0)
        if self.closed:
            raise OSError(errno.ENOTCONN, 'peer is closed')
        time.sleep(min(timeout, .001))
        return None

    def send(self, messages):
        if self.closed:
            raise WriteFailed(OSError(errno.ENOTCONN, 'peer is closed'), [],
                              [path for path, _tags, _args in messages])
        for path, tags, args in messages:
            self.writes.append((path, tags, args))
            probing = len(self.writes) == 1
            if probing and self.behavior == 'ignore':
                continue
            value = 0.0 if probing and self.behavior == 'wrong' else float32(args[0])
            self.state[path] = (tags, (value,))
            family, channel = path.split('/')[1:3]
            if self.state[path.rsplit('/', 1)[0] + '/stereo'][1][0]:
                partner = int(channel) + (1 if int(channel) % 2 else -1)
                self.state['/%s/%d/fx' % (family, partner)] = (tags, (value,))
            if probing and self.behavior == 'lost-ack':
                self.closed = True
                raise WriteFailed(OSError(errno.ECONNRESET, 'lost acknowledgement'), [path], [])
            if probing and self.behavior == 'interrupt':
                signal.raise_signal(signal.SIGTERM)

    def wait(self, _seconds):
        pass

    def close(self):
        self.closed = True


def run(probe, device, *, pair='input:13', member=0, checkpoint=lambda: None, value=None):
    result = probe.plan(pair, member, value=value)
    probe.measure(device, pair, member, result, inventory_for(device.before),
                  lambda: None, checkpoint, seconds=.01, watch_seconds=.001)
    return result


@pytest.mark.parametrize('pair', ['%s:%d' % (family, first)
                                 for family in ('input', 'output')
                                 for first in range(1, 21, 2)])
@pytest.mark.parametrize('member', [0, 1])
@pytest.mark.parametrize('linked', [0, 1])
def test_probe_restores_fresh_values_on_both_partners(probe, pair, member, linked):
    state = initial_state(pair, linked)
    paths = probe.pair_paths(pair)
    family, first = pair.split(':')
    assert paths == tuple('/%s/%d/fx' % (family, int(first) + offset)
                          for offset in (0, 1))
    for path in paths:
        state[path] = ('f', (float32(-12.3),))
    device = DeviceModel(state)
    checkpoints = []
    result = run(probe, device, pair=pair, member=member,
                 checkpoint=lambda: checkpoints.append(len(device.writes)))
    assert result['checks_passed'] is True
    assert result['success'] is False  # END has not been acknowledged by the caller
    assert result['finding'] == 'confirmed'
    assert result['target'] == paths[member]
    assert result['partner_finding'] == ('followed' if linked else 'unchanged')
    assert result['proposed_db'] == -12.4
    assert device.state == state
    assert [path for path, _tags, _args in device.writes] == [paths[member], *paths]
    assert checkpoints[0] == 0
    assert result['baseline']['messages'] == state
    assert all(event['origin'] == 'DEVICE' for event in result['baseline']['events'])
    assert len({event['sequence'] for event in result['baseline']['events']}) == len(state)


@pytest.mark.parametrize(('pair', 'member', 'linked', 'value'), [
    ('input:13', 0, 1, -30.0), ('input:15', 1, 1, 0.0),
    ('output:1', 1, 0, -65.0), ('output:5', 0, 1, -.1),
])
def test_fixed_probe_level_reads_back_and_restores_each_fresh_partner(
        probe, pair, member, linked, value):
    state = initial_state(pair, linked)
    paths = probe.pair_paths(pair)
    for path in paths:
        state[path] = ('f', (float32(-12.3),))
    if not linked:
        state[paths[1 - member]] = ('f', (float32(-9.8),))
    device = DeviceModel(state)
    result = run(probe, device, pair=pair, member=member, value=value)
    assert result['checks_passed'] is True
    assert result['success'] is False  # END still needs its separate acknowledgement
    assert result['requested_db'] == result['proposed_db'] == value
    assert result['step_db'] is None
    assert result['finding'] == 'confirmed'
    assert result['partner_finding'] == ('followed' if linked else 'unchanged')
    assert device.state == state
    assert device.writes == [(paths[member], 'f', (value,)),
                             *[(path, 'f', state[path][1]) for path in paths]]
    assert result['restoration']['messages'] == result['final']['messages'] == state


@pytest.mark.parametrize(('pair', 'member'), [('input:13', 0), ('output:5', 1)])
def test_fixed_probe_already_at_requested_value_refuses_all_writes(probe, pair, member):
    device = DeviceModel(initial_state(pair))
    result = run(probe, device, pair=pair, member=member, value=-65)
    assert not device.writes
    assert device.dumps == 2
    assert 'already matches the fresh baseline' in result['error']
    assert result['checks_passed'] is False
    assert device.state == device.before


@pytest.mark.parametrize(('behavior', 'reports', 'expected'), [
    ('ignore', True, 'unchanged'), ('wrong', True, 'mismatch'), ('follow', False, 'missing'),
])
def test_fixed_level_ack_cannot_hide_missing_or_wrong_readback(
        probe, behavior, reports, expected):
    device = DeviceModel(initial_state(), behavior=behavior, reports=reports)
    result = run(probe, device, value=-30)
    assert result['finding'] == expected
    assert result['checks_passed'] is False
    assert result['restored'] is True
    assert device.state == device.before


@pytest.mark.parametrize(('dump', 'member'), [(3, 0), (3, 1), (4, 0), (4, 1)])
def test_transient_fx_deviation_fails_even_when_probe_and_restore_finish_correctly(
        probe, dump, member):
    paths = probe.pair_paths('input:13')

    class TransientDevice(DeviceModel):
        def request_dump(self):
            if self.dumps + 1 == dump:
                self.sequence += 1
                self.queue.append(Delivery(Source.DEVICE, self.sequence, self.epoch,
                                           encode_osc(paths[member], 'f', 0.0)))
            super().request_dump()

    device = TransientDevice(initial_state())
    result = run(probe, device, value=-30)
    field = 'unexpected_fx_reports' if dump == 3 else 'unexpected_restoration_fx_reports'
    assert result[field] == [paths[member]]
    assert result['finding'] == 'confirmed'
    assert result['restored'] is True
    assert device.state == device.before
    assert result['checks_passed'] is False


def test_transient_mono_partner_following_is_not_hidden_by_unchanged_final_value(probe):
    pair = 'output:1'

    class TransientPartnerDevice(DeviceModel):
        def request_dump(self):
            if self.dumps == 2:
                self.sequence += 1
                self.queue.append(Delivery(Source.DEVICE, self.sequence, self.epoch,
                                           encode_osc('/output/2/fx', 'f', -30.0)))
            super().request_dump()

    device = TransientPartnerDevice(initial_state(pair, linked=0))
    result = run(probe, device, pair=pair, value=-30)
    assert result['finding'] == 'confirmed'
    assert result['partner_finding'] == 'unchanged'
    assert result['unexpected_fx_reports'] == ['/output/2/fx']
    assert result['restored'] is True
    assert result['checks_passed'] is False


@pytest.mark.parametrize(('behavior', 'reports', 'derived', 'expected'), [
    ('ignore', True, False, 'unchanged'), ('wrong', True, False, 'mismatch'),
    ('follow', False, False, 'missing'), ('follow', True, True, 'missing'),
])
def test_ack_or_cached_echo_cannot_hide_missing_or_wrong_values(
        probe, behavior, reports, derived, expected):
    device = DeviceModel(initial_state(), behavior=behavior, reports=reports, derived=derived)
    result = run(probe, device)
    assert result['finding'] == expected
    assert result['restored'] is True
    assert result['checks_passed'] is False
    if derived:
        assert not result['probe']['messages']
        assert result['probe']['events'][0]['origin'] == 'DERIVED'


@pytest.mark.parametrize('invalid', [
    ('f', (float('nan'),)), ('f', (float('inf'),)), ('f', (-65.1,)),
    ('f', (.1,)), ('f', (-64.95,)), ('i', (-65,)), ('f', ()), ('fi', (-65.0, 0)),
])
def test_invalid_baseline_refuses_before_any_write(probe, invalid):
    state = initial_state()
    state['/input/13/fx'] = invalid
    device = DeviceModel(state)
    result = run(probe, device)
    assert not device.writes
    assert result['checks_passed'] is False
    assert result['error']
    assert result['phase'] == 'measured'


@pytest.mark.parametrize('bad_state', ['missing-partner', 'phantom-on', 'phantom-missing',
                                      'link-missing', 'divergent-links', 'divergent-values'])
def test_unsafe_or_unrestorable_baseline_refuses(probe, bad_state):
    state = initial_state()
    if bad_state == 'missing-partner':
        del state['/input/14/fx']
    elif bad_state == 'phantom-on':
        state['/input/2/48v'] = ('i', (1,))
    elif bad_state == 'phantom-missing':
        del state['/input/1/48v']
    elif bad_state == 'link-missing':
        del state['/input/14/stereo']
    elif bad_state == 'divergent-links':
        state['/input/14/stereo'] = ('i', (0,))
    else:
        state['/input/14/fx'] = ('f', (-20.0,))
    device = DeviceModel(state)
    result = run(probe, device)
    assert not device.writes
    assert result['checks_passed'] is False
    assert result['error']


@pytest.mark.parametrize(('path', 'changed'), [
    ('/mix/1/input/1', ('fi', (-float('inf'), 100))),
    ('/input/1/48v', ('i', (1,))), ('/input/13/stereo', ('i', (0,))),
])
def test_complete_messages_expose_indirect_changes_without_repairing_protected_state(
        probe, path, changed):
    device = DeviceModel(initial_state(), drift=(path, changed))
    result = run(probe, device)
    assert path in result['indirect_changes']
    assert result['restored'] is False
    assert path in result['restoration_difference']['changed']
    assert result['checks_passed'] is False
    assert all(written.endswith('/fx') for written, _tags, _args in device.writes)


def test_unexpected_partner_jump_is_not_a_success(probe):
    state = initial_state(linked=0)
    device = DeviceModel(state, drift=('/input/14/fx', ('f', (0.0,))))
    result = run(probe, device)
    assert result['finding'] == 'confirmed'
    assert result['partner_finding'] == 'unexpected'
    assert result['restored'] is True
    assert result['checks_passed'] is False


def test_missing_inventory_path_refuses_before_probe(probe):
    state = initial_state()
    device = DeviceModel(state)
    inventory = inventory_for(state)
    inventory['registers']['/input/1/reflevel'] = ['i', .01]
    result = probe.plan('input:13', 0)
    probe.measure(device, 'input:13', 0, result, inventory, lambda: None, lambda: None,
                  seconds=.01, watch_seconds=.001)
    assert not device.writes
    assert result['checks_passed'] is False
    assert 'incomplete fresh baseline' in result['error']
    assert '/input/1/reflevel' in result['error']


@pytest.mark.parametrize('partial_dump', [1, 2])
def test_one_partial_mix_dump_requires_two_separately_complete_baselines(
        probe, partial_dump):
    class PartialDevice(DeviceModel):
        def request_dump(self):
            super().request_dump()
            if self.dumps == partial_dump:
                self.queue = [delivery for delivery in self.queue
                              if decode_osc(delivery.payload)[0] != '/mix/1/input/1']

    device = PartialDevice(initial_state())
    result = run(probe, device)
    assert result['checks_passed'] is True
    assert device.state == device.before
    assert device.dumps == 6  # three baselines, probe, restoration, final
    attempts = result['baseline_attempts']
    assert len(attempts) == 3
    assert attempts[partial_dump - 1]['missing'] == ['/mix/1/input/1']
    assert attempts[partial_dump - 1]['complete'] is False
    assert '/mix/1/input/1' not in attempts[partial_dump - 1]['messages']
    assert result['baseline_first']['messages'] == result['baseline']['messages'] == device.before
    assert sum(attempt['complete'] for attempt in attempts) == 2
    assert [path for path, _tags, _args in device.writes] == [
        '/input/13/fx', '/input/13/fx', '/input/14/fx']


def test_partial_mix_dumps_cannot_be_merged_to_invent_a_complete_baseline(probe):
    class PartialDevice(DeviceModel):
        def request_dump(self):
            super().request_dump()
            missing = '/mix/1/input/1' if self.dumps % 2 else '/mix/3/input/13'
            self.queue = [delivery for delivery in self.queue
                          if decode_osc(delivery.payload)[0] != missing]

    state = initial_state()
    state['/mix/3/input/13'] = ('fi', (-float('inf'), 0))
    device = PartialDevice(state)
    result = run(probe, device)
    assert not device.writes
    assert device.dumps == 3
    assert all(not attempt['complete'] for attempt in result['baseline_attempts'])
    assert 'two complete fresh baseline windows' in result['error']
    assert result['checks_passed'] is False


@pytest.mark.parametrize('missing_dump', [1, 2])
def test_one_complete_baseline_with_two_partial_dumps_still_refuses(probe, missing_dump):
    class PartialDevice(DeviceModel):
        def request_dump(self):
            super().request_dump()
            if self.dumps != missing_dump:
                self.queue = [delivery for delivery in self.queue
                              if decode_osc(delivery.payload)[0] != '/mix/1/input/1']

    device = PartialDevice(initial_state())
    result = run(probe, device)
    assert not device.writes
    assert device.dumps == 3
    assert sum(attempt['complete'] for attempt in result['baseline_attempts']) == 1
    assert result['checks_passed'] is False


@pytest.mark.parametrize('missing', ['/input/1/48v', '/input/14/fx', '/input/14/stereo',
                                   '/input/1/reflevel'])
def test_extra_baseline_window_is_not_allowed_for_missing_other_state(probe, missing):
    class MissingDevice(DeviceModel):
        def request_dump(self):
            super().request_dump()
            self.queue = [delivery for delivery in self.queue
                          if decode_osc(delivery.payload)[0] != missing]

    state = initial_state()
    state['/input/1/reflevel'] = ('i', (0,))
    device = MissingDevice(state)
    result = run(probe, device)
    assert not device.writes
    assert device.dumps == 1
    assert result['checks_passed'] is False


def test_partial_mix_dump_does_not_defer_a_known_wrong_report_type(probe):
    class InvalidPartialDevice(DeviceModel):
        def request_dump(self):
            self.state['/input/1/reflevel'] = ('f', (0.,))
            super().request_dump()
            self.queue = [delivery for delivery in self.queue
                          if decode_osc(delivery.payload)[0] != '/mix/1/input/1']

    state = initial_state()
    state['/input/1/reflevel'] = ('i', (0,))
    device = InvalidPartialDevice(state)
    result = run(probe, device)
    assert not device.writes
    assert device.dumps == 1
    assert result['baseline_attempts'][0]['missing'] == ['/mix/1/input/1']
    assert result['baseline_attempts'][0]['wrong_tags'] == ['/input/1/reflevel']
    assert result['checks_passed'] is False


@pytest.mark.parametrize('transient', [False, True])
def test_known_drift_in_partial_baseline_is_not_hidden_by_complete_later_windows(
        probe, transient):
    class MovingPartialDevice(DeviceModel):
        def request_dump(self):
            super().request_dump()
            if self.dumps == 1:
                self.queue = [delivery for delivery in self.queue
                              if decode_osc(delivery.payload)[0] != '/mix/1/input/1']
                wrong = Delivery(Source.DEVICE, self.sequence + 1, self.epoch,
                                 encode_osc('/input/13/fx', 'f', -30.0))
                if transient:
                    self.queue.insert(0, wrong)
                else:
                    self.queue = [delivery for delivery in self.queue
                                  if decode_osc(delivery.payload)[0] != '/input/13/fx']
                    self.queue.append(wrong)

    device = MovingPartialDevice(initial_state(linked=0))
    result = run(probe, device)
    assert not device.writes
    assert 'baseline' in result['error']
    assert 'changed' in result['error']
    assert result['checks_passed'] is False
    assert len(result['baseline_attempts']) <= 3


def test_extra_baseline_window_does_not_extend_the_probe_write_budget(probe, monkeypatch):
    class PartialDevice(DeviceModel):
        def request_dump(self):
            super().request_dump()
            if self.dumps == 1:
                self.queue = [delivery for delivery in self.queue
                              if decode_osc(delivery.payload)[0] != '/mix/1/input/1']

    device = PartialDevice(initial_state())
    clock = probe.time.monotonic
    snapshot = probe.snapshot
    offset = 0

    def late_baseline(*args, **kwargs):
        nonlocal offset
        seen = snapshot(*args, **kwargs)
        if device.dumps == 3:
            offset = 36
        return seen

    monkeypatch.setattr(probe.time, 'monotonic', lambda: clock() + offset)
    monkeypatch.setattr(probe, 'snapshot', late_baseline)
    result = run(probe, device)
    assert device.dumps == 3
    assert not device.writes
    assert device.state == device.before
    assert 'measurement write budget expired' in result['error']
    assert result['checks_passed'] is False


def test_baseline_movement_between_windows_refuses_before_probe(probe):
    class MovingDevice(DeviceModel):
        def request_dump(self):
            if self.dumps == 1:
                self.state['/mix/1/input/1'] = ('fi', (-float('inf'), 100))
            super().request_dump()

    device = MovingDevice(initial_state())
    result = run(probe, device)
    assert not device.writes
    assert result['checks_passed'] is False
    assert 'baseline changed' in result['error']


def test_one_unrestored_partner_stops_without_blind_repair(probe):
    class MissingRestoreDevice(DeviceModel):
        def send(self, messages):
            super().send(messages)
            # Reproduce the observed case where only one partner returns.
            if len(self.writes) == 3:
                self.state['/input/14/fx'] = ('f', (float32(-64.9),))

    device = MissingRestoreDevice(initial_state())
    result = run(probe, device)
    assert result['finding'] == 'confirmed'
    assert result['restored'] is False
    assert result['restoration_difference']['changed'] == ['/input/14/fx']
    assert result['checks_passed'] is False
    assert len(device.writes) == 3
    assert 'watch' not in result


def test_transient_delayed_device_change_survives_a_matching_final_dump(probe, monkeypatch):
    device = DeviceModel(initial_state())
    snapshot = probe.snapshot

    def late_reports(connection, check, **options):
        if options.get('refresh') is False:
            for value in (0.0, -65.0):
                device.sequence += 1
                device.queue.append(Delivery(Source.DEVICE, device.sequence, device.epoch,
                                             encode_osc('/input/13/fx', 'f', value)))
        return snapshot(connection, check, **options)

    monkeypatch.setattr(probe, 'snapshot', late_reports)
    result = run(probe, device)
    assert result['finding'] == 'confirmed'
    assert result['restored'] is True
    assert not any(result['final_difference'].values())
    assert result['watch_changes'] == ['/input/13/fx']
    assert result['checks_passed'] is False


def test_identity_change_refuses_the_probe_and_restoration_on_a_new_device(probe):
    device = DeviceModel(initial_state())
    result = probe.plan('input:13', 0)

    def check():
        if device.writes:
            raise ValueError('measurement interface identity changed')

    probe.measure(device, 'input:13', 0, result, inventory_for(device.before), check,
                  lambda: None, seconds=.01, watch_seconds=.001)
    assert len(device.writes) == 1
    assert result['restored'] is False
    assert 'identity changed' in result['error']
    assert 'identity changed' in result['restore_error']
    assert result['checks_passed'] is False


def test_lost_ack_preserves_submission_and_never_replays_on_another_peer(probe):
    device = DeviceModel(initial_state(), behavior='lost-ack')
    result = run(probe, device)
    assert len(device.writes) == 1
    assert result['writes'][0]['submitted'] is True
    assert result['writes'][0]['acknowledged'] is False
    assert result['restored'] is False
    assert result['restore_error']
    assert result['checks_passed'] is False


def test_sigterm_restores_and_returns_failure_with_original_signal_handlers(probe, monkeypatch):
    # This checks signal cleanup, independent of scheduler/coverage overhead
    # during the tiny fake observation windows. Production deadlines stay real.
    ticks = itertools.count(0, .0001)
    monkeypatch.setattr(probe, 'time', SimpleNamespace(monotonic=lambda: next(ticks)))
    previous = {sig: signal.getsignal(sig) for sig in (signal.SIGINT, signal.SIGTERM)}
    device = DeviceModel(initial_state(), behavior='interrupt')
    result = run(probe, device)
    assert device.state == device.before
    assert result['restored'] is True
    assert result['checks_passed'] is False
    assert 'SIGTERM' in result['error']
    assert {sig: signal.getsignal(sig) for sig in previous} == previous


def test_full_evidence_disk_after_probe_does_not_prevent_same_peer_restoration(probe):
    device = DeviceModel(initial_state())

    def checkpoint():
        if device.writes:
            raise OSError(errno.ENOSPC, 'evidence disk full')

    result = run(probe, device, checkpoint=checkpoint)
    assert device.state == device.before
    assert result['restored'] is True
    assert 'evidence disk full' in result['evidence_error']
    assert result['checks_passed'] is False


@pytest.mark.parametrize('value', [None, -30.0])
def test_plan_is_offline_even_with_execute_only_paths(probe, monkeypatch, capsys, value):
    argv = ['probe-fx.py', '--pair', 'output:5',
            '--config', '/not/read', '--inventory', '/not/read']
    if value is not None:
        argv.append('--value=%s' % value)
    monkeypatch.setattr(probe.sys, 'argv', argv)
    monkeypatch.setattr(probe, 'resolve_device', lambda *_args:
                        pytest.fail('plan contacted hardware'))
    assert probe.main() == 0
    result = json.loads(capsys.readouterr().out)
    assert result['target'] == '/output/5/fx'
    assert result['hardware_used'] is False
    assert result.get('requested_db') == value


def test_default_plan_keeps_input_13_when_pair_scope_expands(probe, monkeypatch, capsys):
    monkeypatch.setattr(probe.sys, 'argv', ['probe-fx.py'])
    monkeypatch.setattr(probe, 'resolve_device', lambda *_args:
                        pytest.fail('plan contacted hardware'))
    assert probe.main() == 0
    result = json.loads(capsys.readouterr().out)
    assert result['pair'] == 'input:13'
    assert result['target'] == '/input/13/fx'
    assert result['hardware_used'] is False


@pytest.mark.parametrize('pair', ['input:1', 'input:19', 'output:1', 'output:19'])
@pytest.mark.parametrize('member', ['first', 'second'])
def test_boundary_pair_plans_select_the_requested_member_offline(
        probe, monkeypatch, capsys, pair, member):
    monkeypatch.setattr(probe.sys, 'argv', ['probe-fx.py', '--pair', pair,
                                          '--member', member])
    monkeypatch.setattr(probe, 'resolve_device', lambda *_args:
                        pytest.fail('plan contacted hardware'))
    assert probe.main() == 0
    result = json.loads(capsys.readouterr().out)
    family, first = pair.split(':')
    assert result['target'] == '/%s/%d/fx' % (family, int(first) + (member == 'second'))
    assert result['hardware_used'] is False


@pytest.mark.parametrize('pair', ['input:0', 'input:2', 'input:21', 'output:20',
                                'playback:1', 'INPUT:1', 'input:01', 'input:x'])
def test_invalid_pair_refused_before_inventory_or_evidence_access(
        probe, monkeypatch, tmp_path, pair):
    out = tmp_path / 'must-not-create.json'
    monkeypatch.setattr(probe.sys, 'argv', ['probe-fx.py', '--execute', '--pair', pair,
                                          '--inventory', '/must/not/read', '--out', str(out)])
    monkeypatch.setattr(probe, 'resolve_device', lambda *_args:
                        pytest.fail('invalid pair reached hardware'))
    with pytest.raises(SystemExit) as error:
        probe.main()
    assert error.value.code == 2
    assert not out.exists()


@pytest.mark.parametrize('value', ['nan', 'inf', '-inf', '-65.1', '0.1', '-64.95',
                                 '-1e308', '1e308', '-65.00001', '0.00001'])
def test_invalid_fixed_level_refused_before_inventory_or_evidence_access(
        probe, monkeypatch, tmp_path, value):
    out = tmp_path / 'must-not-create.json'
    monkeypatch.setattr(probe.sys, 'argv', ['probe-fx.py', '--execute', '--value=' + value,
                                          '--inventory', '/must/not/read', '--out', str(out)])
    monkeypatch.setattr(probe, 'resolve_device', lambda *_args:
                        pytest.fail('invalid level reached hardware'))
    with pytest.raises(SystemExit) as error:
        probe.main()
    assert error.value.code == 2
    assert not out.exists()


def test_execute_refuses_old_inventory_before_device_access(probe, monkeypatch, tmp_path):
    recorded = tmp_path / 'old.json'
    recorded.write_text(json.dumps({'registers': {'/input/13/fx': ['f', .01]}}))
    monkeypatch.setattr(probe.sys, 'argv', ['probe-fx.py', '--execute',
                                          '--inventory', str(recorded),
                                          '--out', str(tmp_path / 'evidence.json')])
    monkeypatch.setattr(probe, 'resolve_device', lambda *_args:
                        pytest.fail('old inventory reached hardware'))
    assert probe.main() == 1
    assert not (tmp_path / 'evidence.json').exists()


def test_execute_does_not_overwrite_private_evidence(probe, monkeypatch, tmp_path):
    recorded = tmp_path / 'inventory.json'
    recorded.write_text(json.dumps(inventory_for(initial_state())))
    out = tmp_path / 'evidence.json'
    out.write_text('preserve this evidence\n')
    monkeypatch.setattr(probe.sys, 'argv', ['probe-fx.py', '--execute',
                                          '--inventory', str(recorded),
                                          '--out', str(out)])
    monkeypatch.setattr(probe, 'resolve_device', lambda *_args:
                        pytest.fail('existing evidence reached hardware'))
    assert probe.main() == 1
    assert out.read_text() == 'preserve this evidence\n'


def test_failed_evidence_update_preserves_the_complete_baseline(probe, monkeypatch, tmp_path):
    out = tmp_path / 'evidence.json'
    before = {'phase': 'baseline', 'messages': probe.json_safe(initial_state())}
    probe.save_evidence(out, before)

    def failed_flush(_fd):
        raise OSError(errno.ENOSPC, 'evidence disk full')

    monkeypatch.setattr(probe.os, 'fsync', failed_flush)
    with pytest.raises(OSError, match='evidence disk full'):
        probe.save_evidence(out, {'phase': 'probe', 'messages': {}})
    assert json.loads(out.read_text()) == before
    assert list(tmp_path.iterdir()) == [out]


def test_refresh_with_only_derived_reports_has_no_device_baseline(probe):
    path = short_directory() / 'control'
    server = ScriptedControl(path, derived=[encode_osc('/input/13/fx', 'f', -65)])
    try:
        with closing(Control(path, os.getpid())) as connection:
            connection.begin()
            seen = probe.snapshot(connection, lambda: None, seconds=.03)
            assert not seen
            assert seen.events[0]['origin'] == 'DERIVED'
            connection.finish()
    finally:
        server.close()
    assert WRITE not in server.requests
    assert END in server.requests


def test_lost_write_ack_closes_real_control_instead_of_replaying(probe):
    path = short_directory() / 'control'
    state = initial_state()
    reports = [encode_osc(path, tags, *args) for path, (tags, args) in state.items()]
    server = ScriptedControl(path, reports=reports, lose_reply=WRITE)
    try:
        with closing(Control(path, os.getpid())) as connection:
            connection.begin()
            result = probe.plan('input:13', 0)
            probe.measure(connection, 'input:13', 0, result, inventory_for(state),
                          lambda: None, lambda: None, seconds=.03, watch_seconds=.001)
            assert result['writes'][0]['submitted'] is True
            assert result['restored'] is False
            assert result['checks_passed'] is False
    finally:
        server.close()
    assert server.requests.count(WRITE) == 1
    assert END not in server.requests
    assert decode_osc(server.writes[0])[0] == '/input/13/fx'


@pytest.mark.parametrize('end_fails', [False, True])
@pytest.mark.parametrize('value', [None, -30.0])
def test_main_requires_end_ack_and_keeps_actual_state_evidence_private(
        probe, monkeypatch, tmp_path, end_fails, value):
    state = initial_state()
    device = DeviceModel(state)
    peer = SimpleNamespace(pid=123, epoch=bytes(range(16)))
    evidence = {'pid': 123, 'epoch': peer.epoch.hex(), 'sha256': 'a' * 64,
                'series_sha256': 'b' * 64, 'matches_local_build': True}
    inventory = inventory_for(state)
    inventory.update(serial='24216011', running_backend=evidence)
    recorded = tmp_path / 'inventory.json'
    recorded.write_text(json.dumps(inventory))
    out = tmp_path / 'evidence.json'
    config_path = tmp_path / 'routing.conf'
    config_path.write_text('[device]\nserial = 24216011\n')
    argv = ['probe-fx.py', '--execute', '--inventory', str(recorded), '--out', str(out),
            '--config', str(config_path)]
    if value is not None:
        argv.append('--value=%s' % value)
    monkeypatch.setattr(probe.sys, 'argv', argv)
    interface = SimpleNamespace(serial='24216011', client=24)
    monkeypatch.setattr(probe, 'resolve_device', lambda *_args: interface)
    monkeypatch.setattr(probe, 'read_playback', lambda *_args: SimpleNamespace(mode=None))
    monkeypatch.setattr(probe, 'build_evidence', lambda *_args: evidence)

    @contextmanager
    def desk(config, path):
        assert isinstance(config, Config)
        assert config.serial == interface.serial
        assert path == config_path
        assert not device.writes
        try:
            yield SimpleNamespace(_control=peer, _begin=lambda: device)
            if end_fails:
                raise OSError(errno.ECONNRESET, 'END acknowledgement lost')
        finally:
            device.close()

    monkeypatch.setattr(probe, 'open_desk', desk)
    measure = probe.measure

    def short_measure(*args):
        return measure(*args, seconds=.01, watch_seconds=.001)

    monkeypatch.setattr(probe, 'measure', short_measure)
    assert probe.main() == int(end_fails)
    saved = json.loads(out.read_text())
    assert saved['success'] is not end_fails
    assert saved['restored'] is True
    if value is not None:
        assert saved['requested_db'] == saved['proposed_db'] == value
        assert device.writes[0][2] == (value,)
    assert saved['end_acknowledged'] is not end_fails
    assert device.state == state
    assert saved['baseline']['messages']['/mix/1/input/1'] == ['fi', ['-inf', -100]]
    assert out.stat().st_mode & 0o777 == 0o600
    assert device.closed is True
