#!/usr/bin/python3 -I
"""Mandatory, repository-selected libzypp signature-check protocol, version 0."""

import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from verify import ENVIRONMENT, verify

MAX_FRAME = 4 * 1024 * 1024
PENDING = Path('/var/lib/oscmix-desk-repository/native-key.json')


def pending_key():
    return PENDING.exists() or PENDING.is_symlink()


def read_frame():
    message = bytearray()
    while len(message) <= MAX_FRAME:
        byte = sys.stdin.buffer.read(1)
        if not byte:
            raise ValueError('truncated libzypp frame')
        if byte == b'\0':
            header, _body = bytes(message).decode('utf-8').split('\n\n', 1)
            lines = header.lstrip('\n').splitlines()
            fields = {}
            for line in lines[1:]:
                key, value = line.split(':', 1)
                if key in fields or '\\' in value:
                    raise ValueError('unsupported libzypp signature-check header')
                fields[key] = value
            return lines[0], fields
        message.extend(byte)
    raise ValueError('oversized libzypp frame')


def answer(command, headers='', body=''):
    sys.stdout.write(command + '\n' + headers + '\n' + body + '\0')
    sys.stdout.flush()


def main():
    command, headers = read_frame()
    if command != 'PLUGINBEGIN' or headers.get('version') != '0':
        raise ValueError('unsupported libzypp signature-check protocol')
    if pending_key():
        raise ValueError('native project key update is incomplete; '
                         'repair with oscmix-repository enable as administrator')
    answer('PLUGINSETUP', 'sig_extension:.asc\n')
    while True:
        command, headers = read_frame()
        if command == '_DISCONNECT':
            answer('ACK')
            return
        if command != 'SIGCHECK':
            raise ValueError('unexpected libzypp signature-check command')
        try:
            verify(Path(headers['sig']), Path(headers['data']))
        except (OSError, ValueError, KeyError, subprocess.SubprocessError) as exc:
            answer('ERROR', body='oscmix-desk repository verification refused: ' + str(exc))
        else:
            answer('ACK')


def commit():
    # RPM key imports cannot occur in an RPM scriptlet. libzypp's commit-end
    # notification follows that transaction; it changes only an explicitly
    # pending project certificate, never keys downloaded from a repository.
    while True:
        command, _headers = read_frame()
        if command not in ('PLUGINBEGIN', 'COMMITBEGIN', 'COMMITEND', 'PLUGINEND', '_DISCONNECT'):
            raise ValueError('unexpected libzypp commit command')
        if command in ('COMMITEND', 'PLUGINEND') and pending_key():
            subprocess.run(['/usr/bin/python3', '-I',
                            '/usr/lib/oscmix-desk-repository/configure.py', 'native-key-sync'],
                           check=True, capture_output=True, timeout=45, env=ENVIRONMENT)
        answer('ACK')
        if command in ('PLUGINEND', '_DISCONNECT'):
            return


if __name__ == '__main__':
    try:
        if Path(sys.argv[0]).name == 'oscmix-desk-key-update':
            commit()
        else:
            main()
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        print('oscmix-desk libzypp integration: ' + str(error), file=sys.stderr)
        sys.exit(1)
