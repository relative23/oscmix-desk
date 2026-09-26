#!/usr/bin/python3 -I
"""Current-key verification for the explicitly subscribed project repository.

This is package-manager integration, not part of the mixer runtime. It never
downloads keys or metadata and never consults a user's OpenPGP trust database.
"""

import argparse
import json
import re
import stat
import subprocess
import sys
import tempfile
import time
from pathlib import Path

CONFIG = Path('/etc/oscmix-desk/repository.json')
CERTIFICATE = Path('/usr/share/keyrings/oscmix-desk-repository.asc')
ENVIRONMENT = {'PATH': '/usr/bin:/bin:/usr/sbin:/sbin', 'LC_ALL': 'C'}
MAX_METADATA = 10 * 1024 * 1024


def trusted_file(path):
    """The administrator owns both the trust anchor and its configuration."""
    for current in (path, *path.parents):
        info = current.lstat()
        if info.st_uid != 0 or info.st_mode & 0o022 or stat.S_ISLNK(info.st_mode):
            raise ValueError('untrusted repository configuration path: ' + str(current))
    if not path.is_file():
        raise ValueError('not a repository configuration file: ' + str(path))
    return path.read_bytes()


def signature_status(status, primary, now):
    """gpgv exit zero alone accepts expired and revoked signing subkeys."""
    records = [line.split()[1:] for line in status.splitlines()
               if line.startswith('[GNUPG:] ')]
    codes = [record[0] for record in records if record]
    bad = {'EXPSIG', 'EXPKEYSIG', 'REVKEYSIG', 'BADSIG', 'ERRSIG', 'NO_PUBKEY',
           'NODATA', 'FAILURE', 'ERROR', 'BADARMOR', 'UNEXPECTED'}
    if codes.count('GOODSIG') != 1 or codes.count('VALIDSIG') != 1 or bad.intersection(codes):
        raise ValueError('metadata has no single current, unrevoked signature')
    # KEYEXPIRED can refer to a different, unused subkey. EXPKEYSIG identifies
    # an expired signer; refusing every KEYEXPIRED would break safe rotation.
    valid = next(record for record in records if record and record[0] == 'VALIDSIG')
    if (len(valid) != 11 or valid[10] != primary or valid[8] != '8'
            or not re.fullmatch(r'[0-9A-F]{40}', valid[1])):
        raise ValueError('metadata signature does not match the project key/SHA256 policy')
    created, expires = int(valid[3]), int(valid[4])
    if created > now + 300 or (expires and expires <= now):
        raise ValueError('metadata signature is not currently valid')
    return valid[1]


def verify(signature, source=None):
    config = json.loads(trusted_file(CONFIG))
    primary = config.get('primary', '')
    if config.get('schema') != 1 or not re.fullmatch(r'[0-9A-F]{40}', primary):
        raise ValueError('invalid project repository trust configuration')
    certificate = trusted_file(CERTIFICATE)
    if not certificate.startswith(b'-----BEGIN PGP PUBLIC KEY BLOCK-----\n'):
        raise ValueError('project repository certificate is not public ASCII armor')
    for path in (signature, *([source] if source else [])):
        if not path.is_absolute() or not path.is_file() or path.stat().st_size > MAX_METADATA:
            raise ValueError('invalid repository metadata file: ' + str(path))
    with tempfile.TemporaryDirectory(prefix='oscmix-repository-') as directory:
        home = Path(directory)
        public = home / 'certificate.asc'
        public.write_bytes(certificate)
        keyring = home / 'public.gpg'
        subprocess.run(['gpg', '--batch', '--no-options', '--homedir', str(home),
                        '--output', str(keyring), '--dearmor', str(public)],
                       check=True, capture_output=True, timeout=15, env=ENVIRONMENT)
        result = subprocess.run(
            ['gpgv', '--homedir', str(home), '--keyring', str(keyring), '--status-fd', '1',
             str(signature), *([str(source)] if source else [])],
            check=True, capture_output=True, text=True, timeout=15, env=ENVIRONMENT)
        return signature_status(result.stdout, primary, int(time.time()))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--signature', required=True, type=Path)
    parser.add_argument('--source', type=Path)
    args = parser.parse_args()
    try:
        verify(args.signature, args.source)
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        print('oscmix-desk repository: verification refused: ' + str(exc), file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
