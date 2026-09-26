#!/usr/bin/python3 -I
"""Add the project key policy to APT's actual gpgv/sqv acquisition request.

The native verification method still decides every request. Only requests
with the project's explicit Signed-By certificate receive the extra check.
"""

import signal
import subprocess
import sys
from pathlib import Path
from urllib.parse import unquote

sys.path.insert(0, str(Path(__file__).resolve().parent))
from verify import CERTIFICATE, verify

MAX_FRAME = 1024 * 1024


def frame(stream):
    lines = []
    length = 0
    while True:
        line = stream.readline(MAX_FRAME + 1)
        if not line:
            if lines:
                raise ValueError('truncated APT method frame')
            return b''
        length += len(line)
        if length > MAX_FRAME:
            raise ValueError('oversized APT method frame')
        lines.append(line)
        if line == b'\n':
            return b''.join(lines)


def send(stream, message):
    stream.write(message)
    stream.flush()


def check_request(request):
    headers = {}
    for line in request.decode('utf-8').splitlines()[1:]:
        if ': ' in line:
            key, value = line.split(': ', 1)
            if key not in ('URI', 'Filename', 'Signed-By'):
                continue
            if key in headers:
                raise ValueError('duplicate APT verification header')
            headers[key] = value
    if str(CERTIFICATE) not in headers.get('Signed-By', '').split(','):
        return
    signature = Path(unquote(headers['URI'].split(':', 1)[1]))
    source = Path(headers['Filename'])
    verify(signature, None if signature == source else source)


def run(method):
    if method not in ('gpgv', 'sqv'):
        raise ValueError('invalid native APT verifier')
    with subprocess.Popen(['/usr/lib/apt/methods/' + method], stdin=subprocess.PIPE,
                          stdout=subprocess.PIPE) as native:
        try:
            initial = frame(native.stdout)
            if not initial.startswith(b'100 '):
                raise ValueError('native APT verifier did not announce its capabilities')
            send(sys.stdout.buffer, initial)
            while request := frame(sys.stdin.buffer):
                if not request.startswith(b'600 '):
                    send(native.stdin, request)
                    continue
                try:
                    check_request(request)
                except (OSError, ValueError, KeyError, subprocess.SubprocessError) as exc:
                    uri = next(line for line in request.splitlines() if line.startswith(b'URI: '))
                    message = str(exc).replace('\n', ' ').replace('\r', ' ')[:1000]
                    send(sys.stdout.buffer, b'400 URI Failure\n' + uri +
                         b'\nMessage: oscmix-desk repository verification refused: ' +
                         message.encode() + b'\nTransient-Failure: false\n\n')
                    continue
                send(native.stdin, request)
                while True:
                    response = frame(native.stdout)
                    if not response:
                        raise ValueError('native APT verifier exited before its result')
                    send(sys.stdout.buffer, response)
                    if response.startswith((b'201 ', b'400 ')):
                        break
        finally:
            native.stdin.close()
            try:
                native.wait(timeout=5)
            except subprocess.TimeoutExpired:
                native.kill()
                native.wait(timeout=5)


if __name__ == '__main__':
    # APT deliberately stops idle acquisition workers with SIGINT. The
    # interpreter's KeyboardInterrupt traceback is not a verification error.
    signal.signal(signal.SIGINT, signal.SIG_DFL)
    try:
        run(Path(sys.argv[0]).name.removeprefix('apt-'))
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        print('oscmix-desk APT integration: ' + str(error), file=sys.stderr)
        sys.exit(1)
