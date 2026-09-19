# Changelog, 0.5.0 -- 0.5.2

*Moved out of `CHANGELOG.md` when 0.7.0 began; nothing was rewritten.*

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
