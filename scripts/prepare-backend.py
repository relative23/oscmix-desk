#!/usr/bin/env python3
"""Prepare an exact upstream revision plus the versioned backend/GTK patch series.

This only creates a new source directory. It neither installs nor activates
hardware. Existing worktrees and installed binaries remain outside this step.
"""

import argparse
import hashlib
import json
import os
import re
import subprocess
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def series_record(path):
    raw = path.read_bytes()
    record = json.loads(raw)
    if record.get('schema') != 1 or record.get('protocol') != 'ODK1':
        raise ValueError('unsupported backend series format or protocol')
    if not re.fullmatch(r'[a-f0-9]{40}', record.get('upstream', '')):
        raise ValueError('upstream must be a full commit SHA')
    if not record.get('patches'):
        raise ValueError('the coordinated backend requires its patch series')
    seen = set()
    for patch in record['patches']:
        name = patch['file']
        if name != Path(name).name or name in seen:
            raise ValueError('patch names must be unique basenames')
        seen.add(name)
        if hashlib.sha256((path.parent / name).read_bytes()).hexdigest() != patch['sha256']:
            raise ValueError('backend patch hash mismatch: ' + name)
    return dict(record, series_sha256=hashlib.sha256(raw).hexdigest())


def prepare(upstream, destination, series):
    """Archive the pinned commit, never a possibly modified upstream worktree."""
    record = series_record(series)
    upstream = upstream.resolve()
    destination = destination.resolve()
    actual = subprocess.check_output(
        ['git', '-C', str(upstream), 'rev-parse', '--verify',
         record['upstream'] + '^{commit}'], text=True).strip()
    if actual != record['upstream']:
        raise ValueError('upstream commit did not resolve exactly')
    if destination.exists() and (not destination.is_dir() or any(destination.iterdir())):
        raise ValueError('backend destination must be new or empty')
    destination.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryFile() as archive:
        subprocess.run(['git', '-C', str(upstream), 'archive', '--format=tar', actual],
                       check=True, stdout=archive)
        archive.seek(0)
        subprocess.run(['tar', '-xf', '-', '-C', str(destination)],
                       check=True, stdin=archive)
    # Apply outside any enclosing desk repository. Otherwise git apply can
    # interpret paths against that repository instead of this exported tree.
    environment = dict(os.environ, GIT_CEILING_DIRECTORIES=str(destination.parent))
    for name in ('GIT_DIR', 'GIT_WORK_TREE', 'GIT_INDEX_FILE'):
        environment.pop(name, None)
    for patch in record['patches']:
        path = str((series.parent / patch['file']).resolve())
        for options in (['--check'], []):
            subprocess.run(['git', 'apply', *options, path], cwd=destination,
                           env=environment, check=True)
    record['source_sha256'] = {
        str(path.relative_to(destination)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(destination.rglob('*')) if path.is_file()}
    (destination / '.oscmix-desk-source.json').write_text(json.dumps(record, indent=2) + '\n')
    return record


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--upstream', type=Path, required=True,
                        help='local git checkout containing the pinned upstream commit')
    parser.add_argument('--destination', type=Path, required=True, help='new or empty directory')
    parser.add_argument('--series', type=Path, default=ROOT / 'patches/backend-series.json')
    args = parser.parse_args()
    record = prepare(args.upstream, args.destination, args.series)
    print(json.dumps({key: record[key] for key in ('upstream', 'protocol', 'series_sha256')}))


if __name__ == '__main__':
    main()
