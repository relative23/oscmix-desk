"""Refuse unsafe repository inputs before any signing or publication work."""

import hashlib
import importlib.util
import json
from types import SimpleNamespace

import pytest
from support import repo_file


@pytest.fixture
def builder():
    spec = importlib.util.spec_from_file_location(
        'build_repository', repo_file('scripts', 'build-repository.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize('name', ['../secret', '/etc/passwd', 'a/../../secret',
                                  './secret', 'a//secret', 'missing', ''])
def test_repository_manifest_cannot_name_an_outside_or_missing_file(builder, tmp_path, name):
    with pytest.raises(ValueError, match=r'repository (path|file)'):
        builder.relative_file(tmp_path, name)


@pytest.mark.parametrize('parent_link', [False, True])
def test_repository_manifest_cannot_follow_a_symlink(builder, tmp_path, parent_link):
    outside = tmp_path / 'outside'
    outside.mkdir()
    (outside / 'secret').write_text('not an artifact')
    root = tmp_path / 'repo'
    root.mkdir()
    if parent_link:
        (root / 'linked').symlink_to(outside, target_is_directory=True)
        name = 'linked/secret'
    else:
        (root / 'linked').symlink_to(outside / 'secret')
        name = 'linked'
    with pytest.raises(ValueError, match='regular repository file'):
        builder.relative_file(root, name)


def test_private_key_cannot_be_copied_into_public_repository(builder, tmp_path, monkeypatch):
    private = tmp_path / 'wrong.asc'
    private.write_text('-----BEGIN PGP PRIVATE KEY BLOCK-----\nsecret\n')
    monkeypatch.setattr(builder, 'run', lambda *_: pytest.fail('private material reached GnuPG'))
    with pytest.raises(ValueError, match='only a public'):
        builder.Signer(SimpleNamespace(public_key=private), tmp_path)


@pytest.mark.parametrize('source', ['previous', 'packages', 'gnupghome'])
def test_staging_cannot_write_inside_an_input_or_keyring(builder, tmp_path, source):
    container = tmp_path / 'protected'
    container.mkdir()
    args = SimpleNamespace(output=container / 'new', previous=None, packages=None,
                           gnupghome=tmp_path / 'keyring')
    setattr(args, source, container)
    with pytest.raises(ValueError, match='outside inputs'):
        builder.build(args)
    assert not args.output.exists()


@pytest.fixture
def pair(tmp_path):
    for component, name in [('core', 'oscmix-desk'), ('gtk', 'oscmix-desk-gtk')]:
        artifact = tmp_path / (name + '.deb')
        artifact.write_bytes(component.encode())
        record = dict(artifact=artifact.name,
                      sha256=hashlib.sha256(artifact.read_bytes()).hexdigest(),
                      os_release='ID=debian\nVERSION_ID="13"\n',
                      format='deb', architecture='amd64',
                      source_commit='a' * 40, component=component, package_name=name,
                      package_version='0.8.0-1', development=False, dirty=False,
                      backend_commit='b' * 40, backend_protocol='ODK1',
                      backend_series_sha256='c' * 64)
        artifact.with_suffix('.deb.json').write_text(json.dumps(record))
    return tmp_path


@pytest.mark.parametrize('problem', ['wrong-platform', 'modified-package', 'development'])
def test_invalid_pair_is_refused_before_signing(builder, pair, problem, monkeypatch):
    manifest = pair / 'oscmix-desk.deb.json'
    record = json.loads(manifest.read_text())
    if problem == 'wrong-platform':
        record['os_release'] = 'ID=ubuntu\nVERSION_ID="26.04"\n'
    elif problem == 'modified-package':
        (pair / record['artifact']).write_bytes(b'modified')
    else:
        record['development'] = True
    manifest.write_text(json.dumps(record))
    # Manifests sort GTK first; allow its read-only header inspection, but
    # never permit signing while the complete pair has not been validated.
    def header(command, **kwargs):
        if command[-1] == 'Depends':
            return 'oscmix-desk (= 0.8.0-1)\n'
        return 'Package: oscmix-desk-gtk\nVersion: 0.8.0-1\nArchitecture: amd64\n'
    monkeypatch.setattr(builder, 'run', header)
    with pytest.raises(ValueError, match=r'distribution|digest|development'):
        builder.read_pair(pair, 'debian13', False, 'a' * 40)


def test_gtk_requires_an_exact_core_version_before_pair_is_accepted(builder, pair, monkeypatch):
    def header(command, **kwargs):
        if command[-1] == 'Depends':
            return 'oscmix-desk (>= 0.8.0-1)\n'
        return 'Package: oscmix-desk-gtk\nVersion: 0.8.0-1\nArchitecture: amd64\n'
    monkeypatch.setattr(builder, 'run', header)
    with pytest.raises(ValueError, match='exact core package version'):
        builder.read_pair(pair, 'debian13', False, 'a' * 40)


@pytest.mark.parametrize('epoch', [99, 100])
def test_publication_time_must_advance_before_copying_old_content(builder, tmp_path, epoch):
    previous = tmp_path / 'previous'
    previous.mkdir()
    (previous / 'repository.json').write_text(json.dumps(dict(
        target='debian13', development=True, snapshot='first', epoch=100)))
    (previous / 'repository.json.asc').touch()
    args = SimpleNamespace(previous=previous, output=tmp_path / 'new', target='debian13',
                           development=True, snapshot='second', epoch=epoch)
    with pytest.raises(ValueError, match='epoch must be later'):
        builder.restore_previous(args, SimpleNamespace(verify=lambda *_: None))
    assert not args.output.exists()
