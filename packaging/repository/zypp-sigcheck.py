#!/usr/bin/python3 -I
"""Mandatory, repository-selected libzypp signature-check protocol, version 0."""

import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from verify import verify

MAX_FRAME = 65536


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


if __name__ == '__main__':
    try:
        main()
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        print('oscmix-desk libzypp integration: ' + str(error), file=sys.stderr)
        sys.exit(1)
