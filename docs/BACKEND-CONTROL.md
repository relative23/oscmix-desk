# Coordinated backend wire contract (ODK1)

The local protocol between the patched oscmix backend, the desk and the GTK
mixer. It uses one Linux `AF_UNIX/SOCK_SEQPACKET` connection; each packet
contains one frame. It is not an additional hardware protocol.

All integers are unsigned big-endian. The 24-byte header is:

| Bytes | Meaning |
| --- | --- |
| 0–3 | ASCII `ODK1` |
| 4–7 | Kind |
| 8–11 | Request number |
| 12–15 | Role (HELLO), result code (replies), lease or window state (events 16 and 18), observation origin (event 17) |
| 16–23 | Lease generation; observation sequence or window number for those events |
| 24 onward | Kind-specific payload, at most 8192 bytes |

The backend serves at most 16 connections, and closes one that has not sent
HELLO within three seconds. A client starts request numbering at 1 and
increases it by exactly one. Zero, replay, gaps, wrap, bad framing, excess
payload, a second HELLO, a nonzero code on any request but HELLO and a payload
on any request but WRITE disconnect it.
There is no automatic reconnection or replay. Replies set bit `0x80000000`
on the request kind and retain its number. Events have request number zero.
An observation sequence may have gaps because subscriptions select origins;
gaps alone do not prove loss.

## Requests

| Kind | Name | Code and payload |
| --- | --- | --- |
| 1 | HELLO | Role: 1 desk, 2 GTK, 3 reader. Payload: 32-bit origin mask (1, 2, 4 or their union). Required first. |
| 2 | BEGIN | Acquire an operation lease or return busy (1). Only the desk role may hold one; GTK and readers get 6. No payload. |
| 3 | END | Release this connection's lease, using its current generation. |
| 4 | KEEPALIVE | Renew the five-second inactivity limit. The 90-second total limit remains fixed. |
| 5 | WRITE | One literal OSC message, using the lease generation or GTK's last observed free generation. Refused with 2: bundles, address patterns (`*?[]{}`) or non-printable characters, addresses starting with `/refresh` or `/register`, type tags other than `i`, `f` and `s`, and non-finite floats. |
| 6 | REFRESH | Request a new hardware refresh receive window. No payload. |

Code is zero except on HELLO. Replies normally have no payload. A successful
HELLO contains 16 random epoch bytes, the actual server PID (32 bits),
implementation version 1 (32 bits), and its NUL-terminated MIDI device name.
Compare the PID with kernel `SO_PEERCRED` and the externally resolved
backend/bridge/interface identity. The name is not authentication. A changed
connection invalidates all prior observations.

Result codes: 0 processed, 1 busy or stale generation, 2 invalid command,
6 no matching ownership or a role that cannot hold a lease. Codes 3–5 are
reserved; expiry, shutdown and overflow close the connection. A processing
acknowledgement is not hardware read-back. A WRITE to an address that no
backend node handles is also answered 0: the reply says the message was parsed
and dispatched, not that a register exists or changed. If an acknowledgement
is lost, a submitted write may already have reached the device.

GTK edits are never queued for later execution. During a desk operation
they are rejected. Release advances the generation so an old edit also fails
when the backend becomes free. Readers cannot write or acquire leases.

## Timing

The desk retries a busy BEGIN for up to 30 seconds and a busy REFRESH for up
to 12, waits two seconds for each acknowledgement, and sends a KEEPALIVE
every second while it holds a lease.

A WRITE does not renew the lease, and the backend checks the five-second idle
limit before it handles the next request. A WRITE whose MIDI output is held up
for about five seconds therefore ends the lease even when a KEEPALIVE is
already queued behind it. The backend itself exits when one MIDI write is
not finished within two seconds. A desk that gets no
acknowledgement within two seconds closes the connection. Each of these ends
the operation, and the write in flight counts as possibly sent.

## Events

| Kind | Name | Code and payload |
| --- | --- | --- |
| 16 | Lease state | 0 free, 1 busy; current lease generation; no payload. |
| 17 | Observation | Origin 1 MIDI register handler, 2 command/cache handler, 4 meters; OSC delivery as payload. |
| 18 | Refresh window | 1 started, 0 ended; window number; no payload. |

Origin describes the backend path producing the report. It does not add
hardware generation tags, atomic snapshots or capabilities absent from the
register model. Input-mix reports are suppressed until their level, pan,
links and required stereo partner have been observed since the last
command/refresh.
A cached playback stereo flag is never hardware confirmation
merely because it arrived over this connection.

A refresh reply precedes its window-start event and resulting reports.
Only one ten-second window runs at a time. A desk retries busy requests within
its operation deadline before accepting a fresh window. GTK refresh requests
while busy coalesce into one pending request, dropped on disconnect. Window
end means elapsed receive time; it is not a complete-dump certificate.

Clients preserve delivery boundaries when deciding whether a confirmation
has survived all messages. Invalid or oversized deliveries cannot become
partial successful reads. Backend shutdown, MIDI EOF, blocked MIDI writes,
protocol loss and queue overflow invalidate the connection and its authority.
The ALSA bridge also terminates after reported input FIFO loss (`-ENOSPC`),
so a known gap cannot silently leave old observations or a writer lease valid.
An observed MIDI hangup/error takes precedence over buffered reports and queued
client writes, even when the pipe also reports readable tail data.

## Testing a backend build

The manifest pins the upstream commit and each patch's SHA-256. Preparation
exports that commit without using upstream worktree edits:

```sh
python3 scripts/prepare-backend.py --upstream /path/to/upstream-checkout \
    --destination /path/to/new-build
make -C /path/to/new-build 'CC=cc -std=c11' oscmix alsaseqio gtk
OSCMIX_CONTROL_BINARY=/path/to/new-build/oscmix \
    python3 -m pytest tests/backend_control.py
```

`tests/gtk_control.py` runs the actual GTK executable against the same
backend with simulated MIDI. It requires `OSCMIX_QUALIFY_DESKTOP=1`,
`--backend`, `--gtk`, a new `--output` directory and the system Python's
GI/AT-SPI packages, under Xvfb and a private `dbus-run-session`.
