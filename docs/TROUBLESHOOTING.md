# Troubleshooting

Work through the layers in order -- each one has a quick check.

## 1. Is the device on the bus?

```sh
grep -l 2a39 /sys/bus/usb/devices/*/idVendor   # any hit = connected
cat /proc/asound/cards                          # ALSA card registered?
```

No hit: cable/power/port problem, or USB autosuspend put the device to
sleep. The shipped udev rule sets `power/control=on` for the device; verify
with:

```sh
cat /sys/bus/usb/devices/<dev>/power/control    # should print "on"
```

On systems where the Fireface is connected through an ASMedia ASM4242
controller (`1b21:2426`), the same rule also disables runtime power
management for that controller. This avoids a failed xHCI runtime suspend
leaving its ports unable to enumerate the interface. Verify with the PCI
address reported by `lspci -Dnn`:

```sh
cat /sys/bus/pci/devices/0000:<bus>:00.0/power/control  # should print "on"
```

If `power/runtime_status` already says `error`, changing the policy cannot
repair the controller retroactively. Move the Fireface to a non-USB4 port or
cold-boot once; the rule prevents the same runtime suspend on later boots.

## 2. Is the MIDI control port there?

```sh
cat /proc/asound/seq/clients | grep -A3 Fireface
```

Expected: a client with two ports. Port 0 is regular MIDI, **port 1 is the
SysEx control port** oscmix needs. If the file does not exist, `snd-seq`
is not loaded (`sudo modprobe snd_seq`; oscmix-session normally triggers
this automatically by opening `/dev/snd/seq`).

## 3. Is the backend running?

```sh
systemctl --user status oscmix.service
journalctl --user -u oscmix.service -e --no-pager
oscmix-session --dry-run        # config parse + device discovery only
```

Common findings in the journal:

- `configuration error: ...` -- routing.conf problem; the message names
  the section and option. The service deliberately does **not** restart
  until you fix it and run `systemctl --user restart oscmix.service`.
- `USB device ... connected but no ALSA sequencer client` -- kernel/driver
  problem, see step 2.
- `device 2a39:3fd9 not connected; nothing to do` -- normal when the unit
  is off; the udev rule starts the service again on plug-in.
- `routing verified against device state` -- the read-back confirmed the
  hardware mixer matches routing.conf; this is the "everything works"
  line.
- `routing verification skipped: UDP 8222 in use` -- harmless; the mixer
  GUI was listening on the state port, so the read-back was not possible.
- `unconfirmed after retry: ...` -- the device never reported the listed
  registers back. Check them in the mixer GUI; if the audio is fine, the
  upstream dump format may simply have changed -- please open an issue.

- `no link change reported within ...` -- normal: the output pairs were
  already stereo-linked, so the device had no change to report.
- `mix matrix re-applied against the synchronized link state` -- the
  routing was re-established after the device's register sync. This is
  what guarantees the right-hand channel of every pair; see below.

The mix matrix is written twice on purpose. oscmix only learns that an
output pair is stereo-linked when the device reports `/output/<n>/stereo`
back, and it does not sync its register cache on its own -- it learns the
device's values only from a `/refresh` dump. A mix written before that
dump is evaluated against oscmix's startup link state and only reaches
the odd channel of each pair, so every even output (2, 4, 6, 8) stays
silent.

So the first write gets audio going immediately, and the background
verification pass -- whose dump is what syncs oscmix -- re-applies the
matrix the moment that dump reports the links. Unlike the `/output/*`
registers the matrix cannot be verified: a `/mix` write draws no reply
and the dump omits the playback matrix, so it is re-established rather
than checked. Both jobs deliberately share one `/refresh`; two
overlapping dumps starve each other and confirm fewer registers.

Only the blind fallback has a knob, for the case where the mixer GUI
holds UDP 8222 and the dump cannot be observed at all
(`systemctl --user edit oscmix.service`):

```ini
[Service]
Environment=OSCMIX_LINK_SYNC_DELAY=30
```

## 4. Does the backend accept OSC?

```sh
ss -ulnp | grep 7222            # oscmix should be listening
```

## 5. Sound on the wrong outputs / no sound

PipeWire maps the 8 analog outputs as 7.1 surround; stereo audio goes to
channels FL/FR = outputs 1/2. If your speakers are on other outputs, route
them in `~/.config/oscmix/routing.conf`:

```ini
[route:monitors]
playback = 1/2
output = 5/6      # wherever your speakers are connected
```

then `systemctl --user restart oscmix.service`. The routing happens in the
device's hardware mixer, so it works the same under PipeWire, PulseAudio
and JACK.

**Before guessing, ask the device.** `oscmix-session --diff` compares
every register the device reports against your config and prints exactly
what differs -- exit 0 means the desk matches, exit 3 names each
mismatched register with both values. If something changed a fader or a
link behind your back (a mixer GUI, another client), this is the
fastest way to see it, and `systemctl --user reload oscmix.service`
re-applies the config without a restart. `oscmix-session --snapshot`
prints the complete register state -- all of it, including what a
config cannot express -- which is what to save before and after an
experiment. Both need the mixer GUI closed; they share its port.

## 6. Service does not start on hotplug

```sh
udevadm test --action=add $(udevadm info -q path -n /dev/bus/usb/00X/00Y) 2>&1 \
  | grep -i systemd_user_wants
```

If nothing matches, re-install the udev rule:

```sh
sudo install -m 644 udev/90-rme-fireface.rules /etc/udev/rules.d/
sudo udevadm control --reload-rules
```

Note that hotplug start requires a running systemd *user* session (i.e.
you are logged in). Before login the service cannot run; it is also pulled
in via `default.target` at login, which covers the boot-with-device-on
case.

## 7. GUI crashes immediately

`oscmix-gtk` aborts if its GSettings schema is missing. `install.sh`
installs and compiles it under `~/.local/share/glib-2.0/schemas/`; verify:

```sh
ls ~/.local/share/glib-2.0/schemas/gschemas.compiled
```

If you built oscmix manually, run:

```sh
install -D -m644 build/oscmix/gtk/oscmix.gschema.xml \
  ~/.local/share/glib-2.0/schemas/oscmix.gschema.xml
glib-compile-schemas ~/.local/share/glib-2.0/schemas
```

## 8. Journal lines that look alarming and are not

The lines below come from the upstream backend or ALSA and are routine
on this setup. None of them means the desk lost state -- `oscmix-session
--diff` is the check that would show it if it had (section 5).

- `snd_seq_event_input: No space left on device` -- the ALSA sequencer
  input pool overflowed and events were dropped; ALSA flushes the input
  FIFO on this error, so what was queued at that moment is gone.
  `alsaseqio` treats it as non-fatal and keeps reading. By sheer volume
  the queue is meter traffic (~880 datagrams a second), but a register
  report can be among the drops -- which is why applied state is read
  back and `--diff` exists, rather than trusting every report arrived.
  The kernel counts the damage in `/proc/asound/seq/clients` under the
  client's `Input pool` (`Alloc failures`). Measured 2026-08-28: even a
  ten-dump `/refresh` flood under four parallel test suites peaks at
  96 of the 200 cells with zero failures -- the overflows are rare
  scheduling events under sustained full load, not a capacity problem.
- `ignoring unknown sysex packet (mfr=200d ...)` -- the device sends a
  vendor SysEx that oscmix does not decode. Ignored by design.
- `unexpected enum value -1` -- a register reports a value outside its
  declared enum; `/controlroom/mainout` reports -1 for "no main out".
  The pinned build prefixes this warning with the OSC address.
- `Invalid argument` followed by `ERROR: backend exited with status 1`
  and `Scheduled restart job` -- the backend died and the unit restarted
  it 3 s later (`Restart=on-failure`, `RestartSec=3`); the restart
  re-applies the routing and reads it back like any start, so the
  journal shows `routing verified against device state` right after.
  Seen once here (2026-08-30, after 2 h 24 min of running, with the
  mixer GUI open); the message is the backend's and its cause is not
  known. Worth a look only if it repeats -- then keep the lines around
  it, and check `oscmix-session --diff` after the restart.

**If the enum warning appears *without* an OSC address, an old backend
binary is running.** Builds before upstream `05621e5` print the bare
message. Check what the service actually started:

```sh
journalctl --user -u oscmix.service --no-pager | grep "INFO: starting:" | tail -1
```

It must name `~/.local/bin/alsaseqio` and `~/.local/bin/oscmix`. A path
like `/usr/local/bin/...` is a stale install shadowing the pinned one;
remove it (`sudo rm /usr/local/bin/{oscmix,alsaseqio,oscmix-gtk}`).
The session now prefers `~/.local/bin` even when the systemd user
manager's PATH lacks it, which is how the stale copy won once: measured
2026-08-26, the first hotplug start after boot ran a February build for
six hours.

## 9. Named sinks silent, Fireface missing from the sink list

Symptom: sound settings offer only HDMI/onboard outputs; the `oscmix.*`
sinks may still be listed but play into nothing; `verify-hardware`
fails every route with "tone only 0.0 dB above what was already
playing" or the response column reads `-/-`.

Check the card's PipeWire profile:

```sh
wpctl status   # Settings -> no Fireface under Sinks
pw-dump | grep -A2 alsa_card.usb-RME   # active_profile
```

`active_profile=off` with only `off` and `pro-audio` on offer means a
system update changed the ALSA UCM profiles. Measured 2026-08-27: an
`alsa-ucm-conf` upgrade retired the `Direct`/`HiFi` profiles this
device used; WirePlumber's stored choice (`Direct`) no longer existed,
so it fell back to `off` at the next boot -- no sink node, and the
loopback sinks' `target.object` named a node that was gone.

The recovery, in this order:

```sh
wpctl set-profile <device-id> 1    # pro-audio; persisted by WirePlumber
sleep 5                            # let the sink node publish its layout
oscmix-session --pipewire-sinks > ~/.config/pipewire/pipewire.conf.d/oscmix-sinks.conf
systemctl --user restart pipewire wireplumber pipewire-pulse
```

The `sleep` is not decoration: regenerating in the first seconds after
the profile switch found the sink with no channel layout yet, and the
generator fell back to 7.1 surround position names (`FC`, `LFE`) that
do not exist on the device's `AUX0..AUX19` map -- two of three sinks
silent, with everything looking installed. The generator's log line
says which case you got: `(20-channel channel layout)` is right,
`(unknown channel layout)` means regenerate once the sink is up.

The backend and `routing.conf` are not involved: the mixer state was
intact throughout, this is purely the desktop-audio path into playback
channels.

## 10. Routing verified, still no sound: the level never reaches the device

Symptom: the journal says `routing verified against device state`,
`oscmix-session --diff` says the device matches the config, the mixer
GUI's playback meters barely move, and every output is quiet or silent.
Moving a fader in the GUI seems to do nothing, or something strange.

The routing is fine. The signal is being attenuated *before* it reaches
the interface, in PipeWire, and the trap is the scale: `wpctl` shows a
slider position, and PipeWire maps that position to gain with a cubic
curve. A control at 0.40 is not -8 dB, it is -24 dB. Several of them sit
in series between a player and the device:

| control | shown | actual |
|---|---|---|
| the application's stream (Spotify, a browser) | 0.53 | -16.4 dB |
| the named sink from `--pipewire-sinks` (`oscmix.main-out`) | 0.42 | -22.6 dB |
| the Fireface's own sink (`Fireface UCX II ... Pro`) | 0.40 | -23.9 dB |

Measured 2026-09-05: those three controls, each looking "about half",
added up to 63 dB, and the device's playback meter read -65 dBFS with
music at full level in the player. Every hardware output then sat that
far down before its own fader was even counted. The mixer's fader
behaved exactly as written -- checked against the device's meters to
0.1 dB -- but with the signal 65 dB down its whole audible range was
squeezed into the top few dB, which is what "further down is sometimes
louder" feels like.

Read the actual gains rather than the slider positions:

```sh
pw-dump | python3 -c '
import json, math, sys
for o in json.load(sys.stdin):
    i = o.get("info") or {}; p = i.get("props") or {}
    if p.get("media.class") not in ("Audio/Sink", "Stream/Output/Audio"): continue
    for prm in (i.get("params") or {}).get("Props") or []:
        v = prm.get("channelVolumes")
        if v: print("%-60s %6.1f dB" % (p.get("node.name") or p.get("application.name"), 20*math.log10(max(v[0], 1e-9))))'
```

`channelVolumes` are linear amplitude, so the dB above is the real
attenuation. Bring the chain to 0 dB in an order that cannot get loud:
first the PipeWire controls, while the hardware output faders are still
down, then the output faders in the mixer, by ear. On an audio interface
the digital path belongs at unity; listening level is what the hardware
faders are for.

## 11. A fader moves back by itself after a replug, restart or wake

Symptom: an output fader set in the mixer GUI returns to a fixed value
after the interface is replugged, the service restarts, or the machine
wakes from suspend.

That is a route with a `volume =` line. Declaring it pins the output
fader (README, *Configure your routing*): every backend start writes it,
and so does every reload -- including the one the resume hook sends
after a suspend. Nothing is drifting; the config is doing what it says.
If the fader is one you set by hand, delete the `volume =` line from
that route. The session then never writes that register, and the fader
stays where you put it. `[pin] output.volume` does not change this: it
decides who wins for a value the config declares, not whether the
config declares it.

A profile, by contrast, survives all of this since 0.6.3: the switch
remembers it beside `routing.conf`, and starts and reloads apply the
remembered profile. If the desk came back as `routing.conf` anyway,
the journal's start line says why -- the marker names a profile that
no longer loads -- and `--no-profile` or fixing the file ends it. See
*Profiles* in the README and ADR 0018.
