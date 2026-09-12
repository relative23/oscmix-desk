# Changelog

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
  ([docs/evidence/write-sweep-ucx2.json](docs/evidence/write-sweep-ucx2.json)).
  The refresh-dump fixture was re-recorded at the new pin (2322
  registers, unchanged in shape -- all three fixes are write-side).

- The read-only half of the nested-family split is empty for the first
  time. The split and its assertions stay: the next read-only family
  must not grow a config section that accepts settings and delivers
  none.

## 0.5.2 (2026-08-27)

### Fixed

- **A stale system-wide backend could shadow the pinned install.**
  `resolve_binary` consulted PATH before the pinned location, and the
  systemd user manager's PATH at early boot lacks `~/.local/bin` while
  containing `/usr/local/bin` -- so the first hotplug start after a
  reboot ran a February build from a pre-rename install, for six hours,
  caught only by its enum warning missing the OSC address upstream
  added in `05621e5`. `~/.local/bin` is now consulted before PATH (the
  `OSCMIX_BIN_*` override still first) -- for the backend pair **and for
  `oscmix-gtk`**, whose launcher carried its own PATH-first copy of the
  lookup and would have kept the hole; it now delegates to the one
  rule. Tests pin the order for both, `install.sh` warns when a
  shadowing copy of any of the three sits in `/usr/local/bin`, and the
  supervisor names the signal when the backend dies of one
  ("status -13 (SIGPIPE)") instead of leaving the number to be decoded.
  TROUBLESHOOTING gained the diagnosis, and the journal lines that look
  alarming and are not -- plus the recovery for a Fireface whose
  PipeWire profile went `off` after an `alsa-ucm-conf` update retired
  the UCM profiles it used, which this release's own hardware run
  measured the hard way, twice.

- **The mutation gate's loss-free device fake is loss-free now.** It
  sent one refresh dump as five separate UDP datagrams, while upstream
  sends an OSC bundle, and a dropped register could spuriously take the
  verifier's legitimate retry branch. The same timing failure first
  appeared once in CI and returned in this release run. Normal fake
  dumps now mirror upstream with one bundle; deliberate dropped,
  duplicated and reordered reports remain explicit fault injections.

### Changed

- **The nested-options fallback in `reconcile` is gone.** `_register_for`
  carried a second lookup for `eq/band1freq`-style options behind
  `option_register`. Reading the mutation survivors in the code added
  since 0.4.0 put all 17 of that function's mutants in the fallback
  branch, and they were not test gaps: the branch was unreachable.
  `option_register` assembles a nested option's template from its name,
  and the parser refuses an invalid channel before reconcile sees it.
  Verified at the device (`[eq:input:25]` refused at load,
  `[eq:input:3]` resolved and on the wire). `channel_entries` now calls
  `option_register` directly; nothing that reaches the wire changed.

- **Two behaviours of `built_backend_revision` are asserted that nothing
  tested.** The revision is a `str`, not bytes: without `text=True` the
  length check still passes on `b"..."` and `json.dumps` of the sweep
  artifact crashes. And a broken `build/oscmix/.git` answers `None`
  instead of raising, so evidence collection degrades to "revision
  unknown" rather than aborting the measurement. Both assertions were
  checked to kill their mutants; `registers._channel_in` keeps its one
  survivor, equivalent over every real template.

- **Docs.** `docs/upstream-issues.md` records #35 as filed, the 802
  rework in huddx01's fork, the rewrite of #29 and PR #31 after
  reading them as the maintainer would, and the output-phase fix
  proposed upstream as PR #36 (the write gate on `OUTPUT_PHASE`,
  measured wrong on all 20 outputs and removed), with entry 4's cause
  corrected to match what was actually filed. The release checklist gained
  the write-sweep item it predated, TROUBLESHOOTING gained `--diff` and
  `--snapshot`, and the scratch-home install/uninstall cycle
  (checklist section 5) ran clean from the renamed directory.

## 0.5.1 (2026-08-26)

A cleanup release: everything the 0.5.0 audit left behind, found by
asking what in the repository is stale, wrong, or hand-made.

### Fixed

- **The sweep's evidence artifact carried a false statement and could
  not be reproduced by the tool.** It recorded an `echo_timeout` the
  sweep no longer had -- a leftover of the echo design that could not
  work -- and its provenance (`device`, `oscmix_revision`, `method`)
  had been patched in by hand after the run, so a fresh `--out` run
  produced an artifact that failed the repository's own tests. The tool
  now records its real pacing, its method, the device serial and the
  built backend revision itself, and the artifact test asserts it did.

- **Restoration is verified and retried, not assumed.** The per-pass
  restore was a single write, and this repository has measured that a
  single write can be dropped: one run left `/output/7/eq/band2q` and
  its partner holding a probe value while reporting the desk clean of
  everything else. The repair loop now re-writes what still differs and
  reads back, up to three rounds; only what survives that is reported
  as `not_restored`. The committed artifact is from a fresh run of the
  fixed tool: 1224 of 1238 confirmed, nothing left unrestored.

### Changed

- **`device_serial()` and the built-backend-revision lookup moved into
  the library** (`discovery`), because the write sweep needed the same
  answers as `verify-hardware.py` and two copies would be two places
  for the rule to disagree. Both scripts now delegate.

- **The docs caught up with 0.5.0.** The roadmap's status section said
  0.4.0 was the released state; the README's evidence section did not
  mention the write sweep and undercounted the decision records;
  `docs/upstream-issues.md` was missing entries for the two issues
  filed in 0.4.0 (#33, #34) and now carries the drafted report on
  `/input/5..8/gain`, whose writes are clamped to zero by a gain flag
  with no range in upstream's channel table.

## 0.5.0 (2026-08-25)

### Added

- **Every settable register is proven to accept a write.** 1224 of 1238
  confirmed against the device, 14 skipped as dangerous, none ignored,
  nothing left unrestored, in 101 seconds. `scripts/sweep-writes.py`
  writes each register a different legal value from its own declared
  domain and reads the result back; the per-register verdict is in
  `docs/evidence/write-sweep-ucx2.json` with the device serial and the
  oscmix pin, and two tests check that file against the model rather
  than trusting it. A new settable register fails the suite until the
  sweep has run again.

  Until now the whole verification apparatus ran in the read direction.
  `verifiable` is a promise that a write comes back, and nobody had
  asked the device to keep it.

  **The obvious design cannot work here**: waiting for an echo on the
  path just written waits for the one message that never arrives. See
  *Measured* below. The sweep reads back from a refresh dump instead,
  in passes split by channel parity so a linked pair is never written
  against itself, with the writes paced because a burst is dropped.

### Fixed

- **`--dump-config` silently omitted `/input/1/gain` and
  `/input/2/gain`.** The dump built its worklist as a dict keyed by
  option name, and a dict keeps one row per name. Once gain became
  three rows the survivor was the instrument row, so the loop walked
  only channels 3-4: 1198 channel settings where there should have been
  1200. The device reported all four the whole time.

  Found by running `--dump-config` against the desk while auditing for
  the release, not by a test. There is a test now.

- **Input gain is three registers, not one.** Upstream's channel table
  gives `.gain={0, 750}` to the two mic preamps, `{0, 240}` to the two
  instrument channels, and no range at all to Analog 5-8, which leaves
  `setinputgain` clamping them to `{0, 0}` -- a control present in the
  OSC tree that can never do anything. The model declared one row,
  0..75 dB on channels 1-8, so a config could ask for 75 dB on a
  channel that silently gives 24, or gain on four channels that have
  none.

  Now mic 0..75, instrument 0..24, and line readable with no value
  domain, the same shape as `48v` and the output phase. `register_at`
  and the config lookup resolve by channel; the first of those already
  answered `/input/9/gain` with a register whose capability stops at 8.

- **`/echo/width`, `/reverb/width` and `/reverb/smooth` had no bounds
  while the device enforces some.** They are unbounded in upstream's
  node table, and the standing rule is that an invented range would
  reject values the device accepts. But this device *rejects* an
  out-of-range write outright rather than clamping it -- no change, no
  report -- so a config asking for `width = 1.02` would have been
  ignored in silence.

  Bracketed against the hardware: 0.0 and 1.0 accepted on both width
  registers, -0.01 and 1.02 refused; 0 and 100 on smooth, -1 and 101
  refused. Ten reverb numbers stay unbounded because nobody measured
  them, and `/reverb/volume` still carries no ceiling copied from
  `/echo/volume`.

### Measured

- **Three properties of the write path**, all found while building the
  sweep and all now in the roadmap's standing constraints, because any
  future design has to meet them.

  **A write is never echoed on the path it was written to.** A linked
  pair answers on the *partner* path carrying the value written
  (`/output/3/volume` reports `/output/4/volume`), and a register with
  no linked partner draws no reply at all (`/input/1/gain`) even though
  a dump shows the value landed. So a write is confirmed by re-reading
  a dump, never by waiting on the path just written. This is the same
  behaviour the *Measured* note above describes from the other side,
  and it supplies the rule that note was missing.

  **Bursting writes loses them.** oscmix turns each into a MIDI SysEx
  message and that wire carries roughly a thousand registers a second.
  295 writes in a few milliseconds lost 40; the same registers take the
  value when written one at a time.

  **An out-of-range write is refused, not clamped.** The value stays
  where it was and nothing is reported.

  Each of the three first appeared as a false verdict -- the sweep
  calling a healthy register deaf -- and each was caught by testing the
  unbelievable result alone rather than reporting it. The first full
  run would have filed 44 device defects that do not exist.

### Changed

- **Mutation testing moved from every push to nightly.** It had grown to
  72 minutes and, since the other nine jobs finish in minutes, it alone
  decided when CI was done.

  The workflow comment asked for the slowest tests to be checked before
  the number was touched. They were, and there is no hot spot: tests
  +42%, suite runtime +45%, mutants +16%, mutation +64%. Cost is mutants
  times suite time and the growth is fully explained, so it will keep
  growing multiplicatively and would pass two hours by the next release.

  The score has **never failed its gate**, in any run. What was valuable
  was reading the survivors, which found three real defects in 0.3.0 --
  including pinning silently not working while every test stayed green.
  A nightly score change still prompts that reading. The release
  checklist keeps its own run.

### Added

- **`--diff` exits 3 when the device and the config disagree.** 0 still
  means they match and 1 still means the read failed, and keeping those
  three apart is the point: `diff(1)` returns 1 for "differing", which is
  not available here because 1 already means a failure. A monitoring
  check that cannot tell *the desk drifted* from *the backend never
  answered* reports healthy silence while the backend is down.

  A rewrite does not count as a difference. `/mix/<out>/playback/<pb>` is
  never reported and is written on every apply, so counting it would pin
  the code at 3 for ever.

  Measured against the device: matching config exits 0, a config asking
  for `volume = -6.0` against a desk at 0.0 exits 3 and names both
  values, and an OSC port nobody answers exits 1 with *no reply from the
  backend*.

### Changed

- **Renamed to `oscmix-desk`.** The old name described 0.1.0 exactly and
  names one of two halves now: it is still an autostart, and it is also a
  state layer over 2028 declared registers. "Desk" covers both, and
  keeping `oscmix` in the name keeps the project findable by the people
  who would want it.

  **Nothing a user touches changes.** The unit is still `oscmix.service`,
  the config still lives in `~/.config/oscmix/`, the commands are still
  `oscmix-session` and `oscmix-launch`. What moved is the Python package
  (`oscmix_autostart` to `oscmix_desk`), the install directory and the
  repository name; GitHub redirects the old URLs, so existing clones keep
  working.

  `install.sh` removes a pre-rename install at
  `~/.local/lib/oscmix-autostart`, and `uninstall.sh` cleans both paths.
  Without that an upgrade leaves a complete second copy of the package
  behind, and `oscmix-launch` searches `../lib/*` for a package directory:
  a stale one there is a version nobody chose.

- **The README is rewritten.** It still opened with "what oscmix does not
  ship is the desktop integration", which was true in July and says
  nothing about declaring an EQ in a text file. It now leads with the
  file, lists what 0.4.0 can express, and has a section on how the claims
  in it were arrived at.

- **`LICENSE` and `patches/README.md` say whose code is whose.** A diff
  quotes the lines it changes, so the context in `patches/` is Michael
  Forney's work under ISC while this repository is MIT. The two are
  compatible and nothing is relicensed; the attribution was simply
  missing.

### Measured

- **The device pushes far more than one register**, which ADR 0013's
  opening premise denies. Writing a register from a second client -- the
  path a mixer GUI uses -- makes the device report the **partner channel
  of a linked pair**, unprompted and immediately: volume, mute,
  crossfeed, and every block of EQ, dynamics, low cut and auto level, on
  inputs as well as outputs.

  Silent: `reflevel` on either side, `input/gain`, `input/phase`.
  "Front end versus DSP" was the obvious guess and it is wrong, since
  `hi-z` is front end and pushes while `phase` is DSP and does not.

  **The rule turned up while building the 0.5.0 write sweep, and it is
  not about the register at all: what gets reported is the *partner
  channel*.** A register reports exactly when writing it also moves the
  other half of a linked pair, because the device reports only on
  change and the written channel is the one oscmix has already updated
  in its own cache. Checked in both directions across four families:
  `mute`, `volume`, `eq/band1gain` and `lowcut/freq` move the partner
  and report; `phase` and `gain` leave the partner alone and stay
  silent. That accounts for every entry in both lists above.

  So an event-driven drift signal for linked pairs needs no clock and no
  polling, which ADR 0013 ruled out for lack of one. But the registers
  that stay silent are exactly the installation state PIN exists for, so
  the premise changed and the question did not close. Nothing is built
  on it.

### Added

- **`--snapshot`**: every register the device reports, sorted, one per
  line, meters excluded. Not a config and holds nothing back.

  `--dump-config` renders a *config*, so it can only show what a config
  can express: registers with a value domain. The link flags, phantom
  power, Room EQ and `/clock/samplerate` are invisible in it, and a diff
  of two dumps therefore cannot prove they are unchanged.

  Found the hard way, and against this project's own practice. A
  measurement here left `/output/9/stereo` unlinked on a working desk
  and two dumps compared equal. The link flags are the register class
  that produced every defect in 0.1.3, so a restoration proof blind to
  them is not one. A snapshot shows `/output/9/stereo 1` becoming `0`
  directly; 2252 registers against the dump's 1680 lines.

### Security

- **The unit is hardened as far as a user manager allows**, measured
  rather than assumed: `systemd-analyze security --user` went from
  **8.3 EXPOSED to 5.4 MEDIUM**. Added `UMask=0077`,
  `KeyringMode=private`, `RestrictNamespaces`, `RestrictSUIDSGID`,
  `RestrictRealtime`, `ProtectKernelTunables`, `ProtectControlGroups`
  and `SystemCallFilter=@system-service`. Each was started against a
  probe unit and then against the real service; the routing still
  verifies and a tone still lands on every configured output.

  Three of those had been listed as impossible in a user unit since
  0.2.0 and are not. `ProtectKernelTunables`, `ProtectControlGroups` and
  `RestrictSUIDSGID` all start. They were assumptions that had never
  been run.

  Three directives the manager *accepts* are now forbidden with reasons,
  which is the half a probe unit cannot tell you: `PrivateNetwork` cuts
  the mixer GUI off from the backend, `ProcSubset` hides
  `/proc/asound/seq/clients` from device discovery, and `PrivateUsers`
  is untested against ALSA device access.

- **ADR 0017 states the trust boundary.** The OSC port has no
  authentication: any local process can set any register, including the
  phantom power this project withholds from config files. That guard is
  in the config parser, not on the port, and it cannot be moved there.
  Written down rather than left as an unstated gap.

  No separate source hash was added for `install.sh`, and the ADR says
  why: a git commit SHA already is one, and the checkout is verified
  against the pinned 40 characters. What is missing is authenticity, not
  integrity, and a second hash of the same content would look like it
  closed that.

### Fixed

- **`phase` on an output was accepted and set nothing.** oscmix's
  `ctltoreg` gates `OUTPUT_PHASE` on `INPUT_HAS_REFLEVEL`, bit 2 of the
  *input* flags, while an output only ever sets `OUTPUT_HAS_REFLEVEL`,
  bit 0. The guard therefore always breaks, `ctltoreg` returns -1 and
  `setval` writes nothing, on every output.

  Measured, not deduced: `/input/1/phase` goes 0 to 1 and reads back,
  `/output/1/phase` and `/output/9/phase` stay 0, and a trace of what
  oscmix writes to the MIDI pipe shows register `0x0007` twice for the
  input and nothing at all for the outputs. So the write never leaves,
  rather than the device refusing it. Reported as
  michaelforney/oscmix#34.

  `/output/{ch}/phase` is now declared reported-and-not-settable, the
  line `/clock/samplerate` and Room EQ already sit on. **A config that
  set it now fails to load instead of quietly doing nothing**, which is
  a change in behaviour and the point of it.

## 0.4.0 (2026-08-23)

The rest of the strip. 0.3.0 declared the signal path; this release
declares what sits on it -- EQ, dynamics, auto level, low cut,
crossfeed, and the five settings that have no channel at all. The
register model went from 18 rows to 147, from 9 settable to 102, and
from 246 concrete paths to 2028.

The pattern of the release is that the plan kept losing to the device.
Three families changed shape after a measurement, one turned out not to
be settable at all, and the two questions the roadmap had filed as
"decide, do not measure" were both measurable.

### Everything the strip has

- **`[eq:input:N]` and the nested section format** (ADR 0014). Settings
  with a sub-family get their own section rather than a dotted option,
  because a dotted option makes an installed 0.3.0 refuse the whole
  file, and a family-first header is the one shape it warns about and
  skips instead.
- **EQ** (480 registers), **dynamics** (320), **auto level** (160),
  **low cut** (120), **crossfeed** (20).
- **The five channel-less families**: `[clock]`, `[controlroom]`,
  `[echo]`, `[hardware]`, `[reverb]`. 38 settable of 42; the other four
  are reporters with no setter upstream, and a config cannot set what
  oscmix cannot write.
- **Room EQ** (640) is declared **readable and not settable**. See below.

### `--diff`

`plan()` printed instead of sent: what an apply would write, what
already matches, and what gets rewritten regardless. Nothing is
written. A rewrite is counted apart from a difference, because
`/mix/<out>/playback/<pb>` is never reported (ADR 0002) and is written
every time whatever the device holds -- listing it as drift would answer
"has the desk changed?" with a number that is never zero.

### The upstream pin moved to 55802a6

Both issues this project filed upstream are fixed: discontinuous enum
values (#30) and the Room EQ register folding (#32). Measured on the
same desk, as ADR 0008 requires: the dump goes from 2002 registers to
2322, Room EQ from 320 folded to 640 real, and `/controlroom/mainout`
arrives as `(-1, 'None')` instead of unnamed.

That gave the model its first enum whose value is not its position, and
upstream's `setenum` reads an integer argument as the raw value -- so a
positional encoder would have written 10 where -1 was meant. Registers
can now declare `values` beside `choices`.

### What the measurements changed

- **Room EQ is reported and ignores every write.** The upstream fix was
  to `regtoctl`, the read path. Writing `/output/N/roomeq/band1gain`
  changes nothing while the channel EQ on the same output, in the same
  run, works; and tracing the MIDI pipe shows oscmix *does* send it.
  Filed as [#33][33]. Declared with no value domain, which is the line
  `/clock/samplerate` already sits on.
- **`lowcut/slope` and `crossfeed` carry bounds the device gave**, not
  upstream, which declares none for either. Written and read back: slope
  clamps at 0..3, crossfeed at 0..5. Both are indices, and both say so
  rather than claiming a unit they were never measured to have.
- **Every scaled bound was checked at the device.** `setfixed` divides
  by `.scale`, so `min=-300 max=300 scale=0.1` is -30..30 to a config.
  Declared the raw way, every range would be ten times too wide.
  `dynamics/gain = -10.0` moved the meter by exactly 10 dB, and auto
  level's `maxgain` by exactly 6 and 12 dB at two settings.
- **Crossfeed was measured twice.** The first run read signal on a
  channel that should have been silent and wandered 2 dB between
  identical settings: the mixer GUI held the receive port. The fix was
  not a better statistic but a baseline, and a left-only tone now reads
  -144.0 dBFS on the other side before any bleed is claimed.
- **No register is withheld as dangerous** (ADR 0016). There is no such
  flag in this codebase; `48v` is withheld by having no value domain,
  and the stated bar is equipment damage. Every candidate was written,
  read back and restored -- `lockkeys = All` included -- so a config can
  undo what a config did.
- **The clock source is state.** Set to `Word Clock` with nothing
  connected, the device accepts it, keeps it, and does not fall back. A
  pinned source argues with nothing, so it stays PIN.

### Defects found, none by a failing gate

- **`--dump-config` never took `DUMP_LISTEN_SETTLE`**, for as long as it
  has existed, while that constant's own test said the cost was paid by
  "every verification, every profile switch and every --dump-config".
  Without it, 4 of 8 reads lost all twenty `/playback/N/stereo`. Found
  by `--diff` disagreeing with itself twice in a row.
- **Nested options escaped the cold-plug rule.** 240 paths were called
  "promptly reported" and re-sent on every hotplug, against a recording
  that says a cold plug delivers 332 of 480 EQ registers.
- **A dump of a working device produced a config that would not load**,
  and later one whose second render differed from its first. Both found
  by the round trip, neither visible in the output.
- **A commit reached `main` with no CI at all.** Three pushes inside an
  hour, and the concurrency group cancelled the queued run before a
  single job started.
- **`[roomeq:output:5]` was accepted and set nothing**, a guard for
  unmodelled devices catching a modelled read-only family.

### The 802

Its channel capabilities are recorded, read from upstream's own
`device_ff802.c`: 30 in, 30 out, 48V and hi-Z on channels 9-12 where the
UCX II has 48V on 1-2, and no gain register on its Mic/Inst channels.
The register table cannot follow, because oscmix cannot drive an 802 at
this revision -- `init()` lists one device, and `ff802` has no
`.regtoctl` or `.ctltoreg`. One of the three things "supported" means,
and the other two are upstream's.

### Quality

- 906 tests (665 in 0.3.0); coverage 95%, gate 95.
- Mutation score 0.708, floor 0.700 (0.687 and 0.67 in 0.3.0). ADR 0015
  takes the register *table* out of the score: it is built at import
  time, which mutmut cannot attribute to a covering test, so 640 rows of
  data scored as though nothing tested them. Verified by hand twice
  before the exemption was written, and the recordings check the table
  harder than a mutant would.
- ADR 0014-0016 record the nested format, the mutation scope and the
  danger question, each with the measurement behind it.
- Each push gets its own CI concurrency group, so a queued run can no
  longer be evicted by the next one.

### Compatibility

`routing.conf` files from 0.3.x are read unchanged. Everything new is a
new *section*, which an older install warns about and skips (ADR 0006).
A 0.4.0 config that uses `[eq:input:3]` therefore loses its EQ on an
older install rather than failing to start.

[33]: https://github.com/michaelforney/oscmix/issues/33

## 0.3.0 (2026-08-20)

The whole signal path, declared. 0.2.0 made the existing behaviour
provable; this release spends that on surface -- and the notable thing
about it is how much was decided by measuring the device rather than by
planning against it. Four measurements changed what got built, and one
of them removed a feature.

### The config can describe the signal path

- **Hardware input routing.** `input = 1/2` as a route source: direct
  monitoring inside the device, no round trip through the computer.
  Unlike the playback matrix, `/mix/<out>/input/<in>` *is* reported, so
  an input route is verified after every start rather than only
  re-applied.
- **Per-channel state**, as `[input:N]` and `[output:N]`: `gain`,
  `reflevel`, `hi-z`, `mute`, `phase`, `volume`. Every channel range is
  read from a recorded device dump and independently confirmed against
  upstream's device table, so a config naming a channel the interface
  does not have is a parse error rather than a silent no-op.
- **Phantom power is deliberately not settable.** `48v` is in the
  register model and has no value domain, so no config can reach it. It
  stays that way until a hardware case proves the channel it names is
  the channel it powers.
- **Profiles.** A profile is a whole `routing.conf` in `profiles/`
  beside the main one -- not a new section type, so it is parsed by the
  same code and `--dump-config > profiles/tracking.conf` composes.
  `--profile NAME` switches, `--list-profiles` lists. A switch states one
  of three outcomes and never half-applies; a config that does not parse
  is refused before the first datagram. Measured: 0 datagrams for a
  refusal, 5 for a good profile, through the same counter seconds apart.
- **`--dump-config`** reads the device and writes a `routing.conf` from
  it -- 124 channel settings on a UCX II, plus any input routes. It
  refuses when the mixer GUI holds the read-back port, because half an
  answer rendered as a config reads as authoritative.

### Pin and remember

Which settings the config keeps insisting on, and which the mixer wins,
is now a column in the register model, overridable per option by a
`[pin]` section.

The design was forced by a measurement. Of every register a config can
set, exactly **one** is pushed to listeners when it changes:
`/output/{ch}/stereo`. `volume`, `mute`, `hi-z`, `gain`, `reflevel` and
`/playback/{ch}/stereo` all change silently. So "pin" cannot mean "snaps
back when you touch the mixer" at any sensible price, and this release
does not pretend otherwise: it means the config wins while the session
is still looking.

What it replaced was an accident. A fader turned 0.5 s after a restart
came back at the config's value; the same turn at 1.5, 3 and 6 seconds
survived -- and the 0.5 s case was overwritten by the ordinary start-up
apply, not by the verifier. The line between "the config wins" and "the
user wins" was how long the apply happened to take.

`--dump-config` uses the same column: pinned values are emitted as
config, remembered ones as comments carrying the value, because a dump
cannot tell "I meant this" from "this is where I left it".

### Reconcile on events, never on a clock

`systemctl --user reload oscmix.service` re-reads the config, reads the
device back, re-applies what is pinned and leaves what is remembered.
A system-sleep hook asks for the same thing after resume.

Two of the three planned triggers were not built, and both times the
measurement is the reason:

- **Hotplug was already covered.** udev pulls the unit in on `add` and
  `StopWhenUnneeded` drops it on `remove`, so a replug is a full restart
  with a full apply. Both halves are now asserted by test instead.
- **A sample rate change destroys nothing on this device.** Across
  48 kHz -> 44.1 kHz, 1931 of 1932 reported registers were identical;
  the one that differed was `/clock/samplerate`. The playback matrix
  survived too -- shown by signal, since it is never reported. The
  trigger would have been the cheapest of the three, and there is
  nothing measured for it to repair.

### Defects found in the path every boot already ran

None of these were caught by a failing gate. All three were found by
writing a contract as tests before the code existed, or by reading
mutation survivors instead of accepting the score.

- **Channel state was written to the device and then left out of the
  read-back.** Runs logged "routing verified" without having looked at a
  single `[input:N]` or `[output:N]` register. A structural test now
  fails on any function that rebuilds a `Config` from a subset of its
  fields -- the shape both this and its write-path twin had.
- **`/playback/*` was classified as never-reported.** The recorded dump
  carries 42 registers there, and a cold plug returns all 20
  `/playback/<n>/stereo` at t=0.00 s. A lost input-side link write was
  therefore never counted as a problem and never re-sent, on the one
  register family the two-phase apply exists for.
- **The read-back window closed before channel state could arrive.** The
  stereo flags always come first and always match, so `/output/1/volume`
  came back unconfirmed while sitting correct on the device.
- **`DUMP_LISTEN_SETTLE`.** Upstream writes to a *connected* UDP socket
  and ignores `ECONNREFUSED`, so while nothing is bound the meter stream
  queues an ICMP error and the next write dies of it -- silently. Bind
  and ask for a dump in the same breath and the casualty is the one
  bundle `setrefresh()` flushes by hand: every `/playback/<n>/stereo`.
  Measured 4/12 deliveries at no delay, 12/12 at 0.1 s.

### Quality

- 665 tests from 472 functions (392 from 281 in 0.2.0); coverage 95.13%,
  and the gate raised 94 -> 95 to close the slack rather than carry it.
- Mutation score 0.688, floor 0.67, `not_covered` unchanged at 82 while
  the mutant count grew from 2501 to 4180. Reading the survivors in the
  new code found three more real defects, including one where the
  register model was never consulted, so pinning worked only through an
  explicit `[pin]` override.
- ADR 0011-0013 record the profile-switch contract, the pin/remember
  model and the trigger set, each with its measurements and the
  alternatives that were rejected.
- Every CI job now carries a timeout. One had none, hung in
  `apt-get update` and burned GitHub's 360-minute default -- on a job
  whose measured maximum is 6.7 minutes.

### Compatibility

`routing.conf` files from 0.2.x are read unchanged. The new settings are
all new *sections*, which older versions warn about and skip (ADR 0006);
an older install therefore ignores `[pin]` and channel sections rather
than refusing the file. A profile inherits `[osc]` and `[device]` from
the main config unless it states them itself.

## 0.2.0 (2026-08-17)

A maturity release: no new device features. Everything here makes the
existing behaviour provably correct and cheap to change, because 0.3.0
multiplies the register surface by roughly ten.

### Architecture

- The 1386-line `bin/oscmix-session` is now a package in
  `src/oscmix_desk/` (15 modules, 2119 lines) with both executables
  reduced to shims of 52 and 42 lines; `run_session` went from 106 lines
  to ~40, and the longest function left is 66.
- `tests/test_architecture.py` enforces the properties that motivated the
  split rather than asserting them in a comment: stdlib-only runtime,
  declared per-module layering, an acyclic import graph, `__all__`
  matching what is exported, no function over 70 lines, a docstring per
  module, and every public name named by at least one test. It caught
  `run_session` immediately.

### Contracts

- `tests/test_contracts.py`: 14 property-based tests (hypothesis) --
  codec round-trip and alignment, hostile and corrupted datagrams,
  config parsing totality, `a route writes only what it declares`,
  `route == link + mix`, links before mix, and `level` meaning the same
  gain linked or unlinked.
- `tests/test_lifecycle.py`: the exit-code model and the readiness
  protocol as assertions -- 0 for device-absent and clean stops, 1 for
  runtime failures, 2 for config errors, and `READY=1` on every exit that
  returns 0. Previously these lived in a docstring.

### Testability and proof

- Subprocess coverage (`COVERAGE_PROCESS_START` plus a `sitecustomize`
  hook): measured coverage went from 65% to 86% without a single new
  test, because the integration tests always drove the entry point,
  session and CLI -- the measurement just never followed them. Ratchet
  raised 60 → 84.
- `scripts/verify-hardware.py` and `make verify-hardware`: play a tone,
  read `/output/<n>/level` back, assert the audible result, emit an
  evidence artifact including the upstream revision measured. Exits 77
  when there is no device, so CI stays hardware-free.
- Mutation testing now runs at all (the package extraction unblocked it):
  2501 mutants, 1551 killed, 861 survived, 81 not covered -- score
  **0.643**, floor 0.63. `quality/mutation-baseline.json` plus
  `scripts/mutation-policy.py` gate on the **ratio**, not on counts,
  because absolute survivor numbers rise with every line added.
- That score is lower than the 0.728 recorded mid-release, and the suite
  got better, not worse: `not_covered` fell from 677 to 81 as
  `tests/test_lifecycle.py`, `tests/test_process.py` and
  `tests/test_launcher.py` brought four modules in process, and every
  mutant that stops being uncovered starts being judged. 0.728 described
  the third of the runtime that was under evaluation at the time.
- It found a real weakness: `_register_matches` was tested with an
  expected value of 0.0, where a sign error is invisible.
- The coverage ratchet is 94 against a measured 94%. It had sat at 84
  while the suite earned 91 -- seven points of erosion nothing would have
  noticed. `bin/oscmix-launch` moved into the package, taking it from the
  least covered file in the repository (61%) to 100% and inside the
  architecture test, the mutation scope and the coverage.
- A skipped contract suite no longer looks like a green run. Without
  hypothesis the terminal summary prints what did not run and why, and
  `OSCMIX_REQUIRE_CONTRACTS=1` makes the skip a collection error. CI sets
  it.
- `scripts/record-dump.py` and `tests/data/refresh-dump.json`: a real
  `/refresh` from the pinned revision, recorded as register shape and
  arrival times rather than values, with the continuously streaming level
  meters separated out. `register_promptly_reported` is now tested
  against a measurement instead of a memory.
- `systemd-analyze verify` on the unit in CI, via
  `scripts/verify-unit.sh` -- the tool reports an unknown directive name
  on stderr and still exits 0, so the script reads the output. And
  `install.sh` is now run end to end: three tests install into a
  throwaway `HOME` and run what came out of it.

### Stability

- `tests/test_faults.py`: dropped, duplicated and reordered datagrams; a
  device that never answers; a dead backend port; a flood of unrelated
  registers. Every failure this project has shipped was a timing or
  delivery bug, so that is what the tests attack.
- `tests/test_performance.py` asserts **growth order**, not wall-clock. A
  millisecond budget on a shared runner would mostly measure the runner
  and add a flake source to a project whose bugs are already timing bugs.
- Three fault cases that tear **state** rather than transport: the
  backend killed between the link phase and the mix write, the device
  vanishing while `/refresh` is still streaming, and the receive port
  taken halfway through the dump rather than before it.
- A restart soak that runs. `tests/test_soak.py` drives the real entry
  point through start → `READY=1` → verify → SIGTERM → exit 0 and
  asserts the routing datagrams byte for byte on every cycle;
  `.github/workflows/soak.yml` runs 200 nightly. "Proven by: soak on
  main" had been in the roadmap since the first draft with nothing
  running one.
- The background verifier has a stated contract: it checks for a stop
  between every phase and before each of its three writes, every wait
  wakes early rather than running out, and the session waits for it
  before exiting. It previously read the stop flag once, before starting,
  and then ran for up to two verification windows plus a 20 s blind
  delay.
- The timing budget composes. `constants.startup_budget()` sums the waits
  on the path to `READY=1` (42.0 s against `TimeoutStartSec=75`) and the
  unit is parsed and asserted against it, including `ExecStart`'s own
  `--timeout`.

### Fixed

- `--dry-run` printed an order the apply never uses. It walked route by
  route and printed link, mix, link, mix; `apply_routing` sends every
  link of every route, waits for the barrier, then every mix. With one
  route the two agree by accident -- and the one-route example config was
  exactly what CI grepped to guard the defect that silenced every even
  output, so the cheapest end-to-end check in the pipeline was inspecting
  an artifact nothing sends. One function (`routing_plan`) now produces
  the order and both consume it.

### Security and supply chain

- `install.sh` builds a **pinned upstream commit** instead of `master`,
  verifies the checkout landed on exactly it, and records the revision in
  the hardware evidence. Tracking upstream is now an explicit
  `OSCMIX_REF=master`. The component that talks to the hardware being
  unpinned made "verified" hollow, and it is the only path here that
  compiles code from the network.
- Stale-backend cleanup signals through `os.pidfd_open`, so a PID
  recycled between the `/proc` scan and the signal cannot be hit.
- The systemd unit is sandboxed as far as an unprivileged *user* unit
  can be. The hardening that looks obvious but breaks it with
  `218/CAPABILITIES` is listed in `tests/test_unit_file.py` with the
  reason, having been discovered by the service refusing to start.
- `docs/SECURITY-MODEL.md` states what nobody had written down: UDP 7222
  is unauthenticated and any local process can write any mixer register.
  From 0.3.0 that includes phantom power.
- ... and that "the session writes nothing" is a property of today's
  feature set rather than a principle, with the four questions the first
  writable path has to answer, so the sandbox cannot widen as an
  implementation detail. The empty `ReadWritePaths` is now asserted.
- `install.sh` gets its shallow clone back: `git clone --depth 1
  --branch` takes a branch or a tag but not a commit, which is what the
  pin is, so it uses `git init` + `git fetch --depth 1 origin <sha>`,
  falling back to a full clone if the server refuses a bare SHA.

### Code quality and maintainability

- `mypy --strict` over the package, clean; the twelve errors it reported
  were bare `dict`/`set`/`Popen` generics, now real types.
- Expanded ruff selection (security, logging format, import hygiene,
  exception handling and correctness rules), with every exclusion
  carrying its reason. One rule was overruled on purpose: the launcher
  must not print a traceback to a desktop user.
- `docs/decisions/`: nine ADRs for the choices that each cost a
  measurement session to reach. The four added here are the ones a later
  release cannot cheaply revisit: what `routing.conf` promises across
  versions (unknown section warns, unknown option fails, no schema
  field), why performance gates measure growth order, why upstream is
  pinned and when the pin may move, and the verifier's stop contract.
- `docs/RELEASE-CHECKLIST.md`: what must exist before a tag, including
  the rule that a routing change is not done until its measurement is in
  the release, and what is deliberately *not* on the list.
- Compatibility: an unknown **section** in `routing.conf` is now a
  warning and the rest of the file is applied. An unknown **option in a
  known section** is still an error -- that is what a typo looks like,
  and a silently ignored `levl = -20` is a wrong device state nobody is
  told about.
- `docs/ROADMAP.md` records where this goes next and, explicitly, that
  four of six known constraints are upstream limits -- with the patches
  and issues to raise there treated as work items rather than weather.
- The roadmap also states the goal it had only implied. "Better than
  TotalMix FX" is a claim about the **stack** -- oscmix, oscmix-gtk and
  this project -- so the bar is written down once for all three, as a
  matrix of TotalMix capabilities against what the recorded dump proves
  is reachable. The non-goal that used to read as a refusal of that goal
  now says what it meant: a division of labour, not a ceiling.
- Four decisions are drafted rather than deferred, because each costs a
  paragraph now and a migration after 0.3.0: who wins when the GUI and
  the config both write; what a sample rate change does to state that is
  supposed to survive; whether one device per host is a stated limit or
  a design to undo; and the upgrade path, which is untested in the one
  release that moves every path.
- The 15-20 s dump figure is annotated everywhere it appears rather than
  quietly corrected -- including in the item whose reasoning rested on
  it -- because the measurement that contradicts it was taken under one
  condition and the constant it sized exists for another.

## 0.1.3 (2026-08-16)

### Fixed

- **`stereo = false` routes silenced half the pair.** The option only
  emitted the hard-panned mix messages and never sent
  `/output/<n>/stereo 0`, so it assumed the pair was already unlinked.
  Against a linked pair -- the device default -- both messages address
  the same pair register, the second overwrites the first, and one output
  goes dead. Measured on a UCX II: output 7 fully silent while output 8
  played. The unlink is now stated rather than assumed, and the link
  barrier matches on the expected value instead of only on `1`.
- **`level` meant something different on unlinked routes.** oscmix halves
  the gain on that path (`setlevel()`: `ll = vol / 2`), so `level = 0.0`
  landed 6 dB below the linked equivalent -- measured as exactly 6.1 dB
  before, and identical to the linked routes after. Positive levels
  saturate at unity, because oscmix clamps the gain it derives.
- Routes that disagree on whether an output pair is stereo-linked are now
  a configuration error. The link belongs to the hardware pair, not to a
  route; previously the last link message won while both routes still
  wrote their own mix shape, and the mismatched one silently lost an
  output.
- `find_stale_backends()` skipped its ownership check when `stat()`
  failed and then still matched on argv0, so a process whose owner could
  not be verified could reach the kill list.
- A test stub installed its signal handler only after announcing its
  port, which the tests treat as "up"; a SIGTERM landing in between
  killed it with the default disposition and the SIGTERM->SIGKILL
  escalation went unexercised.

### Documentation

- The README and the example config state that a route rewrites exactly
  the registers it declares. `volume` is opt-in and pins the output
  fader on every start; it is gone from the monitors example so the
  footgun is not the default thing to copy.

## 0.1.2 (2026-08-16)

### Fixed

- **Every even output stayed silent** (right headphone, right monitor).
  oscmix only updates its own stereo-link state when the *device* reports
  `/output/<n>/stereo` back; the OSC setter just forwards the register.
  The routing sent the link and the mix matrix in one burst, so `/mix`
  was evaluated against the startup link state, took the unlinked branch
  in `setlevel()` and never wrote the pair's right channel. Outputs 1, 5
  and 7 received a mono sum, outputs 2, 6 and 8 digital silence.
  Measured on a UCX II by reading `/output/<n>/level` back off the wire.

  The routing now goes out in two phases -- links first, mix matrix
  second -- and the mix is written again once the device dump has
  reported the real link state. The dump is what teaches oscmix that
  state, so verification and the re-apply now share a single `/refresh`;
  two overlapping dumps confirmed measurably fewer registers.
- `oscmix-launch` no longer dies with a traceback when `os.execv` fails
  (stale binary, bad interpreter). It reports the error and notifies,
  like the other startup failures.
- `--pipewire-sinks` skips sinks that have no `node.name` instead of
  emitting a `None` target.

### Added

- Quality gates in CI, all wired into `make check`: ruff, mypy, vulture,
  coverage with a ratchet, and a flakiness gate that repeats the suite.
  The Python matrix now spans 3.9-3.13, and action versions are pinned
  to commit SHAs.
- `tests/test_apply_routing.py`: device stand-ins that model oscmix's
  stereo-link state machine. Three of its tests fail against the previous
  single-burst routing.
- The udev rule keeps ASMedia ASM4242 host controllers (`1b21:2426`)
  out of runtime suspend, which could otherwise leave their ports unable
  to enumerate a powered Fireface after a replug.

### Documentation

- ARCHITECTURE and TROUBLESHOOTING describe the two-phase routing;
  the removed `OSCMIX_VERIFY_DELAY` and the never-shipped
  `OSCMIX_LINK_SYNC_TIMEOUT` are gone from the docs, so no override
  documents a setting that does nothing.
- OSC-PROTOCOL carries the ordering constraint itself, since it is a
  property of oscmix's interface rather than of this project: the device
  has to report `/output/<n>/stereo` back before a `/mix` write is
  evaluated, sending the messages back to back is not enough, and the
  failure is silent because `/mix` can never be read back.

## 0.1.1 (2026-07-11)

- Verification now classifies every expected register dynamically as
  confirmed, mismatched, or unobserved. Warnings and automatic re-sends
  happen only for real problems (a mismatched value, or a register the
  dump reliably reports that went missing); registers the device is
  known not to report in time are logged as information. Registers
  outside the known-reported set are still compared whenever they do
  appear, so a changed upstream dump format is handled without code
  changes. A later matching report now overrides an earlier stale
  mismatch.
- README: screenshot of the mixer on a UCX II.

## 0.1.0 (2026-07-11)

First release.

- Hotplug autostart: udev rule (add + remove via `ENV{PRODUCT}`) with USB
  autosuspend disabled, systemd user service with `Type=notify` readiness
  ("started" means the backend runs and the routing is applied)
- `oscmix-session`: ALSA sequencer discovery via `/proc/asound/seq/clients`,
  process supervision with SIGTERM→SIGKILL escalation, and a clean exit-code
  model (device absent = 0, runtime failure = 1 with restart, config error =
  2 without restart)
- Declarative hardware mixer routing in `~/.config/oscmix/routing.conf`,
  applied on every backend start and verified by reading the device state
  back over OSC (one automatic re-send on mismatch)
- `--pipewire-sinks`: generates named virtual sinks ("Monitors",
  "Headphones") for the desktop sound settings from the same routing config
- Desktop entry, launcher with device/backend checks and notifications,
  application icon
- `install.sh` builds oscmix from upstream and installs everything
  per-user; root is only used for the udev rule; `uninstall.sh` reverts it
- Test suite (pytest, no hardware required) covering OSC encoding/decoding,
  config parsing, discovery, verification, the installer, and the full
  session lifecycle against a stub backend
