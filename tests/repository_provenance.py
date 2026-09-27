"""Check publication provenance with actual released inputs and offline attestations.

Requires a disposable root tool container, gh, public release assets under
INPUTS/vVERSION and a separately obtained gh attestation trusted-root export.
Only disposable GnuPG keys are generated; no credentials or hardware are needed.
"""

import argparse
import importlib.util
import json
import os
import shutil
import subprocess
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace

from repository_fixtures import TestKey

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location('repository_site',
                                            ROOT / 'scripts/prepare-repository-site.py')
SITE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(SITE)


def exercise(args, private):
    args.output.mkdir(parents=True, exist_ok=False)
    root = args.output / 'public'
    root.mkdir()
    key = TestKey(private, 'provenance', int(time.time()) - 60)
    certificate = root / 'certificate.asc'
    key.export(certificate)
    packages = {}
    inputs = args.release_inputs / args.tag
    for manifest in sorted(inputs.glob('*')):
        if not manifest.name.endswith(('.rpm.json', '.deb.json')):
            continue
        record = json.loads(manifest.read_text())
        if record['development'] or record['dirty']:
            raise ValueError('this probe requires actual published release artifacts')
        release = dict(line.split('=', 1) for line in record['os_release'].splitlines()
                       if '=' in line)
        platform = release['ID'].strip('"') + release['VERSION_ID'].strip('"')
        target = next(name for name, fields in SITE.BUILDER.TARGETS.items()
                      if fields[0] == platform)
        artifact = SITE.BUILDER.relative_file(inputs, record['artifact'])
        assert SITE.BUILDER.sha(artifact) == record['sha256']
        signed = private / artifact.name
        shutil.copyfile(artifact, signed)
        if record['format'] == 'rpm':
            with tempfile.TemporaryDirectory(dir=private) as temporary:
                options = SimpleNamespace(public_key=certificate, gnupghome=key.home,
                                          signing_key=key.signing, passphrase_file=key.password)
                SITE.BUILDER.Signer(options, Path(temporary)).rpm(signed)
        digest = SITE.BUILDER.sha(signed)
        destination = root / target / 'pool' / digest / artifact.name
        destination.parent.mkdir(parents=True)
        shutil.copyfile(signed, destination)
        provenance = root / target / 'provenance' / manifest.name
        provenance.parent.mkdir(exist_ok=True)
        shutil.copyfile(manifest, provenance)
        row = dict(path=str(destination.relative_to(root / target)), sha256=digest,
                   unsigned_sha256=record['sha256'],
                   build_manifest=str(provenance.relative_to(root / target)))
        packages.setdefault(target, []).append((row, record, True))

    def authenticate(selected):
        with tempfile.TemporaryDirectory(dir=private) as temporary:
            return SITE.authenticate_builds(root, selected, certificate, args.gh, Path(temporary),
                                             args.release_inputs, args.trusted_root)

    accepted = authenticate(packages)
    assert accepted == sum(len(rows) for rows in packages.values())
    assert accepted >= 2
    result = dict(schema=1, result='passed', release_tag=args.tag,
                  actual_release_input_probe=True, final_release_payloads=False,
                  production_key_used=False, hardware=False, published_https=False,
                  offline_attestation_verification=True,
                  gh_version=SITE.BUILDER.run([args.gh, '--version']).splitlines()[0],
                  gh_sha256=SITE.BUILDER.sha(args.gh),
                  trusted_root_sha256=SITE.BUILDER.sha(args.trusted_root),
                  bundle_sha256=SITE.BUILDER.sha(inputs / 'attestation.jsonl'),
                  authenticated_build_manifests=accepted, checks=[], inputs={
                      record['artifact']: dict(sha256=record['sha256'],
                                                source_commit=record['source_commit'])
                      for rows in packages.values() for _, record, _ in rows}, scripts={
                      name: SITE.BUILDER.sha(ROOT / name) for name in
                      ('scripts/build-repository.py', 'scripts/prepare-repository-site.py',
                       'tests/repository_provenance.py')})

    def refuse(label, selected):
        try:
            authenticate(selected)
        except (ValueError, subprocess.CalledProcessError) as error:
            result['checks'].append(dict(check=label, refused=True, error=str(error)))
        else:
            raise AssertionError(label + ' was accepted')

    target = next(name for name in packages if SITE.BUILDER.TARGETS[name][1] == 'deb')
    row, record, _ = packages[target][0]
    selected = {target: [(row, record, True)]}
    manifest = root / target / row['build_manifest']
    # A genuine release-build attestation is not an independent repository
    # publication proof, even when its bytes and project identity are valid.
    try:
        SITE.BUILDER.verify_publication_provenance(
            manifest, record['source_commit'], args.gh,
            inputs / 'attestation.jsonl', args.trusted_root)
    except subprocess.CalledProcessError:
        result['checks'].append(dict(check='release-attestation-is-not-publication-proof',
                                     refused=True))
    else:
        raise AssertionError('wrong workflow accepted as publication proof')
    original = manifest.read_bytes()
    manifest.write_bytes(original + b' ')
    refuse('altered-attested-manifest', selected)
    manifest.write_bytes(original)
    changed = dict(record, source_commit='0' * 40)
    refuse('wrong-source-commit', {target: [(row, changed, True)]})
    wrong_tag = args.release_inputs / 'v0.0.0'
    wrong_tag.mkdir()
    shutil.copyfile(inputs / 'attestation.jsonl', wrong_tag / 'attestation.jsonl')
    refuse('wrong-source-tag', {target: [(row, dict(record, version='0.0.0'), True)]})
    artifact = root / target / row['path']
    original = artifact.read_bytes()
    artifact.write_bytes(original + b'changed')
    refuse('altered-deb-after-build', selected)
    artifact.write_bytes(original)

    target = next(name for name in packages if SITE.BUILDER.TARGETS[name][1] == 'rpm')
    row, record, _ = packages[target][0]
    selected = {target: [(row, record, True)]}
    artifact = root / target / row['path']
    original = artifact.read_bytes()
    other = root / target / packages[target][1][0]['path']
    artifact.write_bytes(other.read_bytes())
    refuse('validly-signed-different-rpm-payload', selected)
    artifact.write_bytes((inputs / record['artifact']).read_bytes())
    refuse('unsigned-rpm-with-authenticated-payload', selected)
    artifact.write_bytes(original)
    cached = inputs / record['artifact']
    original = cached.read_bytes()
    cached.write_bytes(original + b'changed')
    refuse('altered-original-rpm-download', selected)
    cached.write_bytes(original)
    (args.output / 'result.json').write_text(json.dumps(result, indent=2) + '\n')
    print(json.dumps(result, indent=2))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--release-inputs', type=Path, required=True)
    parser.add_argument('--trusted-root', type=Path, required=True)
    parser.add_argument('--gh', type=Path, required=True)
    parser.add_argument('--tag', required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    if not Path('/.dockerenv').is_file() or os.getuid() != 0:
        parser.error('requires an isolated root tool container')
    with tempfile.TemporaryDirectory(prefix='repository-provenance-') as private:
        try:
            exercise(args, Path(private))
        finally:
            for home in Path(private).iterdir():
                if home.is_dir():
                    subprocess.run(['gpgconf', '--homedir', str(home), '--kill', 'gpg-agent'],
                                   check=False, capture_output=True, timeout=10)


if __name__ == '__main__':
    main()
