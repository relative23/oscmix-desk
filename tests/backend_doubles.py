"""Stand-ins for the backend: what was sent, and what a device answers."""

class RecordingBackend:
    """A backend that records the wire and never confirms anything.

    A double rather than a real socket because the property under test is
    *what was sent*, and specifically that a refused config sends
    nothing. A real socket can only show absence by waiting, which is a
    slow test that passes when the code is merely slow.
    """

    traits = None  # filled in below from the real table

    def __init__(self, reports=None):
        self.sent = []
        self.dumps = 0
        self._reports = reports

    def send(self, messages):
        self.sent.extend((p, t, tuple(a)) for p, t, a in messages)

    def request_dump(self):
        self.dumps += 1

    def listen(self):
        if self._reports is None:
            return None          # port taken: the mixer GUI case
        return ReplayListener(self, self._reports)

class ReplayListener:
    def __init__(self, backend, reports):
        self._backend = backend
        self._reports = reports
        self._done = False

    def messages(self, _timeout):
        if self._done:
            return
        self._done = True
        yield from self._reports(self._backend.sent)

    def close(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        self.close()

def echo_link_flags_only(sent):
    """What a device does promptly: report the stereo flags, nothing else.

    Enough to release the link barrier, which is what the real device
    does and what makes these tests take milliseconds instead of the
    1.5 s the barrier waits when nothing answers. Deliberately *not* an
    echo of everything -- confirming the rest is what
    `confirming_backend` is for, and a double that confirms by accident
    is how a test starts asserting a device nobody has.
    """
    return [(path, tags, args) for path, tags, args in sent
            if path.endswith("/stereo")]

class UnbindableBackend(RecordingBackend):
    """The receive port cannot be bound, and nobody holds it."""

    def listen(self):
        from oscmix_desk.errors import ReceivePortError
        raise ReceivePortError(
            13, "cannot bind the receive port UDP 80: Permission denied")

def echo_within_traits(sent):
    """Echo back what a backend with OSCMIX's traits would report.

    Not everything it was told. The first version of this double echoed
    the lot, including `/mix/<out>/playback/<pb>` -- which upstream never
    reports, measured, and declared as
    `backend.Traits.dumps_playback_matrix = False`. A double more
    capable than the thing it stands in for turns every test that uses
    it into a test of a device nobody has, and hides exactly the
    outcomes that only exist because of the limitation.
    """
    return [(path, tags, args) for path, tags, args in sent
            if not (path.startswith("/mix/") and "/playback/" in path)
            and path != "/refresh"]
