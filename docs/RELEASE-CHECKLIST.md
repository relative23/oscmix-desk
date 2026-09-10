# Release checklist

Every *proven by* clause in the roadmap names an artifact. Until 0.2.0
none of them had a defined place in a release, so `make verify-hardware`
existed, its verdict arithmetic was under test, and no measured artifact
had ever been attached to anything.

This is the list that closes that. Nothing here is a new gate; it is the
existing gates, plus the rule that their output has to be *in* the
release rather than merely producible.

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
- [ ] The roadmap's *Still open* section contains nothing this release
      claims to have done. A release that ships an open item as done is
      the failure mode 0.2.0 spent an entire section fixing.

## 2. The automated gates, on this exact revision

- [ ] `make check` -- lint, `mypy --strict`, vulture, the suite.
- [ ] `make coverage` -- must pass the ratchet in `pyproject.toml`
      **and** be re-read: if the measured number is more than a point
      above the gate, raise the gate now rather than next release.
      Seven points of unnoticed erosion is how item C happened.
- [ ] `make flake` -- five repeats. Timing bugs are this project's
      characteristic defect.
- [ ] `make mutation` -- and `quality/mutation-baseline.json` updated
      with the measured counts, whether the score moved or not. A
      baseline that predates the suite is not a baseline (item K).
- [ ] The scheduled soak has run green on this revision, or run it by
      hand: `make soak SOAK_CYCLES=200`.
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
- [ ] The artifact's `sink_channels` reads `["FL", "FR"]`. If it does
      not, the measurement is not one.
- [ ] The artifact's `firmware` names the USB revision and the DSP
      version (`/hardware/dspvers`). Compare both with the previous
      release's artifact: if either moved, the device is not the one the
      earlier measurements describe, and every "the device does X" this
      release carries forward has to be re-measured rather than kept.
      The sweep artifact and the recorded dump carry the same field.
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
      docs/evidence/write-sweep-ucx2.json`, exit 0, and the committed
      artifact is from that run. The sweep is what proved `verifiable`
      means what it says (0.5.0), and an artifact older than the model
      it vouches for is not evidence.
      `test_the_artifact_covers_every_settable_register` catches a
      register *added* without a fresh run; a changed bound or domain it
      cannot see, which is why this is a checklist item and not only a
      test.

## 5. Install, from nothing

- [ ] `./install.sh` into a scratch `HOME`, then `oscmix-session
      --dry-run` from the installed tree. The 0.2.0 release moved the
      runtime from `lib/` to `src/` and rewrote that path in three
      places; nothing ran the installer end to end at the time.
- [ ] `systemctl --user daemon-reload && systemctl --user start
      oscmix.service` reaches `READY=1`.
- [ ] `./uninstall.sh` leaves nothing behind. Two empty caches remain --
      `mimeinfo.cache` and `icon-theme.cache`, created by
      `update-desktop-database` and `gtk-update-icon-cache` in an
      otherwise empty scratch home. Neither mentions this project;
      removing them would be wrong on a real home, where other
      applications share them.

      **Run this without sudo available.** `uninstall.sh` removes the
      udev rule and the resume hook, and those live in `/etc` and
      `/usr/lib` -- a scratch `HOME` does not move them. A scratch-home
      uninstall with a live sudo ticket therefore deletes the *real*
      system's hotplug rule. It failed safely here only because sudo had
      no terminal. Check `sudo -n true` fails before running it, and
      check the two files are still there afterwards.

      **`systemctl --user` does not follow `HOME` either**, and that was
      the same trap one level down: the user instance is per login
      session, not per `HOME`, so a scratch-home uninstall stopped and
      *disabled* the real user's `oscmix.service`. **Fixed rather than
      documented in 0.4.0**: both scripts compare `$HOME` against the
      session's own, which `systemctl --user show-environment` reports,
      and skip the service entirely when they differ. The unit is still
      installed or removed; only arming and stopping it is withheld,
      with a message saying how to do it from the right session.

      So this step is now safe to run, and its two guards are what to
      check: `sudo -n true` must fail, and the real service must still
      be `active`/`enabled` afterwards.

## 6. The tag

- [ ] Tag `v<version>`, annotated, message = the changelog section.
- [ ] Release notes carry, as text and not as links to a build that can
      expire:
  - the upstream revision built,
  - the mutation score and its counts,
  - the coverage percentage,
  - whether the hardware evidence is attached, and against which device
    serial.

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
