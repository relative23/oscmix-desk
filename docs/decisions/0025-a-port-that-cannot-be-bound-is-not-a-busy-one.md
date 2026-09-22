# 0025 -- A receive port that cannot be bound is not a busy one, and it never tears an apply

## Status

Accepted, 0.6.11.

## Context

`Backend.listen` returned `None` for every `OSError` from `bind`, and
`None` has one meaning to every caller: the mixer GUI holds the receive
port. That is a normal state with a designed answer -- the link barrier
waits blind, the verifier re-establishes the mix blind, a reconcile
stands down, a read tells the user to close the GUI.

An outside review named the catch as too broad. Measured here before
anything changed:

* `[osc] recv-port = 80` is a legal config (1..65535) and fails with
  `EACCES` for an ordinary user. `listen()` answered `None`, the journal
  said `routing verification skipped: UDP 80 in use (mixer GUI
  running?)` on every start, and `--diff` said `close the mixer GUI`.
  Nothing held the port, closing the GUI changed nothing, and the desk
  ran unverified for good.
* The review's other examples do not occur: the host is always
  `127.0.0.1`, and `bind` on it succeeds even in a network namespace
  whose loopback interface is down.

The review's remedy was to re-raise everything but `EADDRINUSE`. Taken
literally that is worse than the defect. `apply_routing` sends the link
writes and *then* crosses the barrier, which is where the port is bound:
an exception from there ends the apply between its phases, with the
pairs linked and no mix written -- the one state the two-phase apply
(ADR 0001) exists to prevent. And `--diff`, `--snapshot` and
`--dump-config` had no handler at all, so the release after the one
that turned tracebacks into exit codes would have added three.

## Decision

`listen()` returns `None` for `EADDRINUSE` and nothing else. Any other
failure to get the port, a socket that cannot be created included,
raises `ReceivePortError` -- an `OSError` carrying the original `errno`
and a `strerror` that names the port and the cause.

Every caller says what it does with it, and none lets it tear a write:

| Caller | What it does |
|---|---|
| the link barrier | logs the cause as an error, waits `LINK_SETTLE` blind exactly as for a held port, and the apply finishes |
| the start-up verifier | re-establishes the mix blind first, then lets the error on: the status line reads `verifier failed`, the journal `routing cannot be verified: ...` |
| a SIGHUP reconcile | stands down, as it does for a held port, naming the cause; status `reconcile skipped` |
| a profile switch or restore | applied, `applied-unverified`, and the outcome's reason is the cause |
| `--diff`, `--snapshot`, `--dump-config` | exit 1 with the cause, not "close the mixer GUI" |
| `sweep-writes.py`, `verify-hardware.py`, `record-dump.py` | exit 1 with the cause; the two that *skip* (77) do so only for a held port |

The start does not fail. The desk is applied and audible, which is what
a start promises (ADR 0021); what cannot happen is verification, and
that is reported as a failure of the verifier on every start rather
than as a skip.

An outcome nobody could read back is worded as one. `Outcome.read_back`
is false when the port was held or unbindable (and when the caller
asked for no read-back, which keeps its own wording), and the line reads `not
read back (<why>), so none of its N register(s) is confirmed`. It used
to read `N register(s) unconfirmed`, which is what a read-back that ran
and came up short says.

## Measured

On the desk, with `recv-port = 80`: `--diff` exits 1 with `cannot bind
the receive port UDP 80: Permission denied`; a switch applies, reports
`not read back (cannot bind ...)` and exits 0; the unit started with
that config reaches READY, logs the barrier's error, re-applies the mix
after the blind delay, and ends on `STATUS=running; verifier failed`.

## Not decided here

Whether the named tri-state (`True`/`False`/`None` from
`await_link_echo`, `None` from `verify_routing`) becomes a result type.
It should, together with the other stringly-typed domains the same
review named; that changes public names and belongs to 0.7.0.

## Amended in 0.7.0

The same distinction on the other side of the bind. `Listener.messages`
caught `socket.timeout` and every other `OSError` alike and yielded
nothing, so a port that was bound and then could not be read looked like
a quiet backend -- and since the error returned at once where a timeout
waits, every reader spun: measured, 1.3 million reads in half a second,
for the 8 s of a `--dump-config` or the 10 s of a read-back, ending in
"no reply from the backend -- is oscmix running?". A timeout is still
how a wait ends. Any other socket error is a `ReceivePortError` naming
the port and the cause, which every caller already handles as this
record decided: the barrier waits blind, the verifier re-establishes the
mix and fails, a reconcile stands down, a switch is applied and not read
back, a read exits 1. Found by a third outside review.

