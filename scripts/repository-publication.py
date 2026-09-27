#!/usr/bin/env python3
"""Check a complete repository deployment and its downloaded public bytes.

Signing stays local. This tool uses release attestations and the explicitly
selected previous publication digest; it neither deploys nor edits a release.
"""

import argparse
import hashlib
import importlib.util
import json
import re
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
from email.utils import parsedate_to_datetime
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location('repository_site',
                                            ROOT / 'scripts/prepare-repository-site.py')
SITE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(SITE)
BASE = 'https://relative23.github.io/oscmix-desk/'
METADATA_LIMIT = 16 * 1024 * 1024


def remote(name, limit, *, collect=False, headers=None):
    url = BASE + urllib.parse.quote(name, safe='/')
    request = urllib.request.Request(url, headers=headers or {})  # noqa: S310 -- fixed HTTPS origin
    digest, size, chunks = hashlib.sha256(), 0, []
    with urllib.request.urlopen(request, timeout=30) as response:  # noqa: S310 -- fixed HTTPS origin
        if response.geturl() != url or response.status != 200:
            raise ValueError('unexpected repository redirect or response')
        while True:
            chunk = response.read(min(1024 * 1024, limit - size + 1))
            if not chunk:
                break
            size += len(chunk)
            if size > limit:
                raise ValueError('published file exceeds its expected size: ' + name)
            digest.update(chunk)
            if collect:
                chunks.append(chunk)
        record = dict(sha256=digest.hexdigest(), size=size,
                      etag=response.headers.get('ETag'),
                      last_modified=response.headers.get('Last-Modified'))
    return record, b''.join(chunks)


def immutable(name):
    return (name.startswith(('pool/', 'provenance/')) or '/by-hash/' in name
            or re.fullmatch(r'repodata/[0-9a-f]{64}-.+', name) is not None)


def transition(site, previous_digest):
    """Refuse stale promotions and removal of files used by in-flight clients."""
    try:
        public, data = remote(SITE.PUBLICATION, METADATA_LIMIT, collect=True)
    except urllib.error.HTTPError as exc:
        if exc.code != 404 or previous_digest != 'none':
            raise
        public, data = None, None
    if previous_digest == 'none':
        if public is not None:
            raise ValueError('an existing publication cannot be treated as initial')
        for target in SITE.BUILDER.TARGETS:
            new = json.loads((site / target / SITE.BUILDER.MANIFEST).read_text())
            if new.get('predecessor') is not None:
                raise ValueError('initial deployment cannot conceal a predecessor')
        return dict(previous=None, conditional_requests={})
    if public['sha256'] != previous_digest:
        raise ValueError('the deployed publication changed; prepare from its actual successor')
    publication = json.loads(data)
    if publication.get('schema') != 1 or set(publication['channels']) != set(SITE.BUILDER.TARGETS):
        raise ValueError('unknown previous publication')
    conditional = {}
    for target in SITE.BUILDER.TARGETS:
        info, data = remote(target + '/' + SITE.BUILDER.MANIFEST, METADATA_LIMIT, collect=True)
        if info['sha256'] != publication['channels'][target]:
            raise ValueError('previous channel differs from selected publication')
        old = json.loads(data)
        new = json.loads((site / target / SITE.BUILDER.MANIFEST).read_text())
        predecessor = new.get('predecessor') or {}
        if (predecessor.get('manifest_sha256') != info['sha256']
                or predecessor.get('snapshot') != old['snapshot']
                or predecessor.get('epoch') != old['epoch']
                or new['epoch'] <= old['epoch'] or new['snapshot'] == old['snapshot']):
            raise ValueError('new channel does not extend the selected publication')
        for name, digest in old['files'].items():
            if immutable(name) and new['files'].get(name) != digest:
                raise ValueError('new publication would discard referenced content: ' + name)
        # Record the actual HTTP validators before deployment for the changing
        # entry point used by each native manager, not just a JSON status page.
        entry = ('dists/stable/InRelease' if SITE.BUILDER.TARGETS[target][1] == 'deb'
                 else 'repodata/repomd.xml')
        path = target + '/' + entry
        info, _ = remote(path, METADATA_LIMIT)
        if info['sha256'] != old['files'][entry]:
            raise ValueError('previous native metadata differs from its manifest')
        conditional[path] = info
    return dict(previous=previous_digest, conditional_requests=conditional)


def freshness(site, now):
    for target, (_, kind, _) in SITE.BUILDER.TARGETS.items():
        snapshot = json.loads((site / target / SITE.BUILDER.MANIFEST).read_text())
        if snapshot['epoch'] > now + 300:
            raise ValueError('repository publication time is in the future')
        if kind == 'deb':
            release = (site / target / 'dists/stable/Release').read_text()
            values = [line[len('Valid-Until: '):] for line in release.splitlines()
                      if line.startswith('Valid-Until: ')]
            if len(values) != 1 or parsedate_to_datetime(values[0]).timestamp() <= now:
                raise ValueError('APT metadata has expired or has no unambiguous expiry')


def prepare(args):
    if (not re.fullmatch(r'v\d+\.\d+\.\d+', args.release_tag)
            or not re.fullmatch('[0-9a-f]{40}', args.expected_commit)
            or not re.fullmatch('[a-zA-Z0-9][a-zA-Z0-9._-]{0,95}', args.snapshot)
            or not re.fullmatch('[0-9a-f]{64}', args.sha256)
            or not re.fullmatch(r'none|[0-9a-f]{64}', args.previous_sha256)):
        raise ValueError('exact release, source, snapshot and publication digests are required')
    name = 'oscmix-repositories-' + args.snapshot + '.tar.gz'
    release = json.loads(SITE.BUILDER.run([
        args.gh, 'release', 'view', args.release_tag, '--repo', SITE.PROJECT,
        '--json', 'tagName,assets']))
    assets = [row for row in release['assets'] if row['name'] == name]
    if (release['tagName'] != args.release_tag or len(assets) != 1
            or not 0 < assets[0]['size'] <= SITE.MAX_BYTES):
        raise ValueError('release lacks the selected bounded repository archive')
    args.output.mkdir(parents=True, exist_ok=False)
    SITE.BUILDER.run([args.gh, 'release', 'download', args.release_tag, '--repo', SITE.PROJECT,
                      '--pattern', name, '--dir', args.output])
    SITE.BUILDER.run([args.gh, 'release', 'download', args.release_tag, '--repo', SITE.PROJECT,
                      '--pattern', 'release-manifest.json', '--dir', args.output])
    release_manifest = args.output / 'release-manifest.json'
    SITE.authenticate_manifest(release_manifest, args.release_tag, args.expected_commit, args.gh)
    identity = json.loads(release_manifest.read_text())
    if (identity['desk_commit'] != args.expected_commit
            or 'v' + identity['version'] != args.release_tag):
        raise ValueError('release manifest does not identify the selected source and tag')
    options = SimpleNamespace(archive=args.output / name, sha256=args.sha256,
                              snapshot=args.snapshot, expected_commit=args.expected_commit,
                              development=False, public_key=args.public_key,
                              output=args.output / 'site', gh=args.gh,
                              release_inputs=None, trusted_root=None)
    verified = SITE.verify_archive(options)
    freshness(options.output, time.time())
    previous = transition(options.output, args.previous_sha256)
    result = dict(schema=1, archive=name, release_tag=args.release_tag,
                  verified=verified, transition=previous, deployed=False)
    (args.output / 'prepared.json').write_text(json.dumps(result, indent=2) + '\n')
    return result


def verify_remote(root):
    prepared = json.loads((root / 'prepared.json').read_text())
    site = root / 'site'
    checked = {}
    for name in sorted(SITE.regular_files(site)):
        path = site / name
        info, _ = remote(name, path.stat().st_size)
        if info['sha256'] != SITE.BUILDER.sha(path):
            raise ValueError('published bytes differ from verified input: ' + name)
        checked[name] = info
    conditional = {}
    for name, old in prepared['transition']['conditional_requests'].items():
        if old['sha256'] == checked[name]['sha256']:
            continue
        for header, key in [('If-None-Match', 'etag'), ('If-Modified-Since', 'last_modified')]:
            if not old[key]:
                raise ValueError('previous native entry point lacked HTTP validator: ' + key)
            info, _ = remote(name, (site / name).stat().st_size, headers={header: old[key]})
            if info['sha256'] != checked[name]['sha256']:
                raise ValueError('conditional request retained outdated native metadata')
            conditional[name + ':' + header] = 'changed bytes returned with HTTP 200'
    result = dict(schema=1, published_https=BASE.startswith('https://'),
                  archive_sha256=prepared['verified']['sha256'],
                  files=checked, conditional_requests=conditional, passed=True,
                  native_package_managers_tested=False)
    (root / 'https-verification.json').write_text(json.dumps(result, indent=2) + '\n')
    return result


def recheck(root):
    prepared = json.loads((root / 'prepared.json').read_text())
    freshness(root / 'site', time.time())
    prepared['transition'] = transition(
        root / 'site', prepared['transition']['previous'] or 'none')
    (root / 'prepared.json').write_text(json.dumps(prepared, indent=2) + '\n')
    return prepared


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    check = commands.add_parser('prepare')
    check.add_argument('--release-tag', required=True)
    check.add_argument('--expected-commit', required=True)
    check.add_argument('--snapshot', required=True)
    check.add_argument('--sha256', required=True)
    check.add_argument('--previous-sha256', required=True,
                       help='selected publication.json or none')
    check.add_argument('--public-key', required=True, type=Path)
    check.add_argument('--gh', default='gh')
    check.add_argument('--output', required=True, type=Path)
    for name in ('recheck', 'verify-remote'):
        deployed = commands.add_parser(name)
        deployed.add_argument('--prepared', required=True, type=Path)
    args = parser.parse_args()
    try:
        if args.command == 'prepare':
            result = prepare(args)
        else:
            result = (recheck(args.prepared) if args.command == 'recheck'
                      else verify_remote(args.prepared))
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError) as exc:
        parser.exit(1, 'repository publication refused: %s\n' % exc)
    print(json.dumps(result, indent=2))


if __name__ == '__main__':
    main()
