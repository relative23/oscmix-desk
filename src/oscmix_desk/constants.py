"""Shared constants and environment overrides.

Values live next to nothing else on purpose: every module may import
this one, so it must never import back."""

from __future__ import annotations

import math
import os

__version__ = "0.8.0"

DEFAULT_DEVICE_NAME = "Fireface UCX II"
DEFAULT_USB_ID = "2a39:3fd9"
DEFAULT_OSC_PORT = 7222
DEFAULT_OSC_RECV_PORT = 8222
DEFAULT_DEVICE_TIMEOUT = 30.0
PORT_READY_TIMEOUT = 10.0
CHILD_STOP_GRACE = float(os.environ.get("OSCMIX_STOP_GRACE", "5"))
VERIFY_TIMEOUT = 10.0
VERIFY_SETTLE = 0.5
# oscmix only learns that an output pair is stereo-linked when the *device*
# echoes /output/<n>/stereo back over MIDI (newoutputstereo() in oscmix.c);
# the OSC setter is a plain setbool that forwards the register and leaves
# oscmix's own state untouched. A /mix write that overtakes that echo is
# evaluated against the stale flag, takes the unlinked branch in setlevel()
# and never writes the pair's right channel -- every even output stays
# silent. Link messages therefore go out first, and the mix matrix only
# after the bounded barrier has processed each complete delivery. A known
# contradiction refuses the mix phase. Missing reports are distinct: an
# unchanged link may produce no immediate echo. Where no retained-link
# dependency requires confirmation, that timeout can proceed, followed by
# the fresh sync window. Its latest observations decide whether mix repair
# is permitted; they do not establish permanent or playback-matrix state.
LINK_ECHO_TIMEOUT = float(os.environ.get("OSCMIX_LINK_TIMEOUT", "1.5"))
# ODK1 client limits. Acquisition precedes a lease whose total duration
# cannot be extended by keepalives: CONTROL_LEASE_TOTAL is the backend's
# protocol.LEASE_TOTAL, restated here for the unit's start budget below
# and held equal to it by tests/test_protocol.py.
CONTROL_ACQUIRE_TIMEOUT = 30.0
CONTROL_LEASE_TOTAL = 90.0
CONTROL_ACK_TIMEOUT = 2.0

LEVEL_MIN, LEVEL_MAX = -65.0, 6.0
# An unlinked pair route reaches oscmix's setlevel() branch that halves the
# gain (ll = vol / 2), so its request is raised by 6.02 dB to make `level`
# mean the same thing on both paths. oscmix clamps the gain it derives at
# 2.0 -- exactly this offset -- so an unlinked route cannot be pushed above
# unity: positive `level` values saturate instead of scaling.
UNLINKED_GAIN_OFFSET = 20.0 * math.log10(2.0)
CHANNEL_MIN, CHANNEL_MAX = 1, 64

#: The systemd user unit that supervises the backend. The launcher
#: starts it and the profile switch reloads it; one name, one place.
SERVICE_UNIT = "oscmix.service"

EXIT_OK = 0
EXIT_FAILURE = 1
EXIT_CONFIG = 2

# `--diff` found the device and the config disagreeing. A separate code
# because 1 already means "something went wrong", and a caller has to be
# able to tell "the desk drifted" from "the backend never answered" --
# those are opposite situations and the second one makes the first
# unknowable.
#
# Only `--diff` ever returns it. The service never runs that flag, so
# systemd never sees a 3; if it ever did, `Restart=on-failure` would
# treat it as a failure, which is the safe direction for a code that
# means "the state is not what was asked for".
EXIT_DIFFERS = 3

# A switch that reached the device but could not be recorded: the desk
# next reload uses the previous PIN state; a new session uses its starting values.
# Exit 0 would tell a provisioning script that the change is permanent,
# which is the one thing it is not.
EXIT_NOT_PERSISTED = 4

# The unit is running and refused the reload, so it may still be acting
# on the previous desk. Distinct from "not running", which is fine.
EXIT_RELOAD_FAILED = 5

# How long the session waits for the background verifier to stop before
# exiting anyway. The verifier checks for a stop between every phase and
# before every write, so it normally returns within one socket timeout
# (0.25 s); this is the bound on being wrong about that. Together with
# CHILD_STOP_GRACE it has to fit inside the unit's TimeoutStopSec.
VERIFIER_STOP_GRACE = 2.0

# The pause _cleanup_stale_backend takes after signalling a leftover
# backend, so the port it held is free before the new one binds.
STALE_BACKEND_SETTLE = 0.5

# How long SIGHUP waits for startup verification before logging a skipped
# reconcile (reload._verifier_finished). Startup and later selective
# reconciliation must not overlap. A timeout requires another explicit
# reload; the backend's hard lease deadline separately bounds each operation.
RECONCILE_WAIT_FOR_VERIFIER = 30.0

# How long a writer waits for the device lock before it gives up. Every
# writer takes it: a switch, `--no-profile`, and the unit's own apply,
# verifier and reconcile (locking.take_device_lock). Two at
# once would interleave their link phases and mix writes on the wire.
# This wait bounds contention rather than guaranteeing that a preceding
# operation finishes in time. The lock and backend lease span verification
# and repair too. If the wait expires, the new switch refuses without writes.
SWITCH_LOCK_WAIT = 30.0


def startup_budget(device_timeout: float = DEFAULT_DEVICE_TIMEOUT) -> float:
    """Bound foreground startup, including contention and the server lease.

    Device discovery, orphan cleanup, endpoint readiness and file-lock wait
    precede connecting/HELLO and lease acquisition. Once acquired, all write
    phases (including their link barrier) must finish within the server's
    hard lease limit. Include one last ACK timeout after that deadline.
    Background verification shares the same lease, after READY; it cannot
    extend this bound. Local process scheduling adds the unit's margin.
    """
    return (device_timeout + STALE_BACKEND_SETTLE + PORT_READY_TIMEOUT
            + SWITCH_LOCK_WAIT + CONTROL_ACQUIRE_TIMEOUT + CONTROL_LEASE_TOTAL
            + 3 * CONTROL_ACK_TIMEOUT)
