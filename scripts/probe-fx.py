#!/usr/bin/env python3
"""Prepare one FX probe; --execute changes live mixer state.

Execution requires a fresh physical safety confirmation outside this tool:
UCX on, monitors off, headphones disconnected, phantom power off. First
record a current inventory with record-dump.py. Each invocation changes
one member of one pair by 0.1 dB, or to an explicit --value in dB, and
restores both partners under the same checked peer and lease. It never
changes links, reference levels or 48 V.

Private evidence contains actual mixer values. DEVICE reports establish
their decoding origin, not an atomic snapshot or acoustic safety. Missing
reports, wrong or transient values, indirect changes and failed restoration remain
distinct failures. A lost peer is never reconnected for restoration.
"""

import argparse
import contextlib
import hashlib
import json
import math
import os
import signal
import sys
import tempfile
import time
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from measurement import build_evidence, observations

from oscmix_desk import open_desk
from oscmix_desk.config import load_config
from oscmix_desk.discovery import resolve_device
from oscmix_desk.errors import ConfigError, DeviceLockUnavailable, WriteFailed
from oscmix_desk.numeric import finite, float32
from oscmix_desk.paths import discover_config_path
from oscmix_desk.protocol import Source
from oscmix_desk.streams import playback_problem, read_playback

PAIRS = tuple('%s:%d' % (family, first)
              for family in ('input', 'output') for first in range(1, 21, 2))
READ_SECONDS = 6.0
WATCH_SECONDS = 24.0
BASELINE_REFRESH_LIMIT = 3


def volatile(path):
    return path.endswith(('/level', '/meter'))


def fx_units(message):
    """Tenths of a dB, accepting only a valid complete Float32 report."""
    tags, args = message
    if tags != 'f' or len(args) != 1 or isinstance(args[0], bool):
        raise ValueError('FX value needs one float argument')
    value = finite(args[0])
    units = round(value * 10)
    if not -650 <= units <= 0 or not math.isclose(value, units / 10,
                                               rel_tol=2e-7, abs_tol=1e-5):
        raise ValueError('FX value is outside the -65..0 dB / 0.1 dB encoding')
    return units


def requested_units(value):
    """Validate a requested level before opening any measurement transport."""
    if isinstance(value, bool):
        raise TypeError('requested FX level must be a number')
    level = finite(value)
    if not -65 <= level <= 0:
        raise ValueError('requested FX level must be between -65 and 0 dB')
    return fx_units(('f', (level,)))


def probe_units(original, value):
    """Choose a different level from this operation's fresh FX baseline."""
    if value is None:
        proposed = original - 1 if original > -650 else -649
    else:
        proposed = requested_units(value)
    if proposed == original:
        raise ValueError('requested FX level already matches the fresh baseline')
    return proposed


def pair_paths(pair):
    if pair not in PAIRS:
        raise ValueError('FX pair must start on an odd input/output channel from 1 to 19')
    family, first = pair.split(':')
    return tuple('/%s/%d/fx' % (family, int(first) + offset) for offset in (0, 1))


def plan(pair, member, *, value=None):
    paths = pair_paths(pair)
    if member not in (0, 1):
        raise ValueError('member must be first or second')
    result = {'schema': 1, 'kind': 'fx-probe', 'pair': pair, 'target': paths[member],
            'partners': paths, 'step_db': .1, 'watch_seconds': WATCH_SECONDS,
            'hardware_used': False, 'phase': 'planned', 'end_acknowledged': False,
            'checks_passed': False, 'success': False,
            'baseline_refresh_limit': BASELINE_REFRESH_LIMIT,
            'note': [
                'Planning opens no device, socket or write lease.',
                'Execution requires fresh physical safety confirmation and fresh inventory.',
                'Only DEVICE-origin values from this operation are restoration values.',
                'Two separately complete stable baselines are required within three refreshes.',
                'Partial mix windows are retained and never merged into a complete snapshot.',
                'DEVICE origin is not an atomic snapshot or proof of acoustic safety.',
                'No reconnect, automatic replay, link, 48v or reference-level writes.',
                'Observed values do not establish the whole domain or all channels.',
            ]}
    if value is not None:
        result['requested_db'] = requested_units(value) / 10
        result['step_db'] = None
    return result


class Snapshot(dict):
    def __init__(self):
        super().__init__()
        self.events = []


def snapshot(connection, check, *, refresh=True, seconds=READ_SECONDS):
    check()
    if refresh:
        connection.request_dump()
    captured = Snapshot()
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        for path, tags, args, origin, sequence in observations(
                connection, min(.25, max(0, deadline - time.monotonic()))):
            if volatile(path):
                continue
            captured.events.append({'path': path, 'tags': tags, 'args': args,
                                    'origin': origin.name, 'sequence': sequence})
            if origin == Source.DEVICE:
                captured[path] = (tags, args)
    check()
    return captured


def compare(before, after):
    """Keep absent reports separate from known changed complete messages."""
    return {'missing': sorted(set(before) - set(after)),
            'changed': sorted(path for path in before.keys() & after.keys()
                              if before[path] != after[path]),
            'new': sorted(set(after) - set(before))}


def valid_inventory(inventory):
    paths = inventory.get('registers')
    if (inventory.get('schema') != 2 or not isinstance(paths, dict) or not paths
            or not inventory.get('running_backend', {}).get('matches_local_build')):
        raise ValueError('execution requires a current schema-2 record-dump inventory')
    for path, message in paths.items():
        if (not isinstance(path, str) or not path.startswith('/')
                or not isinstance(message, list) or len(message) != 2
                or not isinstance(message[0], str)):
            raise ValueError('invalid inventory path/type record')


def inventory_gaps(captured, inventory):
    missing = []
    wrong_tags = []
    for path, message in inventory['registers'].items():
        if volatile(path):
            continue
        if path not in captured:
            missing.append(path)
        elif captured[path][0] != message[0]:
            wrong_tags.append(path)
    return sorted(missing), sorted(wrong_tags)


def require_complete(captured, inventory):
    missing, wrong_tags = inventory_gaps(captured, inventory)
    if missing or wrong_tags:
        raise ValueError('incomplete fresh baseline: missing=%r wrong_tags=%r'
                         % (missing, wrong_tags))


def require_stable(first, before):
    stability = compare(first, before)
    if any(stability.values()):
        raise ValueError('baseline changed between fresh refresh windows')
    return stability


def require_inventory_peer(inventory, evidence, serial):
    old = inventory['running_backend']
    if (inventory.get('serial') != serial
            or any(old.get(key) != evidence[key]
                   for key in ('pid', 'epoch', 'sha256', 'series_sha256'))):
        raise ValueError('inventory belongs to a different peer, build or device')


def measurement_config(path):
    path = path or discover_config_path()
    if path is None:
        raise ConfigError('measurement requires a main config')
    return load_config(path, strict=True), path


class IdentityGuard:
    def __init__(self, config, proc):
        self.proc = proc
        self.interface = resolve_device(config.usb_id, config.device_name, config.serial, proc)
        if not self.interface.serial or self.interface.client is None:
            raise ValueError('measurement interface identity is not available')
        self.config = replace(config, serial=self.interface.serial)
        self.playback = read_playback(self.config, proc)
        self()

    def __call__(self):
        config = self.config
        current = resolve_device(config.usb_id, config.device_name, config.serial, self.proc)
        if current != self.interface:
            raise ValueError('measurement interface identity changed')
        playback = read_playback(config, self.proc)
        if playback != self.playback:
            raise ValueError('USB playback mode changed during measurement')
        if playback.mode is not None:
            problem = playback_problem(config, playback.mode)
            if problem:
                raise ValueError(problem)


def baseline_values(before, paths):
    for channel in (1, 2):
        if before.get('/input/%d/48v' % channel) != ('i', (0,)):
            raise ValueError('both phantom-power reports must confirm 48 V off')
    units = []
    links = []
    for path in paths:
        if path not in before:
            raise ValueError('fresh FX partner report missing: ' + path)
        units.append(fx_units(before[path]))
        link = before.get(path.rsplit('/', 1)[0] + '/stereo')
        if link not in (('i', (0,)), ('i', (1,))):
            raise ValueError('fresh stereo partner report missing or invalid: ' + path)
        links.append(link[1][0])
    if links[0] != links[1] or (links[0] and units[0] != units[1]):
        raise ValueError('linked partner baseline cannot be restored unambiguously')
    return units


def fresh_baselines(connection, paths, inventory, result, check, checkpoint, *, seconds):
    """Two whole fresh windows; retain one incomplete mix window without merging."""
    complete = []
    observed = []
    result['baseline_attempts'] = []
    for _attempt in range(BASELINE_REFRESH_LIMIT):
        seen = snapshot(connection, check, seconds=seconds)
        missing, wrong_tags = inventory_gaps(seen, inventory)
        recorded = {'messages': dict(seen), 'events': seen.events,
                    'missing': missing, 'wrong_tags': wrong_tags,
                    'complete': not missing and not wrong_tags}
        result['baseline_attempts'].append(recorded)
        checkpoint()
        # Never wait out unsafe/invalid FX, phantom power or linkage.
        baseline_values(seen, paths)
        if any(compare(previous, seen)['changed'] for previous in observed):
            raise ValueError('baseline changed between fresh refresh windows')
        observed.append(seen)
        # One additional read window is allowed only for absent compound
        # mix reports. No invalid type or other missing state is deferred.
        if wrong_tags or any(not path.startswith('/mix/') for path in missing):
            require_complete(seen, inventory)
        if missing:
            continue
        complete.append(seen)
        result['baseline_first' if len(complete) == 1 else 'baseline'] = recorded
        checkpoint()
        if len(complete) == 2:
            break
    if len(complete) != 2:
        raise ValueError('two complete fresh baseline windows required '
                         'within three refreshes')
    first, before = complete
    result['baseline_stability'] = require_stable(first, before)
    for seen in observed:
        for event in seen.events:
            if (event['origin'] == Source.DEVICE.name
                    and (event['tags'], event['args']) != before.get(event['path'])):
                raise ValueError('baseline DEVICE report changed within fresh refresh windows: '
                                 + event['path'])
    return before


def finding(before, after, target, units):
    if target not in after:
        return 'missing'
    try:
        received = fx_units(after[target])
    except (TypeError, ValueError):
        return 'invalid-report'
    if received == units:
        return 'confirmed'
    if after[target] == before[target]:
        return 'unchanged'
    return 'mismatch'


def partner_finding(before, after, paths, member, proposed):
    partner = paths[1 - member]
    if partner not in after:
        return 'missing'
    try:
        received = fx_units(after[partner])
    except (TypeError, ValueError):
        return 'invalid-report'
    if after[partner] == before[partner]:
        return 'unchanged'
    linked = before[partner.rsplit('/', 1)[0] + '/stereo'][1][0]
    return 'followed' if linked and received == proposed else 'unexpected'


def unexpected_fx_reports(before, after, paths, member, proposed):
    """Retain transient DEVICE deviations even when the final value matches."""
    allowed = {path: {fx_units(before[path])} for path in paths}
    allowed[paths[member]].add(proposed)
    partner = paths[1 - member]
    if before[partner.rsplit('/', 1)[0] + '/stereo'][1][0]:
        allowed[partner].add(proposed)
    unexpected = set()
    for event in after.events:
        path = event['path']
        if event['origin'] != Source.DEVICE.name or path not in allowed:
            continue
        try:
            received = fx_units((event['tags'], event['args']))
        except (TypeError, ValueError):
            unexpected.add(path)
            continue
        if received not in allowed[path]:
            unexpected.add(path)
    return sorted(unexpected)


@contextlib.contextmanager
def stop_signals():
    def stopped(signum, _frame):
        raise InterruptedError('probe interrupted by ' + signal.Signals(signum).name)

    previous = {sig: signal.getsignal(sig) for sig in (signal.SIGINT, signal.SIGTERM)}
    try:
        for sig in previous:
            signal.signal(sig, stopped)
        yield stopped
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)


def measure(connection, pair, member, result, inventory, check, checkpoint,
            *, seconds=READ_SECONDS, watch_seconds=WATCH_SECONDS):
    """One bounded lease, persisted baseline before WRITE, no second transport."""
    paths = pair_paths(pair)
    target = paths[member]
    started = time.monotonic()
    before = None
    touched = False

    def persist(*, restoring=False):
        try:
            checkpoint()
        except OSError as exc:
            if not restoring:
                raise
            result['evidence_error'] = str(exc)

    def capture(name, *, refresh=True, duration=seconds):
        seen = snapshot(connection, check, refresh=refresh, seconds=duration)
        result[name] = {'messages': dict(seen), 'events': seen.events}
        persist(restoring=name == 'restoration')
        return seen

    def send(path, value, phase):
        nonlocal touched
        if time.monotonic() - started > (35 if phase == 'probe' else 80):
            raise TimeoutError('measurement write budget expired')
        check()
        entry = {'path': path, 'tags': 'f', 'args': (value,), 'phase': phase,
                 'submitted': False, 'acknowledged': False}
        result.setdefault('writes', []).append(entry)
        persist(restoring=phase == 'restore')  # the baseline was persisted before probing
        try:
            if phase == 'probe':
                touched = True  # a lost ACK can still mean the write reached hardware
            connection.send([(path, 'f', (value,))])
        except WriteFailed as exc:
            entry['submitted'] = path in exc.written
            entry['pending'] = list(exc.unwritten)
            persist(restoring=phase == 'restore')
            raise
        entry['submitted'] = entry['acknowledged'] = True
        connection.wait(.01)  # pace MIDI and drive the lease heartbeat
        persist(restoring=phase == 'restore')

    with stop_signals() as stop:
        try:
            result['phase'] = 'baseline'
            before = fresh_baselines(connection, paths, inventory, result,
                                     check, persist, seconds=seconds)
            units = baseline_values(before, paths)
            proposed = probe_units(units[member], result.get('requested_db'))
            result['proposed_db'] = proposed / 10
            result['phase'] = 'probe'
            checkpoint()
            send(target, proposed / 10, 'probe')
            after = capture('probe')
            result['finding'] = finding(before, after, target, proposed)
            result['partner_finding'] = partner_finding(before, after, paths, member, proposed)
            result['unexpected_fx_reports'] = unexpected_fx_reports(
                before, after, paths, member, proposed)
            result['probe_difference'] = compare(before, after)
            result['indirect_changes'] = [path for path in result['probe_difference']['changed']
                                          if path not in paths]
        except (OSError, ValueError, TypeError) as exc:
            result['error'] = str(exc)
        finally:
            if touched and before is not None:
                # Bound cleanup on the same lease; a second stop cannot interrupt it.
                for sig in (signal.SIGINT, signal.SIGTERM):
                    signal.signal(sig, signal.SIG_IGN)
                result['phase'] = 'restore'
                result['restored'] = False
                try:
                    for path in paths:
                        value = float32(fx_units(before[path]) / 10)
                        send(path, value, 'restore')
                    restored = capture('restoration')
                    result['unexpected_restoration_fx_reports'] = unexpected_fx_reports(
                        before, restored, paths, member, proposed)
                    result['restoration_difference'] = compare(before, restored)
                    result['restored'] = not any(result['restoration_difference'].values())
                except (OSError, ValueError, TypeError) as exc:
                    result['restore_error'] = str(exc)
                finally:
                    persist(restoring=True)
        if result.get('restored') and not result.get('error'):
            try:
                result['phase'] = 'watch'
                # A stop during the watch no longer needs a hardware write.
                for sig in (signal.SIGINT, signal.SIGTERM):
                    signal.signal(sig, stop)
                watched = capture('watch', refresh=False, duration=watch_seconds)
                result['watch_changes'] = sorted({event['path'] for event in watched.events
                    if event['origin'] == Source.DEVICE.name
                    and (event['tags'], event['args']) != before.get(event['path'])})
                final = capture('final')
                result['final_difference'] = compare(before, final)
                result['restored'] = not any(result['final_difference'].values())
            except (OSError, ValueError, TypeError, KeyboardInterrupt) as exc:
                result['error'] = str(exc) or 'watch interrupted'
                result['restored'] = False
        result['phase'] = 'measured'
        result['checks_passed'] = (result.get('finding') == 'confirmed'
                             and result.get('partner_finding') in ('followed', 'unchanged')
                             and result.get('restored') is True
                             and not result.get('indirect_changes')
                             and not result.get('watch_changes')
                             and not result.get('unexpected_fx_reports')
                             and not result.get('unexpected_restoration_fx_reports')
                             and not result.get('probe_difference', {}).get('missing')
                             and not result.get('probe_difference', {}).get('new')
                             and not result.get('error'))
        persist(restoring=True)
        if result.get('evidence_error'):
            result['checks_passed'] = False


def json_safe(value):
    if isinstance(value, float) and not math.isfinite(value):
        return repr(value)
    if isinstance(value, dict):
        return {key: json_safe(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [json_safe(item) for item in value]
    return value


def save_evidence(path, result):
    """Keep the last complete baseline if the next write/flush fails."""
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode='w', dir=path.parent,
                                         prefix='.fx-probe-', delete=False) as stream:
            temporary = Path(stream.name)
            json.dump(json_safe(result), stream, indent=2, allow_nan=False)
            stream.write('\n')
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--pair', choices=PAIRS, default='input:13')
    parser.add_argument('--member', choices=('first', 'second'), default='first')
    parser.add_argument('--value', type=float,
                        help='Fixed probe level in dB (-65..0, 0.1 dB steps); '
                             'default: a small change from the fresh baseline')
    parser.add_argument('--execute', action='store_true')
    parser.add_argument('--out', type=Path)
    parser.add_argument('--inventory', type=Path)
    parser.add_argument('--config', type=Path)
    parser.add_argument('--backend-build', type=Path,
                        default=Path(__file__).resolve().parents[1]
                        / 'build/backend-0.8.0-series10')
    args = parser.parse_args()
    try:
        result = plan(args.pair, int(args.member == 'second'), value=args.value)
    except (TypeError, ValueError) as exc:
        parser.error(str(exc))
    if not args.execute:
        print(json.dumps(result, indent=2))
        return 0
    if args.out is None or args.inventory is None:
        parser.error('--execute requires --out and --inventory')
    try:
        inventory_bytes = args.inventory.read_bytes()
        inventory = json.loads(inventory_bytes)
        valid_inventory(inventory)
        # Refuse overwriting evidence and prove it can be persisted before discovery.
        fd = os.open(args.out, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        os.close(fd)
    except (OSError, ValueError, TypeError, AttributeError) as exc:
        print('probe-fx: ' + str(exc), file=sys.stderr)
        return 1
    def checkpoint():
        save_evidence(args.out, result)

    try:
        result['recorded'] = datetime.now(UTC).isoformat()
        result['inventory_sha256'] = hashlib.sha256(inventory_bytes).hexdigest()
        checkpoint()
        config, path = measurement_config(args.config)
        proc = Path(os.environ.get('OSCMIX_PROC_ROOT', '/proc'))
        guard = IdentityGuard(config, proc)
        with open_desk(guard.config, path) as desk:
            evidence = build_evidence(desk._control, args.backend_build, proc)
            require_inventory_peer(inventory, evidence, guard.interface.serial)
            result['running_backend'] = evidence
            result['firmware'] = inventory.get('firmware', {})
            result['serial'] = guard.interface.serial
            connection = desk._begin()
            result['hardware_used'] = True
            measure(connection, args.pair, int(args.member == 'second'), result,
                    inventory, guard, checkpoint)
        result['end_acknowledged'] = True
        result['phase'] = 'complete'
        result['success'] = result['checks_passed']
    except (OSError, ValueError, TypeError, ConfigError, DeviceLockUnavailable) as exc:
        result['operation_error'] = str(exc)
        result['success'] = False
    finally:
        try:
            checkpoint()
        except OSError as exc:
            print('probe-fx: could not save private evidence: ' + str(exc), file=sys.stderr)
            result['success'] = False
    if result.get('restored') is False:
        print('probe-fx: RESTORATION UNCONFIRMED; stop and inspect the device', file=sys.stderr)
    for key in ('error', 'restore_error', 'operation_error', 'evidence_error'):
        if result.get(key):
            print('probe-fx: %s: %s' % (key, result[key]), file=sys.stderr)
    for key in ('unexpected_fx_reports', 'unexpected_restoration_fx_reports'):
        if result.get(key):
            print('probe-fx: %s: %s' % (key, ', '.join(result[key])), file=sys.stderr)
    print('probe-fx: %s; finding=%s; restored=%s; END=%s -> %s'
          % ('passed' if result.get('success') else 'failed', result.get('finding', 'none'),
             result.get('restored', 'not needed'), result['end_acknowledged'], args.out))
    return 0 if result.get('success') and result['end_acknowledged'] else 1


if __name__ == '__main__':
    sys.exit(main())
