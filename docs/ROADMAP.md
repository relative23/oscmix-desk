# Roadmap

Next milestone: **0.8.0**, started 2026-09-25 with the maintainer's explicit
implementation instruction. The N9 historical source assessment is recorded
privately; N0/N1 corrections and production-path regressions are implemented.
The latest interim `make check` passes 2271 tests (two empty parameter sets
skipped); the earlier coordinated-runtime coverage run passes the 97% ratchet.
N2 now uses one checked, backend-owned connection through desk apply,
verification, repair and profile/reload operations. The fresh versioned
backend/GTK build passes 65 preparation/protocol tests and the actual GTK
fader, lease, start-order, disconnect and identity checks with simulated MIDI.
Actual desk CLI processes and GTK also pass a combined simulated-MIDI test:
two profiles serialize through read-back, GTK cannot write during the operation,
and backend loss preserves the marker without replay after restart. Measurement
tools now use the same checked connection and record exact build provenance.
Development UCX II measurements now distinguish device reports from cached
playback flags and withhold incomplete compound input-mix reports. The
[capability probe](evidence/0.8.0/backend-capabilities.json) found no supported
targeted read or playback-matrix read-back. The maintainer explicitly retains
genuine playback read-back as a release condition; N3 remains open. Readable state and known declared routes were restored after each
probe. Source/native staging now checks the exact backend/bridge/GTK patch
series, and the project signing certificate is prepared. OpenRC and runit now
supervise the same unprivileged source installation with explicit activation,
root-owned registration, shared locks and persistent maintenance. Real Alpine
3.24.2/musl and Void/glibc VMs pass the first absent-device lifecycle checks:
reload during discovery, three crash/restart cycles each, maintenance across a
reboot, source/adapter replacement, stop/disable/removal and preserved user state.
An additional [real-VM startup regression](evidence/0.8.0/native-reload-development.json)
reproduced premature SIGHUP termination through all three reload entry points.
They now share a pinned-process readiness check; both VMs pass refusal before
handler installation and successful reload of the same child afterwards.
The NixOS package and opt-in module use the common payload and user service.
Both package variants reproduce their complete outputs in separate check builds.
A real NixOS 26.05 VM passes inactive installation, explicit activation, selected
user identity, crash/restart, maintenance across reboot, actual generation rollback
and removal with user state preserved; the
[development record](evidence/0.8.0/nixos-development.json) identifies the tested
definitions and payload. Fedora Silverblue 44.1.7 now layers the common RPM pair,
with live-host maintenance kept outside its composition sandbox. A real VM passes
installation, activation, three crash/restart cycles, four deployment reboots,
package-revision upgrade, actual rollback, removal and preserved user state.
The [Silverblue development record](evidence/0.8.0/silverblue-development.json)
also identifies the pending-deployment recovery and ordinary Fedora DNF checks.
Native qualification now uses the coordinated C/GTK integration test, including
the actual launcher and CLI. All six native development targets pass, including
the new Debian 13 and Ubuntu 26.04 DEBs. Core/GTK repeat builds are byte-identical;
actual dpkg/rpm/pacman transitions, matching-version refusal, source migration,
maintenance and retained user files pass. Ubuntu 24.04 also passes nested GNOME,
KDE and Xfce with the coordinated mixer. The
[native development record](evidence/0.8.0/native-development.json) identifies
the sources, images, package hashes, installed payload and desktop results.
These packages remain unpublished development builds; signed repository clients
and final 0.7.3-to-0.8.0 transitions have separate gates. Signed staging now passes
actual APT/DNF/zypper lifecycle and fault checks on all five repository targets,
using a disposable certificate and loopback HTTP. This includes killed transactions,
full recovery and a conditional-GET regression for changed indexes. The
[repository development record](evidence/0.8.0/signed-repository-development.json)
identifies the exact generator, fixtures, snapshots and managers. Production-key
lifecycle and published HTTPS checks remain open. A subsequent key matrix found
native acceptance of expired/revoked signatures; those checks remained failing.
The approved [repository-scoped client hooks](decisions/0036-repository-client-verification.md)
now pass all five development client matrices, including current-key policy,
missing-verifier recovery and unaffected unrelated repositories. A same-version
DNF5 5.4.5 control still fails without the hook. The
[client record](evidence/0.8.0/repository-client-development.json) identifies the
exact sources and images. CI now invokes the strict repository matrix, including
the actual separate bootstrap package. All five targets pass opt-in activation,
packaged subkey rotation, revoked-key cache invalidation, rollback with retained
revocations, killed helper updates and recovery, and preserved administrator edits.
APT also passes refusal and recovery with its native lists lock held. See the
[bootstrap record](evidence/0.8.0/repository-bootstrap-development.json). Inclusion
of the third package in native release bundles and signed indexes now passes all
six native and five repository matrices, including independent helper updates
retaining the authenticated core/GTK pair. The
[bundle/index record](evidence/0.8.0/repository-native-bundles-development.json)
identifies those development inputs and results. An encrypted key archive copied
to removable media was read after remounting and both key roles were successfully
restored in isolated temporary keyrings. Physical removal and separate passphrase
custody still await confirmation; publication and final HTTPS qualification remain
open.
Complete-site preparation now verifies all five signed channels before exposing
an archive or deployment directory. Release-mode verification also authenticates
the original build manifests and checks signed RPMs against their original
attested content. A [fresh development record](evidence/0.8.0/repository-publication-input-development.json)
covers full-site assembly, renewal/rotation and a separate provenance probe using
actual published 0.7.3 inputs. Pages deployment, final signed-output attestation,
published HTTPS and final 0.8.0 payloads remain unqualified.
The subscription helper refuses installation/activation on OSTree hosts before
modifying trust or sources. A [limited actual-Silverblue guard check](evidence/0.8.0/repository-ostree-development.json)
passes; status and disabling/removing a previous subscription remain available.
Silverblue continues to use authenticated RPM artifact layering.
RPM rotation now re-signs retained versions while preserving their previous
download paths and indexing exactly one signature form per version. Fresh Fedora
and openSUSE clients pass downgrade and upgrade after revocation of the old subkey.
Complete five-channel assembly and the six historical build-provenance inputs
also pass; `make check` passes 2280 tests with two empty parameter skips in 128.94 s.
The [rotation record](evidence/0.8.0/repository-rpm-rotation-development.json)
identifies the development inputs. Recovery after an already invalid publication
signature, final signed-output provenance and published HTTPS remain open.
A fresh actual Silverblue deep-sleep probe found that the old resume hook
missed an active user service while `user.slice` was frozen. Resume now queues
a bounded system service ordered after the sleep operation. The
[actual-VM regression](evidence/0.8.0/systemd-resume-development.json) passes
six deep-entry/exit cycles, including failed reload, recovery and an inactive
desk. Actual deployment rollback and return preserve user state and restore
the respective integration files. A separate native RPM failure probe found
premature fence removal after a failed manager reload; the ordering correction
and permanent container regression now pass on both RPM targets. All six native
targets pass the updated packaging matrix; Ubuntu 24.04 also passes GNOME/KDE
Wayland and Xfce/X11. The
[packaging regression record](evidence/0.8.0/resume-packaging-development.json)
identifies the new artifacts and distinguishes expected scriptlet failures
from successfully repaired transactions.
A separate [NixOS resume probe](evidence/0.8.0/nixos-resume-development.json)
passes three active and one inactive deep-entry/exit cycles through the existing
native stop action on systemd 260.4. It needed no module change. Exact generation
rollbacks restore the previous disabled system and preserve user state.
Subsequent poweroff panicked after filesystem unmount. A fresh control without
the installed mixer, user unit or mixer resume action reproduces the same panic
after four deep cycles; cold poweroff without suspend succeeds. The underlying
VM/platform cause remains unresolved. Resume ordering is measured, but clean
shutdown after resume is not qualified on this combination.
These development checks do not close the remaining
device-I/O, concurrency, suspend or final-candidate VM gates. Final hardware,
installation, service/VM and repository qualification remain open.
All proposed 0.7.4 corrections are included; there is no separate 0.7.4 milestone.

Latest published release: **0.7.3**. Its implementation, qualification and
publication are complete: corrected read-back classification, clearer
results, runtime diagnosis, profile previews and optional upstream GTK
packages. The [qualification records](evidence/0.7.3/) identify the source,
software gates, desktop environments and measured UCX II state.
Publication uses the [versioned release](https://github.com/relative23/oscmix-desk/releases/tag/v0.7.3)
and its authenticated source/package artifacts.
The [publication record](evidence/0.7.3/release-verification.json) verifies
the tag, GitHub jobs, downloaded packages and attestations.
The separate desk desktop application is deferred by the maintainer.
The product target is a
**Fireface UCX II across Linux distributions**, with and without a desktop.
This is a portability goal, not a claim that every distribution, init
system, architecture or sample rate has already been tested.

oscmix-desk owns declarative state, lifecycle and verification. Upstream
[oscmix](https://github.com/michaelforney/oscmix) provides the device-protocol
implementation and the existing live mixer. The standard-library core and its
hardware guarantees remain the foundation for easier installation and
an optional desktop companion.

## 0.8.0: consistent observations, coordinated control and Linux integration

**Implementation in progress; not qualified or released.** Fix the remaining observation
and REMEMBER write-selection defects first, then implement shared use of
the existing upstream mixer and complete the selected Linux integration
and package-repository work. The separate desk GUI stays deferred.

Detailed contracts, dependencies, target environments and release gates:
[0.8.0 reliability and integration](plans/0.8.0-reliability-integration.md).

- [ ] **N0: one observation rule for all confirmations.** Use the latest
  decoded report in the open observation window for verification, the
  link barrier and link-sync reapply. A contradictory report revokes an
  earlier match. Distinguish contradiction from silence; do not release
  a dependent matrix write on a known wrong link state.
- [ ] **N1: preserve REMEMBER after the initial apply.** Separate initial
  application, repair after application and later selective reconcile.
  Missing read-back must not authorize resetting a remembered fader.
  Keep explicit profile application and PIN overrides deliberate, and
  check dependencies and stereo-partner effects of retained settings.
- [ ] **N2: coordinated upstream-mixer and desk operation.** Establish one
  backend/device ownership contract, fresh reply delivery to both clients
  and ordering of writes across an entire desk operation. Solve read-back
  and writer coordination together; qualify restart, disconnect, lost
  clients and overload before claiming simultaneous operation.
- [ ] **N3: targeted queries and genuine read-back.** Establish what the
  device/backend can actually report, including the playback matrix;
  implement and measure supported improvements through the existing
  backend. Written or cached values do not become hardware confirmations.
  Track feasibility and a delivered read-back capability separately.
- [ ] **N4: OpenRC and runit integration.** Add tested adapters for Alpine
  and Void using the common installer, explicit activation, selected
  runtime user, shared locks, maintenance fencing and lifecycle contract.
- [ ] **N5: declarative and immutable hosts.** Deliver a NixOS package and
  module plus a Fedora Silverblue host-installation path; qualify their
  actual activation, reboot, upgrade and rollback behavior in VMs.
- [ ] **N6: signed native package repositories.** Publish tested APT/RPM
  channels with authenticated metadata, key lifecycle, complete core/GTK
  version pairs and verified package-manager upgrades and downgrades.
  Extend native DEB qualification to Debian 13 and Ubuntu 26.04 before
  publishing channels for them; retain the existing native/source targets.
- [ ] **N7: compatibility and accurate operating instructions.** Update
  PIN/REMEMBER, outcomes, launcher/status, service management, installation,
  security and recovery documentation with the implemented contracts.
  Keep partial profiles, readiness and read-back distinct. Reconcile
  historical claims with later measurements rather than repeating them.
- [ ] **N9: source-based architecture assessment and simplification.** Read
  the actual 0.7.1 → 0.7.2 → 0.7.3 source diffs, then reassess the 0.8.0
  changes. Identify duplicated decisions, unnecessary indirection and
  unclear state ownership; simplify demonstrated problems while retaining
  the established safety and diagnostic contracts. Assess code structure
  separately from test counts, coverage, CI and hardware qualification.
- [ ] **N8: final qualification and publication.** Complete the existing
  release gates plus the new concurrent-client, init-system and repository
  checks. Measure changed routing/backend behavior on the UCX II, verify
  the actual 0.7.3 upgrade/rollback and downloaded release artifacts. N9's
  architecture assessment and any resulting corrections must be complete.

**Order:** N9 baseline assessment with N0/N1 correctness and regressions →
N2/N3 backend contract and capability decision → coordinated runtime →
N4/N5/N6 integration → N7/N9 final compatibility/architecture assessment →
N8 final qualification. Installation preparation may follow the stable
contract, but its final tests must use the final runtime/backend identity.

The playback-mode guard, precise read-back categories, read-only status,
declaration-based profile preview and exact-context mixer launcher already
exist in 0.7.2/0.7.3. Carry them forward as explicit acceptance contracts,
listed in the detailed plan; they are not newly discovered defects or
unimplemented features. The shared reply/writer contract remains N2, and
the concern about unnecessary complexity is an investigation under N9,
not an established conclusion based on release size or subjective scores.

Both correctness cases were reproduced against the unchanged 0.7.3
runtime with simulated I/O and now have production-path regressions and
corrections. These are software results, not a claim of a measured audible
failure or a reason to rewrite historical release results. Final validation
must use the coordinated 0.8.0 backend too.
No delivery date is set before the backend-capability and repository
hosting/signing decisions are resolved. A prototype, unavailable external
prerequisite or measured protocol limit must not be recorded as a shipped
feature. Scope changes stay explicit.

## 0.7.3: clearer results and existing-mixer integration

**Closed and published.** Keep
the UCX II target, config semantics and measured backend pin. This cycle
improves daily use of the existing upstream GTK mixer and the CLI.
The separate desk GUI remains deferred.

Detailed scope, dependencies and acceptance:
[0.7.3 usability and mixer integration](plans/0.7.3-usability.md).

- [x] Correct the README's profile/snapshot analogy, policy-aware
  verification explanation and scope of the cooperating-writer lock.
  The README also names the status command and profile-transition preview.
- [x] **R0: invalidate a contradicted confirmation.** A later differing
  report must replace an earlier matching classification while the
  observation window is open. Reproduced through the public verifier with
  a fake backend; fix this correctness defect before convenience work.
- [x] **R1: clear verification results.** Distinguish matching values,
  deliberately retained REMEMBER values, unobserved settings and
  backend-unreportable settings in the summary. Keep strict profile
  outcomes, policy semantics and existing exit codes.
- [x] **R2: reliable upstream-mixer launch.** Check the installed GTK
  executable and schema before starting a backend; handle manual sessions,
  exact device/backend identity and differing desk/GTK connection settings.
  Remove obsolete profile-port assumptions from the launcher.
- [x] **R3: read-only runtime diagnosis.** Add a common source/native
  status command for installation, effective config/profile, device,
  backend, playback mode and read-back availability, with actionable
  causes and structured output. Inspection must not start or write a desk.
- [x] **R4: optional native GTK companion.** Package the existing upstream
  mixer for the qualified native targets; verify schema/desktop resources,
  package ownership, upgrades and actual desktop startup. Preserve the
  headless installation and explicit activation contract.
- [x] **R5: a tested GUI/desk workflow.** Cover both launch orders, occupied
  receive ports and recovery after closing the GUI. Investigate a shared
  read-back design separately from writer coordination before promising
  simultaneous operation.
- [x] **R6: profile-transition preview.** Extend the existing dry-run to
  identify previously declared routes that the target profile neither
  overwrites nor explicitly mutes. Explain partial-state and partner-link
  effects without claiming to know the complete playback matrix.
- [x] **R7: release qualification.** Add regressions for the changed
  behavior, retain the established software/release gates, qualify native
  GTK packages and verify the actual 0.7.2 upgrade/rollback path.

R0–R7 are complete. The release evidence includes all software gates,
installation/desktop checks and the physical UCX II off/on lifecycle. R5 records
the measured workflow and ADR 0028; a shared reply distributor or upstream
subscription protocol is now tracked with writer coordination in 0.8.0 N2.

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

## 0.7.2: installation, playback-mode validation and hardware evidence

**Closed and published.** The final runtime is
`e9ad292`. The standard-library core, config format and backend pin
remain unchanged. Completed work:

- [x] Validate the exact UCX II's active USB playback capacity before
  writes, using ALSA hardware parameters and USB alternate settings.
- [x] Handle idle/unknown modes, inconsistent observations and mid-apply
  changes. Refuse invalid plans before writes and report exact partial
  outcomes; preserve the profile marker on failure.
- [x] Document offline previews, register read-back and live-mode behavior.
- [x] Measure 13 direct-ALSA modes: 52 link/gain/mute cases and 12
  unavailable-source refusals. The same modes pass 65 known-route checks
  through PipeWire Pro Audio with recorded AUX mapping and restoration.
- [x] Qualify the final register sweep and all five routes: 1,888 confirmed
  entries, 14 protected skips and all 2,252 readable messages restored.
- [x] Upgrade the actual installation to 0.7.2; verify readiness, SIGHUP,
  the full suite with the interface physically off and automatic startup
  after power-on. The returned readable state matches exactly.
- [x] Repeat all eleven distribution targets on the final versioned
  candidate, reproducible builds, extracted installer tests, and actual
  source upgrade/rollback in both layouts.
- [x] Complete 27 software gates: 1,798 tests, Python 3.9–3.14, five full
  repeats, 200-cycle soak and fifteen fault-suite repeats. Coverage is
  97.61%; all 8,752 mutants complete with score 0.799291
  and 13 timeouts counted separately.
- [x] Publish the qualified source/native artifacts after main CI passes.
  Verify downloaded checksums, the annotated tag and GitHub attestations;
  wrong-ref and tampering controls fail as expected.

The [qualification records](evidence/0.7.2/) identify each source revision,
method and result. [Release readiness](evidence/0.7.2/release-readiness.json)
is complete, with no remaining 0.7.2 release task. GUI and physical
dual-device work remain deferred by the maintainer.

## 0.7.2 integration: installation

The installation changes are integrated and qualified with the final
0.7.2 playback-mode patch and published in the versioned release.
These features first belong to 0.7.2; the desktop companion is deferred.

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

Usage: [installation and recovery](INSTALLATION.md).
Qualification: [final 0.7.2 installation matrix](evidence/0.7.2/installation.json)
and [earlier installation/VM records](evidence/installation/).
Design decisions: [installation across Linux distributions](plans/installation.md).

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
- [x] Publish versioned native artifacts and verify their downloaded
  checksums, source identity and tag-workflow attestations.

**Qualification:** source build/install and simulated lifecycle on
Debian 13, Ubuntu 24.04/26.04, Fedora 44, openSUSE Leap 16, Arch and
Alpine 3.22/musl, all x86_64. Native lifecycle and repeated-build checks
pass on Ubuntu 24.04, Fedora 44, openSUSE Leap 16 and Arch. An Ubuntu
24.04 VM also exercises the real user service, a 0.7.0 source-to-package
migration and return, SIGHUP with an explicit PIN, and a maintenance fence
that survives a reboot and clears after successful package repair. These
are software tests with a simulated backend, not hardware qualification
on seven distributions. The reusable CI path passes all eleven source/native
targets. The final tag workflow repeats them, builds and collects all
native assets, and publishes the complete release. Downloaded packages and
source have verified tag-workflow attestations and checksums.
The actual 0.7.0 → native package → 0.7.0 file migration also passes on each
native target with a simulated user bus; Ubuntu has the additional real
user-manager VM check. I2 is closed with the verified package publication.

The original separately qualified implementation and its software matrix are on
[`feature/portable-install`](https://github.com/relative23/oscmix-desk/tree/feature/portable-install).
It was not part of the 0.7.1 correctness release. Final candidate
`e9ad292` repeats all eleven container targets with version 0.7.2.
Its source archive builds identically twice, 51 installer checks pass
from the extracted archive, and actual 0.7.1 upgrade/rollback passes in
both file layouts. The earlier real-user-manager VM experiment retains
its original source identity. The published native artifacts carry
version 0.7.2 and final tag commit `2cf9b02`; their runtime matches the
qualified source exactly.

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
- [x] Check known analog-output playback routes, unavailable playback
  sources, channel links, refresh/read-back, state retention and the
  explicit PipeWire Pro Audio mappings. Exercise the return to the
  baseline; preserve and verify the original configuration and state.
  **Results:** the [0.7.2 recordings](evidence/0.7.2/) cover
  13 direct-ALSA modes, 52 linked/unlinked gain/mute cases and 12 refused
  unavailable playback sources. All restore the 2,252 readable values and
  known main route. The same 13 modes then pass 65 checks through the
  three named PipeWire sinks, with the expected AUX links and full
  device/graph restoration. One direct-ALSA 176.4-kHz / 14-channel startup
  xrun is retained, followed by two successful unchanged repeats. The
  PipeWire measurements use the Pro Audio profile and recorded AUX mappings.
- [x] Derive live USB playback validation from the recordings, add
  regressions for the actual failed combinations and publish a per-mode
  support table. Keep register addresses separate from audio capacity.

**Measured:** [24 USB/ALSA combinations](evidence/0.7.1/sample-rates.md),
18 passing and six incomplete/failed, with full restoration after every
attempt and a final five-route PipeWire check. The 20-channel mode has
only 16 active playback channels at Double Speed; Quad Speed passes with
8/14 channels and fails with 16/20. Register addresses remain present.
0.7.1 does not automatically select or validate the live USB/rate mode.
The 0.7.2 runtime refuses known incompatible playback modes,
checks again between write phases and explicitly reports an idle stream
as unvalidated. It does not own or continuously enforce the hardware rate.
The explicit support table identifies the combinations actually tested.
The additional high-rate link/gain/mute and PipeWire results belong to
the separately identified 0.7.2 recordings.

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
| Further read-back capabilities beyond 0.8.0 N3 | A measured device/backend capability exists. A feasibility result alone does not change the current playback-matrix read-back classification. |
| Platforms beyond the named 0.8.0 N4/N5/N6 targets | A maintained target environment and appropriate lifecycle qualification are available. The common source/manual path remains the portability fallback. |
| A Flatpak GUI | Host integration can be exposed through a small, tested local interface; a sandboxed frontend alone does not install the host backend and udev rules. |
| Other Fireface models | Real hardware and a measured backend/model exist. UCX II evidence does not qualify another interface. |

## Boundaries that still apply

- 0.7.2 adds manual and native installation paths. The 0.7.0/0.7.1
  source installers still require a matching systemd user manager; use
  the instructions for the downloaded version.
- An apply is not a hardware transaction. Validation can prevent invalid
  plans; interrupted sends can still leave partial state.
- Playback mix and other write-only settings cannot be verified through
  the released backend. N3 changes that classification only for genuinely
  measured read-back; a GUI must retain the same distinction.
- In the released version, the device lock coordinates cooperating desk
  writers. Upstream GUI or raw OSC writers can still change state; the
  UDP endpoint is not an authenticated per-user control interface. N2 must
  establish its actual coordination and access boundaries before any
  stronger claim.
- No continuous background reconciliation, second protocol implementation,
  remote-control web server, automatic clock changes or new DSP engine is
  included in this milestone.

Historical headings about an untested upgrade or fixes missing upstream
are not current blockers: the 0.6.11 transition is qualified in 0.7.0,
and the enum/Room EQ fixes documented in [upstream issues](upstream-issues.md)
are already in the measured pin. Old drafts are retained in
[history](history/), not mixed into the active plan.
