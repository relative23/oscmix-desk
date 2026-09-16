# 0013 -- Reconcile on events, never on a clock

## Status

Accepted, 0.3.0. Measured on a Fireface UCX II, serial 24216011.

## Context

[ADR 0012](0012-pin-and-remember.md) gave every register a policy, and
then had to admit what "pinned" could mean. The device does not announce
its own changes -- of everything a config sets, only
`/output/{ch}/stereo` is pushed to listeners -- so nothing can react to a
fader being moved without polling a 2002-register dump.

> **That premise is wrong, measured 2026-08-24.** See *What the device
> actually pushes* at the end. It announces far more than one register,
> and the decision below was taken without knowing it.
 Pinning therefore
means *the config wins while this session is looking*, and the open
question was: **when is it looking?**

The roadmap listed three moments: SIGHUP, resume, hotplug. Only two of
them needed anything built.

## Decision

**Triggers are events. There is no timer, and there will not be one.**

A timer would be a background process that argues with the user on a
schedule, and -- because the device reports nothing -- each tick would
cost a full dump. `tests/test_reconcile_triggers.py` asserts no `.timer`
unit and no `OnCalendar`/`OnUnitActiveSec` exists.

### Hotplug: already covered, and left alone

`udev/90-rme-fireface.rules` pulls `oscmix.service` in on `add`;
`StopWhenUnneeded=yes` drops it on `remove`. A replug is therefore a full
process restart with a full apply, which
`tests/data/cold-plug-timeline.json` recorded directly: *"cold USB replug
-- device unplugged 14.4 s, udev restarted the unit"*.

Building a session-level hotplug trigger would have been a second
mechanism for something already working -- the mistake this release found
three times over. Instead both halves are asserted by test, so if the
rule or the unit ever loses its part, that shows up as a failure rather
than as a device that quietly stops being configured.

### SIGHUP: reconcile, not restart

`ExecReload=/bin/kill -HUP $MAINPID`. A reload re-reads `routing.conf`,
reads the device back, and applies what the config pins while leaving
what it remembers.

**`$MAINPID` and not `systemctl kill`, measured.** `systemctl --user kill
--signal=SIGHUP oscmix.service` signals *every* process in the unit.
Neither oscmix nor alsaseqio installs a SIGHUP handler, so the default
action applies: both died and the service went inactive. `ExecReload`
reaches the session process alone.

The handler sets a flag and returns. A signal handler runs between two
bytecodes of whatever was executing, so opening a socket or waiting out
the link barrier there means doing it *inside* the apply it was meant to
follow. The supervise loop picks the flag up, and clears it before
running the reconcile so a SIGHUP arriving during one queues another
rather than being swallowed.

A config that no longer parses is reported and **not** applied; the
session keeps running on the configuration it has. Exiting would turn a
typo in a file nobody was forced to edit into silence on a desk somebody
is listening to.

Measured end to end on the UCX II, fader moved to −20.0 dB by hand and
then SIGHUP:

| config | log | device |
|---|---|---|
| remembered | `1 left to the device (/output/1/volume)` | keeps −20.0 |
| `[pin] output.volume = pin` | `1 to correct` | back to −6.0 |

### Resume: a system-sleep hook, and it is honest about being unproven

A user unit with `WantedBy=sleep.target` would install, enable, and never
run: on systemd 259 `systemctl --user cat sleep.target` reports *"No
files found for sleep.target"*. The user manager has no such target.
Checked before writing the unit, which is the only reason it is not in
this repository.

So `/usr/lib/systemd/system-sleep/oscmix`, running as root on `post`,
reaching each logged-in user's manager with `--machine=<user>@.host
reload`. It is installed by the same root step as the udev rule and
removed by `uninstall.sh`, because a hook left behind fires on every wake
for a service that is gone.

**Measured 2026-08-20, and this device does not need it.** Two
suspend/resume cycles on the UCX II, woken by an RTC alarm so nothing
depended on somebody pressing a key.

*Without the hook*, across a real S3 cycle: the interface never left the
USB bus -- no USB events for it, same ALSA card, same sequencer client --
the backend was not restarted, and all 1932 reported registers were
identical except the four this test deliberately changed beforehand.

*With the hook*, the trigger fires and does what it is for: the journal
shows `SIGHUP: reloaded routing.conf` and
`reconcile (SIGHUP): 6 confirmed, 2 drifted; re-applying`, and a fader
moved by hand to -22.0 dB before the suspend came back to the config's
0.0 dB, because this desk pins `output.volume`.

So the hook works and, on this machine, has nothing to repair. It is
kept: it is one dump when nothing was lost, and the case it guards --
the interface losing power across a suspend -- is a property of the
platform and the port, not of this device. Note that the udev rule here
sets `power/control = on` for the interface, which is a plausible reason
it stays on the bus at all; a machine without that rule is a different
measurement.

**How to reproduce it, because the obvious way does not work.**
`rtcwake -m mem` writes `/sys/power/state` directly and bypasses systemd
entirely. It therefore runs neither `nvidia-suspend.service` -- on this
machine the GPU then refuses, the suspend is aborted after 6.5 s and S3
is never reached -- nor any `system-sleep` hook, which is to say it
cannot test this feature at all. Arm the alarm and suspend through
systemd instead:

```sh
sudo rtcwake -m no -s 45     # arm only
sudo systemctl suspend
```

## Consequences

- A reconcile reads before it writes. That read is the only way to know
  which remembered registers to protect, which is what makes this a
  reconcile rather than a re-apply.
- **A held receive port is a refusal, not a blind write.** With no dump
  there is no way to tell a pinned register from a remembered one at the
  device, so writing anyway would be the indiscriminate re-apply ADR 0012
  exists to end. The mixer GUI holding UDP 8222 is the normal desktop
  case, so this will happen, and it says so in the log.
- **The unit and the package must be installed together.** `ExecReload`
  against a version without the SIGHUP handler is fatal: the default
  action terminates the session. Seen while testing, on a machine whose
  unit had been updated ahead of its package. `install.sh` writes both.

## Alternatives considered

**Restart the unit on resume.** One line, no new code, and it puts
remembered faders back exactly as they were configured -- undoing the
whole point of ADR 0012 on every wake.

**Reconcile on a sample rate change.** Measured after this ADR was first
written, and the measurement removed the feature rather than justifying
it.

A 48 kHz -> 44.1 kHz change on a UCX II destroys nothing. 1931 of 1932
reported registers were identical across it; the one that differed was
`/clock/samplerate`. The playback mix matrix survived as well -- shown
by signal, because it is never reported: a 1 kHz tone at -40 dBFS into
playback 1/2 still came out at outputs 1, 5 and 7 at the levels
`routing.conf` routes it to.

The trigger would also have been the cheapest of the three, because
`/clock/samplerate` is the *second* register found to be pushed on
change (the first is `/output/{ch}/stereo`): ten seconds of quiet
observation, then the change, then exactly one datagram. So it needs no
poll and no timer.

Not built, because there is no measured loss to repair. If one turns up
-- at 88.2 kHz and above the device changes its channel count, which is
a different question and unmeasured -- the trigger is a few lines, and
this paragraph is where to start.

**A D-Bus listener for logind's `PrepareForSleep`.** Would remove the
root install. Rejected: the runtime imports nothing outside the standard
library, and a D-Bus client is a large dependency for a signal a
five-line shell script already delivers.


## What the device actually pushes (2026-08-24)

The sentence this record opens with is false, and it was never measured
the way it is stated: `/output/{ch}/stereo` was the register this
project had *noticed* being pushed, and that became "the only one".

Measured on a UCX II at 55802a6 by writing a register from a second
client -- the same path a mixer GUI uses -- and listening on the receive
port without asking for anything:

**Pushed: the partner channel of a linked pair.**

| written | reported unprompted |
|---|---|
| `/output/5/volume` | `/output/6/volume` |
| `/output/5/mute` | `/output/6/mute` |
| `/output/5/crossfeed` | `/output/6/crossfeed` |
| `/output/5/eq` and `eq/band1{gain,freq,type}` | the same on `/output/6` |
| `/output/5/dynamics`, `dynamics/gain` | the same on `/output/6` |
| `/output/5/lowcut`, `lowcut/freq` | the same on `/output/6` |
| `/output/5/autolevel`, `autolevel/maxgain` | the same on `/output/6` |
| `/input/3/mute`, `/input/3/hi-z` | the same on `/input/4` |

Enabling a DSP block also pushes `/hardware/dspload`.

**Not pushed:** `/output/5/reflevel`, `/input/3/reflevel`,
`/input/3/gain`, `/input/3/phase`.

Note what is reported: the **partner**, not the channel written. And
note that no rule tested explains the exceptions. "Analogue front end
versus DSP" was the obvious guess and it is wrong -- `hi-z` is front end
and pushes, `phase` is DSP and does not. Rather than write a rule that
was not established, this records the measurement and says the
mechanism is unknown.

### What follows, and what does not

**A drift signal for linked pairs needs no clock and no polling.** The
decision below rules out a timer because "nothing can react to a fader
being moved", and that is not the situation: a listener already sees
volume, mute, crossfeed and every DSP block change on a linked pair, as
one datagram, at the moment it happens.

**It does not cover what pinning is mostly for.** `reflevel`, `gain`
and the input `phase` are silent, and those are exactly the
installation-state registers ADR 0012 says PIN exists for. A signal
built on this would announce the settings a config mostly leaves alone
and stay quiet about the ones it pins.

So this changes the premise without settling the question. Nothing is
built on it here.

### Two writers in one process (0.6.2)

The start-up verifier runs on its own thread for up to about 22 s
after `READY=1`, and a SIGHUP reconcile runs on the main thread. Both
write the whole routing, and the verifier releases the receive port
between its phases, so a reload landing in one of those gaps used to
interleave two link phases and two mix writes on the wire -- the
ordering ADR 0001 exists to guarantee. The resume hook is what makes the
window real: after a suspend the device re-enumerates, udev restarts
the unit, and the hook's reload arrives during the verifier's window.

The reconcile now waits for the verifier (`session._verifier_finished`),
bounded by `RECONCILE_WAIT_FOR_VERIFIER` (30 s, derived from the
verifier's longest path) so a wedged thread cannot hold the supervise
loop; a stop request ends the wait, and a reload that times out is
logged rather than swallowed. The trigger list above does not change.

## Amended in 0.6.10

`StopWhenUnneeded=yes` does not stop the service on `remove`: the unit is
enabled, `default.target` wants it, and a wanted unit is never unneeded.
What ends it is the backend exiting with its device, after which the
session exits 0 -- observed on the desk as the unit going inactive with
exit 0 on every unplug, while its device unit had long been inactive
with the service active. The replug is still a full restart with a
full apply, through the `add` pull-in, so nothing here changes; the
mechanism was described wrongly.

A reload re-reads the desk in effect since 0.6.3 -- the active profile,
else `routing.conf` (ADR 0018) -- not `routing.conf` alone as the
SIGHUP section above says.
