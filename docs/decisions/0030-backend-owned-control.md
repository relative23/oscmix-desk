# 0030 — The existing backend owns replies and operation leases

**Status:** implementation decision for 0.8.0; qualification pending.
Amends [0028](0028-mixer-replies-and-writer-coordination.md).

## Source findings

At upstream `f2fdd5ec78338848754aad32cc07f3440de63395`, `main.c` already
dispatches MIDI input, OSC commands and replies in one event loop. `writeosc`
has one destination. `gtk/mixer.c` creates its own UDP receiver and sends
commands directly; it does not participate in desk's file lock.

This is the smallest existing owner that can provide both contracts. Extend
it rather than add a relay process, a second MIDI writer or a second register
decoder. Keep upstream's routing implementation at this seam. Carry backend,
bridge and GTK changes as a versioned patch series against the full upstream
commit, with patch hashes and resulting binaries in artifact provenance.
Upstream acceptance is not a prerequisite or a claim.

## One mediated path

Desk and the matching GTK use a local `AF_UNIX/SOCK_SEQPACKET` connection to
the existing backend in coordinated mode. Packet boundaries preserve OSC
deliveries. This mode opens no UDP command or reply socket. There is no
automatic direct-UDP fallback. Upstream's standalone UDP mode remains an
explicit uncoordinated mode, which the desk and launcher refuse.

The existing OSC payload is carried inside a small versioned envelope.
The envelope distinguishes control acknowledgements, actual MIDI-derived
observations, backend-derived values, meter reports and lease-state events.
It is not a new register API. In particular, `setrefresh()` currently emits
playback stereo from `inputs[]`; those values must not become hardware
confirmation because they were received on a reliable connection.

A connection is subscribed before an operation begins. Replies are delivered
to each subscribed connection. No `SO_REUSEPORT`, shared receiver, replayed
snapshot or persistent desired-state cache is added. Received sequence order
is explicit; it does not establish when the hardware generated a value or
make a dump atomic.

### Identity and permissions

The socket is named for the resolved USB id and serial in the shared runtime
directory used by the device lock. The backend holds a separate owner lock
for its lifetime. The socket and lock must be real objects of the expected
type, never symlink targets. An active owner is not replaced. A stale socket
is removed only under that owner lock after checking its type and ownership.
The unprivileged runtime uses the existing runtime-directory fallback where
host integration was deliberately omitted; its narrower ownership scope
must be reported explicitly. Path selection is read-only and deterministic;
a permission failure creating that selected directory refuses the operation
instead of falling back to a different lock/owner. Relative XDG runtime
paths are ignored. A pathname too long for the Unix socket is refused before
starting the backend.

The ALSA bridge requests an exclusive sending subscription to the selected
hardware port. This prevents a second cooperating sequencer backend from
silently becoming another writer, including one choosing another socket
directory. The receiving subscription remains observable for diagnosis.
Other raw-MIDI/USB access and physical knobs remain outside this protocol's
coordination guarantee.

The shared directory remains `3770 root:audio`, with socket/owner-lock access
limited to their owner and the directory group. Kernel pathname permissions
authorize access; `SO_PEERCRED` supplies the actual peer PID/UID/GID. Desk
checks the server PID, bridge, selected interface, serial and process identity
against its resolved target, then binds observations to that connection and
a random backend epoch. The GTK launcher supplies the checked socket, PID and
device serial; GTK verifies the kernel peer and handshake serial before
requesting a refresh. A socket symlink is refused by both clients. A matching
GTK companion identifies its protocol with a display-free `--control-version`
command; old UDP GSettings no longer select the endpoint. A claimed model name or an old familiar endpoint is
insufficient. Recheck device identity and active playback capacity at write
boundaries. A disconnected/replaced backend invalidates every observation.

The old loopback UDP mode remains unauthenticated; it gains no protection
from this design. Root and authorized device users can bypass cooperating
clients or deny service. This is local control authorization, not isolation
from an account already entitled to operate the hardware.

### Complete operations and GTK input

Lock order is desk's existing device file lock, then backend lease. Keep the
file lock because it also protects profile selection and marker persistence.
The lease covers the complete initial apply and its repair/verification, an
explicit profile/main-desk operation, or a selective reconcile. Nested routing
helpers borrow the same connection and lease; they do not acquire another
writer or reconnect midway. Read-only status remains procfs/files/service
inspection and neither starts this protocol nor requests device data.

GTK uses the same owner for its individual edits. A desk lease blocks GTK
writes while observation and metering continue. Every GUI command carries
the last lease generation GTK observed. Generations change on acquisition
and release, so input queued before or during an operation cannot be applied
afterwards just because the backend became idle. Reject busy/stale input;
never queue an edit for later replay. GTK visibly disables writing while
busy or disconnected and resumes from fresh observations. No new desk GUI
is introduced.

Desk acquisition returns busy rather than enqueueing an unbounded request.
The client may wait within its existing operation deadline before any write.
An acquired lease has a short inactivity deadline and a hard total deadline;
keepalive cannot extend the hard limit. Expiry closes the owner's connection
and releases the lease, so buffered old commands cannot turn into new work.
No failed write is replayed after timeout or reconnection.

Known ALSA sequencer input loss also ends the bridge. `snd_seq_event_input()`
returning `-ENOSPC` means events were lost and the input FIFO was cleared
([ALSA API contract](https://www.alsa-project.org/alsa-doc/alsa-lib/group___seq_event.html)).
The inherited bridge loop previously logged this and kept reading. Continuing
would hide an observed transport loss from the backend's consumers; a missing
link contradiction could leave an earlier match in their windows. Patch 0006
now exits on that error as on other input failures, closing the MIDI pipe.
The backend also processes an observed MIDI hangup/error before buffered input
or waiting client commands. A pipe can report `POLLIN` and `POLLHUP` together;
reading its tail first previously allowed a queued write despite the known
disconnect. Both boundaries now invalidate the backend, leases and connected
consumers. This changes no device command, register classification or upstream
base pin.

### Refresh and observation windows

One backend arbitrates refresh. It records a bounded receive window after
issuing the existing hardware refresh command. No second hardware refresh
starts during that window. A desk reader waits for a new window within a
bounded deadline; it cannot treat an already partly delivered window as its
fresh complete read. GUI requests during an operation/window coalesce into
at most one pending refresh, discarded on disconnection or shutdown.

The refresh acknowledgement marks the receipt boundary before requesting
hardware data. Reports identify their origin and backend epoch. They can be
classified as received during that window, not as hardware-tagged replies to
that particular request: the current MIDI protocol supplies no such tag.
Quiet, partial, invalid and contradicted results retain N0/N1 semantics.
End-of-window is a timeout boundary, never proof that every register arrived.
Start/end events identify this receive window separately from the write-lease
generation. GTK waits for a complete window containing device-origin reports
before re-enabling controls after a desk operation. This is a UI freshness
rule, not a claim of complete hardware read-back. Backend-derived playback
flags and meter traffic alone cannot satisfy it. An already queued GUI refresh
is not submitted again merely because a lease was released.

### Bounds and failure

Initial implementation limits, to be checked by overload/lifecycle tests:

| Resource | Bound |
| --- | --- |
| Connected consumers | 16 |
| OSC payload | 8192 bytes per packet, matching upstream's buffer |
| Pending outgoing data | 32 packets / 256 KiB per consumer |
| Lease inactivity | 5 seconds |
| Total lease | 90 seconds, not renewable past this limit |
| Refresh receive window | 10 seconds |
| Pending GUI refresh | One coalesced request |

Use nonblocking client I/O and bounded work per poll iteration. A slow client
is disconnected on overflow; it cannot delay another client's verifier or
silently receive a supposedly complete dump. EOF, malformed/oversized input,
MIDI/bridge failure and backend shutdown invalidate the connection. Clients
report the failure; they do not infer silence or fall back to a direct write.
Each blocked MIDI-pipe write has a two-second deadline. A decoded delivery
that cannot fit the backend's OSC buffer terminates the backend instead of
publishing a truncated bundle or writing past that buffer. Both failures can
leave earlier submitted paths applied and require the same partial outcome.

Write acknowledgements describe processing by this backend, not a hardware
read-back. Partial outcomes continue to distinguish paths submitted before a
failure from pending paths. A request whose acknowledgement is lost may have
reached the device and must remain visibly uncertain. Loss of the operation
lease/backend before completion cannot commit a successful profile marker.
Physical changes are observations; they do not acquire the cooperating lease
and do not trigger continuous PIN enforcement.

## Alternatives and qualification

A Python UDP relay would add process ownership and lifecycle state while
still needing GTK cooperation for whole-operation writes. A UDP subscription
extension would reuse the socket but also require loss detection, subscriber
expiry and additional identity/authorization machinery. A local connected
packet socket gives authenticated peer identity, disconnect notification and
ordered packet delivery for the Linux targets already required by 0.8.0.
Keeping two new production paths would double the failure contract without
meeting another requirement. None of these choices creates targeted hardware
queries or playback read-back; those remain N3's measured capability work.

Acceptance must run the real patched backend and GTK against simulated MIDI,
including both launch orders, competing desk processes, overlapping refresh,
busy/stale GUI input, replacement, lease expiry, overflow and shutdown. Then
run the agreed UCX II measurements and final platform/install/release gates.
Until those pass this ADR is a contract under implementation, not a claim of
qualified simultaneous operation or improved hardware read-back.
