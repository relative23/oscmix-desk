"""A newer deployment must preserve downloads and expose verified bytes."""

import hashlib
import importlib.util
import json
import urllib.error
from email.utils import formatdate
from types import SimpleNamespace

import pytest
from support import repo_file


@pytest.fixture
def publisher():
    spec = importlib.util.spec_from_file_location(
        'repository_publication', repo_file('scripts', 'repository-publication.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def promotion(publisher, tmp_path, monkeypatch):
    remote, channels = {}, {}
    site = tmp_path / 'site'
    for target, (_, kind, _) in publisher.SITE.BUILDER.TARGETS.items():
        entry = 'dists/stable/InRelease' if kind == 'deb' else 'repodata/repomd.xml'
        remote[target + '/' + entry] = b'previous native entry point'
        old = dict(schema=1, snapshot='old', epoch=100, files={
            'pool/old/package': 'b' * 64,
            'provenance/old.json': 'c' * 64,
            entry: hashlib.sha256(remote[target + '/' + entry]).hexdigest()})
        manifest = target + '/repository.json'
        remote[manifest] = json.dumps(old).encode()
        channels[target] = hashlib.sha256(remote[manifest]).hexdigest()
        new = dict(schema=1, snapshot='new', epoch=101, files=dict(old['files']),
                   predecessor=dict(snapshot='old', epoch=100,
                                    manifest_sha256=channels[target], publication_commit=None))
        path = site / manifest
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps(new))
        if kind == 'deb':
            release = site / target / 'dists/stable/Release'
            release.parent.mkdir(parents=True)
            release.write_text('Valid-Until: ' + formatdate(1000, usegmt=True) + '\n')
    remote['publication.json'] = json.dumps(dict(schema=1, channels=channels)).encode()
    digest = hashlib.sha256(remote['publication.json']).hexdigest()

    def get(name, _limit, *, collect=False, headers=None):
        data = remote[name]
        return (dict(sha256=hashlib.sha256(data).hexdigest(), size=len(data),
                     etag='"before"', last_modified=formatdate(100, usegmt=True)),
                data if collect else b'')

    monkeypatch.setattr(publisher, 'remote', get)
    return SimpleNamespace(site=site, remote=remote, digest=digest)


def test_new_publication_extends_all_channels_and_records_native_http_validators(
        publisher, promotion):
    result = publisher.transition(promotion.site, promotion.digest)
    assert result['previous'] == promotion.digest
    assert len(result['conditional_requests']) == 5
    assert set(result['conditional_requests']) == {
        target + ('/dists/stable/InRelease' if kind == 'deb' else '/repodata/repomd.xml')
        for target, (_, kind, _) in publisher.SITE.BUILDER.TARGETS.items()}


@pytest.mark.parametrize('problem', ['stale-publication', 'changed-old-channel', 'wrong-parent',
                                   'old-epoch', 'discard-package', 'change-provenance',
                                   'different-live-index', 'conceal-previous-site'])
def test_stale_or_incomplete_promotions_are_refused(publisher, promotion, problem):
    path = promotion.site / 'opensuse16/repository.json'
    record = json.loads(path.read_text())
    if problem == 'stale-publication':
        promotion.digest = '0' * 64
    elif problem == 'changed-old-channel':
        promotion.remote['opensuse16/repository.json'] += b' '
    elif problem == 'wrong-parent':
        record['predecessor']['manifest_sha256'] = '0' * 64
    elif problem == 'old-epoch':
        record['epoch'] = 100
    elif problem == 'discard-package':
        del record['files']['pool/old/package']
    elif problem == 'change-provenance':
        record['files']['provenance/old.json'] = '0' * 64
    elif problem == 'different-live-index':
        promotion.remote['opensuse16/repodata/repomd.xml'] = b'inconsistent edge cache'
    else:
        promotion.digest = 'none'
    path.write_text(json.dumps(record))
    with pytest.raises(ValueError, match=r'publication|channel|content|metadata'):
        publisher.transition(promotion.site, promotion.digest)


@pytest.mark.parametrize('status', [404, 403, 500])
def test_only_initial_404_can_mean_no_previous_publication(
        publisher, promotion, monkeypatch, status):
    def unavailable(*_, **__):
        raise urllib.error.HTTPError(publisher.BASE, status, 'unavailable', {}, None)

    monkeypatch.setattr(publisher, 'remote', unavailable)
    for path in promotion.site.glob('*/repository.json'):
        record = json.loads(path.read_text())
        record['predecessor'] = None
        path.write_text(json.dumps(record))
    if status == 404:
        assert publisher.transition(promotion.site, 'none')['previous'] is None
    else:
        with pytest.raises(urllib.error.HTTPError):
            publisher.transition(promotion.site, 'none')
    with pytest.raises(urllib.error.HTTPError):
        publisher.transition(promotion.site, promotion.digest)


@pytest.mark.parametrize('problem', ['expired', 'future', 'duplicate-expiry'])
def test_invalid_publication_dates_cannot_be_deployed(publisher, promotion, problem):
    if problem == 'future':
        path = promotion.site / 'fedora44/repository.json'
        record = json.loads(path.read_text())
        record['epoch'] = 9999
        path.write_text(json.dumps(record))
    elif problem == 'duplicate-expiry':
        path = promotion.site / 'debian13/dists/stable/Release'
        path.write_text(path.read_text() * 2)
    with pytest.raises(ValueError, match=r'expired|expiry|future'):
        publisher.freshness(promotion.site, 1000 if problem == 'expired' else 200)


def test_final_recheck_refuses_a_changed_predecessor_without_replacing_saved_evidence(
        publisher, promotion, monkeypatch):
    root = promotion.site.parent
    path = root / 'prepared.json'
    path.write_text(json.dumps(dict(transition=dict(previous=promotion.digest))))
    monkeypatch.setattr(publisher.time, 'time', lambda: 200)
    checked = publisher.recheck(root)
    assert len(checked['transition']['conditional_requests']) == 5
    before = path.read_bytes()
    promotion.remote['publication.json'] += b' '
    with pytest.raises(ValueError, match='publication changed'):
        publisher.recheck(root)
    assert path.read_bytes() == before


@pytest.mark.parametrize('problem', [None, 'different-bytes', 'stale-304'])
def test_published_verification_checks_bytes_and_each_old_validator(
        publisher, tmp_path, monkeypatch, problem):
    site = tmp_path / 'site'
    name = 'debian13/dists/stable/InRelease'
    path = site / name
    path.parent.mkdir(parents=True)
    path.write_bytes(b'new verified bytes')
    digest = publisher.SITE.BUILDER.sha(path)
    prepared = dict(verified=dict(sha256='f' * 64), transition=dict(conditional_requests={
        name: dict(sha256='0' * 64, etag='old-etag', last_modified='old-date')}))
    (tmp_path / 'prepared.json').write_text(json.dumps(prepared))
    calls = []

    def downloaded(requested, limit, *, headers=None):
        assert requested == name
        assert limit == path.stat().st_size
        calls.append(headers)
        if problem == 'stale-304' and headers:
            raise urllib.error.HTTPError(publisher.BASE + name, 304, 'unchanged', {}, None)
        return dict(sha256='0' * 64 if problem == 'different-bytes' else digest), b''

    monkeypatch.setattr(publisher, 'remote', downloaded)
    if problem:
        with pytest.raises((ValueError, urllib.error.HTTPError)):
            publisher.verify_remote(tmp_path)
        assert not (tmp_path / 'https-verification.json').exists()
    else:
        result = publisher.verify_remote(tmp_path)
        assert result['passed']
        assert not result['native_package_managers_tested']
        assert calls == [None, {'If-None-Match': 'old-etag'}, {'If-Modified-Since': 'old-date'}]
