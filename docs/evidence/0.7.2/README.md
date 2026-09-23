# 0.7.2 development evidence

**Not a published or fully qualified release.** These measurements identify
local development commits; the runtime version constant still reads 0.7.1.
They do not replace the published 0.7.1 artifacts. Required physical
signal/digital/clock measurements remain open in the [roadmap](../../ROADMAP.md).

## Active playback and higher-rate links

[sample-rate-links.json](sample-rate-links.json) records 13 combinations:
48 kHz / 20 channels as the control, all four USB modes at 88.2 and 96 kHz,
and the 8/14-channel modes at 176.4 and 192 kHz. Runtime `a45c80c`;
UCX II 24216011, USB 3.01 / DSP 36, unchanged backend
`f2fdd5ec78338848754aad32cc07f3440de63395`. Host: Ubuntu 26.04.1,
kernel 7.0.0-31-generic, ALSA 1.2.15.2 and PipeWire 1.6.2.

There are **52 linked/unlinked gain/mute cases** on playback 5/6 → output
5/6, plus **12 unavailable-playback refusals** that send no register writes.
Every selected combination has a successful run. One initial 176.4-kHz /
14-channel attempt suffered capture/playback startup xruns before any
register write. Two unchanged repetitions passed. The failed attempt,
logs and successful restoration remain in the JSON; the cause is not
established, and this is not an xrun-free or low-latency guarantee.

| Rate | USB channels / alternate setting | New result |
| --- | --- | --- |
| 48 kHz | 20 / 1 | Control: linked/unlinked gain and mute pass |
| 88.2 kHz | 8 / 4, 14 / 3, 16 / 2, 20 / 1 | All four pass; unavailable sources refused |
| 96 kHz | 8 / 4, 14 / 3, 16 / 2, 20 / 1 | All four pass; unavailable sources refused |
| 176.4 kHz | 8 / 4 | Pass; unavailable source refused |
| 176.4 kHz | 14 / 3 | Pass on two repeats after one recorded startup xrun |
| 192 kHz | 8 / 4, 14 / 3 | Both pass; unavailable sources refused |

The earlier [24-mode table](../0.7.1/sample-rates.md) remains the source
for capacity boundaries, including the failed 16/20-channel Quad-Speed
modes. They were not opened again here. The new guard also has regression
fixtures for all 24 recordings, identity/read inconsistencies and changes
between write phases. An idle PCM is explicitly unvalidated; the desk
does not select a rate or prevent a DAW from changing it afterwards.

## Method and restoration

The test opens actual S24_3LE ALSA hardware PCMs without resampling,
correlating `stream0`, `hw_params` and `/clock/samplerate`. Capture bytes
are discarded. A -40 dBFS / 1-kHz stimulus drives the two source channels
separately. Only their four known matrix cells, source/output links and
output 5/6 faders may be written. KRK power is off; headphones are unplugged.
The BETA 58A remains on input 1 with 48 V off.

At route level -12 dB and output fader -20 dB, output peaks must equal the
simultaneous playback peak minus 32 dB within 1 dB; the opposite side must
stay below -110 dBFS. The largest observed gain error is 0.000496 dB.
Mute must remain below -120 dBFS. The analysis floors negative infinity at
-144 dBFS; raw meter reports are retained. Each judged window has at least
12 reports per channel after a 600-ms settling interval.

Source stereo linking remains enabled for both pair routes; `stereo =
false` changes the output link. Unlinking the output adds ten input-matrix
reports for output 6. An initial adapter incorrectly required an unchanged
report count, and another incorrectly expected the source link to be
cleared too. These adapter failures and their separate successful
restorations are retained. The corrected control precedes the rate matrix.

Every successful attempt returns to 48 kHz / 20 channels, restores all
2,252 original readable messages exactly and measures both sides of the
known main route again. Playback matrix state is not readable: this
establishes restoration of the four known cells' effect, not arbitrary
unknown routes. The [final comparison](restoration.json) also matches two
initial and two final complete refreshes, including every type tag and
argument; phantom power remains off.

[hardware-evidence.json](hardware-evidence.json), collected with corrected
reporting tool `a16730f`, then passes all five declared routes through the
named stereo PipeWire sinks at baseline. This checks channel response and
isolation; the separate controlled 5/6 cases above check absolute route
gain and mute. The runtime files are byte-identical between these two
measurement commits.

A read-only probe under the service's declared systemd sandbox settings
also observes an active 96-kHz / 20-channel stream, with `NoNewPrivs=1` and
`Seccomp=2`. This does not establish that Ubuntu's user manager applies
all mount-namespace protections; see the [security model](../../SECURITY-MODEL.md).

## Higher-rate PipeWire mapping

[pipewire-rates.json](pipewire-rates.json) exercises the same 13 combinations
through the existing three named stereo sinks in PipeWire's **Pro Audio**
profile. All **65 route checks** pass: five known routes at each mode.
The observed graph links retain AUX0/1 for KRK, AUX4/5 for main and AUX6/7
for phones. Actual ALSA hardware parameters, USB alternate setting and the
device's reported rate agree before and after each measurement.

This uses runtime `0ade75f`, with a temporary explicit channel map for
both ALSA nodes and a forced PipeWire graph rate. The stimulus is generated
at that same rate. It does not claim automatic profile/rate selection by
oscmix-desk or qualify other desktop profiles. No OSC register writes are
sent by this adapter. After each mode, all 2,252 readable messages,
PipeWire clock settings, node parameters and software-mixer properties
match the originals. A preliminary adapter error occurred before changing
the measurement parameters; its correction and restoration are recorded.

## Service and physical disconnect

[candidate-service.json](candidate-service.json) records startup and SIGHUP
of `0ade75f` in a transient user service with the normal sandbox settings
and the user's existing five-route config. It reaches readiness, reloads
in the same process and preserves all 2,252 baseline messages after both
operations. The original installed 0.7.1 service is then restored. This
is a real service/hardware check, not a host package migration.

[power-cycle.json](power-cycle.json) records the final combined candidate's
`make check` with the UCX II physically off: **1,798 passed, two skipped**,
174.68 seconds including the other checks. USB absence is checked every
half second. After the user switches the device on, the installed 0.7.1
service starts automatically. Two complete refreshes again match all
2,252 original messages. No tone is played during this return check.
The earlier off/on run and its different candidate are retained separately.

## Installation integration

[installation.json](installation.json) records combined candidate
`b537416`: seven distribution containers pass source/manual installation
and simulated lifecycle checks; four pass native-package lifecycle and
byte-identical repeated builds. The targets are Debian 13, Ubuntu
24.04/26.04, Fedora 44, openSUSE Leap 16, Arch and Alpine 3.22; native
packages are exercised on Ubuntu 24.04, Fedora 44, openSUSE Leap 16 and
Arch. All are x86_64 and use a simulated backend for lifecycle tests.

The source archive is reproducible, its extracted installer tests pass,
and actual 0.7.1 → candidate → 0.7.1 transitions pass in both per-user and
redirected system-file layouts. Native tests also exercise migration from
0.7.0, interrupted-install recovery, downgrade and removal. These are
development packages carrying the existing 0.7.1 version constant, not
published 0.7.2 assets. The earlier Ubuntu VM experiment remains historical;
the containers do not provide a real user manager or physical hardware.

The runtime and hardware-measurement tools in `b537416` are byte-identical
to `0ade75f`. The integration adds installer/package behavior; it does not
retroactively change the source identity of earlier hardware recordings.

The later combined-main archive at `7ae0fea` includes the integrated
documentation and public evidence. Two builds are byte-identical and all
51 extracted installer tests pass again. Its separate source identity and
archive checksum are retained in the installation record.

## Complete software qualification

[software-qualification.json](software-qualification.json) records all
**27 passing gates** on combined candidate `b537416`, including log hashes,
durations and fingerprints of the 146 tested files and 12 installation
support files. Those sources match the current development tree exactly.

- `make check`: **1,798 passed, two skipped**. Coverage is **97.6096%**
  across statements and branches, above the unchanged 97% gate.
- Python **3.9–3.14** all pass. The two regular skips are empty read-only
  sub-family parameter sets. Python 3.9 additionally skips 32 standard-library
  import checks because `sys.stdlib_module_names` is available only from
  3.10; each runs on the five newer interpreters. Hypothesis is required.
- Five complete flakiness runs, the 200-cycle soak tests, fifteen
  repetitions of the 70-test fault suite and unit verification all pass.
- A fresh mutation run completes **8,752 mutants** in **10,099.61 seconds**:
  6,982 killed, 1,756 survived, 14 timeouts and none uncovered. The score
  is **0.799039**, calculated as killed / (killed + survived); timeouts
  are separate. No previous verdicts or targeted rejudgments are reused.
  The existing score policy remains unchanged.

Earlier incomplete runs are explicitly unqualified in the record. They
exposed temporary-directory quota/path limits and the now-fixed incomplete
mutation/provenance handling; their partial results are not combined with
this run. Short paths on ext4 avoid those temporary-directory limits.
Separate workflow validation confirms that generated bytecode does not
alter the installation support fingerprints.

## Remaining limits

These are device-meter and USB-playback observations. No external analog
return, ADAT, AES, S/PDIF or clock device is connected. Physical output
transfer, Room EQ delay, controlled input EQ/dynamics capture, optical
S/PDIF mode and external clock transitions remain unmeasured. The measured
PipeWire combinations are limited to the Pro Audio setup above.

Final release qualification and publication remain separate from these
development checks. The version has not been advanced or tagged, the
combined candidate has not replaced the host installation, and no new
all-register sweep is claimed. The current release workflow requires
matching final software, installed-runtime, hardware and sweep evidence;
older artifacts cannot be relabelled to satisfy it. Missing physical
H1/H2 measurements require equipment or an explicit release-scope decision.
The [release readiness record](release-readiness.json) separates completed
development work, missing measurements and the remaining publication steps.

The [RME manual](https://rme-audio.de/downloads/fface_ucx2_e.pdf), section
33.2, distinguishes USB modes and the 8/14-channel choice for 192 kHz.
That is consistent with the capacity observations; it does not substitute
for the missing connector/clock measurements.
