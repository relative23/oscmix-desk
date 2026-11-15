"""Shared provenance and decoding for the hardware qualification tools.

These helpers borrow the checked ODK1 connection; they open no second
transport and neither acquire a lease nor write a register on their own.
"""

import hashlib
import hmac
import json
import re
import subprocess
import sys
from pathlib import Path

import oscmix_desk
from oscmix_desk.constants import __version__
from oscmix_desk.osc import decode_delivery

#: What the records name instead of the serial of the box they measured (2026-11-14).
PLACEHOLDER_SERIAL = '12345678'
#: Where a serial stands in what the tools record: the ALSA product name, a serial
#: field or text, the device lock key, the ALSA card id and PipeWire node names.
SERIAL_CONTEXTS = (
    re.compile(r'(?:UCX II|802) \((\d{6,12})\)'),
    re.compile(r'(?i)serial\W{0,12}(\d{6,12})(?!\d)'),
    re.compile(r'2a39-3f[0-9a-f]{2}-(\d{6,12})(?!\d)'),
    re.compile(r'\bII(\d{6,12})(?!\d)'),
    re.compile(r'Fireface_\w*?__(\d{6,12})(?!\d)'),
)
MACHINE_ID_FILES = (Path('/etc/machine-id'), Path('/var/lib/dbus/machine-id'))


def device_pseudonym(serial):
    """A stable name for one box on this installation that does not give its serial away.

    HMAC-SHA256 of the serial, keyed with the machine ID as systemd advises for
    application-specific IDs: without the machine ID nobody can try the few
    possible serials against it. None without a machine ID, as in a container.
    """
    for path in MACHINE_ID_FILES:
        try:
            key = path.read_text().strip()
        except OSError:
            continue
        if key:
            message = b'oscmix-desk device pseudonym\0' + serial.encode()
            return 'box-' + hmac.new(key.encode(), message, 'sha256').hexdigest()[:16]
    return None


def without_serial(record, serial=None):
    """The record with each serial it names replaced by the placeholder, and device_pseudonym.

    The serial the tool resolved and any serial in a known context are replaced
    wherever they stand alone; the same digits inside a hex digest stay. A record
    already without a serial keeps the pseudonym it has.
    """
    text = json.dumps(record, allow_nan=True)
    found = {match.group(1) for pattern in SERIAL_CONTEXTS for match in pattern.finditer(text)}
    serial = serial if serial and re.fullmatch(r'\d{6,12}', serial) else None
    serials = (found | {serial} if serial else found) - {PLACEHOLDER_SERIAL}
    for one in sorted(serials, key=len, reverse=True):
        text = re.sub(r'(?<![0-9A-Fa-f])%s(?![0-9A-Fa-f])' % re.escape(one),
                      PLACEHOLDER_SERIAL, text)
    clean = json.loads(text)
    named = serial if serial in serials else (min(serials) if len(serials) == 1 else None)
    if isinstance(clean, dict):
        if named:
            clean['device_pseudonym'] = device_pseudonym(named)
        else:
            clean.setdefault('device_pseudonym', None)
    return clean


def source_evidence(tool):
    """The desk code a measurement ran with: every runtime module and the tool, by digest.

    The commit, and whether the tree was dirty, when the tool runs from a Git checkout of its
    own; None for both otherwise.
    """
    root = Path(tool).resolve().parents[1]
    package = Path(oscmix_desk.__file__).resolve().parent
    commit = dirty = None
    try:
        top = subprocess.run(['git', '-C', str(root), 'rev-parse', '--show-toplevel'],
                             capture_output=True, text=True, check=True).stdout.strip()
        if Path(top).resolve() == root:
            commit = subprocess.run(['git', '-C', str(root), 'rev-parse', 'HEAD'],
                                    capture_output=True, text=True, check=True).stdout.strip()
            dirty = bool(subprocess.run(['git', '-C', str(root), 'status', '--porcelain'],
                                        capture_output=True, text=True, check=True).stdout.strip())
    except (OSError, subprocess.CalledProcessError):
        pass
    return {'version': __version__, 'commit': commit, 'dirty': dirty, 'python': sys.version,
            'runtime_sha256': {path.name: hashlib.sha256(path.read_bytes()).hexdigest()
                               for path in sorted(package.glob('*.py'))},
            'tool_sha256': hashlib.sha256(Path(tool).read_bytes()).hexdigest()}


def build_evidence(connection, build_dir, proc_root=Path('/proc')):
    """Refuse an unidentified build, modified source, or different running binary."""
    record = json.loads((build_dir / '.oscmix-desk-source.json').read_text())
    series = Path(__file__).resolve().parents[1] / 'patches/backend-series.json'
    if (record.get('schema') != 1 or record.get('protocol') != 'ODK1'
            or record.get('series_sha256') != hashlib.sha256(series.read_bytes()).hexdigest()
            or not record.get('source_sha256')):
        raise ValueError('measurement requires the current prepared backend series')
    for name, expected in record['source_sha256'].items():
        source = build_dir / name
        if (Path(name).is_absolute() or '..' in Path(name).parts
                or source.is_symlink()
                or hashlib.sha256(source.read_bytes()).hexdigest() != expected):
            raise ValueError('prepared backend source changed: ' + name)
    executable = proc_root / str(connection.pid) / 'exe'
    running = hashlib.sha256(executable.read_bytes()).hexdigest()
    built = hashlib.sha256((build_dir / 'oscmix').read_bytes()).hexdigest()
    if running != built:
        raise ValueError('running backend differs from the recorded build')
    return {'pid': connection.pid, 'epoch': connection.epoch.hex(),
            'device': connection.device_name, 'sha256': running,
            'upstream': record['upstream'], 'series_sha256': record['series_sha256'],
            'protocol': 'ODK1', 'matches_local_build': True}


def observations(connection, timeout):
    """Decode one complete delivery, preserving its origin and ordering."""
    delivery = connection.next_delivery(timeout)
    if delivery is None:
        return
    try:
        messages = decode_delivery(delivery.payload)
    except ValueError:
        connection.close()
        raise
    for path, tags, args in messages:
        yield path, tags, args, delivery.origin, delivery.sequence
