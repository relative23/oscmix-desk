# Roadmap

Current plan after **0.7.1**, updated 2026-09-23. The correctness patch has
completed software and UCX II qualification, including corrected mono
routing, numeric comparisons, export and measurement restoration.
The desktop companion is deferred by the maintainer.
The product target is a
**Fireface UCX II across Linux distributions**, with and without a desktop.
This is a portability goal, not a claim that every distribution, init
system, architecture or sample rate has already been tested.

oscmix-desk owns declarative state, lifecycle and verification. Upstream
[oscmix](https://github.com/michaelforney/oscmix) owns the device protocol
and provides the existing live mixer. The standard-library core and its
hardware guarantees remain the foundation for easier installation and
an optional desktop companion.

## Release readiness for 0.7.0

**Closed and published.** The five release-readiness items are complete:
device/writer invariants, the actual 0.6.11 upgrade and rollback, a
verifiable source bundle, evidence/support alignment, and final
qualification. There is no remaining 0.7.0 release blocker in this plan.

The [release](https://github.com/relative23/oscmix-desk/releases/tag/v0.7.0),
[publication record](evidence/0.7.0/release-verification.json),
[hardware manifest](evidence/0.7.0/measurement.json) and
[upgrade guide](UPGRADING.md) hold the results and their provenance.
The full checklist and old proposals are preserved in the
[archived roadmap](history/roadmap-through-0.7.0.md#release-readiness-for-070);
completed work is no longer an open backlog item.

## 0.7.1: correctness and evidence

**Closed and published.** Parser, comparison, export, mono-route and
measurement defects are corrected. The backend pin and supported device
remain unchanged. The [evidence](evidence/0.7.1/) records fresh software
gates, hardware measurements, physical disconnect/reconnect and restoration
of readable state and the known playback routes. Publication is tracked by the
[versioned release](https://github.com/relative23/oscmix-desk/releases/tag/v0.7.1). The
[publication record](evidence/0.7.1/release-verification.json) verifies its
assets, tag identity and matching source archive.

- [x] Enforce explicit write permission during **every sweep restoration**,
  including retries and partner effects. Report protected or missing
  values that remain unrestored; never silently write them back.
- [x] Validate finite numbers, integral integer settings and representable
  wire/backend values before writes. Handle invalid reports without a
  traceback or a false confirmation. Restrict the mute sentinel to the
  signal-level arguments whose backend contract supports it.
- [x] Compare by register/argument semantics in planner, verifier and
  measurement tooling. Preserve actual fixed-point quantization and OSC
  Float32 behavior; a universal `0.5` tolerance is not meaningful for
  seconds, ratios and levels alike.
- [x] Preserve numeric meaning through config export and reparse. Export
  input routes only when known link/pan/level state can be expressed
  faithfully by the existing syntax; name omitted or unknown state.
- [x] Correct the sweep's judgement of changed-but-wrong reports. Retain
  original, requested, encoded and reported values and the comparison
  rule for successful as well as unsuccessful attempts.
- [x] Align current documentation and examples with supported config
  options, physical channel labels, PIN/REMEMBER lifecycle, four apply
  outcomes and partial profile/dump coverage. Qualify historical capability
  and product-comparison claims without rewriting old measurement data.
- [x] Add independent semantic regressions for these gaps. Retain existing
  receive-port, identity, writer-lock, partial-write and persistence tests;
  those mechanisms already exist and do not need replacement designs.
- [x] Qualify the candidate under the [release checklist](RELEASE-CHECKLIST.md),
  including the actual **0.6.11/0.7.0 → 0.7.1 → original-version**
  installation transitions. Fresh hardware measurements follow the
  corrections, with monitoring physically off and readable state restored.
  The physical off/on test also passes.
- [x] Correct mono routing from existing linked pairs and reject conflicting
  mono/stereo declarations. Measure input and playback paths separately.
- [x] Document the [feature surface](FEATURE-SURFACE.md) of config, backend
  and existing GTK controls. Source bindings do not qualify every control
  or physical DSP effect.

The old sweep confirms that reports changed under its historical method;
its tolerance can accept an incorrect value and confirmed findings omit
the actual report. Its counts remain historical results, not proof under
the corrected method. Annotate that limit and retain the original files.
The separately recorded route/meter checks and restoration snapshots must
be assessed on their own methods, not discarded along with a comparison
defect.

The own-code audit also found sub-tenth precision loss in `--snapshot`,
and a mismatch between the Room EQ delay seconds label and RME's physical
duration specification. Snapshot precision is corrected; existing delay
values remain in backend units pending an independent signal measurement.
See [numeric contracts](NUMERIC-CONTRACT.md).

The [software record](evidence/0.7.1/software-qualification.json) records
the final gates, file hashes and durations. Separate hardware artifacts
identify what was measured; publication attestations authenticate released
files, not a test performed by a GitHub runner.

**Release boundary:** no new GUI, packaging platform, full pan/matrix
syntax, global/nested policy-override syntax, continuous polling or OSC
fanout. Higher-rate and physical dual-device qualification remain separate
work unless a reproduced regression in the existing supported setup makes
a specific case necessary. Document stricter rejection of previously
accepted invalid numbers and export omissions in the upgrade notes.

## 0.7.2: playback-mode validation and remaining hardware evidence

**In progress, not released.** Translate the recorded USB playback limits
into runtime checks, qualify higher-rate link/gain/mute behavior, and
retain the separate physical signal/clock evidence gaps below. The
standard-library core, config format and backend pin remain unchanged.
The maintainer's publication condition is completion of the required
work: local commits do not imply permission to push an incomplete release.

- [x] Validate the exact UCX II's active USB playback capacity before
  writes, using recorded ALSA parameters and USB alternate settings.
- [x] Define idle/unknown, inconsistent observation and mid-apply changes;
  test refusals before the first write and exact partial-write outcomes.
  Keep the planner pure and the profile marker unchanged on failure.
- [x] Document offline-preview, register-diff and live-mode limits.
- [ ] Finish H1's available high-rate link/mute and restoration checks.
- [ ] Complete the physical H1/H2 measurements, or make a separate,
  explicit release-scope decision. Missing equipment is not a passed test.
- [ ] Complete fresh candidate release gates and installation checks.

The current setup has a UCX II, monitors, headphones and a microphone,
but no analog return cable or external digital/clock equipment. Physical
output/delay, digital I/O and external-clock tests therefore remain open.
GUI and two-physical-device qualification remain excluded by the maintainer.

## Separate functionality milestone: installation

Installation changes are prepared on their own branch, not included in
the 0.7.2 runtime patch. Their release version and publication date are
not assigned. Completed checks below describe development work, not
features available in 0.7.0 or the 0.7.1 installer. The desktop companion
remains deferred.

The maintainer has requested execution of the installation and hardware
work as well, following the correctness patch. GUI and physical
multi-device work remain excluded. Hardware tasks still require the
appropriate quiet connections and cannot be closed by simulation alone.

| Order | Work | Result required before closing it |
| --- | --- | --- |
| 1 | **I1: installation across distributions** | A common install/staging contract, useful preflight diagnostics, explicit service modes, and a recorded distribution test matrix. |
| 2 | **I2: native distribution packages** | Tested packages/recipes built from the same release and measured backend pin, including migration and recovery. |
| Deferred | **G1: optional desktop companion** | Resume only when requested; complete the feature/interaction design before seeking implementation approval. |

I1 and the read-only preparation for H1 can proceed independently. H1's
channel-capability findings must precede a GUI that offers those channels.
I2 builds on I1's layout and ownership rules, not a second installer.

### I1 / I2 -- Installation comfort

Detailed plan: [installation across Linux distributions](plans/installation.md).

- [x] Separate installing the files from enabling integration and applying
  a desk. Report dependencies, backend revision, active installation,
  device permissions and available service integration before changes.
- [x] Keep a verified release archive and source-build path usable across
  distributions. Use capability detection; distribution names only select
  package hints/recipes. Preserve offline configuration and CLI use.
- [x] Separate the current systemd integration from a manual session mode;
  qualify operation without systemd before claiming it works. Track
  OpenRC/runit automation, musl and immutable/declarative systems explicitly.
- [x] Exercise Debian/Ubuntu, Fedora, openSUSE and Arch families, plus a
  non-systemd environment. Record exact releases, libc, architecture and
  which of install, lifecycle and real hardware were actually tested.
- [x] Build native DEB/RPM packages and an Arch recipe from a shared staging
  layout. Keep the source path for other distributions; define ownership
  and migration from existing per-user installs before publishing packages.
- [x] Verify install, upgrade, interrupted install, downgrade and uninstall
  without losing config, profiles or the active marker. Detect shadowing
  binaries and user-unit overrides. Installation must not silently start
  writing a new default desk to an attached interface.

**Qualification so far:** source build/install and simulated lifecycle on
Debian 13, Ubuntu 24.04/26.04, Fedora 44, openSUSE Leap 16, Arch and
Alpine 3.22/musl, all x86_64. Native lifecycle and repeated-build checks
pass on Ubuntu 24.04, Fedora 44, openSUSE Leap 16 and Arch. An Ubuntu
24.04 VM also exercises the real user service, a 0.7.0 source-to-package
migration and return, SIGHUP with an explicit PIN, and a maintenance fence
that survives a reboot and clears after successful package repair. These
are software tests with a simulated backend, not hardware qualification
on seven distributions. The reusable CI path passes all eleven source/native
targets. A successful GitHub rehearsal also builds and collects native
assets. The prepared tag workflow waits for those jobs before publication;
branch-run attestations are development evidence, not a released package.
The actual 0.7.0 → native package → 0.7.0 file migration also passes on each
native target with a simulated user bus; Ubuntu has the additional real
user-manager VM check. Package publication remains before I2 closes.

The separately qualified implementation and its software matrix are on
[`feature/portable-install`](https://github.com/relative23/oscmix-desk/tree/feature/portable-install).
It has not been merged into the 0.7.1 correctness release.

**Packaging direction:** native packages where tested, a common source
installer elsewhere. `pip`/`pipx` alone do not install the complete host
integration. A Python package remains a possible separate deliverable if
there is a concrete consumer; it is not the main installation project.

## H1 -- Higher sample rates (0.7.2)

Detailed plan: [UCX II sample-rate qualification](plans/high-sample-rates.md).

- [x] Establish a fresh 44.1/48 kHz baseline with firmware, backend pin,
  actual device rate, USB alternate setting, ALSA map and optical mode.
- [x] Measure 88.2/96 and 176.4/192 kHz separately. Distinguish physical
  ADAT capacity, USB stream channels, playback numbering and OSC register
  addresses; do not halve the whole device's channel count by assumption.
- [ ] Check valid and unavailable routes, channel links, refresh/read-back,
  state retention and PipeWire mappings. Exercise the return to the
  baseline; preserve and verify the original configuration and state.
- [x] Derive live USB playback validation from the recordings, add
  regressions for the actual failed combinations and publish a per-mode
  support table. Keep register addresses separate from audio capacity.
- [ ] Qualify physical digital ports, optical S/PDIF mode and external
  clock transitions with the necessary connections. No such equipment
  is present in the current setup.

**Measured:** [24 USB/ALSA combinations](evidence/0.7.1/sample-rates.md),
18 passing and six incomplete/failed, with full restoration after every
attempt and a final five-route PipeWire check. The 20-channel mode has
only 16 active playback channels at Double Speed; Quad Speed passes with
8/14 channels and fails with 16/20. Register addresses remain present.
0.7.1 does not automatically select or validate the live USB/rate mode.
The 0.7.2 development code refuses known incompatible playback modes,
checks again between write phases and explicitly reports an idle stream
as unvalidated. It does not own or continuously enforce the hardware rate.
Separate high-rate link/mute cases, external digital connections and
clock-source transitions remain outside the 0.7.1 measurement.
The explicit support table is complete for the combinations actually
tested; it does not close those remaining H1 checks.

## H2 -- Independent signal and DSP measurements (0.7.2)

- [ ] Measure an analog output through an independent return path,
  including the physical duration of Room EQ delay versus backend units.
- [ ] Compare captured PCM with input EQ/dynamics and `eqdrecord` disabled
  and enabled, using a repeatable source and unchanged gain/clock.
- [ ] Record restored state, actual connections and residual limitations.

The existing microphone capture establishes capture and gain behavior;
it does not establish a controlled EQ/dynamics transfer function. Device
meters and register echoes cannot replace the missing independent signal
path. These checks need additional connections or a repeatable source and
remain open; no claim of physical seconds is added to the delay setting.

## G1 -- Desktop companion proposal

**Deferred by the maintainer on 2026-09-22.** Retain the design materials;
no further GUI design or implementation is scheduled. The existing visual
prototype covers selected flows, not the full supported feature surface.

Existing local drafts in `docs/plans/desktop-app.md` and `docs/design/`
are retained unchanged while the work is deferred. They are not part of
this patch release and have no hardware connection.

- [ ] **Design approval before implementation:** present complete layout
  alternatives, all first-version screens and states, language/toolkit,
  accessibility and task flows. The maintainer selects and approves the
  design before work on the actual GUI begins.
- [ ] Validate setup, profile preview/apply and failure recovery with users
  who do not normally edit `routing.conf`.
- [ ] Prototype a separate, optional GTK 4/libadwaita application on GNOME,
  KDE Plasma and Xfce, covering Wayland/X11 where available. Confirm the
  toolkit/version floor against the distribution matrix before adopting it.
- [ ] Define a versioned boundary to the existing core and preserve its
  device selection, locks, validation, write order and outcome semantics.
- [ ] Prove a small end-to-end flow: select the exact device, inspect the
  proposed changes, apply explicitly, display confirmed/unverified/partial
  outcomes, and reopen with the persisted profile correctly identified.

The initial concept is setup, profiles, route editing and diagnosis. Live
faders, metering, FX editors and a full TotalMix-style mixer are outside
this first scope; users can open the existing upstream mixer.

## Later, with an explicit entry condition

| Open work | When to take it on |
| --- | --- |
| Two physical interfaces and per-device service instances | Two devices are available to measure discovery, concurrent lifecycle and isolation; simulated identity tests alone do not qualify it. |
| Targeted register queries / better read-back | Upstream support or a reproducible backend change exists; measure it before moving the pin. Playback-matrix read-back is still unavailable. |
| Additional init-system adapters and immutable-system packages | The common/manual installation contract is proven and a maintained target environment is available. These stay part of the Linux portability goal. |
| A Flatpak GUI | Host integration can be exposed through a small, tested local interface; a sandboxed frontend alone does not install the host backend and udev rules. |
| Other Fireface models | Real hardware and a measured backend/model exist. UCX II evidence does not qualify another interface. |

## Boundaries that still apply

- The published 0.7.0 installer and the 0.7.1 patch installer require a
  matching systemd user manager. The separately developed manual/native
  installation paths are not part of that patch release.
- An apply is not a hardware transaction. Validation can prevent invalid
  plans; interrupted sends can still leave partial state.
- Playback mix and other write-only settings cannot be verified through
  the current backend. A GUI must retain that distinction.
- The device lock coordinates cooperating desk writers. Upstream GUI or
  raw OSC writers can still change state; the UDP endpoint is not an
  authenticated per-user control interface.
- No continuous background reconciliation, second protocol implementation,
  remote-control web server, automatic clock changes or new DSP engine is
  included in this milestone.

Historical headings about an untested upgrade or fixes missing upstream
are not current blockers: the 0.6.11 transition is qualified in 0.7.0,
and the enum/Room EQ fixes documented in [upstream issues](upstream-issues.md)
are already in the measured pin. Old drafts are retained in
[history](history/), not mixed into the active plan.
