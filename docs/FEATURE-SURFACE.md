# Which layer provides which features

This inventory describes oscmix-desk 0.7.2 and the pinned oscmix revision
`f2fdd5ec78338848754aad32cc07f3440de63395`. Backend handlers and GTK bindings
were checked in that revision's source. Listed GTK controls describe those
bindings. The [release evidence](evidence/0.7.2/) identifies the performed
measurements, their methods and firmware.

## Mixer state

| Area | oscmix-desk configuration and verification | Pinned backend / existing GTK mixer |
| --- | --- | --- |
| Input and playback routing | Mono channels or odd/even stereo pairs, route level, linked/unlinked outputs and optional output volume. Conflicting pair requirements are refused. Input matrix reports can be checked; playback writes remain unverifiable. | The backend accepts mix level/pan messages. GTK provides output selection and mix faders. It does not edit desk route declarations or run the desk's validation. |
| Arbitrary pan or a complete matrix editor | No general pan field or arbitrary matrix syntax. Export omits states the route syntax cannot reproduce. | Backend mix messages include pan. GTK's output-pan control is separate from a complete declarative matrix. |
| Channel faders and switches | The register table defines which flat input/output options are writable on each channel. Some reports, such as output mute, are unreliable and retain that classification. | GTK binds faders, mute, stereo, phase, FX and other channel controls where its channel flags enable them. |
| Microphone and instrument inputs | Gain, autoset, hi-Z and supported reference levels have channel-specific domains. 48 V is observed but has no writable config domain. | Backend and GTK expose phantom-power writes. Connecting a microphone does not make desk enable 48 V. The 0.7.1 BETA 58A test kept it off. |
| Three-band EQ and low cut | Nested input/output sections configure the supported parameters. These use REMEMBER by default; declaring a value supplies an initial setting. | Backend handlers and GTK parameter controls exist for both families. |
| Dynamics and auto level | Nested sections expose the register-table parameters, including thresholds, times and gains. | The backend exposes the parameters. GTK binds the enable switches; its channel code does not bind the nested parameter editors. |
| Room EQ and crossfeed | Supported output settings and nine-band Room EQ are configurable. Delay uses the backend's OSC units. | Backend handlers exist. No Room EQ or crossfeed controls are bound by the pinned GTK channel code. |
| Output volume calibration | No `volumecal` config option or declared register. This is separate from ordinary output volume. | The backend has a setter/reporter; no corresponding control is bound by the pinned GTK channel code. |
| Reverb and echo | Global sections expose their declared parameters, with parameter-specific numeric validation and comparisons. | Backend and GTK provide controls. Their bindings are not identical: GTK uses `/reverb/damp`, while the backend declares `/reverb/highdamp`; GTK width bindings use integers while the backend uses fixed-point floats. Full GTK equivalence is not claimed. |
| Control room | Main output, mono, mute-enable, dim and recall settings are supported, with the register table's PIN/REMEMBER policies. | Backend handlers and GTK controls exist. |
| Clock and hardware settings | Supported clock-source/word-clock and hardware options can be declared. Sample rate, DSP version/load and CC status are observations, not configurable rate-selection commands. | GTK displays the reported sample rate and binds clock/hardware controls. Opening an ALSA stream is a separate operation; the backend's sample-rate node has no setter. |
| DURec | No transport, file deletion or recorder-management CLI/config interface. | The backend has recorder commands and reports; GTK has transport/file controls. |
| Names and internal loopback | Their known write-only paths have no config domain. They are not a route-export or snapshot restoration mechanism. | The backend declares setters. Its output-loopback addressing issue is documented in [numeric contracts](NUMERIC-CONTRACT.md); it was checked offline and was not used for hardware measurements. |

The desk's supported options come from [the device/register table](../src/oscmix_desk/devices.py)
and [section handling](../src/oscmix_desk/sections.py), not from the number
of controls drawn in an upstream window. The backend's [node table](https://github.com/michaelforney/oscmix/blob/f2fdd5ec78338848754aad32cc07f3440de63395/oscmix.c),
[GTK channel bindings](https://github.com/michaelforney/oscmix/blob/f2fdd5ec78338848754aad32cc07f3440de63395/gtk/channel.c)
and [GTK global bindings](https://github.com/michaelforney/oscmix/blob/f2fdd5ec78338848754aad32cc07f3440de63395/gtk/main.c)
are separate sources of capability information.

## Desk operations and host integration

| Operation | Current interface and limits |
| --- | --- |
| Preview | `--dry-run` parses and prints the planned writes, including without a connected interface. `--diff` compares the effective desk with reported state without applying it. |
| Profiles | `--list-profiles`, `--profile NAME` and `--no-profile`; switches validate, lock, apply and report an outcome. A profile is partial declared state, not a complete device snapshot. Machine identity belongs to the main config. |
| Apply results | Verified, written but unverifiable, refused before writes, or written in part. A partial apply is not automatically rolled back. |
| State ownership | Register defaults select PIN or REMEMBER. `[pin]` overrides cover flat input/output options; there is no global/nested override syntax. |
| Inspection and export | `--snapshot` retains reported values; `--dump-config` exports expressible reported settings and explains omissions. Neither recovers the unreadable playback matrix. |
| Lifecycle | The user service handles start, hotplug/resume and SIGHUP reload, with device identity checks and cooperating-writer locks. Verification follows the initial apply. There is no continuous reconciliation loop. |
| Metering | The backend streams meters and GTK displays them. Release measurement scripts consume them; there is no desk live-meter UI or recording application. |
| Desktop audio | `--pipewire-sinks` generates named stereo sinks mapped onto hardware playback channels. PipeWire routes application audio; desk configures the hardware mixer. Neither replaces the other. |
| Installation | Source installation has preflight, manual operation and explicit activation; upgrades preserve service state. DEB/RPM/Arch packages use the same staging contract and provide per-user setup, migration and recovery. |
| Playback mode | UCX II playback writes validate the exact active ALSA/USB mode before each write phase. Known incompatible modes are refused; idle playback is explicitly reported as unvalidated. |
| Desktop companion | Deferred. The launcher opens upstream GTK; there is no implemented desk GUI for profiles, previews, outcomes or installation. |

Upstream GTK and raw OSC clients do not use the desk's writer lock.
Changes they make can become observed REMEMBER state or be replaced by
declared PIN state on the next applicable desk operation. They do not
rewrite `routing.conf`.

## Hardware boundary

The measured device is one UCX II with USB firmware 3.01 / DSP 36.
Input/output numbering at Single Speed is analog 1–8, S/PDIF 9/10,
AES 11/12 and ADAT 13–20; headphones are outputs 7/8. Register addresses,
USB stream channels and usable physical digital channels differ at higher
rates. Use the [measured per-mode table](evidence/0.7.1/sample-rates.md).
The runtime checks live USB playback modes against the recorded capacity
table. Rate selection belongs to the audio application or audio server.

Register read-back establishes reported state. The routing measurements
establish effects at the device's meters, and the microphone experiment
also captures actual ALSA PCM. Identity and concurrent-writer checks use
simulated devices and real CLI processes.
