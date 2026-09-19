# Changelog

## Unreleased

0.7.0 in progress. The first step, alone and with no change in
behaviour: modules a person can read.

### Fixed

- **A switch whose write gives out part of the way says how far it
  came.** A third outside review asked what a switch reports when the
  wire fails half-way. Measured: nothing -- `OSError` went out of
  `switch_profile` as a traceback from `--profile`, with some of the
  profile on the device and no word on which part, from the one function
  whose contract is "an outcome, never an exception". The backend now
  accounts for every burst (`WriteFailed`, an `OSError` carrying what was
  handed to the kernel and what was not), the apply extends that to its
  whole plan, and a switch turns it into an outcome: nothing written is
  a refusal and exit 2; something written is a fourth state,
  `written-in-part`, with both lists, the marker left alone, exit 1, and
  the unit asked to reload -- its reconcile writes the desk in effect
  back, which is the repair. A read-back whose request cannot be sent is
  "applied, not read back" with the cause. A start, a verifier and a
  reconcile stand down as before. ADR 0027. "Switched as a transaction"
  is gone from the README and the architecture page: there is none on
  this wire, and ADR 0011 always said so.

- **A receive port that cannot be read is not a quiet backend.** The
  listener treated every socket error like a timeout and yielded
  nothing; since an error returns at once where a timeout waits, every
  reader then spun -- measured, 1.3 million reads in half a second --
  for the 8 s of a `--dump-config` or the 10 s of a read-back, and ended
  by asking whether oscmix was running. A timeout is still how a wait
  ends; any other error is a `ReceivePortError` naming the port and the
  cause, which the barrier, the verifier, the reconcile, the switch and
  the three reads already handle (ADR 0025, amended). Found by the same
  review.

### Changed

- **A profile that names another machine is refused. A file that loaded
  in 0.6.x now does not.** A profile is the desk, not the machine (ADR
  0026; 0.6.11 warned): one whose `[osc]` or `[device]` resolve to
  another port, receive port, device name, usb id or serial than its
  `routing.conf` raises a `ConfigError` naming the profile, the setting
  that differs and the remedy -- take the two sections out. A switch to
  it exits 2 and writes nothing; a marker that still points at one
  falls back to `routing.conf` with a warning at the next start or
  reload, as for any active profile that no longer loads (ADR 0018).
  Restating `routing.conf`'s own values stays accepted, since
  `--dump-config > profiles/x.conf` writes them into every profile.
  With it go the 0.6.11 warning, `Config.main` and the advice by cause
  of a refused reload: only `routing.conf` can name another machine
  now, and a restart follows it. A desk that really is for another
  backend needs its own directory and `--config`.
- **A register value has a type, and no `type: ignore` is left.** A
  value on the wire was `object`, and so was the device handed to the
  section parsers; 24 `type: ignore` marked the places that cast past
  the checker (first outside review). `osc.Value`, `Args` and `Message`
  name what oscmix's `,i` `,f` `,s` carry, `model.SettingValue` what a
  setting may hold, and the parsers take `Optional[Device]` and
  `Register`. With the real types mypy asked two questions the ignores
  had answered for it -- whether the device can be `None` where its
  channels and its name are read -- and both now say so in the code.
- **A phase and a write reason are enums.** `reconcile.Phase` (an
  `IntEnum`, so its order is still the order of an apply) and
  `reconcile.WriteReason`; `PHASE_LINK`, `MISSING` and the rest are the
  same objects under their old names, and compare as the numbers and
  strings they were. `--diff` prints a reason by its `.value`, because
  what `str()` makes of a string enum differs between the Python
  versions this runs on (first outside review).
- **The wait for the link echo answers with a name.** `await_link_echo`
  returned `True`, `False` or `None` -- arrived, timed out, port held --
  told apart by an `is None` and a `not` that the mutation run had
  swapped without a test noticing. It returns `routing.LinkEcho`
  (`CONFIRMED`, `SILENT`, `UNOBSERVABLE`), truthy only when confirmed,
  so `if await_link_echo(...)` means what it meant; a caller that
  compared with `None` or `False` has to compare with the member. Each
  answer's consequence at the barrier has its own test (first outside
  review).
- **The oracle builds its own messages, and both sides are held to
  literals.** `tests/oracle.py`, which the reconciler is compared
  against, imported `link_messages` and `mix_messages` from the
  reconciler: a wrong register there was wrong on both sides and the
  comparison green (third outside review). It is written from what the
  device and upstream do now, and `tests/test_golden_messages.py` spells
  out registers, type tags, values and order for a desk of every route
  shape -- a linked pair, two routes into one pair, unlinked pairs below
  and above unity, a mono route, an input source, channel and global
  state. Panning the left half of an unlinked pair right fails three
  tests where it failed none that did not share the mistake.
- **No source module is over 600 lines.** Six were, up to 1038. Split at
  seams that were already there: `devices` (the register tables, with
  their mutation exemption, ADR 0015) out of `registers`; `locking`,
  `marker` and `outcome` out of `profiles`, which keeps the order of a
  switch and nothing else; `model`, `paths`, `sections` and `notices`
  out of `config`, which keeps the loader; `dump` out of `reconcile`;
  `reload` out of `session`; `reads` out of `cli`. The import graph got
  simpler with it -- the reconciler, the router, the verifier and the
  sink generator depended on the parser only for the dataclasses and
  depend on `model` now. Nothing a `routing.conf` means changes, and
  the package root exports the names it did.
- **The tests follow their subjects.** The five largest test files were
  in the order the review rounds arrived, up to 1869 lines; they are
  split by what they test, which after the source split is mostly by
  module -- `test_locking`, `test_marker`, `test_reload`, `test_sighup`
  and so on -- with what they share in small helper modules, and
  `conftest` reduced to the isolation and the fixtures. 1468 tests
  before and after.
- **The roadmap and the changelog keep what is ahead and what changed
  lately.** Their history is in `docs/history/`, unchanged: the
  roadmap's chapters for 0.2.0 to 0.5.0 and the status of every 0.6
  release, and the changelog before 0.6.9.
- **A test's patch that nothing reads fails the test.** The suite
  isolates itself from the machine, and steers the code under test, by
  replacing module attributes, some three hundred times. A function
  that moves takes its reads with it while the patch on the old module
  still succeeds and changes nothing, which is how tests reached the
  machine's lock directory and user manager before. `monkeypatch.setattr`
  on one of this project's modules now fails when no code in that module
  reads the name. It found no inert patch in 0.6.11 and caught the
  suite's own lock-directory isolation the moment the lock moved; the
  socket guard of 0.6.11 caught the one case it cannot see, a name two
  modules read.

## 0.6.11 (2026-09-19)

What two outside reviews of 0.6.10 named, checked against the tree
before anything changed, and cut in two: the defects and the small
repairs here, the tighter types -- enums for phases and write reasons, a
named result instead of `Optional[bool]`, a frozen `Config`, modules a
person can read -- in 0.7.0, because they change public names. The pin
does not move and the register table has no new row.

### Fixed

- **A receive port that cannot be bound is no longer reported as a busy
  one.** `Backend.listen` answered `None` for every `OSError`, and
  `None` means "the mixer GUI has the port" to every caller. Measured:
  `[osc] recv-port = 80` fails with `EACCES`, and the desk ran
  unverified for good under `UDP 80 in use (mixer GUI running?)`. `None`
  is `EADDRINUSE` alone now; anything else is a `ReceivePortError` with
  the real cause, and the status line reads `verifier failed` rather
  than `verifier finished`. The review's own remedy -- re-raise --
  would have ended an apply between its phases, pairs linked and no mix,
  because the barrier binds the port after the links are on the wire:
  the barrier waits blind instead, the verifier re-establishes the mix
  before it reports the failure, and the three reads exit 1 with the
  cause rather than a traceback. ADR 0025.

- **A switch nobody could read back says so.** With the receive port
  held or unbindable the outcome line read `N register(s) unconfirmed`,
  which is what a read-back that ran and came up short says. It reads
  `not read back (<why>), so none of its N register(s) is confirmed`.

- **`--pipewire-sinks` survives an object without properties.** pw-dump
  prints `"info": null` for an object that went away, and the sink
  search raised `AttributeError` on it.

- **A profile is validated for the desk's device, not the default one.**
  A profile inherits `[device]` from `routing.conf`, but it was parsed
  first and given the name afterwards -- so it was checked against the
  UCX II whatever the desk was for. Measured on a desk naming another
  interface: a profile routing to output 25/26 was refused because
  "channel 25 does not exist on a Fireface UCX II", and a profile's
  `[output:1] volume` was accepted through the UCX II's table and would
  have been written, while the same section in `routing.conf` has been
  ignored with a warning since 0.6.2. The profile is read onto the main
  config's machine settings now; the second parser that decided what a
  profile "states" is gone with the patching it served. A third effect
  follows the same rule `routing.conf` has always had: on a desk for the
  802 or an unmodelled interface, a profile with a `[pin]` section is
  refused, where it used to load against the UCX II's options -- and an
  *active* profile that no longer loads falls back to `routing.conf`
  with a warning at the next start (ADR 0018).

- **`verify-hardware.py` and `record-dump.py` skip only for a held
  port.** Any failure to bind was a skip (exit 77) with "close the mixer
  GUI"; another cause is an error now.

### Changed

- **Routes on a device nobody modelled are unchecked out loud.** A
  channel section on such a device has warned since 0.6.2, while its
  routes went to the hardware without a channel check and without a
  word. Still no opinion (ADR 0006); the warning names the device, the
  number of routes and what is modelled, and comes from the places that
  load a desk to write or show it -- a start, a switch, a restore, a
  SIGHUP reload, the dry runs, `--diff` and `--pipewire-sinks` -- about
  that desk. A listing shows no route and `--dump-config` shows the
  device's, so neither is warned about the file's.
- **A desk for somewhere else is not applied here.** A running session
  keeps the backend and interface it was started for, and until now it
  pinned whatever it re-read to them and wrote it. Measured by review:
  `routing.conf` edited to name another box with `output = 41/42`,
  reloaded under a session bound to a UCX II, sent `/output/41/stereo`
  to an interface with twenty outputs. And from a second outside review:
  a profile stating its own port and serial was written to its
  interface by the switch, and then to the unit's by the reload the
  switch sent -- one persisted profile meant one target for a switch,
  another for a reload, and the first again after a restart. A re-read
  desk whose file resolves to another device name, usb id, serial or
  port is refused now: `reconcile skipped`, and an error naming what
  differs and what to do -- a restart for a `routing.conf` that moved;
  for a profile that itself names another machine, taking the sections
  out and then reloading, because a restart would move the unit off its
  interface, with `--no-profile` named only where it can work. The
  re-read file is resolved the way a restart would resolve it: a
  `Config` records what its file said (`Config.loaded`) and what the
  command line replaced (`Config.overrides`), so a file under `--device`
  or `--osc-port`, or one that names the box the start pinned, is the
  session's own. Two differences from a restart are left. The session
  does not count the boxes again, so with a second one plugged in
  since, a desk naming no serial still goes to the pinned box, where a
  restart would ask which. And a device name is compared as written,
  where a start looks for it as a substring, so another spelling that
  finds the same client is refused until a restart. The switch still
  reloads the unit and leaves the decision to it, and says so.
- **`--device` over a file for another model says so**, with the other
  notices about a desk: at a start, a reload, a dry run, `--diff` and
  `--pipewire-sinks`. The override arrives after the file was
  validated, so the channels were checked for one interface and written
  to another in silence. A start says it before it looks for the
  interface, and again under the device lock when the desk it re-read
  there is another one by then. Validating *for* the override needs the
  parser to know it, which is part of 0.7.0.
- **The marker's temporary file has a name of its own.** Two switches
  holding different device locks shared `active-profile.tmp`, and one
  could rename the file the other was still writing.
- **A marker that may not survive a power cut says so in the outcome.**
  When the directory cannot be synced the marker is in effect and the
  unit is reloaded as before; that the change may not have reached the
  disk was a log line only, and is now part of the outcome line and of
  `Outcome.durable`.
- **A reload sent without knowing the unit's desk says it guessed.**
  When the unit's `/proc` entry cannot be read the reload still goes
  out -- nearly every switch is for the unit's own desk, and an untold
  unit lets its verifier re-apply the old one -- but with a warning. A
  unit that was read and resolves no config is not a guess; one whose
  command line this version cannot parse is.
- **An empty `[device] name` is a configuration error.** The name is a
  substring match, and the empty string is a substring of every name.
  Measured on the start path: with one MIDI-capable card `name =`
  selected it, with a second one -- a USB keyboard beside the interface
  -- the start refused as "2 interfaces match ''" and pointed at
  `serial`; and either way the desk had no model, so nothing in it was
  checked. **A file that worked this way now exits 2**, naming the
  option; write the interface's name. ADR 0006 is amended for it.
- **`--device` and `--osc-port` are refused with `--profile` and
  `--no-profile`.** A switch and a restore take their interface and
  ports from the config; the overrides were dropped on that path without
  a word, while the dry run of the same switch looked for the interface
  `--device` named and so showed something the switch would not do.
  Exit 2, naming the pair. An empty `--device ''` is refused as well --
  it was skipped in silence -- and a given name is stripped like the
  file's, where padding used to find no client.
- **A dry run shows the desk as the switch would load it.** The device
  name and ports of the desk *in effect* were written over it first, and
  the name is what a dry run acts on: a profile naming its own interface
  was looked for, and warned about, as the active desk's.
- **One rule for what a re-read desk may not change.** The start's
  re-read under the lock and the SIGHUP's each assigned the five machine
  settings by hand; both go through `profiles.keep_machine_settings`,
  which walks the table a test already holds against `Config`.
- **A profile that names another machine is told that 0.7.0 refuses
  it.** It still wins for the switch in 0.6.x. A profile is the desk,
  not the machine: with profiles able to name a backend, one marker per
  config directory cannot say which desk is where, and two such profiles
  hold two device locks over that one marker. ADR 0026. A profile that
  restates its `routing.conf`'s own `[osc]` and `[device]` -- every one
  made with `--dump-config`, until `routing.conf` changes them -- is not
  meant and stays accepted. One main config per directory is now a
  stated rule for the same reason: the marker and `profiles/` belong to
  the directory, not to the file.
- **The public surface grows, and nothing in it changes shape.**
  `ReceivePortError`, since `await_link_echo`, `verify_routing` and
  `verify_and_repair` raise it; `Machine` and `CommandLine`, and with
  them `Config.loaded`, `Config.main` and `Config.overrides`;
  `Outcome.read_back` and `Outcome.durable`; `load_config(path,
  base=None)`; and `blind_reapply_mix(config, should_stop, why=None)`.
  No existing signature changes. Behaviour behind existing names
  changes where this section and Fixed say so: `verify_routing` and
  `await_link_echo` raise where `None` used to cover every failure to
  bind, `load_config` refuses an empty `[device] name`, and
  `load_profile` validates for the desk's device.
- **The mutation run's survivors were read.** They showed missing
  assertions and no defect, among them both halves of what the marker
  functions answer and the temporary file beside the marker, the port
  and timeout the link barrier hands the echo wait, the two ends of the
  `--osc-port` range, the config `--no-profile` hands the reload
  decision, and a sink found by the desk's own name rather than because
  every sink in the tests was a Fireface. Score 0.794 on 7485 mutants,
  the not-covered bucket still empty, `min_score` 0.77 -> 0.79. The full
  run used a mutant tree removed beforehand; ten functions were
  re-judged by name afterwards.
- **Two integration tests wait for what they assert.** They stopped the
  session the moment the verifier's `/refresh` was on the wire, and a
  verifier that is told to stop does not judge the answer: the
  "verified" line they look for was then never written. One run in five
  of the nightly flakiness gate on 0.6.10 (2026-09-18). They wait for
  the line, then stop the session.
- **No test opens the machine's `/dev/snd/seq`.** The device wait opens
  it to make the kernel load `snd-seq`; read-only and harmless, and
  still the machine's. The suite points `OSCMIX_SEQ_DEV` at nothing.
- **No test talks to the machine's backend.** A desk with no `[osc]`
  section resolves to UDP 7222 and 8222, where a developer's oscmix
  listens; a test written during this cycle ran `--dump-config`
  unstubbed and read the interface through it (found by review before
  any release). An autouse guard now fails a test that binds, connects
  or sends to either default port.
- **The layer map is exact.** `tests/test_architecture.py` held every
  module to the imports it is allowed; it now also fails on an allowed
  edge nothing uses. Three had outlived their imports, and the package
  docstring described the graph of 0.2.0.
- **CI uploads artifacts on Node 24.** `actions/upload-artifact` moves
  from the v5 pin, which targets the deprecated Node 20, to v7.0.1.

## 0.6.10 (2026-09-17)

What a full check of the released 0.6.9 found -- gates, the live desk,
and independent reviews of the whole tree rather than of a diff. Two
behaviours were wrong in ways a user meets; behind them a layer of
smaller defects nothing had exercised, and documentation that described
an earlier release. The pin does not move and the register table has no
new row.

### Fixed

- **A second session no longer kills the running one's backend.** The
  stale-backend cleanup treated every `oscmix` of this user on the port
  as stale. Measured: `oscmix-session` started by hand terminated the
  unit's backend, the unit restarted and terminated the manual one. A
  backend whose parent is a live `oscmix-session` is somebody's desk; the
  newcomer exits 2 and says whose.

- **A switch of another desk does not reload the unit.** `--config X
  --profile Y` asked the unit to reconcile, and the unit re-applied its
  own `routing.conf` over the switch. The reload is sent only when the
  switch is for the config the unit runs -- worked out as the unit did,
  from `--config` on its command line and its own environment, read
  from `/proc/<MainPID>/`, since the shell's `OSCMIX_CONFIG`,
  `XDG_CONFIG_HOME` or `HOME` can name another file.

- **Every pair of actions is refused, not three.** `--no-profile --diff`
  restored the desk and never diffed; `--profile X --snapshot` switched
  and printed nothing; `--list-profiles --profile X` listed and did not
  switch. One action per invocation now, and `--dry-run` only with a
  start, a switch or a restore. The refusal comes before any file is
  read: after it, a broken `routing.conf` answered a conflicting pair
  with a configuration error that named neither flag.

- **`--profile ''` is a switch, and refused.** The empty name was falsy,
  fell through every action and started a session; it is exit 2 now,
  like any name that is not a profile.

- **A config reads the same whatever the order of its sections.**
  `[pin]` or `[clock]` above `[device]` was checked against the default
  model: accepted or refused for the wrong reason. `[device]` is read
  first wherever it stands.

- **Ctrl-C is an exit code, not a traceback.** Before the backend runs --
  the device wait, a lock wait, a read of the device -- an interrupt
  printed a stack trace. It exits 130 now. `--timeout nan` never expired
  and `--timeout -1` never waited; both are refused.

- **Four tracebacks a user could reach are handled.** A profile marker
  that is not UTF-8 raised past the OSError guard on every start; it is
  a warning now, and the start applies `routing.conf`. A backend the
  socket cannot reach raised out of the start, the background verifier
  and the SIGHUP reconcile. The start fails with exit 1 and stops its
  backend. The verifier and the reconcile log the reason and stand down.
  `systemctl status` then shows `running; verifier failed at ...` or
  `running; reconcile skipped at ...`.

- **Every reconcile that stands down says so.** Only a held receive port
  changed the status line to `reconcile skipped`; the device lock held
  elsewhere, a start-up verifier still running after the wait and a
  config that no longer parses returned before it, and `systemctl
  status` went on showing the previous line -- often `verifier finished`,
  which reads as all well.

- **A start names an interface the kernel has not authorized.** With
  `authorized=0` -- USBGuard, a policy, a hand -- the device keeps its
  sysfs entry and no driver binds, and every start said "is
  snd-usb-audio loaded?". It says what it is now. The unit still retries
  every half minute, which is what brings the desk up by itself once the
  device is allowed: authorizing adds interfaces, not the device, so udev
  starts nothing (measured on the desk).

- **A route name with a quote no longer breaks the generated PipeWire
  conf**; the description is escaped.

- **`--pipewire-sinks --pipewire-target X` warns when it cannot vouch for
  X.** The 7.1 layout it prints for a target it did not find is still
  printed, with a warning that says so -- and that tells "no sink named
  X" from "pw-dump could not be read".

- **The launcher polls the port the backend runs on.** A profile that
  states its own `[osc] port` runs the backend there; the launcher read
  `routing.conf` only, warned that the backend was unreachable, and
  notified a failure that had not happened. It polls the active
  profile's port, then `routing.conf`'s, which is where the backend runs
  if that profile no longer loads. It finds its config by the backend's
  rule too: a missing `OSCMIX_CONFIG` is not a reason to read another
  file, and an empty `HOME` is looked up rather than read as `/`.

- **A relative `XDG_CONFIG_HOME` is ignored**, as the XDG specification
  says, by the session, the launcher, the installer and the uninstaller
  alike. Each resolved it against its own working directory: the
  installer wrote `relative-config/oscmix/routing.conf` into whatever
  directory it was run from, and a session started elsewhere never found
  it. Both scripts refuse a `HOME` that is empty or relative, which
  `set -u` lets through and which would have put every file under `/` or
  the working directory.

- **The installer survives a start that fails**, and prints its advice
  instead of dying under `set -e`; the uninstaller survives a missing user
  bus. Run as root, the test suite reached the real system: the udev
  rule, the resume hook and the tmpfiles.d entry through their paths,
  and `/run/oscmix-desk` through `systemd-tmpfiles --create`. The three
  paths point into the test's scratch directory now and
  `systemd-tmpfiles` is stubbed, and a test fails if a command either
  script runs through `$SUDO` is neither stubbed nor confined to those
  paths.

### Changed

- **`StopWhenUnneeded` is described as what it is.** The udev rule, the
  unit, the architecture page and ADR 0013 said it stops the service on
  unplug. An enabled unit is wanted by `default.target` and never
  unneeded; what ends the service is the backend exiting with its device.
  The directive stays, the story is corrected.
- **Ten decision records carry an amendment** where a later release, or
  this one, changed what they describe: the removed `route_messages`
  (0001), the recorded dump time (0002, 0007), the exit codes (0011), the
  unplug story (0013), the lock's location, ownership and fallback
  (0018, 0022, 0023), the reload rule (0019) and what counts as a stale
  backend (0021).
- **The public surface names the resolver.** `find_seq_client` and
  `wait_for_seq_client`, superseded in 0.6.9 and called by nothing, are
  gone; `resolve_device`, `wait_for_device`, `select_seq_client`,
  `Device`, `lock_key`, `take_device_lock`, `port_holder`,
  `DeviceAmbiguous` and `DeviceLockUnavailable` are exported.
- **Documentation catches up with 0.6.9.** The unit file, the installer
  and the architecture page described the lock, the root steps and the
  installed files of earlier releases; the security model listed half of
  the hardening the unit declares and the unit's own comment contradicted
  it; the README counted seventeen decision records of twenty-four; the
  release checklist gains the cleared mutant tree, the `audio` group and
  the check with the interface switched off. Exit code 2 is described
  everywhere as the refusal it has become -- an ambiguous interface, a
  session already running -- not only as a `routing.conf` error.
- **The upstream record states what the pin carries.** All five issues
  this project filed are fixed at the pinned revision, and have been
  since 0.6.0; `docs/upstream-issues.md` still listed output phase as
  filed and the Analog 5-8 gain fix as newer than the pin.
- **The mutation run's survivors were read.** They showed missing
  assertions and no defect, among them the READY line a start sends, the
  path the reconcile locks, MainPID asked for with `--value`, argv[2] and
  pid 1 told apart from a session, the refusal's `--timeout 0` boundary,
  and the pw-dump call itself. Score 0.777 on 7118 mutants, the
  not-covered bucket still empty, `min_score` 0.76 -> 0.77. The full run
  used a mutant tree removed beforehand; the survivors of eighteen
  functions and two functions changed after it were re-judged by name.
- **A test can no longer put the machine's lock directories back in
  effect.** A stand-down test added in this release called
  `monkeypatch.undo()`, which reverts every autouse fixture, and took a
  real lock file in `/run/oscmix-desk` on each run; the suite now fails
  any test that ends that way.
- **The tests no longer read the developer's desk.** The action-pair
  tests resolved `~/.config/oscmix/routing.conf` and its marker; every
  test now starts from an empty config home and no system config, the
  sessions and launchers the suite starts as subprocesses included
  (`OSCMIX_SYSTEM_CONFIG`, a test seam like `OSCMIX_LOCK_DIR`).
- **CI finishes again.** The installer tests inherited the suite's fake
  interface once `install.sh` honoured `OSCMIX_SYSFS_USB`, took the
  restart path and its 2 s sleep on every install, and pushed the
  flakiness gate past its 15-minute timeout on the 0.6.10 push; they get
  an empty sysfs unless a test plugs one in. Two restore tests waited out
  the full 10 s read-back window against a double that never answers.
  The suite takes 112 s instead of 160 locally. The nightly mutation
  job had hit its 90-minute timeout every night since 2026-09-12 without
  turning anything red; the workflow's timings are re-measured, the
  flakiness gate gets 25 minutes and the mutation job 180.

## 0.6.9 (2026-09-16)

What a review of the 0.6.8 device lock found, in the order a second
reviewer put it. Six defects, each reproduced by a probe before it was
fixed, and each probe a regression test now -- together with the
architecture test that review asked for. The pin does not move and the
register table has no new row.

### Fixed

- **The interface is resolved once.** Serial, sequencer client and lock
  key come from one answer, `discovery.resolve_device`: the service binds
  and pins from it, a switch and a restore check against it, and a
  reconcile uses the serial the service pinned. 0.6.8 worked it out four
  times: with two identical interfaces and no `[device] serial` the
  service keyed on `2a39-3fd9-24216011` while a switch keyed on
  `2a39-3fd9-ambiguous`, two lock files over one desk, and a config
  naming box B still bound box A. `[device] serial` now selects the
  client the backend bridges; without it, more than one candidate is a
  configuration error for all of them -- exit 2 for the service, which
  `RestartPreventExitStatus` keeps from looping. ADR 0024.

- **A switch writes only to the backend of its interface.** A bound OSC
  port was enough: a plain Python socket on it received a whole routing,
  the switch reported `applied-unverified` and recorded the marker. The
  holder has to be an `oscmix` of this user now, and when the
  `alsaseqio` beside it names the client it bridges, that has to be the
  resolved interface.

- **A start without the device lock fails.** 0.6.7 and 0.6.8 refused to
  write and then sent `READY=1`, so systemd reported a desk that had
  never been written and nothing retried -- while ADR 0022 said the start
  fails. It does: the backend is stopped, the exit code is 1, and
  `Restart=on-failure` tries again. `READY=1` follows only an apply that
  returned, in the same block.

- **A FIFO at the lock path no longer hangs every writer.** The read-only
  fallback blocked in `open()` until a writer appeared; the probe was
  still blocked after 25 s, the 30 s lock wait never reached, and the
  service's start would have hung the same way. Lock files are opened
  `O_NONBLOCK` and accepted only when `fstat` says regular file.

- **A symlink at the lock path is refused, not followed.** 0.6.8 followed
  a planted link and chmod'ed its target to 0666; only
  `fs.protected_symlinks` stood in the way. `O_NOFOLLOW`, and a lock
  file's mode and group change only when this process owns it and it
  has exactly one link.

- **The lock directory's trust circle is the group `audio`.**
  `/run/oscmix-desk` is 3770 root:audio instead of 1777, and lock files
  are 0660 with the directory's group. With 1777 any local account could
  pre-create a lock file nobody else could open, or hold one for ever.

- **The unit can write the lock directory wherever its sandbox applies.**
  `ReadWritePaths=` was empty. Ubuntu's user manager drops the mount
  sandbox without a word -- AppArmor denies it a mount namespace -- so the
  desk worked here, but under a manager that applies
  `ProtectSystem=strict` `/run` is read-only: measured under the system
  manager, the 0.6.8 unit
  could not create a lock file and every start would have been refused.
  `ReadWritePaths=-/run/oscmix-desk` now, and a refusal in a read-only
  directory names that directive instead of "No such file or directory".

- **The start budget includes the lock wait.** `startup_budget()` had no
  term for the 30 s the start has waited for the device lock since 0.6.5,
  so the test against `TimeoutStartSec` could not see it. It is in the
  sum, and `TimeoutStartSec` is 100 to keep 10 s of margin.

- **A reload keeps the interface of the running process.** The desk read
  under the lock took `usb-id` and `serial` from the file while keeping
  the running ports, so a profile naming another box described a
  different interface than the lock and the backend belonged to.

- **A scratch-home uninstall leaves the system files alone.** It reached
  for the udev rule, the resume hook and the tmpfiles.d entry the
  session's own installation depends on. It now skips them, with a
  warning, whenever systemd's session serves another home -- as it already
  did for the service.

- **What an independent review of this release found before it shipped.**
  A switch checked the port holder before a lock wait of up to 30 s and
  never after it; it checks again once the lock is held. Any process whose
  kernel-truncated name was not valid UTF-8 made every switch raise; /proc
  is decoded leniently. A backend left without its card and client took a
  switch keyed on `unknown` beside the unit's lock; an interface with no
  visible sequencer client is refused. A Fireface of another model beside
  a UCX II made both ambiguous; the card list is matched on the model. A
  configured serial that was not plugged in, beside another box of the
  model, looped the start; it is the clean no-op now. Only a kernel
  sequencer client named exactly like a card counts as an interface, a
  client number listed twice is refused as forged, a
  model is matched exactly before it is matched as a substring, a serial
  must be digits, the
  start binds and pins from one read of the machine, and the snapshot
  header names the box it actually read.

### Changed

- **The mutation run's survivors were read.** They showed missing
  assertions and no defect, among them the serial read from a client when
  the card list is unreadable, the restore's re-check after the lock, the
  start's no-client path against a real sysfs, and the port holder's
  detection by program as well as by name. Score 0.762 on 6649 mutants,
  the not-covered bucket still empty, `min_score` 0.74 -> 0.76. The full
  run used a mutant tree removed beforehand; thirteen functions were
  re-judged by name.

- **The security model says what applies.** On Ubuntu the user manager
  silently skips `ProtectSystem`, `ProtectHome` and `PrivateTmp`;
  `NoNewPrivileges` and the seccomp filter do apply. The document says
  so, and how to check a machine.
- **`[device] serial` selects the interface**, not only the name of its
  lock, and is required when two identical interfaces are connected.
- **A lock that cannot be opened says why:** a missing group
  membership, a symbolic link, something that is not a regular file.
- **The installer warns when the user is not in `audio`.**
- **Upgrading from 0.6.8:** the user running the desk has to be in
  `audio`. The installer's root step turns the directory into 3770
  root:audio and regroups a lock file 0.6.8 left behind; the unit cannot
  do the latter itself, because a sandboxed user service runs in a user
  namespace where `audio` is not mapped and `fchown` fails with EINVAL.
- **The write sweep resolves its interface** and refuses to run with two.
- **`device_key` and `device_serial` are gone.** Once every path
  resolves the interface, a second way to name it is the split this
  release removes. The hardware evidence and the write sweep name the
  resolved box, the snapshot header names the box its backend drives,
  and the evidence tool refuses a machine with two it cannot tell apart.
- **The layering gains two edges:** `discovery` imports `errors`, and
  `profiles` imports `process`. Both point at a leaf or down the graph.

## Earlier releases

In [docs/history/](docs/history/), unchanged:
[0.6.0 -- 0.6.8](docs/history/changelog-0.6.0-0.6.8.md),
[0.5.0 -- 0.5.2](docs/history/changelog-0.5.md),
[0.1.0 -- 0.4.0](docs/history/changelog-0.1-0.4.md). This file keeps the
release in progress and the three before it.
