# 0006 -- An unknown section warns, an unknown option fails

**Status:** accepted (0.2.0)

## Decision

`routing.conf` promises this, in both directions:

- An **unknown section** is a warning. It is ignored, named in the log,
  and the rest of the file is applied.
- An **unknown option inside a section this version owns** is a
  `ConfigError`: exit 2, and `RestartPreventExitStatus=2` means systemd
  does not retry.
- The option names this version defines keep their meaning. A later
  version may add sections and options; it may not redefine these.

There is **no schema version field** in the file.

## Why

The file is hand-written today, so refusing everything unknown is
correct and cheap. 0.3.0 ends that: `[input:N]`, `[output:N]`, profiles
and `--dump-config` make the file machine-generated and plural, and it
starts travelling between machines and versions.

A config written by 0.3.0 and read by a 0.2.0 install, under today's
rule, produces **no routing at all, no restart, and one line in the
journal.** The device keeps whatever state the last boot left it in.
That is the worst available outcome for a project whose premise is that
the text file is the reviewable source of truth -- the file is *more*
correct than the parser reading it, and the parser refuses it.

The asymmetry is the whole decision, and it follows from what the two
cases actually are:

| | what it usually is | cost of accepting | cost of refusing |
|---|---|---|---|
| unknown section | a newer version's feature | that feature is absent | no routing at all |
| unknown option in a known section | a typo | a device state that differs from the file, silently | one clear error |

`levl = -20` in a `[route:...]` block is not a future feature. Accepting
it would apply a routing at 0.0 dB while the file says -20, which is
exactly the class of defect this project exists to prevent, and the
class that the `volume` bug (ADR 0003) was.

## What this costs

`[routes:x]`, a typo for `[route:x]`, is now a warning and a silently
dropped route rather than an error. Nothing can tell that apart from a
future section name without a list of every name that will ever exist.
Two things bound it: the warning names the section and says the file may
come from a newer version, and the startup log already states how many
routes were loaded (`configuration: <path> (N route(s))`), so a route
that vanished is visible in the same journal.

## Why no schema version

A version field would let the parser say "this file is newer than me"
precisely rather than by inference. It was rejected because it buys
little and costs on every write:

- Every hand-written config needs the line, or it means "schema 1"
  forever -- so the field is either mandatory (a new failure mode for a
  file people edit by hand) or optional (and then absent exactly when it
  would have helped).
- It only tells the parser what it already learns from the unknown
  section itself. The action does not change: warn, name it, continue.
- It would need its own migration story per bump, before there is a
  single migration to write.

Reconsider when the first *incompatible* change to an existing option
appears -- a renamed key, a changed unit, a default that moves. That is
when the parser needs to know which meaning a file intends, and no amount
of unknown-name inference gets there. This ADR is the record that the
question was asked in 0.2.0 and answered "not yet".

## What this rules out

Silently ignoring anything inside `[device]`, `[osc]` or `[route:...]`.
Adding an option there is a decision with a test, per ADR 0003.

It also rules out treating a section as known-but-unsupported. There is
no third state: a section is either parsed or warned about, so no future
version can quietly downgrade a section this version applies.

## Enforced by

`tests/test_config.py`, a test per direction:

- `test_a_config_from_a_newer_version_still_applies_what_we_understand`
  -- a 0.3.0-shaped file with `[input:1]`, `[output:5]` and
  `[profile:tracking]` on today's parser still routes, and warns once per
  unknown section.
- `test_todays_config_keeps_meaning_what_it_means` -- every option this
  version defines, pinned to its meaning.
- `test_a_typo_in_a_known_section_is_still_an_error`
- `test_the_known_surface_is_stated_rather_than_discovered` -- removing
  an option becomes a visible edit rather than a silent break.

## Amended in 0.6.11

The promise is about what option *names* mean. It says nothing about
tightening the values an option accepts, and 0.6.11 does that once:
`[device] name =`, empty, is a `ConfigError`.

It is recorded here because it is the one case so far where a file that
started a desk now refuses. The name is a substring match, and the empty
string is a substring of every name, so an empty name selected "whatever
card has a MIDI port": the interface, on a machine with no other such
card; `2 interfaces match ''`, with `serial` offered as the remedy, the
day a USB keyboard was plugged in beside it; and in both cases a desk
with no register model, so none of its channels or sections was checked.
That never was a meaning the option had -- it was what the matching did
with no input.

The rule this adds: a value may be refused in a later version when it
selected hardware by accident rather than by what it said, the refusal
names the option and the remedy, and the changelog says in so many words
that a working file now exits 2. A value that *meant* something keeps
meaning it.

## Amended in 0.7.0

Two files that loaded now refuse, each said in the changelog under its
own heading, and neither changes what an option *name* means:

* A profile whose `[osc]` or `[device]` resolve to another machine than
  its `routing.conf` ([ADR 0026](0026-a-profile-is-the-desk-not-the-machine.md)).
  0.6.11 warned for one release first.
* A desk that does not fit the interface `--device` sends it to. The
  parser knows the command line now and validates for the device the
  desk goes to; until then it validated for the device the file names,
  the override arrived afterwards, and outputs 29/30 of an 802 desk
  reached a UCX II with a warning at most. The file alone means what it
  meant; it is the combination with `--device` that is held to the
  hardware it names.

