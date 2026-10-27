# Which layer provides which features

What the desk configures and verifies, compared with the pinned upstream
backend `f2fdd5ec78338848754aad32cc07f3440de63395` plus the
[backend/GTK patch series](../patches/backend-series.json). Listed GTK controls
describe source bindings, not a claim that every control was hardware-tested.

## Mixer state

| Area | oscmix-desk configuration and verification | Pinned backend / existing GTK mixer |
| --- | --- | --- |
| Input and playback routing | Mono channels or odd/even stereo pairs, route level, linked/unlinked outputs and optional output volume. Conflicting pair requirements are refused. Input matrix reports can be checked; playback writes remain unverifiable. | The backend accepts mix level/pan messages. GTK provides output selection and mix faders. It does not edit desk route declarations or run the desk's validation. |
| Whole-state desk (0.9.0 development) | `[desk] base = clear-inputs | clear-submixes | straight-playback | daw` (default `keep`) writes, beneath the routes, every input and/or playback cell no route sets: silenced at the backend's explicit zero, or playback N to output N at 0 dB. A base links nothing to silence a cell: where no route declares a pair's link it writes every channel's register, which the backend folds onto a linked pair, and compares only the register the device always reports. Straight playback is declared as routes of the base, linking a playback or output pair only when the desk sets both channels alike, never an input pair; a mirror target gets its source's. Written on an explicit start or switch, then REMEMBERed. `total-reset` (channel effects too) needs measured defaults and is not offered. | Up to 800 mix writes per switch for `clear-submixes` on an otherwise empty desk; not yet measured on hardware as a whole. |
| Watching the device (0.9.0 development) | `[desk] enforce = watch` in routing.conf keeps one reader connection open: a device report that contradicts a value the desk pins, once the reports rest for two seconds, leads to the reconcile a reload runs. Reports during desk's own operations, REMEMBER values, a base and AutoSet gains are never a reason; a register the device sets back three times within a minute is left to it until the desk changes or a reload. Profiles inherit the setting and may not set it. | Event-driven, so it sees only what reaches subscribers: measured so far are link changes, which the device announces itself, and every value of a state dump another client asks for (the GTK mixer when it connects). Whether the device announces other front-panel changes is not yet measured; tested with a scripted reader only. |
| Output mirror (0.9.0 development) | `[output:N] mirror = S` or `= S/S+1` declares every route of the source output again for the mirror, without its output volume, so each apply keeps the two submixes equal (manual 27.3). Mismatched widths, chains, shared targets and mirror targets with routes of their own are refused when the config loads. Only declared routes are mirrored. | Writes ordinary mix cells and links; a mirror gets no desktop sink of its own. |
| Route pan (0.9.0 development) | `pan =` accepts integers -100..100 for linked stereo source and output pairs with level above -65 dB. Input routes use DEVICE feedback; playback routes are re-established. Mono, split-output and nonzero pan at the mute floor are refused before writing. Export omits states the route syntax cannot reproduce. | Backend mix messages include pan. GTK's output-pan control is separate from declarative route pan and a complete matrix editor. |
| Channel faders and switches | The register table defines which flat input/output options are writable on each channel. Some reports, such as output mute, are unreliable and retain that classification. | GTK binds faders, mute, stereo, phase, FX and other channel controls where its channel flags enable them. |
| Microphone and instrument inputs | Gain, hi-Z and supported reference levels have channel-specific domains. The 0.9.0 development tree accepts remembered `autoset` Boolean flags on UCX II inputs 1–4. Repairs preserve AutoSet-owned gains and linked partners; explicit gain changes require fresh off confirmation. Direct warm linked flags and restoration were measured, not overload protection. 48 V is observed but has no writable config domain. | The backend has AutoSet and phantom-power writes; GTK exposes them. Connecting a microphone does not make desk enable 48 V. |
| Three-band EQ and low cut | Nested input/output sections configure the supported parameters. These use REMEMBER by default; declaring a value supplies an initial setting. | Backend handlers and GTK parameter controls exist for both families. |
| Dynamics and auto level | Nested sections expose the register-table parameters, including thresholds, times and gains. | The backend exposes the parameters. GTK binds the enable switches; its channel code does not bind the nested parameter editors. |
| Room EQ and crossfeed | Supported output settings and nine-band Room EQ are configurable. Delay uses the backend's OSC units. The device runs Room EQ on at most 16 outputs and Crossfeed on at most 4 stereo outputs; Room EQ on a linked pair counts both channels. A config or a switch that would exceed either, counting the outputs it does not set as the device reports them, is refused before any write; a start, repair or reload instead leaves those switch-ons to the device, says so, and applies the rest. | Backend handlers exist. No Room EQ or crossfeed controls are bound by the pinned GTK channel code. |
| Output volume calibration (0.9.0 development) | `volumecal =` accepts -24.00..+3.00 dB in 0.01 dB steps on outputs 1..20. DEVICE feedback is verifiable; PIN is the default. This is separate from ordinary output volume. | The backend has a setter/reporter; no corresponding control is bound by the pinned GTK channel code. |
| FX send and return (0.9.0 development) | `fx =` in input/output sections sets the send/return from -65 to 0 dB; the backend rounds to 0.1 dB. DEVICE feedback is verifiable. FX is remembered after initial application or explicit selection; `[pin] input.fx = pin` and `output.fx = pin` override that policy. | The backend has `/input/N/fx` and `/output/N/fx`; GTK binds FX send/return. |
| Output balance (0.9.0 development) | `pan =` in output sections accepts integers from -100 to 100. DEVICE feedback is verifiable; REMEMBER is the default, with `[pin] output.pan = pin` available. Direct warm linked measurements cover all 20 outputs; -100/+100 were measured via output 1. Linked partners follow the change. | The backend and GTK have output-pan controls, separate from route pan and a complete matrix editor. |
| M/S processing (0.9.0 development) | `msproc = true` or `false` in input sections; verifiable DEVICE feedback, REMEMBER by default, `[pin] input.msproc = pin` for later repair. Direct warm linked measurements cover all 20 inputs, with independent unlinked tests on 13/14. Register flags were measured; acoustic decoding and cold-start completeness were not qualified. | The backend has `/input/N/msproc`; GTK binds M/S processing. |
| Playback mute (0.9.0 development) | `mute =` in playback sections re-establishes the write-only state; REMEMBER is the default. Both members of a pair must be declared and agree. OFF requires the complete declared pair-to-20-output matrix, including silence; unknown cached columns cannot authorize unmute. Apply writes temporary ON before that matrix and final OFF after it. | `/playback/N/mute` has no DEVICE reporter. The prepared backend writes the complete column(s), including zero cells and repeated ON/OFF, in one bounded MIDI frame; this does not establish an atomic audio transition. |
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
| Preview | `--dry-run` prints planned writes even without an interface. With `--profile NAME`, it also identifies previously declared route crosspoints the target leaves undeclared and link/partner effects. `--diff` compares the effective desk with reported state without applying it. `--diff-profile A B` compares what two desks set -- profiles, or `:main` for routing.conf -- including policy changes, without a device; it exits 3 when they differ. |
| Profiles | `--list-profiles`, `--profile NAME` and `--no-profile`; switches validate, lock, apply and report an outcome. A profile is partial declared state, not a complete device snapshot. Machine identity belongs to the main config. |
| Apply results | Verified, written but unverifiable, refused before writes, or written in part. A partial apply is not automatically rolled back. |
| State ownership | Initial application and explicit profile selection write declared starting values. Repair and later reconcile retain every REMEMBER value, including missing or matching feedback. PIN permits restoration subject to link/partner and playback checks. `[pin]` overrides cover flat input/output options; there is no global/nested override syntax. |
| Inspection and export | `--snapshot` retains reported values; `--dump-config` exports expressible reported settings and explains omissions. Neither recovers the unreadable playback matrix from the device. `--dump-config --include-written` adds the playback routes and mute flags desk itself last wrote, from its write journal beside the device lock (`/run/oscmix-desk/<interface>.written.<uid>.json`, one per user, this boot only; a reader takes each register's latest write), in a section marked unconfirmed; mute flags stay comments. `--capture NAME` saves that state, journal included, as the new profile NAME: every value declared (remembered ones as starting values a switch writes) except gains AutoSet owns, `[desk] base = clear-inputs` so the input routes are the whole monitoring matrix, playback cells desk did not write left alone, device and ports inherited from routing.conf; it never replaces a file, and the profile appears only after it has loaded. |
| Runtime diagnosis | `--status [--json]` inspects config/profile, installation, exact backend/device/control endpoint, service, playback mode and GTK protocol without OSC or audio operations. JSON schema 2 removes reply-port contention and never equates service readiness with verification. |
| Lifecycle | The user service handles start, hotplug/resume and SIGHUP reload, with device identity checks and cooperating-writer locks. Verification follows the initial apply. There is no continuous reconciliation loop. |
| Metering | The backend streams meters and GTK displays them. Release measurement scripts consume them; there is no desk live-meter UI or recording application. |
| Desktop audio | `--pipewire-sinks` generates named stereo sinks mapped onto hardware playback channels. PipeWire routes application audio; desk configures the hardware mixer. Neither replaces the other. |
| Installation | Source installation has preflight, manual operation and explicit activation. DEB/RPM/Arch packages share payload staging and maintenance rules, as do the OpenRC/runit adapters, the opt-in NixOS module, Silverblue layering and the signed APT/RPM repositories. |
| Playback mode | UCX II playback writes validate the exact active ALSA/USB mode before each write phase. Known incompatible modes are refused; idle playback is explicitly reported as unvalidated. |
| Existing mixer | Matching `oscmix-desk-gtk` packages provide the coordinated upstream GTK. The launcher checks protocol and exact backend identity. Desk and GTK share observations; GTK edits are inhibited during whole desk operations and are never replayed after busy/disconnect. |

The coordinated GTK uses the backend lease, while desk also holds its file
lock for configuration and marker decisions. The old standalone UDP mode is
uncoordinated and rejected by desk. Physical controls and direct raw-device
access remain outside the lease. Manual changes can be retained as REMEMBER
state or replaced by PIN on an applicable desk operation; they never rewrite
`routing.conf` automatically.

## Hardware boundary

Hardware measurements come from one UCX II with USB firmware 3.01 / DSP 36.
Input/output numbering at Single Speed is analog 1–8, S/PDIF 9/10,
AES 11/12 and ADAT 13–20; headphones are outputs 7/8. Usable USB playback
channels per mode (channels / USB alternate setting):

| Rate | 20 / alt 1 | 16 / alt 2 | 14 / alt 3 | 8 / alt 4 |
| --- | --- | --- | --- | --- |
| 44.1, 48 kHz | 1–20 | 1–16 | 1–14 | 1–8 |
| 88.2, 96 kHz | only 1–16 carry audio | 1–16 | 1–14 | 1–8 |
| 176.4, 192 kHz | fails | fails | 1–14 | 1–8 |

The runtime checks the live USB playback mode against this table before
writing. Rate selection belongs to the audio application or audio server.
Register read-back establishes reported state, not audio; routing effects are
measured at the device's meters.
