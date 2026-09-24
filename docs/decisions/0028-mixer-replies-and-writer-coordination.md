# 0028 -- Reply distribution and writer coordination are separate contracts

**Status:** accepted for 0.7.3; simultaneous read-back remains a future change.

## Context and evidence

At pinned upstream revision `f2fdd5ec78338848754aad32cc07f3440de63395`,
`main.c` opens one send destination. `gtk/mixer.c` binds its own receiver
without reuse and does not check the bind result. Two receivers on the
same port cannot both be assumed to receive a complete stream. Different
port numbers alone do not make the backend send to both consumers.

`tests/gtk_lifecycle.py` uses the real GTK executable, private settings,
an isolated display/bus and a simulated UDP backend. An accessible GTK
sample-rate label demonstrates actual receipt. GUI-first operation makes
desk's read-back unavailable; a selective reconcile sends nothing, while
an explicit profile switch reports applied/unverified. Closing GTK permits
a deliberate reconcile that preserves a differing REMEMBER fader. Desk-first
operation is refused by the launcher until the receiver is released.

## Decision for this release

Keep the backend pin and one receiver owner. Diagnose contention, document
close/read-back/reopen, and test recovery. Never kill the other owner, use
`SO_REUSEPORT` to split replies, infer success from an absent dump or replay
an earlier write request automatically. Port inspection before GTK launch
narrows a common failure case; it cannot reserve a socket upstream will bind.

## Requirements before a shared receiver is implemented

Prefer an upstream subscription protocol if it can be accepted and measured:
each consumer needs an explicit subscription identity, loopback endpoint,
expiry/unsubscribe semantics and device/backend epoch. Replies must be sent
to every current consumer; replacing a backend invalidates prior freshness.
Loss, partial dumps and restarts must remain visible. A packet arriving later
is not necessarily a newer hardware observation without producer sequencing.

A local distributor is an alternative when upstream cannot provide this
contract. It must exclusively own the configured reply socket, fan out to
distinct consumer endpoints, bound queues and expire subscribers, preserve
the OSC payload, and make missing/stale data explicit. Startup must establish
ownership before consumers write; shutdown must drain or cancel pending
operations and release subscriptions. The owner must be tied to the exact
device and backend instance, not merely a familiar UDP port. A distributor
must not advertise a cached dump as current after disconnect or replacement.
Loopback binding limits the network surface but does not authenticate users.

Neither option makes GTK/direct OSC commands take the desk's device lock.
Write coordination requires upstream cooperation or a separately designed
single write gateway with defined ordering, identity, failure and shutdown
semantics. It must not hold a lock for the lifetime of an interactive GUI.

A new daemon or changed backend revision therefore needs its own design,
protocol tests, lifecycle qualification and hardware evidence. Diagnostic
improvements in 0.7.3 do not claim that concurrent GUI writes are serialized
or that both clients now see every reply.
