# Changelog

## 0.9.0 -- unreleased

### Added

- `[desk] peak-safe = true` in routing.conf makes an explicit switch or
  restore quiet: every output the switch changes goes to -65 dB first, the
  links, matrix and channel state follow, and each output gets back the
  volume the desk declares, else the one it reported -- on a failed switch
  too. Starts, repairs and reloads are not quieted; profiles inherit the
  setting.
- Desk keeps a write journal per interface and user beside its lock
  (`/run/oscmix-desk/<interface>.written.<uid>.json`): every write the
  backend acknowledged, stored after each apply and when the operation ends,
  under the lock. A reader takes each register's latest write from every
  user's file. It covers this boot only and is never taken as proof of the
  hardware state.
- `[desk] enforce = watch` in routing.conf keeps a reader connection open
  while the service runs: a report that contradicts a value the desk pins
  -- a change at the unit's front panel or a link change, which the
  device announces itself, or a value in a state dump another client asks
  for -- leads, once the reports rest for two seconds, to the same
  selective reconcile a reload runs. Reports during desk's own
  operations, remembered values, a
  base and gains AutoSet owns never do. A register the device sets back
  three times within a minute is left to it until the desk changes or a
  reload. `apply`, the default, never watches; profiles inherit the setting
  and may not set it.
- `--dump-config --include-written` adds the playback routes and mute
  flags desk itself last wrote, which the device does not report, in a
  section marked unconfirmed; mute flags stay commented out.
- `[desk] base = clear-inputs | clear-submixes | straight-playback | daw`
  makes a desk a whole matrix: every input and/or playback cell no route
  sets is silenced, or playback pair N plays on output pair N. Silencing
  links nothing, and where the device folds a register onto its linked
  partner the cell is written but not compared, so `--diff` and the
  read-back stay quiet after a clean apply. Straight playback is declared
  as routes of the base, which link a playback or output pair no route
  names only when the desk sets both its channels alike, and never an
  input pair. It is written on an
  explicit start or switch and remembered afterwards; `keep` (the default)
  changes nothing.
- `[output:N] mirror = S` (or a pair `S/S+1`) mirrors an output: every
  route declared for the source is declared again for the mirror, without
  its output volume, and each apply keeps both submixes equal. Widths
  must match, and a mirror may neither mirror a mirror nor have routes of
  its own. A mirror gets no desktop sink of its own.
- `--capture NAME` saves the device's reported state, plus the playback
  routes desk last wrote, as the new profile NAME: every value declared,
  the remembered ones as starting values a switch writes, gains AutoSet
  owns left to it, `[desk] base = clear-inputs` so the input routes are
  the whole monitoring matrix, device and ports inherited from
  routing.conf. Playback cells desk did not write since the boot are left
  alone, never silenced, and the command says when it knows none. It never
  replaces a file, and the profile appears only once it has loaded.
- `--diff-profile A B` lists what two desks set differently -- profile
  names, or `:main` for routing.conf -- with each register's value on
  both sides and any policy change, without connecting to the device.
  It exits 3 when they differ, like `--diff`.
- `--status` reports the playback matrix of the interface
  (`playback_matrix`), which the device never reports: `known (CC Mix
  preset <name>)` when, since the interface appeared, the device reported
  CC Mode on and a CC Mix preset and nothing wrote playback -- static
  evidence about the firmware, not a read-back; `re-established by the
  desk` when, since then, the desk wrote every playback register it
  declares with the declared value and the device reported CC Mix
  "TotalMix App" -- a record of acknowledged writes, never a hardware
  confirmation; or `unknown`, with the reason. "Since the interface
  appeared" is since the running backend started. Playback writes by the
  GTK mixer, other clients without a journal or the front panel are not
  visible.
- A start with input routes compares the device's input mix with the desk
  before its first write, as TotalMix FX does when it connects: within
  0.07 dB for a level and 0.007 of full scale for a pan. A difference is a
  warning naming the route, the device value and the declared value, and
  `--status` shows the last check (`connect_check`); the start then
  applies the desk as before, with no prompt and no refusal. The check
  costs one refresh before the first write, the same one the apply uses
  when it needs a fresh AutoSet observation.
- Desk keeps the CC Mode and CC Mix reports it saw and the last connect
  check beside the write journal
  (`/run/oscmix-desk/<interface>.seen.<uid>.json`), under the journal's
  rules.

### Changed

- The read-back after an apply or reconcile warns when the device reports
  a DSP load of 100 % or more: at 96 kHz, Room EQ on 16 channels reads
  back as on at a load of 110, so some reported effects do not run and no
  flag shows it.
- Desk paces its writes 10 ms apart. The backend acknowledges a write once
  the MIDI message is queued, and an unpaced burst loses writes on the
  wire: measured on a UCX II, a 400-write base left 3 of 4 changed cells
  unchanged; paced, every write landed and the read-back confirmed it.
- Room EQ on more than 16 outputs, or Crossfeed on more than 4 stereo
  outputs, is refused before any write: when the config loads, and when
  a switch would turn either on beyond the limit, counting the outputs it
  does not set as the device reports them and both channels of a linked
  pair. A start, repair or reload leaves those switch-ons to the device
  instead, says so, and applies the rest. The device's own load limiter
  would otherwise leave the extra ones off.
- Output sections accept `volumecal =` from -24.00 to +3.00 dB in 0.01 dB
  steps. DEVICE feedback is verifiable and PIN is the default. Calibration
  is separate from the normal output fader.
- Stereo routes accept `pan =` as an integer from -100 to 100, with a
  finite level above -65 dB and linked source/output pairs. Conflicting
  aliases and unsupported mono/split configurations refuse before writes;
  export omits states this syntax cannot reproduce.
- Playback sections accept `mute =`, with REMEMBER as the default and
  re-established rather than DEVICE-verified state. Both pair members
  must agree. OFF requires a complete declared pair-to-20-output matrix,
  including silent cells. Temporary ON precedes the matrix and final OFF
  follows it; unknown cached ownership blocks indirect unmute repairs.
- The prepared backend reconstructs stereo pan without endpoint divisions
  or negative-width singularities. Mix coefficients and metadata share
  one MIDI frame. Playback mute re-establishes every column cell, including
  zero values and repeated OFF. MIDI writes have a bounded deadline and
  propagate failures; these rules do not make audio transitions atomic.
- UCX II inputs 1–4 accept `autoset = true` or `false`, with verifiable
  warm DEVICE feedback and REMEMBER flags. Repairs preserve AutoSet-owned
  gains and linked partners, including when ownership feedback is unknown.
  Initial or explicit gain changes require fresh AutoSet-off confirmation;
  explicit off flags are confirmed first and on flags follow gains.
  Declared AutoSet-on conflicts with explicit PIN for an affected gain.
  Direct measurements cover flags and restoration, not overload protection.
- Input channel sections accept `msproc = true` or `false` for M/S
  processing. DEVICE feedback is verifiable; REMEMBER is the default
  and `[pin] input.msproc = pin` enables later repair. Direct warm linked
  measurements cover all 20 inputs; independent unlinked behavior was
  measured on inputs 13/14.
- Programmatic boolean settings reject values outside 0/1 before writes.
  A known channel option on an unsupported channel also refuses the
  entire write plan, matching file-based validation.
- Input and output channel sections accept `fx =` for FX send/return,
  from -65 to 0 dB with the backend's 0.1 dB rounding. DEVICE feedback
  is verifiable; values default to REMEMBER and `[pin]` overrides apply.
  Config validation, write planning and config export share these rows.
- Output channel sections accept `pan =` for balance, as an integer
  from -100 to 100. DEVICE feedback is verifiable, REMEMBER is the
  default, and `[pin] output.pan = pin` enables later repair. Direct
  warm linked measurements cover all 20 outputs and both endpoints.
- Python callers use `open_desk()` and `Desk.apply()`, `.verify()` and
  `.repair()`. Startup, profile switching, SIGHUP and read commands share
  its connection lifetime and writers retain one device lock and lease.
  `ApplyIntent`, `VerifyResult` and all documented exit constants are
  exported. `apply_routing`, `verify_routing`, `verify_and_repair` and
  CLI-internal `run_session` leave the root API; their modules remain importable.
- `Policy` and `Control` are exported for the existing config fields and
  optional profile-backend argument. Profile injection remains compatible;
  ordinary operations still manage the checked connection themselves.
- `Overrides` and `overrides=` name config/profile invocation settings;
  `CommandLine`, the existing positional arguments and `said=` remain
  compatible. Supplying both override inputs is a `TypeError` before I/O.
  `BackendControlError` names failed coordinated backend operations;
  `ReceivePortError` remains an alias for the same exception class.
- `Outcome.state` is an `OutcomeState` enum. The four original exported
  state constants are its members and still compare equal to their string values.
  Path fields are immutable tuples; construction and `dataclasses.replace`
  reject contradictory facts. Read-back, persistence and durability default
  to false and must be stated explicitly when constructing a result.
- Profile results preserve differing, missing and invalid feedback in
  separate immutable fields. `APPLIED_MISMATCHED` reports a completed write
  with a known read-back problem; the CLI returns `EXIT_DIFFERS` (3), while
  retaining the write, marker and service facts and existing failure priorities.
  Missing or unreportable feedback remains `APPLIED_UNVERIFIED`.
- Restore and its dry-run preview share main-config validation. Both refuse
  when no main config exists, instead of previewing defaults as a valid restore.
- `--strict` and keyword-only `strict=True` config/profile API options reject
  unknown or unsupported sections instead of ignoring declarations. Strict
  effective selection refuses an unusable active profile. Startup re-reads
  under the lock retain this mode: an invalid desk fails before writes or
  readiness; an invalid SIGHUP desk is skipped. Explicit valid selection or
  restore can recover a broken active profile, and previews validate the
  same target. The default compatibility and startup fallback remain.
- `--check` strictly validates the main config, every profile and the stored
  selection offline. Unreadable directories and broken `.conf` entries
  refuse the check; it never opens a device connection or changes a marker.
- Active profiles are remembered per config file. `routing.conf` keeps
  `active-profile`; a custom config uses `active-profile.<config filename>`.
  Selecting or restoring one desk no longer changes another desk's marker
  in the same directory. `active_profile()` now discovers the config when
  called without a path.
- Legacy `[osc]` ports remain accepted and validated but no longer make a
  profile or reloaded desk belong to another machine. Device name, USB ID
  and serial still determine compatibility.
- Python `switch_profile()` and `restore_main()` now honour installation
  maintenance fences and the same service hand-over as the CLI.
  `Outcome.reload_failed` reports a refused service reload while preserving
  the device and profile marker result; partial writes still exit with code 1.
- Property tests have no per-example deadline: host load must not turn a
  correctness check into a latency test.
- The source service requires the same per-user opt-in as native packages.
  `install.sh --disable` stops automatic operation without rebuilding or
  replacing the runtime; hotplug cannot restart the disabled desk. Older
  source units retain their custom contents and receive the condition with
  a backup. Upgrades adopt old activation once and then preserve an explicit
  opt-out, including after a failed disable. Source uninstall removes this
  permission, and native migration refuses a remaining opt-in.

### Documentation

- The UCX II cannot report its playback matrix: USB captures (2026-09-28
  to 2026-11-02), a hardware test (2026-10-30), static analysis of its
  firmware (2026-10-27 to 2026-11-04) and TotalMix FX on macOS (static,
  2026-11-03) and on an iPad (hardware, 2026-11-04) agree. The status,
  feature and troubleshooting pages say so instead of treating read-back
  as still to come.

## 0.8.1 -- 2026-10-02

### Changed

- Every failure that closes the backend connection raises `ReceivePortError`,
  a failed send included; it was a plain `OSError`. A send timeout carries
  `ETIMEDOUT`. Refusals that leave the connection usable remain `OSError`.
- The text of `--status` says `none` for a desk without a profile or
  configured serial, where it said `unknown`. The JSON is unchanged.

### Documentation

- 0.8.0 changed six signatures of the root API: `apply_routing()`,
  `verify_routing()` and `verify_and_repair()` take a connected backend
  control object instead of OSC ports, `verify_routing()` no longer returns
  `None`, `verify_and_repair()` returns a result, `VerifyResult` gained
  `invalid`, and `switch_profile()`/`restore_main()` take the new control
  object as `backend`. The 0.8.0 notes did not mention this; `UPGRADING.md`
  now lists them.
- Troubleshooting, the example config, the README and `--help` no longer
  describe UDP ports: `[osc]` is explained as legacy, the backend check looks
  for the control socket, and `--diff`/`--snapshot` work with the mixer open.
  The fader section describes remembered output volume, the status results
  are tabulated, and new journal messages are listed.
- The README states the config search order and the remaining options, and
  that every stereo output pair gets a PipeWire sink.
- The backend control contract covers the refused WRITE forms, the answer
  for an unknown address, the connection limits and disconnect rules, the
  desk's retry and keepalive timing, and how a slow MIDI write ends a lease.
- The release artifacts page describes the four verification records and
  how each is tied to the tagged files, and how the repository archive
  attached later is authenticated.
- `CONTRIBUTING.md` names `shellcheck` as a requirement of `make check`, the
  README explains why the packages declare the Unlicense, and comments no
  longer describe UDP sockets or an installer that enables the service.
- The installation guide says how to move a source installation's system
  files aside before installing the Arch package, which pacman otherwise
  refuses; the repository page says what an expired APT `Release` means.
- The architecture page follows one profile switch through the modules and
  states the layering, `constants` and `process` correctly. The feature table
  no longer claims an `autoset` option and lists the backend functions the
  desk cannot set yet. The numeric contract describes sweep schema 3.

### Fixed

- `switch_profile()`, `restore_main()`, `load_profile()`,
  `describe_profiles()` and `effective_config()` called without a config path
  use the config that discovery finds, as the command line does.
  `restore_main()` used to apply the empty default desk and report success,
  `switch_profile()` validated against the defaults and never remembered the
  profile. A restore with no config anywhere is now refused, and a restore
  is named after the config file it applies.
- A config, profile or active-profile marker behind a directory that cannot
  be searched is a configuration error that names the file (a warning for
  the marker) on every Python version. Before Python 3.14 it was a traceback,
  and on SIGHUP it ended the session without stopping the backend.
- A lock file that cannot be opened is reported as that, not as another
  writer holding the device lock for 30 seconds. The wait for the lock ends
  when the service is stopped.
- A stop during the start no longer fails the start with exit 1, and a
  backend that exits during the start is reported through its own exit
  status (exit 0 after an unplug), not as a failed start or as a stop.
- A reload that ends early reports `reconcile skipped` only when nothing was
  sent, and `reconcile incomplete` otherwise. The start-up verifier tells a
  repair it refused, an unfinished backend operation and a stop apart from a
  lost connection; a stop is no longer logged as an error.
- A refused write no longer prints `[Errno None]`.
- The desk sends a keepalive every second instead of every two, which keeps
  it three seconds inside the backend's idle limit instead of one. A lease
  request from a connection that may not hold one is reported as that.
- `uninstall.sh` refuses to run while a native package is installed, and
  points to `oscmix-setup --migrate-source`; it used to stop and disable the
  package's service and remove the system files the package owns. Without a
  reachable user manager it now removes the service's enable links and, for
  the account's own home and not as root, the system files; it left both
  behind.
- A package installation keeps its own files when another package tool is
  also installed: every available package database is asked whether it owns
  a system file, not only the first one found.
- The installer keeps one backup per replaced file instead of adding one on
  every upgrade, `uninstall.sh` removes them with the files, and a source build
  removes the build directories of earlier builds.
- The Debian package byte-compiles its runtime in `/usr/lib/oscmix-desk`; its
  `py3compile` call named no directory and compiled nothing.
- Backend peer credentials are read as unsigned uid and gid.
- `--list-profiles` counts channel settings, not sections.
- Tests that bind Unix sockets no longer fail when `TMPDIR` is long.

### Internal

- The release workflow refuses software and hardware evidence recorded
  against another backend patch series, and hashes `patches/`, `service/`
  and `desktop/` with the other installation files. Changes there now also
  trigger the distribution workflow.
- Upgrade and rollback checks start from 0.8.0, which is also added to the
  installer upgrade test.
- The backend protocol's request, event, status, role and source numbers are
  named once, in `oscmix_desk.protocol`, and a test compares them with the
  backend's `control.h`.

## 0.8.0 -- 2026-09-29

### Changed

- Repair and later reload/resume checks keep every remembered value (volume,
  mute, phase) after a session's first application, even when the device does
  not report it. A new session and an explicit profile or `--no-profile`
  selection still write the declared starting values. `[pin] input.stereo` and
  `output.stereo` choose the policy of route links.
- The desk and the upstream GTK mixer share one backend over a local control
  socket. Both receive device reports, and a desk operation holds the backend
  until its read-back is done, so mixer edits cannot interleave with it. The
  old UDP-only backend is refused; backend, ALSA bridge and GTK mixer are built
  from a fixed patch series and must be upgraded together.
- `--status --json` reports schema 2: the control socket replaces the removed
  receive-port section.
- Playback link flags are backend state, not device state: they are written
  on every apply, like the playback matrix, and no longer awaited or counted
  as verified.
- Resume reloads wait until the user services have been thawed after sleep.

### Fixed

- A later contradictory or invalid report revokes an earlier confirmation,
  including at the end of an OSC bundle; a wrong or unconfirmed link stops the
  writes that depend on it.
- A malformed OSC delivery invalidates the operation instead of confirming a
  partial prefix.
- Lost MIDI input or a MIDI hangup ends the backend operation before further
  reports or writes are used. The ALSA bridge asks for the largest sequencer
  input pool; the default one overflowed during device refreshes.
- Input-mix reports are withheld until all their dependencies were observed.
- An invalid nested channel number is a configuration error, also on reload.
- PipeWire target names containing quotes or backslashes are encoded exactly.
- Native reloads wait until the session can handle SIGHUP.

### Added

- OpenRC (Alpine) and runit (Void) supervision, a NixOS package and module,
  and Fedora Silverblue installation by RPM layering.
- Native packages for Debian 13 and Ubuntu 26.04.
- Signed APT and RPM repositories with a subscription helper that refuses
  expired or revoked signing keys.

## 0.7.3 -- 2026-09-24

### Added

- `oscmix-session --status [--json]`: a read-only diagnosis of configuration,
  installation, device, backend, playback mode and mixer.
- `--dry-run --profile NAME` names previously declared routes the target
  profile leaves undeclared and the link changes it causes.
- Verification summaries separate matching values, kept remembered values,
  differing pinned values, missing reports and unreportable settings.
- Optional `oscmix-desk-gtk` packages with the upstream GTK mixer.

### Fixed

- The latest report in a read-back window decides a register's result.
- The launcher checks the GTK executable, schema and connection before
  starting a service and reuses a matching manual session.
- The GTK mixer builds with the selected C compiler (openSUSE Leap 16).

## 0.7.2 -- 2026-09-24

### Added

- `install.sh --check` (read-only preflight) and `--manual` foreground mode;
  source and native installations share one payload.
- `oscmix-setup` for explicit per-user setup, migration from a source
  installation and recovery; activation stays opt-in.

### Fixed

- The active USB playback mode of the UCX II is checked before writing and
  before every apply phase; an idle stream is reported as unvalidated.

## 0.7.1 -- 2026-09-23

### Fixed

- Mono routes unlink both the source and the destination pair.
- `level = -65` keeps digital mute on unlinked stereo routes.
- Values are compared in their register encoding; non-finite and fractional
  integer values are refused; exports keep sub-tenth precision.
- The installer compares `HOME` and `XDG_CONFIG_HOME` before touching the
  service.

### Documentation

- The UCX II's front headphones are outputs 7/8. Room EQ delay is in OSC units,
  not seconds.

## 0.7.0 -- 2026-09-22

### Changed

- Releases carry a reproducible source archive, checksums and a GitHub build
  attestation.
- A profile that names another interface or port is refused.
- A desk is validated for the interface `--device` names.
- A dry run shows its plan without the interface.
- A start reports a firmware that differs from the measured one.

### Fixed

- A switch that fails part of the way reports what was and was not written.
- A stale backend is only signalled through a pidfd after its identity is
  checked again.
- An interrupted installation completes when run again.

## 0.6.11 -- 2026-09-19

- A receive port that cannot be bound is no longer reported as busy.
- A profile is validated for the desk's device.
- A running session does not apply a desk that names another machine.
- `--device` and `--osc-port` are refused with profile actions.
- `--pipewire-sinks` tolerates PipeWire objects without properties.

## 0.6.10 -- 2026-09-17

- A second session no longer stops the running session's backend.
- Switching the profile of another desk does not reload the unit.
- Conflicting actions and `--profile ''` are refused; section order in a
  config no longer matters.
- Ctrl-C and other reachable errors exit with a message, not a traceback.
- A USB device the kernel has not authorized is named at start.
- Route names with quotes work in generated PipeWire sinks; a relative
  `XDG_CONFIG_HOME` is ignored.

## 0.6.9 -- 2026-09-16

- The interface (serial, sequencer client, lock) is resolved once, and a
  switch writes only to that interface's backend.
- `[device] serial` selects among identical interfaces.
- A start that cannot take the device lock fails. The lock directory
  `/run/oscmix-desk` belongs to the group `audio`: **add the desk's user to
  `audio`** when upgrading. Symlinks and FIFOs at the lock path are refused.
- An uninstall in a scratch home leaves the system files alone.

## Earlier releases

Releases before 0.6.9 are described in their release notes on GitHub.
