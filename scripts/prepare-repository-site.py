#!/usr/bin/env python3
"""Assemble or verify a complete public APT/RPM site, without publishing it.

Only a public certificate is used here. Signing takes place separately, before
this tool receives the channels. Development archives cannot pass release mode.
"""

import argparse
import gzip
import hashlib
import importlib.util
import io
import json
import os
import re
import shutil
import subprocess
import tarfile
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path, PurePosixPath

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location('repository_builder',
                                            ROOT / 'scripts/build-repository.py')
BUILDER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(BUILDER)
MAX_BYTES = 900 * 1024 * 1024  # Leave space below the published Pages site limit.
MAX_FILES = 50000
PUBLICATION = 'publication.json'
ROOT_FILES = {PUBLICATION, 'index.html', 'archive-key.asc'}
PROJECT = 'relative23/oscmix-desk'


class IndexTreeBuilder(ET.TreeBuilder):
    def doctype(self, _name, _pubid, _system):
        raise ValueError('repository XML cannot declare a DTD or entities')


def index_xml(data):
    if len(data) > 16 * 1024 * 1024:
        raise ValueError('RPM index exceeds the verification limit')
    parser = ET.XMLParser(target=IndexTreeBuilder())  # noqa: S314 -- DTD handler rejects entities
    return ET.fromstring(data, parser=parser)  # noqa: S314 -- bounded input; DTDs refused by parser


def verify_rpm_index(directory, snapshot):
    """Current metadata must select exactly one active signed form per version."""
    repo_ns = '{http://linux.duke.edu/metadata/repo}'
    package_ns = '{http://linux.duke.edu/metadata/common}'
    try:
        metadata = index_xml((directory / 'repodata/repomd.xml').read_bytes())
        primary = [node for node in metadata.findall(repo_ns + 'data')
                   if node.get('type') == 'primary']
        if len(primary) != 1:
            raise ValueError('RPM repository requires one primary index')
        location = primary[0].find(repo_ns + 'location')
        checksum = primary[0].find(repo_ns + 'checksum')
        if (location is None or checksum is None or checksum.get('type') != 'sha256'
                or snapshot['files'].get(location.get('href')) != checksum.text):
            raise ValueError('RPM primary index differs from the signed file index')
        path = BUILDER.relative_file(directory, location.get('href'))
        if path.suffix != '.gz':
            raise ValueError('the current RPM primary index must use gzip')
        with gzip.open(path, 'rb') as stream:
            data = stream.read(16 * 1024 * 1024 + 1)
        rows = index_xml(data).findall(package_ns + 'package')
        expected = {row['path']: row for row in snapshot['packages']}
        seen = set()
        for row in rows:
            fields = {node.tag.removeprefix(package_ns): node for node in row}
            path = fields['location'].get('href')
            if path not in expected or path in seen:
                raise ValueError('RPM index selects an unlisted or repeated package')
            record = expected[path]
            version = fields['version']
            if (fields['name'].text != record['name']
                    or fields['arch'].text != 'x86_64' or version.get('epoch') != '0'
                    or version.get('ver', '') + '-' + version.get('rel', '') != record['version']
                    or fields['checksum'].get('type') != 'sha256'
                    or fields['checksum'].text != record['sha256']
                    or fields['size'].get('package') != str((directory / path).stat().st_size)):
                raise ValueError('RPM index differs from the authenticated package identity')
            seen.add(path)
        if seen != expected.keys():
            raise ValueError('RPM index omits an authenticated package')
    except (ET.ParseError, KeyError, gzip.BadGzipFile) as exc:
        raise ValueError('invalid RPM repository index') from exc


def regular_files(directory):
    """Count directories too, so an empty foreign directory cannot be published."""
    files = set()
    for path in directory.rglob('*'):
        if path.is_symlink() or not (path.is_file() or path.is_dir()):
            raise ValueError('non-regular site entry: ' + str(path))
        if path.is_file():
            files.add(str(path.relative_to(directory)))
        elif not any(path.iterdir()):
            raise ValueError('empty site directory: ' + str(path))
    return files


def package_records(directory, snapshot, target, development):
    """Bind signed package paths to the original build manifests and exact pairs."""
    packages, pairs, allowed = [], {}, {'archive-key.asc'}
    identities, paths = {}, set()
    os_target, kind, architecture = BUILDER.TARGETS[target]
    indexed = [(row, True) for row in snapshot['packages']]
    retained = [(row, False) for row in snapshot.get('retained_packages', [])]
    for row, active in indexed + retained:
        manifest = BUILDER.relative_file(directory, row['build_manifest'])
        record = json.loads(manifest.read_text())
        artifact = record['artifact']
        if not re.fullmatch(r'[a-zA-Z0-9_.+~:-]+\.' + kind, artifact):
            raise ValueError('invalid native package filename')
        expected = dict(path='pool/' + row['sha256'] + '/' + artifact,
                        build_manifest='provenance/' + artifact + '.json',
                        unsigned_sha256=record['sha256'], name=record['package_name'],
                        version=record['package_version'])
        if any(row[name] != value for name, value in expected.items()):
            raise ValueError('package differs from its original build manifest')
        if snapshot['files'].get(row['path']) != row['sha256']:
            raise ValueError('package is absent from the signed file index')
        release = dict(line.split('=', 1) for line in record['os_release'].splitlines()
                       if '=' in line)
        platform = release['ID'].strip('"') + release['VERSION_ID'].strip('"')
        if (platform, record['format'], record['architecture']) != (os_target, kind, architecture):
            raise ValueError('package belongs to another distribution or architecture')
        if not development and (record['development'] or record['dirty']):
            raise ValueError('development or dirty package cannot be published')
        if (not re.fullmatch(r'[0-9a-f]{40}', record['source_commit'])
                or not re.fullmatch(r'\d+\.\d+\.\d+', record['version'])):
            raise ValueError('package has no release source identity')
        component = record['component']
        identity = row['name'], row['version']
        if (component not in BUILDER.PACKAGE_NAMES
                or row['name'] != BUILDER.PACKAGE_NAMES[component]
                or row['path'] in paths or (active and identity in identities)):
            raise ValueError('duplicate or unknown native package component')
        if active:
            identities[identity] = row
        elif (kind != 'rpm' or identity not in identities
              or any(identities[identity][key] != row[key]
                     for key in ('build_manifest', 'unsigned_sha256'))):
            raise ValueError('retained signature has no matching indexed RPM build')
        paths.add(row['path'])
        if active and component in ('core', 'gtk'):
            pairs.setdefault(row['version'], {})[component] = record
        allowed.update((row['path'], row['build_manifest']))
        packages.append((row, record, active))
    if {record['component'] for _, record, active in packages if active} != set(
            BUILDER.PACKAGE_NAMES):
        raise ValueError('site is missing a required package component')
    for pair in pairs.values():
        if set(pair) != {'core', 'gtk'}:
            raise ValueError('site contains an incomplete core/GTK version pair')
        BUILDER.validate_pair(pair['core'], pair['gtk'])
    if kind == 'deb' and any(row['sha256'] != row['unsigned_sha256'] for row, _, _ in packages):
        raise ValueError('DEB changed after its authenticated build')
    return packages, allowed


def channel_files(directory, verifier, args, target):
    snapshot = BUILDER.read_snapshot(directory, verifier)
    if (snapshot['target'] != target or snapshot['snapshot'] != args.snapshot
            or snapshot['source_commit'] != args.expected_commit
            or snapshot['development'] is not args.development):
        raise ValueError('channel does not match the selected publication: ' + target)
    if (not isinstance(snapshot['epoch'], int) or snapshot['epoch'] <= 0
            or snapshot['signing_key'] not in verifier.fingerprints):
        raise ValueError('channel lacks a known signing key or publication time')
    if (directory / 'archive-key.asc').read_bytes() != args.public_key.read_bytes():
        raise ValueError('channel certificate differs from selected trust')
    packages, allowed = package_records(directory, snapshot, target, args.development)
    kind = BUILDER.TARGETS[target][1]
    if kind == 'deb':
        allowed.update('dists/stable/' + name for name in
                       ('Release', 'Release.gpg', 'InRelease', 'main/binary-amd64/Packages',
                        'main/binary-amd64/Packages.gz'))
        pattern = r'dists/stable/main/binary-amd64/by-hash/SHA256/([0-9a-f]{64})'
        verifier.verify(directory / 'dists/stable/Release.gpg', directory / 'dists/stable/Release')
        verifier.verify(directory / 'dists/stable/InRelease')
    else:
        allowed.update('repodata/repomd.xml' + suffix for suffix in ('', '.asc', '.key'))
        pattern = r'repodata/([0-9a-f]{64})-(?:primary|filelists|other)\.xml\.(?:gz|zst)'
        verifier.verify(directory / 'repodata/repomd.xml.asc', directory / 'repodata/repomd.xml')
    if not allowed <= snapshot['files'].keys():
        raise ValueError('channel is missing required metadata or provenance')
    for name in snapshot['files'].keys() - allowed:
        match = re.fullmatch(pattern, name)
        if not match or match[1] != snapshot['files'][name]:
            raise ValueError('unrecognized or non-content-addressed repository file: ' + name)
    names = set(snapshot['files']) | {BUILDER.MANIFEST, BUILDER.MANIFEST + '.asc'}
    if regular_files(directory) != names:
        raise ValueError('channel contains unlisted files')
    if kind == 'rpm':
        verify_rpm_index(directory, snapshot)
    return snapshot, names, packages


def index_page(record):
    links = '\n'.join('<li><a href="%s/repository.json">%s</a></li>' % (target, target)
                      for target in BUILDER.TARGETS)
    qualifier = 'Development fixture' if record['development'] else 'Package repository'
    return ('<!doctype html>\n<html lang="en"><meta charset="utf-8">\n'
            '<title>oscmix-desk packages</title>\n<h1>oscmix-desk</h1>\n<p>' + qualifier
            + ': ' + record['snapshot'] + '</p>\n<ul>\n' + links + '\n</ul>\n'
            '<p><a href="archive-key.asc">Public signing certificate</a> · '
            '<a href="https://github.com/' + PROJECT + '/blob/main/docs/PACKAGE-REPOSITORIES.md">'
            'Installation and verification</a></p>\n</html>\n').encode()


def inspect_channels(root, args, *, published):
    files, snapshots, packages = {}, {}, {}
    with tempfile.TemporaryDirectory(prefix='oscmix-public-verify-') as temporary:
        verifier = BUILDER.Verifier(args.public_key, Path(temporary))
        for target in BUILDER.TARGETS:
            directory = root / target
            snapshot, names, packages[target] = channel_files(directory, verifier, args, target)
            snapshots[target] = snapshot
            for name in sorted(names):
                files[target + '/' + name] = BUILDER.relative_file(directory, name)
    allowed = set(files) | (ROOT_FILES if published else set())
    if regular_files(root) != allowed:
        raise ValueError('site contains missing or unlisted top-level content')
    if len(files) > MAX_FILES or sum(path.stat().st_size for path in files.values()) > MAX_BYTES:
        raise ValueError('site exceeds publication limits; choose explicit retention first')
    record = dict(schema=1, snapshot=args.snapshot, source_commit=args.expected_commit,
                  development=args.development,
                  epoch=max(row['epoch'] for row in snapshots.values()),
                  channels={target: BUILDER.sha(root / target / BUILDER.MANIFEST)
                            for target in BUILDER.TARGETS})
    generated = {PUBLICATION: (json.dumps(record, indent=2) + '\n').encode(),
                 'index.html': index_page(record), 'archive-key.asc': args.public_key.read_bytes()}
    if published and any(BUILDER.relative_file(root, name).read_bytes() != data
                         for name, data in generated.items()):
        raise ValueError('site publication record, page or certificate differs from its channels')
    return record, files, generated, packages


def stage(args):
    record, files, generated, _ = inspect_channels(args.channels, args, published=False)
    if args.output.exists():
        raise ValueError('refusing to replace an existing immutable archive')
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='oscmix-site-', dir=args.output.parent) as temporary:
        scratch = Path(temporary)
        archive = scratch / 'site.tar.gz'
        with archive.open('wb') as stream, gzip.GzipFile(fileobj=stream, mode='wb', mtime=0,
                                                       filename='') as zipped, \
                tarfile.open(fileobj=zipped, mode='w') as tar:
            for name in sorted(set(files) | set(generated)):
                data = generated[name] if name in generated else files[name].read_bytes()
                if name in files and BUILDER.sha(files[name]) != hashlib.sha256(data).hexdigest():
                    raise ValueError('repository file changed while archiving: ' + name)
                entry = tarfile.TarInfo(name)
                entry.mode, entry.mtime, entry.size = 0o644, record['epoch'], len(data)
                tar.addfile(entry, io.BytesIO(data))
        # Validate the actual archive bytes before making its final name visible.
        unpack(archive, scratch / 'check')
        inspect_channels(scratch / 'check', args, published=True)
        archive.chmod(0o644)
        os.link(archive, args.output)  # No replacement if another publication won the race.
    return dict(record, archive=args.output.name, sha256=BUILDER.sha(args.output))


def unpack(archive, output):
    output.mkdir()
    seen, total = set(), 0
    with tarfile.open(archive, 'r:gz') as tar:
        for entry in tar:
            name = entry.name
            path = PurePosixPath(name)
            if (not entry.isfile() or path.is_absolute() or '..' in path.parts
                    or str(path) != name or not path.parts or name in seen
                    or (path.parts[0] not in BUILDER.TARGETS and name not in ROOT_FILES)):
                raise ValueError('unsafe or repeated publication archive entry: ' + name)
            seen.add(name)
            total += entry.size
            if len(seen) > MAX_FILES or total > MAX_BYTES or entry.size < 0:
                raise ValueError('publication archive exceeds its limits')
            destination = output / name
            destination.parent.mkdir(parents=True, exist_ok=True)
            with tar.extractfile(entry) as source, destination.open('xb') as target:
                shutil.copyfileobj(source, target)
            destination.chmod(0o644)
            os.utime(destination, (entry.mtime, entry.mtime))


def authenticate_builds(root, packages, public_key, gh, temporary,
                        release_inputs=None, trusted_root=None):
    """Authenticate original build manifests, including retained historical versions.

    RPM signing changes the artifact bytes. Compare its immutable header and
    payload with the original release artifact named by the attested manifest,
    then verify the signed RPM's actual signature and digests in an isolated DB.
    """
    count = 0
    for target, rows in packages.items():
        for row, record, active in rows:
            manifest = root / target / row['build_manifest']
            tag = 'v' + record['version']
            command = [gh, 'attestation', 'verify', manifest, '--repo', PROJECT,
                       '--signer-workflow', PROJECT + '/.github/workflows/release.yml',
                       '--source-ref', 'refs/tags/' + tag,
                       '--source-digest', record['source_commit'], '--deny-self-hosted-runners']
            if release_inputs is not None:
                command += ['--bundle', release_inputs / tag / 'attestation.jsonl']
            if trusted_root is not None:
                command += ['--custom-trusted-root', trusted_root]
            BUILDER.run(command)
            artifact = root / target / row['path']
            if record['format'] == 'rpm':
                if release_inputs is not None:
                    original = BUILDER.relative_file(release_inputs / tag, record['artifact'])
                else:
                    cache = temporary / target / record['source_commit']
                    cache.mkdir(parents=True, exist_ok=True)
                    original = cache / record['artifact']
                    if not original.exists():
                        BUILDER.run([gh, 'release', 'download', tag, '--repo', PROJECT,
                                     '--pattern', record['artifact'], '--dir', cache])
                if BUILDER.sha(original) != record['sha256']:
                    raise ValueError('original RPM differs from its authenticated build manifest')
                original_identity = BUILDER.rpm_payload_identity(original)
                if original_identity != BUILDER.rpm_payload_identity(artifact):
                    raise ValueError('signed RPM changed its authenticated header or payload')
                # Old signature bytes remain reachable for cached indexes, but
                # only the freshly signed form enters current native metadata.
                # Both forms must match the independently attested build.
                if active:
                    BUILDER.verify_rpm(artifact, public_key, temporary / 'rpmdb')
            elif BUILDER.sha(artifact) != record['sha256']:
                raise ValueError('DEB differs from its authenticated build manifest')
            count += 1
    return count


def verify_archive(args):
    if (args.archive.stat().st_size > MAX_BYTES
            or BUILDER.sha(args.archive) != args.sha256):
        raise ValueError('publication archive differs from the selected digest')
    if args.output.exists():
        raise ValueError('verified site output must be new')
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='oscmix-site-', dir=args.output.parent) as temporary:
        scratch = Path(temporary) / 'site'
        unpack(args.archive, scratch)
        record, _, _, packages = inspect_channels(scratch, args, published=True)
        authenticated = 0
        if not args.development:
            authenticated = authenticate_builds(scratch, packages, args.public_key,
                                                args.gh, Path(temporary),
                                                args.release_inputs, args.trusted_root)
        scratch.chmod(0o755)
        for path in scratch.rglob('*'):
            if path.is_dir():
                path.chmod(0o755)
        scratch.rename(args.output)
    return dict(record, sha256=args.sha256, signatures_and_files_verified=True,
                authenticated_build_manifests=authenticated)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    for name in ('stage', 'verify'):
        command = commands.add_parser(name)
        command.add_argument('--public-key', required=True, type=Path)
        command.add_argument('--snapshot', required=True)
        command.add_argument('--expected-commit', required=True)
        command.add_argument('--development', action='store_true')
        command.add_argument('--output', required=True, type=Path)
        if name == 'stage':
            command.add_argument('--channels', required=True, type=Path)
        else:
            command.add_argument('--archive', required=True, type=Path)
            command.add_argument('--sha256', required=True)
            command.add_argument('--gh', default='gh', help='GitHub CLI with attestation support')
            command.add_argument('--release-inputs', type=Path,
                                 help='offline vVERSION/attestation.jsonl and original RPMs')
            command.add_argument('--trusted-root', type=Path,
                                 help='authenticated gh attestation trusted-root export')
    args = parser.parse_args()
    if (not re.fullmatch('[0-9a-f]{40}', args.expected_commit)
            or not re.fullmatch('[a-zA-Z0-9][a-zA-Z0-9._-]{0,95}', args.snapshot)):
        parser.error('a full source SHA and simple snapshot identifier are required')
    if args.command == 'verify' and not re.fullmatch('[0-9a-f]{64}', args.sha256):
        parser.error('--sha256 must be the complete selected archive digest')
    for name in ('public_key', 'output', 'channels', 'archive', 'release_inputs', 'trusted_root'):
        if getattr(args, name, None) is not None:
            setattr(args, name, getattr(args, name).absolute())
    try:
        result = stage(args) if args.command == 'stage' else verify_archive(args)
    except (OSError, ValueError, KeyError, TypeError, tarfile.TarError,
            subprocess.SubprocessError) as exc:
        parser.exit(1, 'repository publication refused: %s\n' % exc)
    print(json.dumps(result, indent=2))


if __name__ == '__main__':
    main()
