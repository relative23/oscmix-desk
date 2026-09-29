# Changelog

## Unreleased

### Documentation

- 0.8.0 changed the Python signatures of `apply_routing()`,
  `verify_routing()` and `verify_and_repair()`: they take a connected backend
  control object instead of OSC ports, and `verify_and_repair()` returns a
  result. The 0.8.0 notes did not mention this; `UPGRADING.md` now does.

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
- A stop, or the backend exiting, during the start no longer fails the start
  with exit 1, and a backend that exits while the start waits for the lock is
  no longer logged as a stop.
- A reload that ends early reports `reconcile skipped` only when nothing was
  sent, and `reconcile incomplete` otherwise. The start-up verifier tells a
  repair it refused, an unfinished backend operation and a stop apart from a
  lost connection; a stop is no longer logged as an error.
- A refused write no longer prints `[Errno None]`.
- The desk sends a keepalive every second instead of every two, which keeps
  it three seconds inside the backend's idle limit instead of one. A lease
  request from a connection that may not hold one is reported as that.
- Backend peer credentials are read as unsigned uid and gid.
- `--list-profiles` counts channel settings, not sections.
- Tests that bind Unix sockets no longer fail when `TMPDIR` is long.

### Internal

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
