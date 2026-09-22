# oscmix-desk 0.7.1

Corrects mono routing, digital mute, numeric verification and measurement
restoration. The oscmix backend pin is unchanged.

## Changes

- **Mono routes address only their named channels.** Source and destination
  pairs are explicitly unlinked first. Previously, existing stereo links
  could fold an even-channel address onto its neighbour and change extra
  mix cells. Configurations with conflicting mono/stereo requirements are
  refused before writing.
- **Unlinked stereo mute is digital silence.** `level = -65` bypasses gain
  compensation. Unlinked boosts above 0 dB are rejected instead of silently
  becoming unity gain.
- **Verification follows the parameter encoding.** Scalar comparisons
  account for OSC Float32, fixed-point rounding and input-gain truncation.
  Nonfinite values, fractional integers and wire/storage overflow are
  rejected. Malformed reports cannot falsely confirm a setting.
- **Exports preserve small values.** Snapshots and config exports retain
  sub-tenth precision. Matrix states that the route syntax cannot reproduce
  are omitted with an explanation; playback routing still needs its original
  declarations.
- **The sweep restores permitted state between passes.** It retains all
  message arguments and type tags, checks backend identity and source
  fingerprints, stops on unresolved drift and records interrupted/failed
  restoration. Phantom power and protected reference levels are excluded
  from restoration as well as probing.
- **Installer actions require the intended user manager.** Both `HOME` and
  the effective `XDG_CONFIG_HOME` must match before changing files or service
  integration. Uninstall refuses a conflicting configuration sharing the
  same runtime.

## Qualification

The corrected runtime is `abd859486ee8b61e7ed2567545642d202595e842`;
attached records identify its files by SHA-256. Later release metadata and
workflow commits do not change that runtime.

- 1,677 tests pass, with two empty-parameter skips. Python 3.9–3.14,
  five repetitions, a 200-cycle soak and fifteen fault repetitions pass.
- Coverage including branches is 97.58%, above the 97% gate.
- The final mutation counts are recorded in `software-qualification.json`.
- A complete check with the physical interface switched off passes.
  Reconnection starts the installed service automatically and completes its
  verifier. All 2,252 readable messages match the original state.
- Actual 0.6.11 and 0.7.0 installation, upgrade and rollback paths are tested
  in isolated rootless and redirected system-file layouts.

Hardware: **Fireface UCX II, serial 24216011, USB 3.01 / DSP 36**;
oscmix **`f2fdd5ec78338848754aad32cc07f3440de63395`**. The measured running
binary SHA-256 is
`3336bafe9a1fcdc4f49796ba2ac2ee4b6c37a4e2625fb2b0adbaa8a64624002f`.

The corrected sweep confirms **1,888 entries**, deliberately skips **14**
reference-level entries and leaves **no unrestored state**. Separate checks
cover mono input/playback selection, linked/unlinked gain and mute, and all
five configured playback routes. Actual 24-bit/48-kHz microphone capture
confirms input 1 with a BETA 58A, phantom power off and temporary gain restored;
no speech recording is published or retained.

Twenty-four USB/ALSA rate/mode combinations were measured. Eighteen pass;
six are incomplete or fail: the 20-channel mode carries only 16 playback
channels at 88.2/96 kHz, and 16/20-channel modes fail at 176.4/192 kHz.
The 8/14-channel modes pass at those higher rates. Every attempt restores
48 kHz and the original readable state. See the
[per-mode results](https://github.com/relative23/oscmix-desk/blob/v0.7.1/docs/evidence/0.7.1/sample-rates.md).
This does not add automatic live-rate validation or qualify physical digital
connectors.

## Installation and limits

Use the verified source archive or tag **v0.7.1**, then `./install.sh` from
the intended user's login session. Read
[upgrade notes](https://github.com/relative23/oscmix-desk/blob/v0.7.1/docs/UPGRADING.md)
for newly rejected configurations and backup/rollback steps. Checksums and
the GitHub attestation bundle accompany the archive and evidence.

0.7.1 retains the source installer; it is not a PyPI or native-package
release. The separately tested portable installer and native packages remain
on the installation branch. The proposed desktop companion is deferred.

Playback matrix read-back remains unavailable. Hardware meters establish
routing effects, not independent analog connector performance. Physical
Room EQ delay still needs an external return measurement; existing delay
numbers are labelled OSC units, not seconds. Two physical devices, other
Fireface models and external digital/clock configurations are unqualified.
