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
| 12–15 | Role, result or observation origin, according to kind |
| 16–23 | Lease generation; observation sequence or window number for those events |
| 24 onward | Kind-specific payload, at most 8192 bytes |

A client starts request numbering at 1 and increases it by exactly one.
Zero, replay, gaps, wrap, bad framing and excess payload disconnect it.
There is no automatic reconnection or replay. Replies set bit `0x80000000`
on the request kind and retain its number. Events have request number zero.
An observation sequence may have gaps because subscriptions select origins;
gaps alone do not prove loss.

## Requests

| Kind | Name | Code and payload |
| --- | --- | --- |
| 1 | HELLO | Role: 1 desk, 2 GTK, 3 reader. Payload: 32-bit origin mask (1, 2, 4 or their union). Required first. |
| 2 | BEGIN | Desk only; acquire an operation lease or return busy. No payload. |
| 3 | END | Release this connection's lease, using its current generation. |
| 4 | KEEPALIVE | Renew the five-second inactivity limit. The 90-second total limit remains fixed. |
| 5 | WRITE | One literal OSC message, using the lease generation or GTK's last observed free generation. No bundles, address patterns or `/refresh` bypass. |
| 6 | REFRESH | Request a new hardware refresh receive window. No payload. |

Code is zero except on HELLO. Replies normally have no payload. A successful
HELLO contains 16 random epoch bytes, the actual server PID (32 bits),
implementation version 1 (32 bits), and its NUL-terminated MIDI device name.
Compare the PID with kernel `SO_PEERCRED` and the externally resolved
backend/bridge/interface identity. The name is not authentication. A changed
connection invalidates all prior observations.

Result codes: 0 processed, 1 busy or stale generation, 2 invalid command,
6 no matching ownership. Codes 3–5 are reserved; expiry, shutdown and overflow
close the connection. A processing acknowledgement is not hardware read-back.
If it is lost, a submitted write may already have reached the device.

GTK edits are never queued for later execution. During a desk operation
they are rejected. Release advances the generation so an old edit also fails
when the backend becomes free. Readers cannot write or acquire leases.

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
