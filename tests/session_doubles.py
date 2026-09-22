"""Stand-ins for a backend process and its notifications, for the tests
of the session's start: the lifecycle, the start-up helpers and the
start-up transaction."""

import argparse


class RunningChild:
    """A backend that is still up, so a verifier may start."""

    def __init__(self, pid=202):
        self.pid = pid
        self.returncode = None
        self.terminated = False
        self.waited = None

    def poll(self):
        return self.returncode

    def terminate(self):
        self.terminated = True
        self.returncode = -15

    def wait(self, timeout=None):
        # Recorded rather than ignored: the grace the caller allows is
        # part of what a stop does.
        self.waited = timeout
        return self.returncode


def make_args(**overrides):
    values = dict(timeout=1.0, dry_run=False)
    values.update(overrides)
    return argparse.Namespace(**values)

class FakeChild:
    """A backend that is already finished."""

    def __init__(self, returncode=0):
        self.returncode = returncode
        self.terminated = False

    def poll(self):
        return self.returncode

    def terminate(self):
        self.terminated = True

class TimeoutExpired(Exception):
    """subprocess.TimeoutExpired, for the double that stands in for it."""


def ready_count(notifications):
    return notifications.count("READY=1")
