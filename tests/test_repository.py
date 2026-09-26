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


@pytest.mark.parametrize('secret_kind', ['sec', 'ssb'])
def test_public_armor_cannot_disguise_secret_key_packets(
        builder, tmp_path, monkeypatch, secret_kind):
    public = tmp_path / 'disguised.asc'
    public.write_text('-----BEGIN PGP PUBLIC KEY BLOCK-----\nencoded packets\n')
    args = SimpleNamespace(public_key=public, gnupghome=tmp_path / 'key', signing_key='A' * 40)
    listing = secret_kind + ':unknown:key:fields\nfpr:::::::::' + args.signing_key + ':\n'
    monkeypatch.setattr(builder, 'run',
                        lambda command, **_: listing if '--show-keys' in command else '')
    with pytest.raises(ValueError, match='private key packets'):
        builder.Signer(args, tmp_path)


@pytest.mark.parametrize('status', ['EXPKEYSIG', 'REVKEYSIG', 'EXPSIG',
                                   'BADSIG', 'ERRSIG', 'NO_PUBKEY'])
def test_cryptographically_valid_signature_does_not_override_key_failure(
        builder, tmp_path, monkeypatch, status):
    signer = builder.Signer.__new__(builder.Signer)
    signer.verify_home = tmp_path
    signer.keyring = tmp_path / 'public.gpg'
    # Even adding one good signature cannot hide another failed signer.
    output = '[GNUPG:] GOODSIG good signer\n[GNUPG:] VALIDSIG fingerprint\n'
    output += '[GNUPG:] ' + status + ' failed signer\n'
    monkeypatch.setattr(builder, 'run', lambda *_: output)
    with pytest.raises(ValueError, match='current, unrevoked signature'):
        signer.verify(tmp_path / 'signature.asc', tmp_path / 'repository.json')


def test_unrelated_expired_subkey_does_not_reject_a_current_signature(
        builder, tmp_path, monkeypatch):
    signer = builder.Signer.__new__(builder.Signer)
    signer.verify_home = tmp_path
    signer.keyring = tmp_path / 'public.gpg'
    output = ('[GNUPG:] KEYEXPIRED 100\n[GNUPG:] GOODSIG current signer\n'
              '[GNUPG:] VALIDSIG fingerprint\n')
    monkeypatch.setattr(builder, 'run', lambda *_: output)
    signer.verify(tmp_path / 'signature.asc', tmp_path / 'repository.json')


@pytest.mark.parametrize('codes', [('VALIDSIG',), ('GOODSIG',),
                                  ('GOODSIG', 'VALIDSIG', 'GOODSIG', 'VALIDSIG')])
def test_staging_requires_one_complete_signature_result(builder, tmp_path, monkeypatch, codes):
    signer = builder.Signer.__new__(builder.Signer)
    signer.verify_home = tmp_path
    signer.keyring = tmp_path / 'public.gpg'
    monkeypatch.setattr(builder, 'run',
                        lambda *_: ''.join('[GNUPG:] ' + code + ' value\n' for code in codes))
    with pytest.raises(ValueError, match='current, unrevoked signature'):
        signer.verify(tmp_path / 'signature.asc', tmp_path / 'repository.json')


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
        builder.read_packages(pair, 'debian13', False, 'a' * 40)


def test_gtk_requires_an_exact_core_version_before_pair_is_accepted(builder, pair, monkeypatch):
    def header(command, **kwargs):
        if command[-1] == 'Depends':
            return 'oscmix-desk (>= 0.8.0-1)\n'
        return 'Package: oscmix-desk-gtk\nVersion: 0.8.0-1\nArchitecture: amd64\n'
    monkeypatch.setattr(builder, 'run', header)
    with pytest.raises(ValueError, match='exact core package version'):
        builder.read_packages(pair, 'debian13', False, 'a' * 40)


def test_repository_client_update_does_not_require_rebuilding_the_backend(
        builder, pair, monkeypatch):
    source = pair / 'oscmix-desk.deb.json'
    record = json.loads(source.read_text())
    for path in pair.iterdir():
        path.unlink()
    record.update(component='repository', package_name='oscmix-desk-repository',
                  artifact='oscmix-desk-repository.deb', package_version='0.8.0-2')
    for field in list(record):
        if field.startswith('backend_'):
            del record[field]
    package = pair / record['artifact']
    package.write_bytes(b'repository-client')
    record['sha256'] = hashlib.sha256(package.read_bytes()).hexdigest()
    package.with_suffix('.deb.json').write_text(json.dumps(record))

    def header(command, **kwargs):
        if command[-1] == 'Depends':
            return 'python3, gnupg, gpgv, apt, python3-apt\n'
        return 'Package: oscmix-desk-repository\nVersion: 0.8.0-2\nArchitecture: amd64\n'

    monkeypatch.setattr(builder, 'run', header)
    packages = builder.read_packages(pair, 'debian13', False, 'a' * 40)
    assert len(packages) == 1
    assert packages[0][2]['component'] == 'repository'


def test_initial_repository_requires_the_packaged_subscription(builder, tmp_path, monkeypatch):
    args = SimpleNamespace(output=tmp_path / 'out', packages=tmp_path / 'input', previous=None,
                           gnupghome=tmp_path / 'keyring', target='debian13',
                           development=True, expected_commit='a' * 40)
    monkeypatch.setattr(builder, 'read_packages',
                        lambda *_: [(None, None, dict(component='core')),
                                    (None, None, dict(component='gtk'))])
    with pytest.raises(ValueError, match='initial repository requires'):
        builder.build(args)
    assert not args.output.exists()


@pytest.mark.parametrize('epoch', [99, 100])
def test_publication_time_must_advance_before_copying_old_content(builder, tmp_path, epoch):
    previous = tmp_path / 'previous'
    previous.mkdir()
    (previous / 'repository.json').write_text(json.dumps(dict(
        schema=1, files={}, target='debian13', development=True, snapshot='first', epoch=100)))
    (previous / 'repository.json.asc').touch()
    args = SimpleNamespace(previous=previous, output=tmp_path / 'new', target='debian13',
                           development=True, snapshot='second', epoch=epoch)
    with pytest.raises(ValueError, match='epoch must be later'):
        builder.restore_previous(args, SimpleNamespace(verify=lambda *_: None))
    assert not args.output.exists()


def test_previous_snapshot_is_fully_checked_before_any_file_is_copied(builder, tmp_path):
    previous = tmp_path / 'previous'
    previous.mkdir()
    (previous / 'first').write_bytes(b'valid')
    (previous / 'second').write_bytes(b'modified')
    (previous / 'repository.json').write_text(json.dumps(dict(
        schema=1, files={'first': builder.sha(previous / 'first'), 'second': '0' * 64},
        target='debian13', development=True, snapshot='first', epoch=100)))
    (previous / 'repository.json.asc').touch()
    args = SimpleNamespace(previous=previous, output=tmp_path / 'new', target='debian13',
                           development=True, snapshot='second', epoch=101)
    with pytest.raises(ValueError, match='altered file: second'):
        builder.restore_previous(args, SimpleNamespace(verify=lambda *_: None))
    assert not args.output.exists()


@pytest.mark.parametrize('record', [None, [], {}, dict(schema=2, files={}),
                                   dict(schema=1, files=[])])
def test_unknown_snapshot_format_cannot_be_reused(builder, tmp_path, record):
    (tmp_path / 'repository.json').write_text(json.dumps(record))
    (tmp_path / 'repository.json.asc').touch()
    with pytest.raises(ValueError, match='unknown or incomplete'):
        builder.read_snapshot(tmp_path, SimpleNamespace(verify=lambda *_: None))


@pytest.mark.parametrize('digest', [None, [], 'not-a-sha256'])
def test_invalid_snapshot_digest_cannot_be_reused(builder, tmp_path, digest):
    (tmp_path / 'repository.json').write_text(json.dumps(dict(schema=1, files={'file': digest})))
    (tmp_path / 'repository.json.asc').touch()
    with pytest.raises(ValueError, match='missing or altered file'):
        builder.read_snapshot(tmp_path, SimpleNamespace(verify=lambda *_: None))
