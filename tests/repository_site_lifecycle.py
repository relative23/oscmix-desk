"""Exercise the complete site with public fixtures signed by disposable test keys.

Run after repository_fixtures.py has generated all five targets in one isolated
tool container. This does not publish, use production keys or claim HTTPS testing.
"""

import argparse
import importlib.util
import json
import os
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location('repository_site',
                                            ROOT / 'scripts/prepare-repository-site.py')
SITE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(SITE)


def exercise(args):
    args.output.mkdir(parents=True, exist_ok=False)
    first_manifest = args.fixtures / 'debian13/first/repository.json'
    commit = json.loads(first_manifest.read_text())['source_commit']
    record = dict(schema=1, source_commit=commit, development=True, production_key_used=False,
                  published_https=False, scripts={
                      name: SITE.BUILDER.sha(ROOT / name) for name in
                      ('scripts/prepare-repository-site.py', 'scripts/build-repository.py',
                       'tests/repository_site_lifecycle.py')}, checks=[])
    for name, certificate in [('first', 'original'), ('second', 'original'),
                              ('client-only', 'original'), ('renewed', 'original'),
                              ('rotated', 'rotated')]:
        channels = args.output / (name + '-inputs')
        channels.mkdir()
        for target in SITE.BUILDER.TARGETS:
            shutil.copytree(args.fixtures / target / name, channels / target)
        public_key = args.fixtures / 'certificates' / (certificate + '.asc')
        options = SimpleNamespace(channels=channels, public_key=public_key,
                                  snapshot=name, expected_commit=commit, development=True,
                                  output=args.output / (name + '.tar.gz'))
        staged = SITE.stage(options)
        options.output = args.output / (name + '-repeated.tar.gz')
        repeated = SITE.stage(options)
        assert repeated['sha256'] == staged['sha256']
        options.archive, options.sha256 = options.output, staged['sha256']
        options.output = args.output / (name + '-verified')
        verified = SITE.verify_archive(options)
        assert verified['signatures_and_files_verified'] is True
        assert len(list(options.output.glob('*/repository.json'))) == 5
        record['checks'].append(dict(snapshot=name, reproducible=True,
                                     sha256=staged['sha256'], signatures_and_files_verified=True))

    options.channels = args.output / 'first-inputs'
    options.snapshot, options.public_key = 'first', args.fixtures / 'certificates/original.asc'
    for label, development, key in [('production-refuses-development', False, 'original'),
                                     ('wrong-key', True, 'foreign'),
                                     ('revoked-key', True, 'revoked')]:
        options.output = args.output / (label + '.tar.gz')
        options.development = development
        options.public_key = args.fixtures / 'certificates' / (key + '.asc')
        try:
            SITE.stage(options)
        except (ValueError, subprocess.CalledProcessError):
            assert not options.output.exists()
        else:
            raise AssertionError(label + ' was accepted')
        record['checks'].append(dict(check=label, refused_before_publication=True))

    options.development, options.public_key = True, args.fixtures / 'certificates/original.asc'
    options.output = args.output / 'altered-last-channel.tar.gz'
    altered = options.channels / 'opensuse16/repository.json'
    altered.write_bytes(altered.read_bytes() + b' ')
    try:
        SITE.stage(options)
    except subprocess.CalledProcessError:
        assert not options.output.exists()
    else:
        raise AssertionError('altered last channel was accepted')
    record['checks'].append(dict(check='altered-last-channel', refused_before_publication=True))
    record['result'] = 'passed'
    (args.output / 'result.json').write_text(json.dumps(record, indent=2) + '\n')
    print(json.dumps(record, indent=2))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--fixtures', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    if not Path('/.dockerenv').is_file() or os.getuid() != 0:
        parser.error('requires the isolated root tool container used for repository fixtures')
    exercise(args)


if __name__ == '__main__':
    main()
