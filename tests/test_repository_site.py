"""A deployment must contain one complete, authenticated set of public channels."""

import gzip
import importlib.util
import io
import json
import tarfile
from types import SimpleNamespace

import pytest
from support import repo_file


def apt_index(site, directory, packages):
    root = directory / 'dists/stable'
    index = root / 'main/binary-amd64/Packages'
    index.write_text('\n\n'.join(
        'Package: ' + row['name'] + '\nVersion: ' + row['version']
        + '\nArchitecture: amd64\nFilename: ' + row['path']
        + '\nSize: ' + str((directory / row['path']).stat().st_size)
        + '\nSHA256: ' + row['sha256'] for row in packages) + '\n\n')
    index.with_suffix('.gz').write_bytes(gzip.compress(index.read_bytes(), mtime=0))
    sums = ''.join(' %s %d main/binary-amd64/%s\n' % (
        site.BUILDER.sha(path), path.stat().st_size, path.name)
        for path in (index, index.with_suffix('.gz')))
    (root / 'Release').write_text(
        'Suite: stable\nCodename: stable\nArchitectures: amd64\nComponents: main\n'
        'Acquire-By-Hash: yes\nSHA256:\n' + sums)


def rpm_index(site, directory, packages):
    rows = []
    for row in packages:
        version, release = row['version'].split('-', 1)
        rows.append('<package type="rpm"><name>' + row['name'] + '</name><arch>x86_64</arch>'
                    '<version epoch="0" ver="' + version + '" rel="' + release + '"/>'
                    '<checksum type="sha256">' + row['sha256'] + '</checksum>'
                    '<size package="' + str((directory / row['path']).stat().st_size) + '"/>'
                    '<location href="' + row['path'] + '"/></package>')
    primary = gzip.compress(('<metadata xmlns="http://linux.duke.edu/metadata/common">'
                             + ''.join(rows) + '</metadata>').encode(), mtime=0)
    digest = site.hashlib.sha256(primary).hexdigest()
    name = 'repodata/' + digest + '-primary.xml.gz'
    (directory / name).write_bytes(primary)
    (directory / 'repodata/repomd.xml').write_text(
        '<repomd xmlns="http://linux.duke.edu/metadata/repo"><data type="primary">'
        '<checksum type="sha256">' + digest + '</checksum><location href="' + name
        + '"/></data></repomd>')


@pytest.fixture
def site(monkeypatch):
    spec = importlib.util.spec_from_file_location(
        'repository_site', repo_file('scripts', 'prepare-repository-site.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    def verify(signature, source=None):
        if signature.read_bytes() != b'signature':
            raise ValueError('invalid signature')
        if source is None:
            return signature.with_name('Release').read_bytes()
        return None

    monkeypatch.setattr(module.BUILDER, 'Verifier', lambda *_: SimpleNamespace(
        fingerprints={'D' * 40}, verify=verify))
    return module


@pytest.fixture
def channels(site, tmp_path):
    root = tmp_path / 'channels'
    certificate = tmp_path / 'public.asc'
    certificate.write_bytes(b'public certificate')
    releases = {'debian13': ('debian', '13'), 'ubuntu2404': ('ubuntu', '24.04'),
                'ubuntu2604': ('ubuntu', '26.04'), 'fedora44': ('fedora', '44'),
                'opensuse16': ('opensuse-leap', '16.0')}
    for target, (_, kind, architecture) in site.BUILDER.TARGETS.items():
        directory = root / target
        directory.mkdir(parents=True)
        (directory / 'archive-key.asc').write_bytes(certificate.read_bytes())
        packages = []
        for component, name in site.BUILDER.PACKAGE_NAMES.items():
            filename = name + '-0.8.0-1.' + kind
            data = (target + component).encode()
            digest = site.hashlib.sha256(data).hexdigest()
            package = directory / 'pool' / digest / filename
            package.parent.mkdir(parents=True)
            package.write_bytes(data)
            build = dict(artifact=filename, package_name=name, component=component,
                         package_version='0.8.0-1', version='0.8.0', sha256=digest,
                         os_release='ID=%s\nVERSION_ID="%s"\n' % releases[target],
                         format=kind, architecture=architecture,
                         development=False, dirty=False, source_commit='a' * 40,
                         backend_commit='b' * 40, backend_series_sha256='c' * 64,
                         backend_protocol='ODK1')
            manifest = directory / 'provenance' / (filename + '.json')
            manifest.parent.mkdir(exist_ok=True)
            manifest.write_text(json.dumps(build))
            packages.append(dict(name=name, version='0.8.0-1',
                                 path=str(package.relative_to(directory)), sha256=digest,
                                 unsigned_sha256=digest,
                                 build_manifest=str(manifest.relative_to(directory))))
        paths = (['dists/stable/' + name for name in
                  ('Release', 'Release.gpg', 'InRelease', 'main/binary-amd64/Packages',
                   'main/binary-amd64/Packages.gz')] if kind == 'deb' else
                 ['repodata/repomd.xml' + suffix for suffix in ('', '.asc', '.key')])
        for name in paths:
            path = directory / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b'signature')
        if kind == 'rpm':
            rpm_index(site, directory, packages)
        else:
            apt_index(site, directory, packages)
        files = {str(path.relative_to(directory)): site.BUILDER.sha(path)
                 for path in directory.rglob('*') if path.is_file()}
        snapshot = dict(schema=1, files=files, packages=packages, target=target, snapshot='first',
                        development=False, source_commit='a' * 40, signing_key='D' * 40,
                        epoch=1790460000)
        (directory / 'repository.json').write_text(json.dumps(snapshot))
        (directory / 'repository.json.asc').write_bytes(b'signature')
    return SimpleNamespace(channels=root, public_key=certificate, snapshot='first',
                           expected_commit='a' * 40, development=False,
                           output=tmp_path / 'snapshot.tar.gz', gh='gh',
                           release_inputs=None, trusted_root=None)


def test_complete_archive_is_reproducible_and_restores_verified_site(
        site, channels, tmp_path, monkeypatch):
    monkeypatch.setattr(site, 'authenticate_builds', lambda *_: 15)
    first = site.stage(channels)
    channels.output = tmp_path / 'repeated.tar.gz'
    second = site.stage(channels)
    assert first['sha256'] == second['sha256']
    options = SimpleNamespace(**vars(channels))
    options.archive, options.output = channels.output, tmp_path / 'public'
    options.sha256 = second['sha256']
    result = site.verify_archive(options)
    assert result['signatures_and_files_verified'] is True
    assert result['authenticated_build_manifests'] == 15
    assert set(result['channels']) == set(site.BUILDER.TARGETS)
    assert len(list(options.output.glob('*/repository.json'))) == 5
    assert (options.output / 'index.html').stat().st_mode & 0o777 == 0o644


@pytest.mark.parametrize('problem', ['bad-signature', 'modified-package', 'unlisted-file',
                                    'listed-secret', 'missing-last-channel', 'wrong-channel',
                                    'development', 'different-source'])
def test_bad_channel_never_exposes_partial_publication(site, channels, problem):
    directory = channels.channels / 'opensuse16'
    manifest = directory / 'repository.json'
    snapshot = json.loads(manifest.read_text())
    if problem == 'bad-signature':
        (directory / 'repository.json.asc').write_bytes(b'bad')
    elif problem == 'modified-package':
        (directory / snapshot['packages'][0]['path']).write_bytes(b'modified')
    elif problem in ('unlisted-file', 'listed-secret'):
        (directory / 'private.asc').write_bytes(b'not for publication')
        if problem == 'listed-secret':
            snapshot['files']['private.asc'] = site.BUILDER.sha(directory / 'private.asc')
    elif problem == 'missing-last-channel':
        manifest.unlink()
    elif problem == 'wrong-channel':
        snapshot['target'] = 'fedora44'
    elif problem == 'development':
        snapshot['development'] = True
    else:
        snapshot['source_commit'] = 'e' * 40
    if problem != 'missing-last-channel':
        manifest.write_text(json.dumps(snapshot))
    errors = {'bad-signature': 'invalid signature', 'modified-package': 'missing or altered',
              'unlisted-file': 'unlisted files', 'listed-secret': 'unrecognized',
              'missing-last-channel': 'regular repository file',
              'wrong-channel': 'selected publication', 'development': 'selected publication',
              'different-source': 'selected publication'}
    with pytest.raises(ValueError, match=errors[problem]):
        site.stage(channels)
    assert not channels.output.exists()


@pytest.mark.parametrize('problem', ['development', 'dirty', 'wrong-pair', 'wrong-target',
                                    'different-unsigned-deb'])
def test_signed_site_does_not_override_build_identity(site, channels, problem):
    directory = channels.channels / 'debian13'
    manifest = directory / 'repository.json'
    snapshot = json.loads(manifest.read_text())
    row = snapshot['packages'][0]
    build_path = directory / row['build_manifest']
    build = json.loads(build_path.read_text())
    if problem in ('development', 'dirty'):
        build[problem] = True
    elif problem == 'wrong-pair':
        build['backend_commit'] = 'f' * 40
    elif problem == 'wrong-target':
        build['os_release'] = 'ID=ubuntu\nVERSION_ID="26.04"\n'
    else:
        build['sha256'] = row['unsigned_sha256'] = 'f' * 64
    build_path.write_text(json.dumps(build))
    snapshot['files'][row['build_manifest']] = site.BUILDER.sha(build_path)
    manifest.write_text(json.dumps(snapshot))
    errors = {'development': 'development or dirty', 'dirty': 'development or dirty',
              'wrong-pair': 'build identities differ', 'wrong-target': 'another distribution',
              'different-unsigned-deb': 'DEB changed'}
    with pytest.raises(ValueError, match=errors[problem]):
        site.stage(channels)
    assert not channels.output.exists()


def test_existing_immutable_archive_is_never_replaced(site, channels):
    channels.output.write_bytes(b'previous archive')
    with pytest.raises(ValueError, match='replace an existing'):
        site.stage(channels)
    assert channels.output.read_bytes() == b'previous archive'


def retain_old_rpm(site, channels):
    directory = channels.channels / 'fedora44'
    manifest = directory / 'repository.json'
    snapshot = json.loads(manifest.read_text())
    old = snapshot['packages'][0]
    package = directory / old['path']
    data = package.read_bytes() + b'new signature'
    digest = site.hashlib.sha256(data).hexdigest()
    replacement = directory / 'pool' / digest / package.name
    replacement.parent.mkdir()
    replacement.write_bytes(data)
    snapshot['retained_packages'] = [old]
    snapshot['packages'][0] = dict(old, path=str(replacement.relative_to(directory)),
                                   sha256=digest)
    rpm_index(site, directory, snapshot['packages'])
    snapshot['files'] = {str(path.relative_to(directory)): site.BUILDER.sha(path)
                         for path in directory.rglob('*') if path.is_file()
                         and path.name not in ('repository.json', 'repository.json.asc')}
    manifest.write_text(json.dumps(snapshot))
    return directory, manifest, snapshot


def test_replaced_rpm_signature_stays_downloadable_outside_the_current_index(site, channels):
    directory, _, snapshot = retain_old_rpm(site, channels)
    staged = site.stage(channels)
    assert staged['sha256'] == site.BUILDER.sha(channels.output)
    with tarfile.open(channels.output) as archive:
        old = snapshot['retained_packages'][0]['path']
        assert archive.extractfile('fedora44/' + old).read() == (directory / old).read_bytes()


@pytest.mark.parametrize('problem', ['select-retained', 'duplicate', 'omit-version',
                                   'changed-identity', 'unrelated-retained', 'repeated-retained'])
def test_rpm_index_and_retention_cannot_select_ambiguous_or_unattested_content(
        site, channels, problem):
    directory, manifest, snapshot = retain_old_rpm(site, channels)
    indexed = list(snapshot['packages'])
    if problem == 'select-retained':
        indexed[0] = snapshot['retained_packages'][0]
    elif problem == 'duplicate':
        indexed.append(indexed[0])
    elif problem == 'omit-version':
        indexed.pop()
    elif problem == 'changed-identity':
        indexed[0] = dict(indexed[0], version='0.8.0-999')
    elif problem == 'unrelated-retained':
        old = snapshot['retained_packages'][0]
        original = directory / old['build_manifest']
        record = json.loads(original.read_text())
        record['source_commit'] = 'f' * 40
        changed = original.with_name('different.rpm.json')
        changed.write_text(json.dumps(record))
        old['build_manifest'] = str(changed.relative_to(directory))
    else:
        snapshot['retained_packages'].append(snapshot['retained_packages'][0])
    rpm_index(site, directory, indexed)
    snapshot['files'] = {str(path.relative_to(directory)): site.BUILDER.sha(path)
                         for path in directory.rglob('*') if path.is_file()
                         and path.name not in ('repository.json', 'repository.json.asc')}
    manifest.write_text(json.dumps(snapshot))
    with pytest.raises(ValueError, match=r'RPM|package|signature'):
        site.stage(channels)
    assert not channels.output.exists()


@pytest.mark.parametrize('encoding', ['utf-8', 'utf-16'])
def test_signed_native_index_cannot_expand_declared_entities(site, encoding):
    xml = '<?xml version="1.0" encoding="' + encoding + '"?>'
    xml += '<!DOCTYPE metadata [<!ENTITY payload "expanded">]><metadata>&payload;</metadata>'
    with pytest.raises(ValueError, match='DTD or entities'):
        site.index_xml(xml.encode(encoding))


@pytest.mark.parametrize('problem', ['foreign-download', 'duplicate', 'omit-version',
                                   'changed-identity', 'different-compressed-index'])
def test_apt_signed_indexes_must_select_exactly_the_authenticated_packages(
        site, channels, problem):
    directory = channels.channels / 'debian13'
    manifest = directory / 'repository.json'
    snapshot = json.loads(manifest.read_text())
    indexed = list(snapshot['packages'])
    if problem == 'foreign-download':
        old = directory / indexed[0]['path']
        foreign = directory / 'pool/foreign-download.deb'
        foreign.write_bytes(old.read_bytes())
        indexed[0] = dict(indexed[0], path=str(foreign.relative_to(directory)))
    elif problem == 'duplicate':
        indexed.append(indexed[0])
    elif problem == 'omit-version':
        indexed.pop()
    elif problem == 'changed-identity':
        indexed[0] = dict(indexed[0], version='0.8.0-999')
    apt_index(site, directory, indexed)
    if problem == 'different-compressed-index':
        compressed = directory / 'dists/stable/main/binary-amd64/Packages.gz'
        compressed.write_bytes(gzip.compress(b'different index'))
        # Re-signing a self-consistent Release must not mask two index contents.
        release = directory / 'dists/stable/Release'
        lines = release.read_text().splitlines()
        lines[-1] = ' %s %d main/binary-amd64/Packages.gz' % (
            site.BUILDER.sha(compressed), compressed.stat().st_size)
        release.write_text('\n'.join(lines) + '\n')
    snapshot['files'] = {str(path.relative_to(directory)): site.BUILDER.sha(path)
                         for path in directory.rglob('*') if path.is_file()
                         and path.name not in ('repository.json', 'repository.json.asc')}
    manifest.write_text(json.dumps(snapshot))
    with pytest.raises(ValueError, match=r'APT|repository file'):
        site.stage(channels)
    assert not channels.output.exists()


def test_distinct_valid_inrelease_and_release_cannot_select_different_indexes(
        site, channels, monkeypatch):
    monkeypatch.setattr(site.BUILDER, 'Verifier', lambda *_: SimpleNamespace(
        fingerprints={'D' * 40}, verify=lambda *_: b'other authenticated cleartext'))
    with pytest.raises(ValueError, match='InRelease and detached Release disagree'):
        site.stage(channels)
    assert not channels.output.exists()


@pytest.mark.parametrize(('name', 'kind'), [
    ('link', tarfile.SYMTYPE), ('hardlink', tarfile.LNKTYPE), ('debian13/pipe', tarfile.FIFOTYPE),
    ('../secret', tarfile.REGTYPE), ('/secret', tarfile.REGTYPE),
    ('debian13/../secret', tarfile.REGTYPE), ('.', tarfile.REGTYPE),
    ('secret.asc', tarfile.REGTYPE)])
def test_archive_cannot_escape_or_create_special_files(site, tmp_path, name, kind):
    archive = tmp_path / 'untrusted.tar.gz'
    with tarfile.open(archive, 'w:gz') as tar:
        entry = tarfile.TarInfo(name)
        entry.type, entry.linkname = kind, '/outside'
        tar.addfile(entry, io.BytesIO())
    with pytest.raises(ValueError, match='unsafe or repeated'):
        site.unpack(archive, tmp_path / 'out')


def test_duplicate_archive_entries_are_refused(site, tmp_path):
    archive = tmp_path / 'duplicate.tar.gz'
    with tarfile.open(archive, 'w:gz') as tar:
        for _ in range(2):
            tar.addfile(tarfile.TarInfo('publication.json'), io.BytesIO())
    with pytest.raises(ValueError, match='repeated'):
        site.unpack(archive, tmp_path / 'out')


def test_compressed_archive_cannot_exceed_expanded_limit(site, tmp_path, monkeypatch):
    monkeypatch.setattr(site, 'MAX_BYTES', 10)
    archive = tmp_path / 'oversized.tar.gz'
    with tarfile.open(archive, 'w:gz') as tar:
        entry = tarfile.TarInfo('index.html')
        entry.size = 11
        tar.addfile(entry, io.BytesIO(b'x' * 11))
    with pytest.raises(ValueError, match='exceeds its limits'):
        site.unpack(archive, tmp_path / 'out')


def test_archive_digest_must_be_selected_before_extraction(site, channels, tmp_path):
    site.stage(channels)
    channels.archive, channels.output = channels.output, tmp_path / 'public'
    channels.sha256 = '0' * 64
    with pytest.raises(ValueError, match='differs from the selected digest'):
        site.verify_archive(channels)
    assert not channels.output.exists()


def test_unavailable_build_provenance_cannot_expose_a_site(site, channels, tmp_path, monkeypatch):
    staged = site.stage(channels)
    channels.archive, channels.output = channels.output, tmp_path / 'public'
    channels.sha256 = staged['sha256']

    def refused(*args):
        raise ValueError('build attestation does not match the selected source')

    monkeypatch.setattr(site, 'authenticate_builds', refused)
    with pytest.raises(ValueError, match='build attestation'):
        site.verify_archive(channels)
    assert not channels.output.exists()


def test_attestations_are_scoped_to_the_original_release_workflow_tag_and_commit(
        site, channels, tmp_path, monkeypatch):
    _, _, _, packages = site.inspect_channels(channels.channels, channels, published=False)
    calls = []
    monkeypatch.setattr(site.BUILDER, 'run', lambda command, **_: calls.append(command))
    count = site.authenticate_builds(channels.channels, {'debian13': packages['debian13']},
                                     channels.public_key, 'selected-gh', tmp_path)
    assert count == 3
    for call in calls:
        assert call[:3] == ['selected-gh', 'attestation', 'verify']
        assert call[call.index('--repo') + 1] == 'relative23/oscmix-desk'
        assert call[call.index('--signer-workflow') + 1] == (
            'relative23/oscmix-desk/.github/workflows/release.yml')
        assert call[call.index('--source-ref') + 1] == 'refs/tags/v0.8.0'
        assert call[call.index('--source-digest') + 1] == 'a' * 40
        assert '--deny-self-hosted-runners' in call
