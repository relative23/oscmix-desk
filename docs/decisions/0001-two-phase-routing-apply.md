# 0001 -- Channel links are written before the mix matrix

**Status:** accepted (0.1.2, extended in 0.1.3)

## Decision

A routing is applied in two phases. First every `/playback/<n>/stereo`
and `/output/<n>/stereo`, then every `/mix/...` and `/output/<n>/volume`.
Never interleaved, never per route.

## Why

oscmix keeps its own copy of the link state and updates it **only** in
`newoutputstereo()` -- the handler for the *device reporting* the
register back. The OSC setter is a plain `setbool` that forwards the
value and leaves that copy alone.

A `/mix` write evaluated against a stale copy takes the unlinked branch
of `setlevel()`, which writes only the addressed output and never
`out+1`. Measured on a UCX II: outputs 1, 5 and 7 carried a mono sum,
outputs 2, 6 and 8 were digitally silent. Audibly: one working headphone
channel, one working monitor.

## What this rules out

Sending a route's messages as one burst, however natural that reads. The
`route_messages()` helper still exists for the dry run and the expected
register set, but `apply_routing()` must use `link_messages()` and
`mix_messages()` separately.

## Evidence

`/output/<n>/level` read off the wire with a left-only and a right-only
test tone, before and after. See the 0.1.2 entry in CHANGELOG.md.

## Amended in 0.6.10

`route_messages()` is gone; the dry run and the expected register set
use `link_messages()` and `mix_messages()` like the apply does.
