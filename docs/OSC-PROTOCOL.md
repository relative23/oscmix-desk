# The oscmix OSC interface

[oscmix](https://github.com/michaelforney/oscmix) exposes the Fireface's
hardware mixer via OSC 1.0 over UDP. This is what `oscmix-session` uses to
apply routing.conf, and what you can use to script the mixer yourself.

- oscmix **listens** on `udp://127.0.0.1:7222` (commands in)
- oscmix **sends** state changes to `udp://127.0.0.1:8222` (where
  oscmix-gtk listens)

## The mix matrix

`/mix/<output>/playback/<channel>` controls how much of a software
playback channel reaches a hardware output -- the routing matrix from
TotalMix FX. Arguments: `,fi` = level (float, dB) + pan (int).

- level: `0.0` = unity gain, `-65.0` = mute
- pan: `-100` (left) … `100` (right), `0` = center
- all indices are 1-based

**Stereo-linked pairs fold onto the odd channel:** a `/mix` message
addressed to either half of a linked pair writes the *same* pair
register, and pan acts as the pair's balance. Do NOT send per-channel
messages panned hard left/right for linked pairs (the TotalMix pattern
for unlinked channels) -- they overwrite each other and the last pan
wins, leaving the whole mix panned hard to one side.

A plain stereo pass-through of playback 1/2 to output pair 5/6 is one
matrix entry, with both pairs linked:

```
/playback/1/stereo  ,i   1
/output/5/stereo    ,i   1
/mix/5/playback/1   ,fi  0.0 0
```

This is exactly what a `[route:...]` section with `playback = 1/2` and
`output = 5/6` generates.

### The link has to be effective *before* the mix write

Sending those three messages back to back is not enough, and the failure
is silent. oscmix keeps its own copy of the link state and updates it
only in `newoutputstereo()` -- the handler for the **device reporting**
`/output/<n>/stereo`. The OSC setter is a plain `setbool` that forwards
the register to the device and leaves oscmix's copy alone.

If `/mix/5/playback/1` is evaluated while that copy still says
"unlinked", `setlevel()` takes the unlinked branch: it writes only the
addressed output and never touches `out+1`. Output 5 receives a mono sum
of playback 1 and 2, output 6 receives nothing. Applied to every pair,
that is silence on outputs 2, 4, 6 and 8 while the odd ones still play --
one working headphone channel, one working monitor.

Two properties make this awkward to wait out, both measured on a UCX II:

- The device reports a register only when it **changes**. Writing
  `stereo 1` to an already-linked pair produces no report at all.
- oscmix never synchronizes its cache on its own. It learns the device's
  values only from a `/refresh` dump, which streams over MIDI. This said
  "~15-20 s" from an unrecorded observation; the recorded one
  (`tests/data/refresh-dump.json`, pinned revision, backend restarted on
  an already-enumerated UCX II) is **1.9 s for 2002 registers**.
  The cold device after a replug -- the condition that was still
  unmeasured when this paragraph was written -- has since been recorded
  too (`tests/data/cold-plug-timeline.json`): the link registers come
  back **0.01 s after the `/refresh`** that asks for them, and the dump
  is over in ~4 s. `LINK_SYNC_BLIND_DELAY` is 5 s on the strength of
  that, see ADR 0010.

The reliable sequence is therefore: send the links, send the mix so audio
works, then send `/refresh` and re-send the mix once the dump has reported
`/output/<n>/stereo`. Note that `/playback/<n>/stereo` needs no such care
-- `setinputstereo()` updates oscmix's state synchronously.

`/mix` writes cannot be verified at all: they draw no reply, and the dump
contains `/mix/*/input/*` but not `/mix/*/playback/*`. The matrix can only
be re-established from a known link state, never read back.

## Other useful addresses

| Address | Args | Meaning |
|---|---|---|
| `/output/<n>/volume` | `,f` dB | hardware output volume |
| `/output/<n>/stereo` | `,i` 0/1 | stereo-link with the next channel |
| `/output/<n>/pan` | `,i` | pan −100…100 |
| `/input/<n>/gain` | `,f` | input gain |
| `/mix/<out>/input/<in>` | `,fi` | hardware input → output routing |
| `/refresh` | none | re-send the complete device state |

The authoritative list is the upstream source (`oscmix.c`).

## Scripting example

OSC messages are trivial to construct with the Python standard library --
address and type tag are NUL-terminated strings padded to 4 bytes,
arguments are big-endian:

```python
import socket, struct

def osc(path, types="", *args):
    def s(x):
        b = x.encode() + b"\x00"
        return b + b"\x00" * (-len(b) % 4)
    data = s(path) + s("," + types)
    for tag, value in zip(types, args):
        data += struct.pack(">f" if tag == "f" else ">i", value)
    return data

sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
sock.sendto(osc("/output/1/volume", "f", -12.0), ("127.0.0.1", 7222))
```

For one-off experiments, `oscmix-session --dry-run` prints the messages it
would send for your current routing.conf.
