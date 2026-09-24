# oscmix-desk 0.7.2

Adds easier Linux installation, native packages and validation of the
UCX II's active USB playback mode. Configuration syntax and the measured
oscmix backend pin remain unchanged.

## Changes

- **Install files before enabling hardware control.** `./install.sh --check`
  reports prerequisites without changing the host. A new installation
  starts no mixer until explicitly enabled; upgrades preserve the existing
  service state. `--manual` supports foreground operation without systemd.
- **Native DEB, RPM and Arch packages share the source installer's payload.**
  `oscmix-setup` checks the installation, initializes a desk on request and
  enables service integration explicitly. Migration backs up the old source
  installation and preserves config, profiles and the active marker.
  Package updates refuse a running mixer and block automatic startup across
  an interrupted update or reboot until package repair succeeds.
- **Writes check the exact device's active playback mode.** ALSA card,
  serial, USB alternate setting and hardware PCM parameters must agree.
  Unavailable playback channels and the measured failing Quad-Speed modes
  are refused. Checks repeat between write phases; a later refusal reports
  the writes already submitted and preserves the profile marker.
- **Idle and offline states remain explicit.** A stopped PCM has no
  validated live mode. The desk does not choose the rate or control a DAW's
  subsequent clock changes. Dry-run and diff retain their existing roles.
- **Evidence handling is stricter.** Incomplete mutation runs cannot pass;
  backend metadata cannot inherit another repository's identity; generated
  bytecode does not change source fingerprints. Hardware reports retain
  each measured peak independently, including exact 0 dBFS and missing data.

## Installation

The release includes x86_64 packages for **Ubuntu 24.04 (DEB), Fedora 44
(RPM), openSUSE Leap 16 (RPM) and Arch**, plus a verified source archive.
Select the artifact for your distribution; verify `SHA256SUMS` against
the attached GitHub attestation before installing. Native packages include
the pinned backend and use the headless path. Source builds can also build
the optional upstream GTK mixer when its dependencies are present.

After native installation, use `oscmix-setup --check`, then
`oscmix-setup --init-config` and `oscmix-session --dry-run --timeout 0`.
Review the configuration before `oscmix-setup --enable`. Source users use
`./install.sh --check`, `./install.sh`, and explicitly opt in with
`./install.sh --no-build --enable` after reviewing the desk.

Read the [installation and migration guide](https://github.com/relative23/oscmix-desk/blob/v0.7.2/docs/INSTALLATION.md),
[upgrade guide](https://github.com/relative23/oscmix-desk/blob/v0.7.2/docs/UPGRADING.md)
and [artifact verification recipe](https://github.com/relative23/oscmix-desk/blob/v0.7.2/docs/RELEASE-ARTIFACTS.md).
The core requires Python 3.9+ and uses only its standard library. `pip`
alone is not the whole-product installation path. The proposed desk GUI
remains deferred.

## Qualification

The final runtime, tests, tools and installation files are from
**`e9ad292475c55c9bb965fc933191e13b30c4dcab`**. Attached qualification fingerprints cover 146 tested
files and 12 installation support files; later documentation and evidence
commits preserve those bytes.

- **1,798 tests pass**, with two empty-parameter skips. Python 3.9–3.14,
  five full repetitions, the 200-cycle soak and fifteen fault repetitions
  pass. Python 3.9 has 32 additional skips for the 3.10+ standard-library
  inventory; those checks run on every newer interpreter.
- Statement/branch coverage is **97.6096%**, above the 97% gate.
- Fresh mutation: **8,752 total, 6,985 killed,
  1,754 survived, 13 timeouts, 0 uncovered**.
  Score **0.799291** is killed / (killed + survived), with timeouts separate.
- Seven source/manual containers pass: Debian 13, Ubuntu 24.04/26.04,
  Fedora 44, openSUSE Leap 16, Arch and Alpine 3.22/musl. The four native
  targets pass package lifecycle, source migration, recovery, downgrade,
  removal and identical repeated builds. All targets are x86_64 and use
  simulated backends. The separately recorded Ubuntu VM also exercises
  a real user manager and recovery across reboot.
- Real-version source upgrade and rollback paths pass in isolated layouts;
  the final source archive builds identically twice and 51 extracted
  installer tests pass. The actual host is upgraded from 0.7.1 to 0.7.2,
  reaches readiness and reloads in the same process.

Measured durations: full check 154.93 s, coverage
154.52 s, five repeats 807.59 s,
each Python version 153.55–177.05 s,
200-cycle soak 265.04 s, each fault repeat
32.83–36.06 s, mutation
9881.99 s and physical-interface-off check
164.99 s. These are observed test durations on the recorded host.

## UCX II measurements

Hardware: **Fireface UCX II 24216011, USB 3.01 / DSP 36**. All hardware
artifacts use backend **`f2fdd5ec78338848754aad32cc07f3440de63395`**;
its measured running binary SHA-256 is
`3336bafe9a1fcdc4f49796ba2ac2ee4b6c37a4e2625fb2b0adbaa8a64624002f`.

- On **2026-09-24 Europe/Berlin**, the final `e9ad292` runtime confirms
  **1,888 register entries**, skips 14 protected entries and restores all
  **2,252 readable messages** exactly. All **five declared routes** pass
  device-meter checks with a -40 dBFS stimulus through named stereo sinks.
- The same final installation passes the full suite while the UCX II is
  physically off. Power-on starts the service automatically. Two complete
  refreshes match the original state after the verifier finishes; no tone
  is played during the return check. `lifecycle.json` records this result.
- On **2026-09-23**, runtime `a45c80c` measures 13 direct-ALSA combinations:
  **52 linked/unlinked gain/mute cases and 12 zero-write refusals**.
  Runtime `0ade75f` passes **65 known-route checks** in the same modes
  through PipeWire Pro Audio, with explicit AUX mapping. An initial
  176.4-kHz / 14-channel startup xrun is retained alongside two unchanged
  successful repetitions. Earlier service, power-cycle and restoration
  artifacts retain their measured development revision and date.

The 24-mode capacity table identifies the measured boundaries: at
88.2/96 kHz the 20-channel USB mode has 16 active playback channels;
at 176.4/192 kHz the 8/14-channel modes pass and 16/20-channel modes fail.
The new runtime guard applies these results. Each measurement identifies
the actual rate, USB mode, backend and source; full readable state and
saved PipeWire settings are restored after the completed modes.

Playback-matrix writes remain unverified by register read-back; device
meters measure their routing effect. Multi-device identity and concurrent
writers are covered by simulated devices. The Fireface 802 implementation
in the pinned backend is a register stub and is not supported hardware.
All measurement JSON, source/native manifests, checksums and the GitHub
attestation accompany this release.
