# Changelog, 0.6.0 -- 0.6.8

*Moved out of `CHANGELOG.md` when 0.7.0 began; nothing was rewritten.*

## 0.6.8 (2026-09-14)

What an adversarial review of the 0.6.7 device lock found. Six ways the
guarantee came apart, four of them measured on a live UCX II, plus the
writer that never took the lock at all. The pin does not move and the
register table has no new row.

### Fixed

- **One lock path for every writer on the machine.** `/run/oscmix-desk/`,
  created by the installer through `tmpfiles.d` with mode 1777, is
  searched before anything else. `$XDG_RUNTIME_DIR` is per user and
  absent from `sudo`, `cron` and a bare `ssh host oscmix-session ...`:
  measured on the desk, a switch with the variable cleared computed a
  path beside the config, took it in two seconds and wrote the whole
  routing while a holder held. Two user sessions over one interface held
  two locks for the same reason, and a runtime directory that went with
  the last logout freed a lock nobody had released. ADR 0023.

- **An existing shared directory is never fallen back from.** A lock
  there that cannot be opened is a refusal. Quietly locking somewhere
  else is the behaviour that walks past the holder.

- **A writer with no config directory takes a real lock.** It used to
  get a lock object with no descriptor, because the path search needed
  a config directory once the runtime directory was out of reach.

- **The device key is pinned at discovery.** It is recomputed on every
  write from `/proc/asound/cards`, which empties the moment the
  interface is unplugged. Measured across a real unplug: the key moved
  from `2a39-3fd9-24216011` to `2a39-3fd9-unknown` and a second lock
  file appeared beside the first, so a reconcile in that window wrote
  beside the holder rather than after it. After a resume the device
  re-enumerates, udev restarts the unit and the resume hook sends its
  reload into exactly that gap.

- **The key never guesses between two boxes.** The serial came from the
  first matching line of the card list, whichever interface the process
  was driving, so a writer of the second box keyed on the first box's
  name and unplugging the first moved the survivor's key. One interface
  now gives its serial, several give a shared key and a warning naming
  the remedy, none gives the model alone. The new `[device] serial`
  names a box outright and always wins.

- **A switch that would reach nobody writes nothing.** No write path
  checked whether the datagrams would arrive. Measured with the UCX II
  unplugged: the switch wrote into a port nobody bound, printed
  `applied`, exited 0 and recorded the marker -- a desired state that
  had never been at the device, which the next start then applied. The
  question asked is whether anything is bound to the OSC port, not
  whether sysfs still lists the interface: a logical disconnect leaves
  that entry in place, measured, so presence there is not reachability.
  It refuses now, before the lock, like a bad config.

- **The write sweep holds the device lock.** `scripts/sweep-writes.py`
  walks every settable register and writes each one, it is in the
  release checklist, and it took no lock at all.

### Changed

- **`[device] serial`** is a new optional setting, inherited across a
  profile switch like the ports and the USB id. Only a machine with two
  interfaces of one model needs it, and the example config ships it
  commented out: under ADR 0006 an unknown option in a known section is
  an error, so a `routing.conf` that names `serial` is rejected whole by
  a 0.6.7 install.
- **The test suite gets its own `OSCMIX_LOCK_DIR` and a sysfs with the
  interface present.** `/run/oscmix-desk` is real wherever the installer
  ran, and without the first the suite would take the lock of the desk
  the developer is listening to.

- **The mutation run's survivors were read.** They showed seven missing
  assertions and no defect: the configured serial reaching the lock key
  of a switch and of a restore, the profile name in a refusal for an
  unreachable device, a restore without a runtime directory locking
  beside the config, the real default paths for the lock directory,
  sysfs and proc, the empty serial pinned when the device shows none,
  and the count in the two-box warning. Score 0.742 against a floor of
  0.730, the not-covered bucket still empty, `min_score` unchanged at
  0.74. A first run that deleted only `mutmut-stats.json` re-used the
  0.6.7 verdicts for fourteen modules and was discarded.

## 0.6.7 (2026-09-14)

What a sixth outside review of 0.6.6 found. Two of its points are new
defects, two are the guarantees the lock did not actually give. The pin
does not move and the register table has no new row.

### Fixed

- **A bound port counts only when its owner is this backend.** An owner
  that could not be resolved was read as readiness, while the stale
  cleanup reads the same uncertainty as "touch nobody". One doubt, two
  opposite answers; the strict reading is the one that matches the
  comment and the ADR.

- **A backend that exits before it binds no longer collects READY.**
  The start-failure branch asked whether the child was still alive, so
  a backend that bound nothing and then exited 0 skipped it and reached
  `READY=1` through the exit mapping. The question is now whether the
  device is still there: if it is, the start fails; if the interface
  went away, the clean no-op start is what it always was.

- **The lock names the device, not the config directory.** Two
  `--config` directories over one interface held two different locks
  over one piece of hardware. The key is the USB id and the serial, and
  the file lives in `$XDG_RUNTIME_DIR/oscmix-desk/`, which the unit
  creates through `RuntimeDirectory=` instead of the installer creating
  it in a read-only home. ADR 0022.

- **A writer that cannot hold the lock does not write.** A missing lock
  file, a filesystem that cannot lock, and a wait that ran out all
  ended in the start applying the routing anyway. That made "every
  writer holds one lock" conditional on nothing going wrong. The start
  now fails and systemd retries.

### Changed

- **`reload_service`'s docstring** described the two-state function it
  stopped being in 0.6.6.

- **The test suite gets its own `XDG_RUNTIME_DIR`.** Without it a test
  would take the lock of the desk the developer is listening to.

- **The mutation run's survivors were read.** They showed five missing
  assertions and no defect: the stranger on the port, reported once
  rather than on every poll; the fallback beside the config when there
  is no runtime directory, for a start and for a switch; the runtime
  directory that cannot be made, which says so; and a broken row in
  `/proc/net/udp`, which does not end the table. Score 0.741 against a
  floor of 0.730, the not-covered bucket still empty, `min_score`
  unchanged at 0.74.

## 0.6.6 (2026-09-13)

What a fifth outside review of 0.6.5 found, checked against the code
and fixed: a lock that serialised the writes but not the decision of
what to write, three paths that reported success for work that did not
happen, and a cleanup that signalled by name rather than by ownership.
The pin does not move and the register table has no new row.

### Fixed

- **The desk is read inside the lock that writes it.** The start parsed
  the config before it waited for the device, and the reconcile read it
  before it took the lock. A profile switch committed in that window was
  overwritten by a snapshot older than it: two writers, correctly
  serialised, wrong end state. Both now take the lock first and read
  after, and the machine settings stay with the running process. ADR
  0020.

- **A backend that never binds its port fails the start.** The port wait
  logged a warning and carried on, so the routing was written into a
  port nobody bound -- UDP drops that silently -- and `READY=1` told
  systemd the desk was set. The start now stops the backend it spawned
  and exits non-zero instead. ADR 0021.

- **The stale cleanup signals the process that holds the port.** It
  terminated every `oscmix` of this user as soon as anything held the
  port, which could stop a second interface's backend and leave the
  actual holder running. The socket inode from `/proc/net/udp` is
  resolved through `/proc/<pid>/fd` now, and only that process is
  signalled, and only when it is an `oscmix` of this user.

- **A reconcile that wrote nothing says so.** `reconcile_now` refuses to
  write blind when the receive port is held and returns False; the
  status line said `reconciled` either way. It now reports `reconcile
  skipped`, which is what an operator reads.

- **A refused reload is no longer reported as "not running".** Both
  answers were False, so a unit that was up and rejected the reload was
  described as stopped, with exit 0. Three states now, and a switch
  whose reload was refused exits 5.

- **A switch that could not be recorded exits 4, not 0.** Suppressing
  the reload was right and shipped in 0.6.5; telling a provisioning
  script that the desk is permanent was not.

- **A stop that arrives while waiting for the device lock writes
  nothing.** The stop check ran only inside the verifier, so a process
  on its way out could still apply a full routing once the lock came
  free.

- **The marker write is complete, and its durability is reported.** A
  short `write(2)` could truncate the profile name, the directory fsync
  swallowed every error, and removing the marker never synced the
  directory at all. All three are fixed; a directory that cannot be
  synced is now a warning that says what is at risk.

- **A lock error is no longer read as contention.** Any `OSError` from
  `flock` meant "somebody else has it", so a filesystem that cannot lock
  produced a 30 second wait and then the wrong message. Only `EACCES`,
  `EAGAIN` and `EWOULDBLOCK` are contention now.

- **A dry run never switches.** `--profile X --dry-run` took the device
  lock, wrote the registers and recorded the marker. It loads the
  profile, prints what would go out and touches nothing now;
  `--no-profile --dry-run` shows `routing.conf` for the same reason.

- **Two desks asked for in one command are refused before anything is
  written.** `--profile` with `--no-profile`, `--diff` or
  `--dump-config` let the first branch in the dispatch win, so a switch
  happened and was then compared against something else.

- **A port held by a stranger is not backend readiness.** The port wait
  resolves the socket owner and keeps waiting unless the holder is the
  backend this process started. It is the other half of a cleanup that
  deliberately leaves strangers alone (ADR 0021).

- **A failed apply releases the device lock.** An exception from
  `apply_routing` kept it until the process exited, and the next switch
  waited out the full timeout and then refused.

- **Invalid UTF-8 in `routing.conf` is a configuration error.** It
  names the file like every other config error instead of ending in a
  traceback: `UnicodeDecodeError` is a `ValueError`, and the read
  caught only `OSError` and the parser's own errors.

- **Deeply nested OSC bundles no longer exhaust the interpreter
  stack.** A bundle may contain bundles, which costs four bytes a level
  on the wire and one Python frame here, and this reads whatever the
  network hands it. The unwrapping is iterative.

### Changed

- **The mutation run's survivors were read.** They showed five missing
  assertions and no defect: the marker's write loop, which a mutant
  turned back into a single write; the two warnings that report a
  directory fsync that did not happen; the conjunction in the
  socket-owner check, where either half alone names the first process
  that has any socket open; the return values of the port wait; and the
  machine settings carried across the read under the lock. Score 0.740
  against a floor of 0.720, the not-covered bucket still empty,
  `min_score` 0.73 -> 0.74.

- **Documentation that had drifted from the code.** The architecture
  said Room EQ and `/output/{ch}/phase` cannot be set from a config,
  which stopped being true when the pin moved in 0.6.0, and described
  the config parser as refusing everything it does not model, when
  unknown sections are warned about and ignored on purpose. The README
  no longer offers the verification line as proof that the playback mix
  matrix is right: nothing can prove that, because the device never
  reports it. The mutation policy's own docstring promised absolute
  survivor and kill gates that it does not enforce.

## 0.6.5 (2026-09-12)

One lock for every writer of the device, and a switch the marker did
not record is no longer handed to the unit. The fourth outside review
named the first as the last architectural gap in the concurrency model
and asked what the second does; both were measured on the desk before
and after. ADR 0019 records them. The pin does not move and the
register table has no new row.

### Fixed

- **The unit takes the same lock a switch takes.** Its start-up apply,
  its verifier and every reconcile write the whole routing. 0.6.4 kept
  them apart from a switch by polling the unit's `Status:` line before
  the switch took its lock, and the switch sent its reload after
  releasing it. `ExecReload` is `kill -HUP`, so that reload returns
  before the unit reconciles, and the next switch could take the lock
  and write into the reconcile. The unit now holds
  `active-profile.lock` across its apply and verifier as one
  transaction, and around every reconcile. A switch waits for it as it
  waits for another switch and refuses after 30 s with nothing written;
  a start that cannot get it applies anyway, because a desk with no
  routing is worse; a reconcile that cannot get it stands down and
  names the reload to send again. Measured on the desk: a switch sent
  right after a restart waited for the unit and then applied, and two
  switches at once still serialise.

- **A switch whose marker could not be written no longer reloads the
  unit.** Measured with the config directory read-only: the profile
  landed, the switch printed `applied` and exited 0, and the reload's
  own reconcile re-read `routing.conf` and undid it two seconds later,
  with the main output at -1.0 dB and back at 0.0 dB. The outcome
  carries whether the marker says what the device does, `--no-profile`
  carries the same fact about removing it, and the line on stdout says
  when it is not remembered. Re-measured after the fix: the profile's
  -1.0 dB stayed.

### Changed

- **The phase polling is gone.** `_wait_for_the_unit_to_settle`,
  `process.service_phase` and `service_is_writing` were half of what a
  lock does, done a second way, and the release that gives the service
  a lock deletes more lines from the CLI than it adds. `STATUS=` stays:
  it is what `systemctl --user status` shows, and it is worth having on
  its own.

- **`install.sh` creates `active-profile.lock`.** The unit cannot: its
  home is mounted read-only. `flock` needs no write access, only a file
  that is already there, so the unit opens the existing one read-only.
  A missing lock file is a warning and an unlocked write rather than a
  refusal to run, so an install older than this release still drives
  the device.

- **The mutation run's survivors were read.** The lock has none of its
  own: the open, the handle and the wait loop are killed entirely. What
  the reading found was assertions that did not exist -- the reconcile's
  trigger and its stop check, its two `STATUS=` notices, the path in
  the warning about a missing lock file, the marker result in both
  `verify=False` paths, and the config-path fallback when no `--config`
  is given -- and no defect. Score 0.739 against a floor of 0.720, the
  not-covered bucket still empty, `min_score` unchanged at 0.73.

## 0.6.4 (2026-09-11)

Two switches at once, a marker that survives a crash, and a switch that
waits while the unit writes the device. The third outside review named
the first two -- concurrent switching and crash semantics -- and
checking them led to the third; ADR 0018 records all three. The pin
does not move and the register table has no new row.

### Fixed

- **Two profile switches at once no longer interleave on the wire.** A
  switch and `--no-profile` hold a lock beside the marker
  (`active-profile.lock`, `flock`) from the first datagram to the last
  read-back; a second switch waits, says so, and refuses with the
  reason after 30 s. Taken after the profile parsed, so a refusal for a
  bad config still writes nothing and needs no lock. A test runs two
  switches against two slow backends and holds that neither's datagrams
  appear inside the other's.

- **The active-profile marker is written atomically.** It was written
  in place; a crash between open and close could leave an empty file,
  which reads as "no profile" -- the choice silently gone on the next
  start. It is written to `active-profile.tmp`, fsynced and renamed
  over the marker now; a failed write leaves the old marker whole.

- **A switch waits while the unit is writing the device.** Found while
  checking the lock: with the receive port held -- the mixer GUI's
  normal state -- the reload after a switch cannot reconcile, and a
  switch that overlapped the start-up verifier's blind re-apply stayed
  reverted after all. `--profile` and `--no-profile` now read the unit's
  `Status:` line and wait while it says applying, verifying or
  reconciling, bounded and logged. One overlap is left. This release's
  live test logged the ordering that allows it: a switch's reload goes
  out after its lock is released, so a second switch started at the
  same moment can write while the unit reconciles for the first, if
  that reconcile starts in a gap where the second switch does not hold
  the receive port. The second switch's own reload then re-applies its
  desk, so the end state is right. Roadmap item G has the fix: one lock
  for every writer, the unit included.

### Changed

- **The switch lock is under mutation testing.** It carried
  `@contextlib.contextmanager`, and mutmut leaves decorated functions
  unmutated, so the loop the no-interleave guarantee rests on had no
  mutants at all. It is a plain generator wrapped at assignment now.
  Its survivors found one real gap: the "waiting for it" line could
  repeat on every 0.1 s poll without a test noticing. Asserted now.
- **The mutation run's survivors were read.** In the new profile,
  process and CLI code they showed seven missing assertions and no
  defect -- among them a lock refusal's outcome losing its name, the
  status-line call's arguments, and a restore reading back through a
  socket of its own. A by-name re-run also showed that mutmut's
  incremental stats attribute no existing test to a function that is
  new under its name; the baseline records how that was caught. Score
  0.738 against a floor of 0.720, not-covered bucket empty;
  `min_score` 0.72 -> 0.73.
- **The coverage gate rose from 95 to 96.** Measured 96.4 on the
  release revision, more than a point above the gate.

## 0.6.3 (2026-09-11)

A profile survives a start, and a switch survives the start-up
verifier. Both are the second half of the review audit 0.6.2 shipped
the first half of; the pin does not move and the register table has no
new row.

### Fixed

- **A profile switch right after a start was reverted by the start-up
  verifier.** The switch writes from a second process; for up to about
  22 s after a start the unit's verifier is still re-applying the config
  it started with, and it overwrote the switch. Measured on the desk: a
  switch sent immediately after a restart read back at the old fader
  value fifteen seconds later. An applied switch, and `--no-profile`,
  now reload the running unit; it re-reads the desk in effect and its
  reconcile queues behind the verifier, so the last write is the
  profile's. The unit reports its phase through systemd (`Status:` in
  `systemctl --user status`): applying, verifying, reconciling, running.

### Added

- **A profile survives a start.** `--profile X` used to write X to the
  device and exit while the service kept `routing.conf`; the next
  replug, restart or reload -- including the resume hook's after every
  suspend -- re-applied `routing.conf` over it, with the journal naming
  exactly what it had applied. The switch now remembers the profile in
  `active-profile` beside `routing.conf`; every start and reload applies
  the remembered profile, and the start line says which desk is in
  effect and that `routing.conf`'s routes are not. `--no-profile`
  applies `routing.conf` again and forgets; `--list-profiles` marks the
  active one; `--diff` and `--dry-run` speak for the effective config. A
  remembered profile that no longer loads falls back to `routing.conf`
  with a warning that stays until somebody decides. The service only
  reads the marker, so the unit's read-only home holds. ADR 0018 has
  the reasoning and the alternatives; roadmap item F is closed.

### Changed

- **What the device ever reports is answered by the register table.**
  `verify.register_ever_reported` excluded the playback matrix by a
  string rule beside a table that already classed it `REESTABLISHED`
  -- two places for one fact. With a model the table decides; the rule
  remains only for a device without one.
- **The README says what the single device is.** Every number here was
  measured on one UCX II because upstream oscmix is written for it; the
  section on other models now states what an 802 gets today and what a
  second device would take, instead of "reports welcome" alone.
- **The mutation run's survivors were read, and one gap was a whole
  command.** `--pipewire-sinks` had no in-process test; it was counted
  as covered while its code sat inside `cli.main`, and moving the block
  into its own function in 0.6.2 showed 43 mutants no test reached.
  Four tests cover it now. In the new profile code, four assertions
  that did not exist were added -- among them that the marker
  functions answer `None` and `False` without a config rather than
  raising. Score 0.720 against a floor of 0.710, not-covered bucket
  empty again.

- **No in-process test can reach the machine's user manager.** All
  `systemctl` calls outside the launcher go through one function, and
  an autouse fixture stubs it for every test.

## 0.6.2 (2026-09-10)

An outside review of 0.6.1 named stale documentation in `reconcile.py`,
the single-device specialisation, the complexity of the reconciler, the
gap between tests and firmware, and the limits of a coverage gate. The
audit that followed found the stale claim, one silent behaviour of the
same shape as 0.6.1's route defect, and a concurrency hole; this
section is what it fixed. The pin does not move.

### Fixed

- **A config for a device without a register table dropped its channel
  sections in silence.** `[input:3] gain = 12.0` on a `Fireface 802`, or
  on any name the model does not know, parsed to nothing with no
  message -- the 0.6.1 route defect one file over: accepted, shown in
  nothing, delivered nowhere. Nested and global sections on the same
  device did get a warning, and it named the wrong cause ("may have been
  written by a newer version of oscmix-desk"). Both now say what is
  true: no register model for that device declares the section, nothing
  in it could reach the device, and which devices are modelled. Routes
  keep working on such a device, as ADR 0006 intends; a global family
  the model lists with nothing settable is refused, like Room EQ was.

- **A reload could write routing while the start-up verifier was still
  writing it.** `_reconcile` ran on the main thread with no regard for
  the verifier thread, which releases the receive port between its
  phases; a SIGHUP in one of those gaps interleaved two link phases and
  two mix writes on the wire, the ordering ADR 0001 exists to guarantee.
  The resume hook makes the window real: after a suspend the device
  re-enumerates, udev restarts the unit, and the hook's reload arrives
  during the verifier's window. The reconcile now waits for the verifier
  (`RECONCILE_WAIT_FOR_VERIFIER`, 30 s, derived from its longest path),
  abandons the wait on a stop, and logs rather than swallows a reload
  that outlives the bound. ADR 0013 records the rule; four tests hold it.

- **`--osc-port` accepted any integer.** `[osc] port` in the file
  refused 0 and 70000; the command-line override passed them to the
  backend, whose bind failure was the first sign. Bounded the same way,
  exit 2.

### Added

- **Every evidence artifact names the firmware it was taken against.**
  The device offers two version numbers and none of the artifacts
  recorded either: `bcdDevice` from sysfs (the USB device release,
  3.01 on the reference unit) and the register `/hardware/dspvers`
  (36), which the model has carried since 0.4.0 without anything
  reading it. A firmware that behaved differently would have been
  invisible in the evidence -- the review's point about tests and
  firmware, made concrete. `discovery.device_firmware` gives both one
  shape, and the hardware evidence, the write sweep, the recorded dump
  and the `--snapshot` header carry it; the release checklist compares
  it against the previous release's artifact and says what a change
  means. The committed sweep artifact and the refresh-dump fixture
  predate the field and are not backfilled by hand; each is re-recorded
  when its own rule says so, and carries it then.

### Changed

- **Three functions with no caller are gone, and the trait the barrier
  promised to read is read.** `reconcile.routes_of`, `reconcile.unreachable`
  and `registers.channel_limit` had no caller outside a test, and the
  dead-code gate at 80 % confidence could not see them; it runs at 60 %
  now with a reasoned allowlist (`quality/vulture-allowlist.py`) for
  the names that exist as test contracts or documented data.
  `backend.Traits.reports_link_state_on_write` said since 0.2.0 that
  flipping it would be the change when upstream fixed the stereo cache,
  and no branch consulted it; `_cross_the_barrier` does now, and a test
  holds that a backend which updates its link state on write pays no
  barrier while still sending every link before any mix. The other two
  traits say, per field, that they are documented and not read. The
  route-by-route walk `reconcile.plan` replaced in 0.4.0
  (`routing_plan`, `route_messages`) had no runtime caller either and
  was public; it is the test oracle it always was, in `tests/oracle.py`.

- **`reconcile.py` no longer claims that nothing writes through it.**
  The module docstring said so since 0.2.0, was false from 0.4.0 on, and
  stayed for three releases until the review read it. It now names what
  writes through the planner and since when. The last path that did not
  -- `send_mix`, the verifier's mix re-apply -- does now, so a register
  two routes share goes out once there too; a test pins the wire change.
  Sixteen more "today" and "this release" phrases in runtime docstrings
  became the version they were written in, so a reader no longer needs
  `git blame` to know whether a sentence is still true.

- **The two start-up helpers only subprocess tests reached have unit
  tests.** The 0.6.1 mutation run left exactly 50 mutants with no
  covering test: 26 in `_await_backend_port`, 23 in
  `_install_stop_handlers`, one in a dead function. Both are driven in
  process now -- the port wait returns on listen and on a dead child and
  warns on timeout; the stop handlers set the flag, tell systemd first
  and terminate only a backend that is still there.

- **The mutation run's survivors in the new code were read, and one
  was a defect.** `usb_revision` accepted hex digits and then called
  `int()` on them; a `bcdDevice` of `0a01` would have raised. BCD is
  decimal, and the code and a test say so now. Three test stubs that
  ignored their arguments or asserted no timing were tightened. The
  not-covered bucket is empty for the first time: 50 -> 0, score 0.716
  against a floor of 0.710.

- **The performance gate skips on an overloaded host instead of
  failing.** `make flake` failed once on 2026-09-05 with the
  bundle-walking test at 39.6x for 10x the work, on a load average of
  36 over 8 cores from unrelated suites; the next quiet run read 9.3x.
  Best-of-5 instead of best-of-3, and a ratio that would fail is a skip
  when the load average exceeds twice the core count -- never on a CI
  runner, which is where the gate matters.

- **Docs.** TROUBLESHOOTING gained two sections from the 2026-09-05
  incident: the PipeWire gain chain, where three controls "about half"
  cost 63 dB before the device saw anything (measured, with the command
  that shows the real gains), and the fader that moves back by itself
  because a route declares `volume =`. The README says that a profile
  lasts until the next start and why; the roadmap carries the fix as
  item F with the design question it needs answered first.

## 0.6.1 (2026-09-05)

Two defects found by auditing the tree rather than by a failing test,
one of them shipped since 0.4.0. The pin does not move.

### Fixed

- **A config with no `[route:*]` section was never applied at start.**
  `session._apply_and_verify` returned before `apply_routing` whenever
  `config.routes` was empty -- a guard from 0.1.0, when routes were all
  a file could say, that survived every release the surface grew in.
  Since 0.4.0 a file may consist of `[input:3]`, `[eq:input:3]` or
  `[clock]` sections alone; such a file parsed, printed its writes in
  `--dry-run` (which promises to print what the apply sends, roadmap
  item G), went out on `systemctl --user reload`, and wrote nothing on
  boot or hotplug, with the journal saying `no routes configured;
  leaving mixer state untouched`. The check is on what the config
  *declares* now, routes, channel state and global state alike, and
  says `nothing declared in the config` only when that is empty.
  Pinned by a unit test on the rule and an end-to-end test that starts
  the session on a channel-only file and reads the three writes off the
  stub backend, then the read-back confirming them. The start-up log
  line counts channel and global settings beside routes now.

- **One integration test started the developer's real `oscmix.service`.**
  `test_launcher_reports_a_failing_exec_instead_of_crashing` ran
  `bin/oscmix-launch` with a faked device and client but the real
  `PATH`, so the launcher asked the machine's own user manager whether
  the service was up and, when it was not, started it: observed in the
  journal on 2026-09-05 as `Starting oscmix.service` with no USB event,
  30 s of waiting for a device the fake `/proc` could not show, exit 0.
  `make_env` now puts a refusing `systemctl` stub first on `PATH` for
  every subprocess test, and the test that leaked asserts its start
  attempt landed on the stub. The one test that needs specific answers
  keeps its own stub in front.

### Changed

- **Two test-harness timing holes, closed at the source.** The flake
  gate caught `test_a_rewrite_alone_does_not_make_it_differ` reading
  empty stdout once in CI (run 33161748663): `free_udp_port()`'s
  bind-close-return lets the OS hand the same port out twice, so a
  send/recv pair can collide -- a fake backend answering itself while
  the CLI reads silence. The helper now remembers its recent draws,
  which heals every one of the 34 two-port call sites centrally, and a
  regression test holds it there. The shared `FakeBackend` and the
  diff CLI's recording fake also deliver dumps as one OSC bundle now,
  as upstream does and as the apply-routing fixture already did --
  deliberate dropped, duplicated and reordered reports stay per-datagram
  fault injections. Nothing shipped changes; 0.6.0's own CI was green.

- **The mutation run's survivors were read, as they are meant to be.**
  Fixing the apply above made a unit test drive `_apply_and_verify` in
  process for the first time -- everything reaching it before went
  through the subprocess integration tests, which load the checked-out
  source and never the mutant (ADR 0005). 32 mutants entered the judged
  pool with that, and 19 of them survived: the test asserted that the
  apply had been called and that a thread came back, so the ports handed
  to the apply, the stop check given to the verifier and the thread's
  target, name and daemon flag could all be mutated in silence. The
  test pins each of them now, and the re-run kills all 19. Score 0.714
  against a floor of 0.710; `not_covered` fell 82 to 50.

- **Docs caught up.** The README counted "five issues, two fixed"
  upstream; all five are fixed at the pin, and `docs/upstream-issues.md`
  entry 2 now says so. The README's pin table listed `48v` beside the
  settable options although no config can set it (the parser refuses
  it by design), which invited exactly the line it refuses; it is named
  as modelled-but-unsettable instead. `config/routing.conf.example` was
  still headed `oscmix-autostart`, recommended `restart` where `reload`
  is the reconcile, and showed none of the nested or global sections
  that exist since 0.4.0; it does now. TROUBLESHOOTING section 8 gained
  the `Invalid argument` / exit 1 / restart sequence the journal showed
  once on 2026-08-30, with what it means and what it does not.

## 0.6.0 (2026-08-28)

The pin moves to upstream `f2fdd5e`, and three register families move
with it -- from "reported and not settable" to settable, each one fixed
upstream within a single day and measured here before the model was
allowed to say so.

### Added

- **`[roomeq:output:N]` sections.** Room EQ's 640 registers carry the
  channel EQ's domains now, plus `delay` with upstream's own bounds
  (0 to 0.425 s). The writes failed for a measured reason: the UCX II
  takes Room EQ writes at `0x3400` while reporting the family from
  `0x35D0`, a split range found through upstream #33 and fixed in
  `f2fdd5e`. Measured at the new pin: `band1gain` -6.0 goes out as
  `setreg 3403` and reads back -6.0, where it had always read 0.0.
  `docs/register-addresses.md` records the write range, confirmed on
  the wire on two outputs.

- **`phase` on outputs.** Fixed by this project's PR #36, merged
  upstream as `9dba36f`, and measured on all 20 outputs before the
  model changed sides.

- **`gain` on Analog 5-8**, 0 to 24 dB -- the range their "Pre Gain"
  field always had, added upstream in `fdc47f7` after the 0.5.0 write
  sweep filed #35. Measured: 12.0 dB round-trips.

### Changed

- **The write sweep now proves 1902 registers**, up from 1238: every
  newly opened row was written a different legal value and confirmed
  against the device's own report -- 1888 confirmed, the same 14
  skipped as dangerous, at `f2fdd5e`
  ([docs/evidence/write-sweep-ucx2.json](../evidence/write-sweep-ucx2.json)).
  The refresh-dump fixture was re-recorded at the new pin (2322
  registers, unchanged in shape -- all three fixes are write-side).

- The read-only half of the nested-family split is empty for the first
  time. The split and its assertions stay: the next read-only family
  must not grow a config section that accepts settings and delivers
  none.
