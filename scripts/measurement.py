"""Shared provenance and decoding for the hardware qualification tools.

These helpers borrow the checked ODK1 connection; they open no second
transport and neither acquire a lease nor write a register on their own.
"""

import hashlib
import json
from pathlib import Path

from oscmix_desk.osc import decode_delivery


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
