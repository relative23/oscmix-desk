"""Measurement provenance comes from the running peer and exact prepared build."""

import hashlib
import importlib.util
import json
from types import SimpleNamespace

import pytest
from support import repo_file


@pytest.fixture
def measurement():
    spec = importlib.util.spec_from_file_location(
        'measurement', repo_file('scripts/measurement.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def prepared(tmp_path):
    build = tmp_path / 'prepared'
    build.mkdir()
    (build / 'main.c').write_text('source\n')
    (build / 'oscmix').write_bytes(b'matching binary')
    proc = tmp_path / 'proc'
    (proc / '123').mkdir(parents=True)
    (proc / '123/exe').write_bytes(b'matching binary')
    record = {
        'schema': 1, 'protocol': 'ODK1', 'upstream': 'a' * 40,
        'series_sha256': hashlib.sha256(repo_file('patches/backend-series.json')
                                       .read_bytes()).hexdigest(),
        'source_sha256': {'main.c': hashlib.sha256(b'source\n').hexdigest()},
    }
    (build / '.oscmix-desk-source.json').write_text(json.dumps(record))
    peer = SimpleNamespace(pid=123, epoch=bytes(range(16)), device_name='UCX II (24216011)')
    return build, proc, peer, record


def test_matching_peer_binary_and_source_record_are_required(measurement, prepared):
    build, proc, peer, record = prepared
    evidence = measurement.build_evidence(peer, build, proc)
    assert evidence['pid'] == 123
    assert evidence['epoch'] == bytes(range(16)).hex()
    assert evidence['upstream'] == record['upstream']
    assert evidence['series_sha256'] == record['series_sha256']
    assert evidence['sha256'] == hashlib.sha256(b'matching binary').hexdigest()


@pytest.mark.parametrize('change', ['source', 'binary', 'series', 'missing-record', 'symlink'])
def test_other_or_unidentified_build_refuses_before_measurement(measurement, prepared, change):
    build, proc, peer, record = prepared
    if change == 'source':
        (build / 'main.c').write_text('different source\n')
    elif change == 'binary':
        (proc / '123/exe').write_bytes(b'other binary')
    elif change == 'series':
        record['series_sha256'] = '0' * 64
        (build / '.oscmix-desk-source.json').write_text(json.dumps(record))
    elif change == 'missing-record':
        (build / '.oscmix-desk-source.json').unlink()
    else:
        (build / 'main.c').rename(build / 'elsewhere.c')
        (build / 'main.c').symlink_to('elsewhere.c')
    with pytest.raises((ValueError, FileNotFoundError),
                       match=r'changed|differs|series|No such file'):
        measurement.build_evidence(peer, build, proc)


def test_malformed_delivery_cannot_enter_measurement_evidence(measurement):
    from support import osc_bundle

    from oscmix_desk.backend import Delivery
    from oscmix_desk.osc import encode_osc
    from oscmix_desk.protocol import Source

    good = encode_osc('/output/5/stereo', 'i', 1)
    bad = encode_osc('/output/5/stereo', 'i', 0)[:-4]
    delivery = Delivery(Source.DEVICE, 1, bytes(range(16)), osc_bundle([good, bad]))
    closed = []
    connection = SimpleNamespace(next_delivery=lambda _: delivery,
                                 close=lambda: closed.append(True))
    with pytest.raises(ValueError, match='truncated OSC arguments'):
        next(measurement.observations(connection, .5))
    assert closed == [True]
