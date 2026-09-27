# 0029 -- Observe the whole delivery before authorizing dependent writes

## Status

Accepted for 0.8.0 implementation; final integration and hardware qualification
are still pending.

## Decision

The pure `Observation` primitive keeps the fixed expectations separately from
the latest classification. A decoded nonmatch, including an invalid value,
revokes a match. A later match restores it. Missing reports remain distinct
from contradicted expectations; malformed packets do not invent observations.

The foreground link barrier and general verifier share that rule. Link sync
uses the verifier's completed result, replacing its independent callback state.
This removes both the destructive pending-link dictionary and a writer invoked
inside the decode loop. In particular, A=1, B=1, A=0 in one OSC bundle cannot
release the mix after B. Separate packets are not an atomic hardware snapshot.

The verifier still issues one refresh per attempt. Waiting until its observation
window ends can delay the background mix reapply by the existing verification
timeout on incomplete dumps; the initial foreground apply retains its own
barrier. There is no extra dump, transport or persistent observation cache.
Each retry starts without confirmations from a previous window.

Wrong links prevent dependent writes. The bounded PIN repair may resend links,
but it requires fresh link confirmation before writing the matrix. Silence or
a busy receiver cannot undo a known contradiction. Pure silence retains the
pinned backend's documented settle/reapply behavior and is never reported as
confirmation. Cancellation stops dependent writes.

This amends ADR 0025: a receive failure during a barrier now stops the apply
with `WriteFailed`, listing the links already sent and the remaining paths.
A profile marker is not changed for that incomplete apply. A failed read-back
also propagates without blind reapply. Treating a failed observation as silence
could otherwise erase knowledge of a contradiction before continuing writes.
A receive failure after a completed profile apply still means applied/unverified.

## Software evidence

`tests/test_observation_windows.py` exercises revocation and reconfirmation for
both link values, separate deliveries and bundles, invalid/empty values,
timeouts, cancellation, receive errors, repair with missing fresh evidence,
and the actual foreground/background write paths. The original regressions
fail against the unchanged 0.7.3 runtime. These are simulated I/O results,
not claims about a newly measured audible hardware effect.

## Encoded delivery correction during N7/N9 review

The shared observation rule alone did not cover malformed wire data. The
Python receiver still skipped an undecodable message, and bundle traversal
silently stopped at a truncated element. A=1, B=1 followed by a truncated
A=0 therefore let both the foreground apply and later mix repair write from
the remaining valid prefix. Nine production-client regressions reproduce
this on the pre-correction 0.8.0 development source with simulated peers.

OSC framing and every contained message are now decoded before any report
in the delivery reaches an observer. Bad bundle sizes, missing arguments,
invalid padding and trailing data refuse the delivery and invalidate the
control connection. The existing partial-apply mapping preserves the sent
link paths and withholds the pending matrix. Measurement tools share this
pure decoder and cannot record a valid prefix from a damaged delivery.
Well-formed but invalid register values still use revocation and later
reconfirmation through `Observation`; they are a different case from a
delivery whose remaining reports cannot be decoded.
