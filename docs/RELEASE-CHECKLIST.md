# Release checklist

Every *proven by* clause in the roadmap names an artifact. Until 0.2.0
none of them had a defined place in a release, so `make verify-hardware`
existed, its verdict arithmetic was under test, and no measured artifact
had ever been attached to anything.

This is the list that closes that. 0.7.0 adds the operational checks from
the [roadmap](ROADMAP.md#release-readiness-for-070): actual upgrade and
rollback, interrupted installation and a verifiable source bundle. Gate
results belong in the release rather than merely being producible.

The ordering matters in one place only: the hardware measurement comes
after the upstream pin is final, because it is evidence about a
particular binary (ADR 0008).

---

## 1. The tree is what it claims to be

- [ ] `git status` is clean and on `main`.
- [ ] `src/oscmix_desk/constants.py::__version__` matches the tag
      to be created.
- [ ] `CHANGELOG.md` has a section for this version with a date, and no
      `## Unreleased` heading above it.
- [ ] The roadmap's *Where we are* paragraph for this version exists and
      claims nothing the release does not do. A release that ships an
      open item as done is the failure mode 0.2.0 spent an entire
      section fixing.

## 2. The automated gates, on this exact revision

- [ ] `make check` -- lint, `mypy --strict`, vulture, the suite.
- [ ] Python 3.9–3.14 all pass with the property-test dependencies
      present. Record environmental skips and investigate failures before
      counting a retry. The install transition tests require the actual
      0.6.11 and 0.7.0 commits; use a full checkout, as CI does.
- [ ] `make coverage` -- must pass the ratchet in `pyproject.toml`
      **and** be re-read: if the measured number is more than a point
      above the gate, raise the gate now rather than next release.
      Seven points of unnoticed erosion is how item C happened.
- [ ] `make flake` -- five repeats. Timing bugs are this project's
      characteristic defect.
- [ ] `make mutation` **after `rm -rf mutants`** -- and
      `quality/mutation-baseline.json` updated with the measured counts,
      whether the score moved or not. A baseline that predates the suite
      is not a baseline (item K). Deleting only `mutmut-stats.json` is
      not clearing the cache: mutmut re-uses the verdicts of every module
      whose source did not change, and a changed test suite changes those
      verdicts (0.6.8 measured a "full" run in 11 minutes that way).
      Functions whose tests were written after the run took its copy are
      re-judged by name with the stats file cleared.
- [ ] `make check` once with the interface **switched off**. A test that
      resolves the interface from the machine's own `/proc` passes only
      while the box is on, and CI has none (0.6.9 caught one at release
      time).
- [ ] The scheduled soak has run green on this revision, or run it by
      hand: `make soak SOAK_CYCLES=200`.
- [ ] Repeat `tests/test_faults.py`, `tests/test_apply_routing.py` and
      `tests/test_verify.py` fifteen times, matching the scheduled fault
      soak. Record durations for the full gates and any retries.
- [ ] Record the device/writer review against the candidate: two simulated
      identical devices, backend replacement during lock wait, concurrent
      CLI writers, refused start and interrupted phase writes. Existing
      tests provide these cases; only add tests for uncovered findings.
      A refusal sends nothing; partial sends, kernel submission and
      hardware confirmation remain distinct. Two real interfaces are
      still outside the measured hardware scope.
- [ ] CI is green on `main` at this commit, including the
      `build-oscmix` job -- that is what proves the pinned SHA still
      resolves and still compiles.

## 3. The upstream pin

- [ ] `OSCMIX_REF` in `install.sh` is a full 40-character SHA, not a
      branch.
- [ ] If the pin moved since the last release: a **fresh** hardware
      measurement was taken against it (ADR 0008). A bump without one is
      not a bump.
- [ ] The pinned SHA and the `oscmix_revision` in the evidence artifact
      below are the same string. Check it, do not assume it -- an
      artifact naming a different binary reads as evidence and is not.

## 4. The hardware evidence

Needs a Fireface attached, a running backend and a quiet bus. This is
the only check that measures audio rather than messages, and it is the
one that found all three defects in 0.1.3.

- [ ] `make verify-hardware` -- exit 0. **Name the sink explicitly**:
      `python3 scripts/verify-hardware.py --sink <stereo-sink> --evidence
      hardware-evidence.json`. The default sink is whatever PipeWire last
      decided, and a USB replug can leave it as the interface's raw
      20-channel `Direct` sink -- a stereo tone has nothing to land on
      there, and the 0.2.0 release run got three convincing FAILs that
      way with nothing wrong at all. The tool now *skips* (77) rather
      than failing in that case, and records the sink in the artifact.
- [ ] The artifact's `sink_channels` maps every used sink to
      `["FL", "FR"]`. Otherwise the measurement is not one.
- [ ] The artifact's `firmware` names the USB revision and the DSP
      version (`/hardware/dspvers`). Compare both with the previous
      release's artifact: if either moved, the device is not the one the
      earlier measurements describe, and every "the device does X" this
      release carries forward has to be re-measured rather than kept.
      New sweep artifacts and recorded dumps must carry the same field.
      The retained 2026-08-27 fixtures predate it; state that provenance
      gap explicitly, and never backfill it from a newer observation.
- [ ] `hardware-evidence.json` is attached to the release.
- [ ] `complete` is **true** and `unmeasured` is empty. A five-route
      config used to produce a three-route artifact -- the tool played
      one tone into one sink and silently skipped every route not fed
      from playback 1/2, which was both *direct* routes. `ok` alone
      cannot catch that: a route nobody measured did not fail.
- [ ] Every entry in `routes` has `ok: true`, and each names the
      `playback` pair and `sink` it was measured through.
- [ ] Its three regression cases from 0.1.3 are among them: even
      outputs not silent, unlinked pair not half-dead, unlinked route
      not 6 dB low.
- [ ] **If any routing behaviour changed in this release**, the
      measurement is from *after* that change. A routing change is not
      done until its measurement is in the release (roadmap item 4).
- [ ] **If the register model changed** -- a row added, a domain or
      bound changed, a capability split -- the write sweep has run
      against the change: `python3 scripts/sweep-writes.py --out
      docs/evidence/<version>/write-sweep-ucx2.json`, exit 0, and the committed
      artifact is from that run. The sweep is what proved `verifiable`
      means what it says (0.5.0), and an artifact older than the model
      it vouches for is not evidence.
      `test_the_artifact_covers_every_settable_register` catches a
      register *added* without a fresh run; a changed bound or domain it
      cannot see, which is why this is a checklist item and not only a
      test.
      Schema 2 must retain requested/encoded/reported values, comparison
      rule, backend binary identity, original/final state and any error.
      An interrupted run or residual drift is not release qualification.
      Keep historical artifacts unchanged and label their method limits.

## 5. Install, from nothing

- [ ] `./install.sh` into a scratch `HOME`, then `oscmix-session
      --dry-run` from the installed tree. The 0.2.0 release moved the
      runtime from `lib/` to `src/` and rewrote that path in three
      places; nothing ran the installer end to end at the time.
- [ ] The real 0.6.11 and 0.7.0 → candidate → original-version tests pass in rootless and
      redirected system-file layouts. Config, profiles and marker survive;
      installed entry points and module inventory match the selected
      version. Failed staging and an interrupted activation recover by
      rerunning the installer, with the service stopped during replacement.
- [ ] [Upgrade instructions](UPGRADING.md) describe the config/API changes,
      full backup and software rollback limits for this version.
- [ ] `systemctl --user daemon-reload && systemctl --user start
      oscmix.service` reaches `READY=1`, and the user running it is in the
      group `audio`: since 0.6.9 the lock directory `/run/oscmix-desk` is
      3770 root:audio, and a user outside the group is refused every
      start and switch (ADR 0024). The installer warns; the checklist
      checks.
- [ ] `./uninstall.sh` leaves nothing behind. Two empty caches remain --
      `mimeinfo.cache` and `icon-theme.cache`, created by
      `update-desktop-database` and `gtk-update-icon-cache` in an
      otherwise empty scratch home. Neither mentions this project;
      removing them would be wrong on a real home, where other
      applications share them.

      **Run this without sudo available.** `uninstall.sh` removes the
      udev rule, the resume hook and the tmpfiles.d entry, and those
      live in `/etc` and `/usr/lib` -- a scratch `HOME` does not move
      them. A scratch-home uninstall with a live sudo ticket would
      delete the *real* system's hotplug rule; it failed safely once
      only because sudo had no terminal. Since 0.6.9 the script leaves
      the three files alone whenever systemd's session serves another
      home, and says so. Check `sudo -n true` fails before running it
      anyway, and check the three files are still there afterwards.

      **`systemctl --user` does not follow `HOME` either**, and that was
      the same trap one level down: the user instance is per login
      session, not per `HOME`, so a scratch-home uninstall stopped and
      *disabled* the real user's `oscmix.service`. **Fixed rather than
      documented in 0.4.0**: both scripts compare `$HOME` against the
      session's own, which `systemctl --user show-environment` reports,
      and skip the service entirely when they differ. The unit is still
      installed or removed; only arming and stopping it is withheld,
      with a message saying how to do it from the right session.

      So this step is now safe to run, and its guards are what to
      check: `sudo -n true` must fail, the three system files must
      still be there, and the real service must still be
      `active`/`enabled` afterwards.

## 6. The tag

- [ ] Tag `v<version>`, annotated, message = the changelog section.
- [ ] Release notes carry, as text and not as links to a build that can
      expire:
  - the upstream revision built,
  - the mutation score and its counts,
  - the coverage percentage,
  - whether the hardware evidence is attached, and against which device
    serial.
  - the date, backend pin and firmware (or explicit historical gap) of
    each hardware artifact; the tested desk commit and gate durations;
  - measured UCX II support, simulated multi-device cases, the unsupported
    802 backend and unverifiable playback writes.

## 7. The installation artifact

- [ ] `scripts/build-release.py` builds twice from the selected commit
      with identical archive and manifest bytes. The archive's extracted
      installer tests pass. This measures source archive reproducibility,
      not C binary reproducibility.
- [ ] The release workflow attaches the source archive,
      `release-manifest.json`, `SHA256SUMS` and `attestation.jsonl` to the
      release; its version agrees with the tag. Hardware evidence is
      attached separately and named in the release notes.
- [ ] Follow [the verification recipe](RELEASE-ARTIFACTS.md) as a user:
      authenticate the checksum manifest for the expected repository,
      workflow and tag, then verify its artifact digests. Tampering with
      a local copy must fail. A branch workflow or local checksum file
      alone does not satisfy the published-tag attestation gate.
- [ ] Installation instructions name the released tag. A build
      attestation for this project does not authenticate upstream's
      unsigned history.

---

## What is deliberately not on this list

**Signature verification of upstream.** It publishes no signed tags. The
pin is what stands in for it; see ADR 0008.

**A wall-clock performance number.** ADR 0007 -- the gates measure
growth order, and a benchmark against a stub would measure the runner.
If a duration is ever quoted in release notes it is a measurement from
real hardware, labelled as such.

**Testing the 802.** It has never been tested and the release notes say
so. A device is supported when its register table is declared, its
channel capabilities are recorded, and one evidence artifact exists for
it. Since 0.4.0 the 802 has **one** of the three: its capability map is
read from upstream's own `device_ff802.c`. The other two are upstream's
to unblock, because oscmix cannot drive an 802 at the pinned revision.
