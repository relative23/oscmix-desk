"""Shared constants and environment overrides.

Values live next to nothing else on purpose: every module may import
this one, so it must never import back."""

from __future__ import annotations

import math
import os

__version__ = "0.6.9"

DEFAULT_DEVICE_NAME = "Fireface UCX II"
DEFAULT_USB_ID = "2a39:3fd9"
DEFAULT_OSC_PORT = 7222
DEFAULT_OSC_RECV_PORT = 8222
DEFAULT_DEVICE_TIMEOUT = 30.0
PORT_READY_TIMEOUT = 10.0
CHILD_STOP_GRACE = float(os.environ.get("OSCMIX_STOP_GRACE", "5"))
# Bind the receive port, then wait this long before asking for a dump.
#
# Upstream writes to a *connected* UDP socket, and `writeosc` in main.c
# ignores ECONNREFUSED. While nothing is bound on the receive port, each
# of the ~880 meter datagrams a second draws an ICMP port-unreachable
# that Linux queues as a pending socket error, and the next write fails
# with exactly that errno -- silently, by design, because the normal case
# is "the GUI is not running and we do not care".
#
# So the first datagram after a bind is spent absorbing that error. And
# the first datagram after a /refresh is the one thing setrefresh()
# flushes by hand: every /playback/<n>/stereo, all twenty, in one bundle.
# Bind and ask immediately and they are gone.
#
# Measured on a UCX II, twelve trials per gap: 0.0 s delivered them 4
# times out of 12, 0.1 s and 0.3 s twelve out of twelve. The mechanism
# needs one datagram, which at the meter rate is about 1.1 ms, so 0.1 s
# is roughly ninety times what it takes -- a margin, not a tuned value.
#
# Nothing noticed for two releases because /playback/* was wrongly
# classified as never-reported, so losing it was not counted as a
# problem. Fixing that classification is what made this visible.
DUMP_LISTEN_SETTLE = float(os.environ.get("OSCMIX_DUMP_SETTLE", "0.1"))

VERIFY_TIMEOUT = 10.0
VERIFY_SETTLE = 0.5
# oscmix only learns that an output pair is stereo-linked when the *device*
# echoes /output/<n>/stereo back over MIDI (newoutputstereo() in oscmix.c);
# the OSC setter is a plain setbool that forwards the register and leaves
# oscmix's own state untouched. A /mix write that overtakes that echo is
# evaluated against the stale flag, takes the unlinked branch in setlevel()
# and never writes the pair's right channel -- every even output stays
# silent. Link messages therefore go out first, and the mix matrix only
# after the echo arrived (or LINK_SETTLE elapsed, when nobody can listen).
#
# The echo only fires on an actual *change*: writing stereo=1 to a pair
# the device already has linked changes nothing and stays silent. The
# barrier below is therefore opportunistic -- short, and a timeout is
# normal rather than an error. What closes the gap for good is that
# oscmix reports every register once its initial sync completes (measured
# on a UCX II: /output/1..12/stereo arrive ~15 s after start, far too late
# to block readiness on). So the mix is written twice: immediately, so
# audio works, and again after that sync, when oscmix's link state is
# guaranteed correct.
LINK_ECHO_TIMEOUT = float(os.environ.get("OSCMIX_LINK_TIMEOUT", "1.5"))
LINK_SETTLE = float(os.environ.get("OSCMIX_LINK_SETTLE", "1.5"))
# Used when the receive port is taken and the sync cannot be observed --
# which is the *normal* desktop case, because oscmix-gtk holds that port
# whenever the mixer window is open. The session cannot see the dump, so
# it waits this long and then rewrites the mix from what it hopes is a
# synchronised link state.
#
# 20 -> 5 (2026-08-17), on a measurement rather than a guess. The 20 s
# came from the same unrecorded observation as the "15-20 s dump" figure.
# tests/data/cold-plug-timeline.json is that observation done properly:
# a real USB replug, captured on both OSC ports so a request can be told
# apart from a device push. The registers this wait exists for --
# /output/<n>/stereo -- came back 0.01 s after the /refresh that asked
# for them, 2.26 s after the backend started, and the whole dump was over
# by ~4 s with nothing further in the remaining 272 s.
#
# 5 s is still more than twice the measurement. It is not tuned to the
# number; it is the smallest round value that keeps a comfortable margin,
# because the cost of being too short (a mix rewritten against a stale
# link state) is worse than the cost of being too long (a few seconds
# before the routing is re-established).
#
# Raise it with OSCMIX_LINK_SYNC_DELAY if a slower device needs it, and
# say so -- that would mean this measurement does not generalise beyond
# a UCX II, which is worth knowing.
LINK_SYNC_BLIND_DELAY = float(os.environ.get("OSCMIX_LINK_SYNC_DELAY", "5"))

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
# holds until the next reload or start, which will apply something else.
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

# How long a SIGHUP reconcile waits for the start-up verifier before
# giving up on it. Both write the whole routing to one device, so they
# are serialised (see session._verifier_finished). The verifier's longest
# path is a full observation window, a re-apply and a second window:
# VERIFY_SETTLE + VERIFY_TIMEOUT + LINK_ECHO_TIMEOUT + VERIFY_SETTLE +
# VERIFY_TIMEOUT = 22.5 s; the blind path is about 6 s. 30 s covers the
# longer one with margin and is short enough that a reload during a
# shutdown does not hold the supervise loop for long.
RECONCILE_WAIT_FOR_VERIFIER = 30.0

# How long a writer waits for the device lock before it gives up. Every
# writer takes it: a switch, `--no-profile`, and the unit's own apply,
# verifier and reconcile (profiles.take_device_lock, ADR 0019). Two at
# once would interleave their link phases and mix writes on the wire.
# A switch holds it for at most a barrier, the writes and a read-back
# window -- about 13 s -- and the unit's start-up transaction for about
# 22 s, so 30 s covers one queued writer with margin; past that a switch
# refuses, which writes nothing, and that is the honest answer.
SWITCH_LOCK_WAIT = 30.0


def startup_budget(device_timeout: float = DEFAULT_DEVICE_TIMEOUT) -> float:
    """Worst-case seconds from process start to ``READY=1``.

    Five waits govern this path and two systemd deadlines have to
    contain it. The relationship used to live in a comment in the unit
    file, where nothing checked it and `--timeout` -- a command-line
    argument in `ExecStart` -- could push the start past
    `TimeoutStartSec` and have the unit killed *mid-apply*. That is a
    torn routing state reached by editing a number.

    The terms, in the order `run_session` reaches them:

    * ``device_timeout``    -- ``wait_for_device``
    * ``STALE_BACKEND_SETTLE`` -- ``_cleanup_stale_backend``
    * ``PORT_READY_TIMEOUT``   -- ``_await_backend_port``
    * ``SWITCH_LOCK_WAIT``  -- ``take_device_lock`` in
      ``_apply_and_verify``: the start has waited for the device lock
      since 0.6.5, and until 0.6.9 the budget did not say so
    * the link barrier      -- ``LINK_ECHO_TIMEOUT`` when the receive
      port is observable, ``LINK_SETTLE`` when the mixer GUI holds it.
      Never both, so the worst case is the larger.

    Verification is deliberately *not* in here: it runs on a daemon
    thread after ``READY=1``, which is the whole point of deferring it.
    """
    return (device_timeout
            + STALE_BACKEND_SETTLE
            + PORT_READY_TIMEOUT
            + SWITCH_LOCK_WAIT
            + max(LINK_ECHO_TIMEOUT, LINK_SETTLE))
