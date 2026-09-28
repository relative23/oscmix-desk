# The oscmix OSC interface

[oscmix](https://github.com/michaelforney/oscmix) implements the OSC paths
described here. In 0.8.0, desk and its matching GTK companion carry those
payloads over the [ODK1 control connection](BACKEND-CONTROL.md), with shared
observations, device identity checks and coordinated write ownership.
The coordinated backend opens no UDP command or reply port.

Unmodified upstream and its separate standalone mode use UDP commands at
`127.0.0.1:7222` and one reply destination at `127.0.0.1:8222`. That mode is
uncoordinated and rejected by the 0.8.0 desk/launcher. Changing legacy `[osc]`
port values does not create a second transport or select the ODK1 endpoint.

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
  an already-enumerated UCX II) is **1.9 s for the 2252 registers a dump
  reports** (2322 with the 70 meters that stream on their own).
  The cold device after a replug -- the condition that was still
  unmeasured when this paragraph was written -- has since been recorded
  too (`tests/data/cold-plug-timeline.json`): the link registers come
  back **0.01 s after the `/refresh`** that asks for them, and the dump
  is over in ~4 s.

Desk sends links, waits at the link barrier, and only then sends the matrix.
The later verification window can authorize a PIN repair or mix reapply.
A complete decoded delivery determines the latest link state: a contradiction
at its end revokes an earlier match. Malformed feedback or connection loss
stops the operation; a known wrong link cannot be bypassed by timeout or
blind reapply. An unchanged, silent link remains a distinct unconfirmed case.

`setinputstereo()` updates the backend's playback flags synchronously, but
that cached value is not hardware confirmation. The current patch series
labels it as derived state. Computed input-matrix reports are withheld until
their required device inputs are observed. A refresh returns input-matrix
reports but no playback-matrix coefficients, so a re-established playback route
is not read back.

## Other useful addresses

| Address | Args | Meaning |
|---|---|---|
| `/output/<n>/volume` | `,f` dB | hardware output volume |
| `/output/<n>/stereo` | `,i` 0/1 | stereo-link with the next channel |
| `/output/<n>/pan` | `,i` | pan −100…100 |
| `/input/<n>/gain` | `,f` | input gain |
| `/mix/<out>/input/<in>` | `,fi` | hardware input → output routing |
| `/refresh` | none | request the backend's device refresh; reports can be partial and include derived values |

The path list is in the pinned upstream `oscmix.c`. ODK1 permits refresh only
through its dedicated request; a raw `/refresh` write cannot bypass its
window/ownership rules.

## Encoding a payload offline

OSC messages are trivial to construct with the Python standard library --
address and type tag are NUL-terminated strings padded to 4 bytes,
arguments are big-endian:

```python
import struct

def osc(path, types="", *args):
    def s(x):
        b = x.encode() + b"\x00"
        return b + b"\x00" * (-len(b) % 4)
    data = s(path) + s("," + types)
    for tag, value in zip(types, args):
        data += struct.pack(">f" if tag == "f" else ">i", value)
    return data

print(osc("/output/1/volume", "f", -12.0).hex())
```

This example only encodes bytes. For desk automation, use the CLI's checked
profile/main-desk operations; `oscmix-session --dry-run` previews their
messages without sending them. A custom control client must implement the
ODK1 identity, ownership and failure contract; a processing ACK is not a
hardware observation.
