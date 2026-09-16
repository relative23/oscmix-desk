# 0002 -- Verification and the mix re-apply share one `/refresh`

**Status:** accepted (0.1.2)

## Decision

The background pass issues a single `/refresh`. Its dump both verifies
the routing and, the moment it has reported every `/output/<n>/stereo`,
triggers the mix matrix to be written a second time.

## Why

Two facts, both measured on a UCX II:

* The device reports a register only when it **changes**. Writing
  `stereo = 1` to an already-linked pair produces no report, so waiting
  for an echo times out in the common case.
* oscmix does not sync its register cache on its own. It learns the
  device's values only from a dump, which streams for ~15-20 s -- far too
  late to block the readiness signal on.

So the mix is written twice: immediately, so audio works, and again from
a link state that is known to be correct.

Two dumps were tried first. They starve each other: verification
confirmed 5 registers instead of 8 when a second `/refresh` overlapped
the first.

## What this rules out

A separate "sync" step with its own `/refresh`, and any design where the
re-apply waits on something other than the verification dump.

## Amended in 0.6.10

The "15-20 s" dump was an unrecorded observation; the recorded one is
1.9 s for the 2252 registers a dump reports on a warm device (2322 with
the 70 streamed meters, `tests/data/refresh-dump.json` at the pin), and
the cold replug timeline is in `tests/data/cold-plug-timeline.json`
(ADR 0010, OSC-PROTOCOL.md).
