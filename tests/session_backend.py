#!/usr/bin/env python3
"""Process fixture for session lifecycle tests; never opens MIDI, USB or PCM.

The real entry point launches this in place of alsaseqio. It publishes simulated
process/device metadata only after its actual local control socket is listening.
The peer credentials still identify this real subprocess. C backend semantics
are independently tested by backend_control.py against simulated MIDI pipes.
"""

import ctypes
import json
import os
import signal
import socket
import struct
import sys
import time
from pathlib import Path

sys.path[:0] = [os.environ['STUB_TESTS_DIR'], os.environ['STUB_SOURCE_DIR']]
from control_peer import REFRESH, REPLY, WRITE, ScriptedControl  # noqa: E402
from support import control_owner  # noqa: E402


def main():
    # Linux/musl too: the current process's libc, not a glibc filename.
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(1, signal.SIGKILL) != 0:
        raise OSError(ctypes.get_errno(), 'cannot arm fixture parent-death signal')
    if os.getppid() == 1:
        return
    root = Path(os.environ['STUB_DIR'])
    proc = Path(os.environ['OSCMIX_PROC_ROOT'])
    args = sys.argv[1:]
    assert args[:2] == ['-x', '42:1']
    assert args[3] == '-c'
    endpoint = Path(args[4])
    running = [True]
    if os.environ.get('STUB_IGNORE_TERM'):
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
    else:
        signal.signal(signal.SIGTERM, lambda *_: running.__setitem__(0, False))

    with (root / 'datagrams.hex').open('a') as traffic, \
            (root / 'requests.jsonl').open('a') as requests:
        class LoggedPeer(ScriptedControl):
            def emit(self, kind, request=0, code=0, sequence=None, payload=b''):
                if kind & REPLY:
                    request_kind = kind & ~REPLY
                    pid, _, _ = struct.unpack('3i', self.peer.getsockopt(
                        socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize('3i')))
                    requests.write(json.dumps([pid, request_kind, code]) + '\n')
                    requests.flush()
                    if request_kind == WRITE and code == 0:
                        traffic.write(self.writes[-1].hex() + '\n')
                        traffic.flush()
                    elif request_kind == REFRESH and code == 0:
                        traffic.write(b'/refresh\0\0\0\0,\0\0\0'.hex() + '\n')
                        traffic.flush()
                        # A fixture's simulated register state, not hardware evidence.
                        latest = {raw.split(b'\0', 1)[0]: raw for raw in self.writes}
                        self.reports = list(latest.values())
                super().emit(kind, request, code, sequence, payload)

        peer = LoggedPeer(endpoint, echo=True)
        try:
            control_owner(proc, endpoint, os.getpid(), 42, parent=os.getppid())
            # The session is visible so another start may not clean up this backend.
            parent = proc / str(os.getppid())
            parent.mkdir(exist_ok=True)
            (parent / 'cmdline').write_bytes(b'python3\0oscmix-session\0')
            (root / 'argv.json').write_text(json.dumps(args))
            (root / 'pid').write_text(str(os.getpid()))
            while running[0] and peer.error is None:
                time.sleep(.05)
        finally:
            peer.close()
            endpoint.unlink()


if __name__ == '__main__':
    main()
