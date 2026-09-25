"""A backend build is the pinned archive plus checked patches, never local edits."""

import hashlib
import importlib.util
import json
import subprocess

import pytest
from support import repo_file


@pytest.fixture
def preparation(tmp_path):
    spec = importlib.util.spec_from_file_location(
        'prepare_backend', repo_file('scripts', 'prepare-backend.py'))
    builder = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(builder)
    upstream = tmp_path / 'upstream'
    upstream.mkdir()
    subprocess.run(['git', 'init', '-q', str(upstream)], check=True)
    (upstream / 'value').write_text('original\n')
    subprocess.run(['git', '-C', str(upstream), 'add', 'value'], check=True)
    subprocess.run(['git', '-C', str(upstream), '-c', 'user.name=Backend test',
                    '-c', 'user.email=backend@example.invalid', '-c', 'commit.gpgsign=false',
                    'commit', '-qm', 'fixture'], check=True)
    pin = subprocess.check_output(['git', '-C', str(upstream), 'rev-parse', 'HEAD'],
                                  text=True).strip()
    patch = tmp_path / 'change.patch'
    patch.write_text('diff --git a/value b/value\n--- a/value\n+++ b/value\n'
                     '@@ -1 +1 @@\n-original\n+coordinated\n')
    manifest = tmp_path / 'series.json'
    manifest.write_text(json.dumps(dict(
        schema=1, protocol='ODK1', upstream=pin,
        patches=[dict(file=patch.name, sha256=hashlib.sha256(patch.read_bytes()).hexdigest())])))
    return builder, upstream, patch, manifest


def test_preparation_archives_pinned_source_and_records_patched_files(preparation, tmp_path):
    builder, upstream, _patch, manifest = preparation
    (upstream / 'value').write_text('private unfinished work\n')
    destination = tmp_path / 'new'
    result = builder.prepare(upstream, destination, manifest)
    assert (upstream / 'value').read_text() == 'private unfinished work\n'
    assert (destination / 'value').read_text() == 'coordinated\n'
    header = '#define OSCMIX_DESK_BUILD_ID "' + result['series_sha256'] + '"\n'
    assert result['source_sha256'] == {
        'desk-build-id.h': hashlib.sha256(header.encode()).hexdigest(),
        'value': hashlib.sha256(b'coordinated\n').hexdigest()}
    assert json.loads((destination / '.oscmix-desk-source.json').read_text()) == result


def test_tampered_patch_is_refused_before_preparing_any_source(preparation, tmp_path):
    builder, upstream, patch, manifest = preparation
    patch.write_text(patch.read_text() + '\nchanged\n')
    destination = tmp_path / 'new'
    with pytest.raises(ValueError, match='hash mismatch'):
        builder.prepare(upstream, destination, manifest)
    assert not destination.exists()


def test_existing_work_is_never_replaced(preparation, tmp_path):
    builder, upstream, _patch, manifest = preparation
    destination = tmp_path / 'existing'
    destination.mkdir()
    (destination / 'notes').write_text('preserved')
    with pytest.raises(ValueError, match='new or empty'):
        builder.prepare(upstream, destination, manifest)
    assert list(destination.iterdir()) == [destination / 'notes']
    assert (destination / 'notes').read_text() == 'preserved'


@pytest.mark.parametrize(('change', 'reason'), [
    ({'schema': 2}, 'format'), ({'protocol': 'other'}, 'protocol'),
    ({'upstream': 'main'}, 'full commit'), ({'patches': []}, 'requires'),
    ({'patches': [{'file': '../change.patch', 'sha256': ''}]}, 'basenames'),
])
def test_incompatible_or_ambiguous_series_is_rejected(preparation, change, reason):
    builder, _upstream, _patch, manifest = preparation
    record = json.loads(manifest.read_text())
    record.update(change)
    manifest.write_text(json.dumps(record))
    with pytest.raises(ValueError, match=reason):
        builder.series_record(manifest)


@pytest.mark.parametrize('identity', ['ODK1', 'old-series', 'empty'])
def test_incompatible_existing_binary_never_qualifies(preparation, tmp_path, identity):
    builder, _upstream, _patch, manifest = preparation
    executable = tmp_path / 'oscmix'
    executable.write_text('#!/bin/sh\nprintf "%s\\n"\n' % identity)
    executable.chmod(0o755)
    with pytest.raises(ValueError, match='required coordinated backend series'):
        builder.verify_binaries([executable], manifest)
