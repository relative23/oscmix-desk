"""A route, a config, and a fake oscmix that answers a dump: what the
tests of the apply and of the mix re-apply share."""

import socket
import threading
import time

from support import osc_bundle


def make_route(session_mod, **kwargs):
    defaults = dict(name="monitors", playback=(1, 2), output=(5, 6),
                    level=0.0, volume=None, stereo=True)
    defaults.update(kwargs)
    return session_mod.Route(**defaults)

def make_config(session_mod, routes, port, recv_port):
    return session_mod.Config(routes=routes, osc_port=port,
                              osc_recv_port=recv_port)

class DumpingOscmix(threading.Thread):
    """Records every write and answers /refresh with a canned dump.

    Whatever the dump contains, oscmix's link state is only correct once
    it has reported ``/output/<n>/stereo`` -- which is what the mix
    re-apply hangs off.
    """

    def __init__(self, session_mod, send_port, recv_port, dump):
        super().__init__(daemon=True)
        self.session_mod = session_mod
        self.recv_port = recv_port
        self.dump = dump
        self.order = []
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind(("127.0.0.1", send_port))
        self.sock.settimeout(0.2)
        self.stopping = threading.Event()

    def stop(self):
        self.stopping.set()

    def drain(self, quiet=0.3, limit=5.0):
        """Wait until no further datagram arrives for ``quiet`` seconds.

        UDP sends return immediately, so stopping the moment
        verify_and_repair() returns would race the last datagrams and
        make the order assertions flaky.
        """
        deadline = time.monotonic() + limit
        seen = -1
        while time.monotonic() < deadline:
            if len(self.order) == seen:
                return
            seen = len(self.order)
            time.sleep(quiet)

    def run(self):
        while not self.stopping.is_set():
            try:
                data, _ = self.sock.recvfrom(65536)
            except socket.timeout:
                continue          # keep listening past the verify window
            except OSError:
                return
            for message in self.session_mod.iter_osc_messages(data):
                try:
                    path, _tags, _args = self.session_mod.decode_osc(message)
                except ValueError:
                    continue
                self.order.append(path)
                if path == "/refresh":
                    self.sock.sendto(osc_bundle(self.dump),
                                     ("127.0.0.1", self.recv_port))
