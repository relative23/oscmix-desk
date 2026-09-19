# After 0.4.0: what actually threatens this

*A chapter of the roadmap as it stood when that work was planned and
done, moved here when 0.7.0 began. The roadmap itself keeps what is
still ahead.*

0.4.0 finished the surface. Every register the device reports is
declared, measured and either settable or explicitly not. There is no
obvious next feature, and that is the point at which a project either
finds out what it is for or accumulates ornament.

So this section is not a wish list. It is the five things that would
make this unmaintainable, unusable or untrue in a few years, in the
order they are likely to bite.

## A. Everything rests on one maintainer's project

oscmix is the reason this works and the reason it can stop working. One
person, no signed releases, and four of our findings are sitting in his
issue tracker. Room EQ writes (#33), output phase (#34) and the 802 (#4)
are all blocked there, and nothing here can unblock them.

**Started, and the shape is right.** `docs/register-addresses.md` holds
the arithmetic, the wire format and eleven addresses confirmed on the
wire rather than copied out of upstream's source. If oscmix stopped
being maintained tomorrow, the OSC paths would be worth nothing and that
table would still be true.

**Answered, and the answer was that the question was the wrong shape.**
2028 addresses are not 2028 facts: they are three rules over a table of
82 control offsets. The rules are the part that can be misunderstood and
all three are confirmed on the wire. The offsets are a table, and the
risk in a table is availability rather than understanding -- extracted
from upstream on demand, they were not independent of upstream, which
was the entire point.

So "enough" meant *store the table*, not *measure more registers*.
`docs/register-offsets.json` holds all 82 with the revision they came
from, and a test requires it to reproduce every measured address from
the file in this repository rather than from upstream's source. One
commit, not a measurement campaign.

*What this does not do:* it does not make this project independent.
oscmix also carries the SysEx transport, the device discovery and the
register-to-control mapping in both directions. A table of addresses is
the notes, not the instrument.

## B. Nothing notices when the desk drifts

The session applies once, verifies for a minute, and then stops looking.
Turn a knob afterwards and the config and the hardware disagree with
nobody the wiser. `--diff` answers the question, but only when asked.

**The premise this was decided on was wrong**, and it took until 0.4.x
to measure it. ADR 0013 says only `/output/{ch}/stereo` is pushed, so
there is nothing to react to. Measured: the device reports the **partner
channel of a linked pair** for volume, mute, crossfeed and every block of
EQ, dynamics, low cut and auto level. A drift signal needs no clock and
no polling.

*The reason it is still not built:* the registers that stay silent are
`reflevel`, `input/gain` and `input/phase` -- exactly the installation
state ADR 0012 says PIN exists for. A signal built on this would
announce the settings a config mostly leaves alone and say nothing about
the ones it pins.

**Decided, once the measurement was finished: do not build it.** The
half that had never been measured turned out to settle it. Fourteen of
the eighteen settable PIN registers have no channel -- clock, control
room, hardware -- and **all fourteen are silent**. Together with
`input/gain` and both `reflevel`s, that is **one of eighteen** pinned
registers that announces itself: `input/hi-z`.

Meanwhile everything that *does* announce itself -- volume, mute,
crossfeed, EQ, dynamics, low cut, auto level -- is REMEMBER by default,
which is to say precisely what a config leaves to the device.

So a signal built on pushes would report what the file does not own and
stay silent for seventeen of eighteen things it does. That is worse than
no signal, because it is believed: somebody who knows it exists and
hears nothing concludes their pinned reference level still stands, and
it may not. Not a close call; at ten of eighteen it would have been.

**What was built instead:** `--diff` exits 3 when the device and the
config disagree, so the question can be asked by a script rather than
guessed at by a listener. 0 still means they match and 1 still means the
read failed, and keeping those apart is the whole value -- a check that
cannot tell drift from a dead backend reports healthy silence at exactly
the wrong moment.

## C. Security is as closed as it can be here

Done in 0.4.x, and the useful part was finding out how little was left.
`systemd-analyze security --user` went from 8.3 to 5.4, three directives
had been listed as impossible for two releases and were not, and ADR
0017 states the trust boundary instead of implying one.

*What remains is not closable here.* The OSC port authenticates nothing:
any local process can set any register, including the phantom power this
project withholds from config files. That guard is in the config parser
and cannot be moved to the port, which is oscmix's. Stated, not fixed,
and a fix would have to happen upstream.

*The one thing worth revisiting:* `PrivateUsers=yes` is refused here as
untested against ALSA device access. Testing it is an afternoon, and it
is the largest single item left on the exposure score.

## D. The documents cannot answer a question quickly

This file is 2000 lines and is a chronicle: it records what was measured
and when, which is exactly right for auditing a claim and exactly wrong
for finding out how something works. Most facts about this project live
here and nowhere else.

**Done.** `docs/ARCHITECTURE.md` is rewritten in the present tense: the
system it sits in, the pipeline every command is a stopping point along,
the module table, the register model as data, pin and remember, the
two-phase apply and the two seams. History stays here, decisions stay in
the ADRs.

The page had not "failed to keep pace". It described the 0.2.0 system
and mentioned none of `reconcile`, `desired`, `plan`, `snapshot` or
`profiles` -- because nothing checked it, so there was nothing to
notice. **Three tests now do**: every runtime module must appear in its
table, every module in the table must exist, and version numbers in the
body fail because a sentence about how we got here is the kind nobody
updates. All three were verified against a deliberately broken page.

*Why it matters more than it sounds:* this project's whole method is
that reasoning is written down. A chronicle nobody can navigate is a
place where reasoning goes to be lost, which is the failure it was
built to avoid.

## E. The mutation score is expensive and says less each release

An hour per run, and ADR 0015 had to exempt the register table before
the number meant anything at all. Since then it has moved by a
thousandth across four register families, which is the exemption
working -- and also the number telling us very little.

**Answered, and the answer was in the history rather than in a
measurement at the device.**

The workflow's own comment said *"re-measure when a run passes 60 -- and
check the slowest tests before touching this number"*. Three runs in a
row had passed it, at 69, 74 and 73 minutes, so the instruction applied.
Checked, and **there is no hot spot**:

| | 0.3.0 | now | |
|---|---|---|---|
| tests | 665 | 945 | +42% |
| suite | 73 s | 106 s | **+45%, proportional** |
| mutants | 4180 | 4866 | +16% |
| mutation | 44 min | 72 min | +64% |

Cost is mutants × suite time, and `4180×73 → 4866×106` predicts +69%
against +64% measured. The growth is entirely explained. Shortening the
link barrier was tried: it saves 1.6 s of 106 and breaks two tests.

*So the cost is not a defect, which is the problem.* It grows
multiplicatively with the project and would be past two hours by the
next release, for a number that **has never failed its gate in any
run**.

*What the value actually was:* reading survivors found three real
defects in 0.3.0, including pinning silently not working while every
test stayed green because they all used a `[pin]` override. The commit
says it plainly: *"the policy was green either way"*. The score did not
catch them; a score **movement** prompted somebody to look.

**Moved to nightly.** A nightly change still prompts that reading, one
day later, and the release checklist keeps its own run where the number
has to be current rather than recent. Per-push feedback drops from 72
minutes to about five.

Three tests hold the arrangement: the mutation job must be conditioned
on `schedule`, the workflow must actually have one -- a job conditioned
on a trigger the workflow lacks never runs again and nothing would say
so -- and no other job may quietly follow it off the push path.

## F. A profile is undone by the next start -- **closed, ADR 0018**

`--profile X` wrote X to the device and exited; the service kept
`routing.conf`, and the next start or reload re-applied it over X. The
resume hook made that a routine event: suspend, wake, profile gone,
nothing in the journal saying why. 0.6.2 documented it; the release
after remembers the profile beside `routing.conf`, applies it on every
start and reload, falls back to `routing.conf` with a standing warning
when it no longer loads, and adds `--no-profile`. What an active
profile means when `routing.conf` changes underneath it is decided in
[ADR 0018](../decisions/0018-the-active-profile-survives-a-start.md):
the machine settings follow `routing.conf`, the desk follows the
profile, and the start line says so.

## G. A second switch can still overlap the unit's reconcile -- **closed, ADR 0019**

Found in the 0.6.4 release's live test and recorded in ADR 0018. A
switch holds the lock while it writes and reads back, then releases it
and reloads the unit; `ExecReload` is a plain `kill -HUP`, so the
reload returns before the unit reconciles. A second switch waiting on
the lock starts at once, and if the unit's reconcile begins in a gap
where that switch does not hold the receive port, the unit re-applies
the first switch's desk while the second writes its own. The second
switch's reload re-applies its desk afterwards, so the end state is
right; the guarantee in between is not.

**The fix is one lock for every writer.** The unit takes the same
`flock` around its start-up apply, its verifier and every reconcile, so
a switch waits for the unit exactly as it waits for another switch, and
the `Status:` polling becomes unnecessary. What that needs: the
installer creates `active-profile.lock`, because the unit cannot create
it under `ProtectHome=read-only` (`flock` itself works on a read-only
descriptor); the verifier's hold of about 22 s has to stay inside
`SWITCH_LOCK_WAIT`; and a switch's reload moves inside its lock.
**Closed in 0.6.5.** The unit holds `active-profile.lock` across its
start-up apply and verifier, and around every reconcile; the installer
creates the file because `ProtectHome=read-only` stops the unit from
creating it, and `flock` holds on the read-only descriptor it can open.
The phase polling is deleted. *Proven by:* tests that hold a reconcile
against a lock taken elsewhere and a start against the same, plus the
live run on the desk -- a switch right after a restart waited for the
unit's transaction, and two switches at once still serialised.

## The limit under all of this

**Every measurement in this repository was taken on one Fireface UCX II,
serial 24216011, on one machine.** Every bound, every timing constant,
every "the device does X" is that device's behaviour. The 802 has never
been tested and its register table cannot be written. There is no second
unit, no second Linux, and no user other than the author.

That does not make the measurements wrong. It makes them narrower than
their wording, and the roadmap should say so once, plainly, rather than
qualify every sentence.
