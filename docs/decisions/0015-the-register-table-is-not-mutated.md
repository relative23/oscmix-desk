# 0015 -- The register table is exempt from mutation, and checked against recordings instead

**Status:** accepted (0.4.0)

## Decision

The region of `registers.py` from `_EQ_BANDS` to `DEVICES` is wrapped in
`# pragma: no mutate start` / `end`. That covers the two table-expanding
helpers and both device literals. Everything below it -- every function
that *queries* the table -- stays under mutation.

The score is therefore a statement about the code that decides
behaviour, not about the data it decides over.

## Why

mutmut picks the tests to run against a mutant from runtime coverage.
The register table is built at module import, inside the
`UCX2 = Device(...)` literal, and import-time code is attributable to no
test in particular -- so mutmut runs a subset that does not contain the
tests which would kill it, and reports a survivor.

This is not a hypothesis. Three `_eq_registers` mutants were applied by
hand and run against the full suite:

| mutant | result against the whole suite |
|---|---|
| `prefix = None` | import error, every test fails |
| `"XX/eqXX"` in the path | one test fails |
| tag `"I"` instead of `"i"` | one test fails |

All three are killed by tests that already exist. mutmut counted all
three as survivors. `_eq_registers` alone carried 117 of them -- the
largest count of any function in the codebase.

Declaring dynamics added 32 more and moved the score 0.692 -> 0.687
against a floor of 0.680. Three families remain (auto level, low cut,
crossfeed), each the same shape, and the arithmetic runs out before they
do. The floor would have failed on a measurement artefact rather than on
a test getting worse, and the fix at that point would have been to lower
the floor -- a ratchet that runs backwards is not a ratchet.

## Measured

| | before | after |
|---|---|---|
| killed | 3310 | 3319 |
| survived | 1509 | 1340 |
| not_covered | 82 | 82 |
| score | 0.687 | **0.712** |
| survivors in `registers.py` | ~180 | 14 |

The 14 that remain are in `channel_limit`, `nested_families`,
`declared_paths`, `_matches` and `global_families` -- functions that
*query* the table, which is exactly where a survivor should still count.
`_seq` was outside the region on the first pass and showed 7; dropping
its `+ 1` by hand fails
`test_an_option_the_channel_does_not_have_is_refused`, so those were the
same artefact and it moved inside.

`min_score` follows to 0.71, which is the baseline's own rule -- set at
the measured score rounded down, so the policy neither fails nor nags.
Stated plainly: **this rise is a denominator change, not better tests.**
What makes it safe to lock in, where 0.69 was not, is that the erosion
mechanism is gone: auto level, low cut and crossfeed are the same shape
as dynamics, and their rows will land inside the exemption instead of
walking the score down 0.005 at a time.

## What replaces it

Not nothing, and this is the part that makes the exemption defensible
rather than convenient. The table is checked *harder* than mutation
checks it, against `tests/data/refresh-dump.json` -- a recording of what
the device itself reports:

- every declared path must equal the set the device reports for that
  family, exactly, in both directions;
- every declared type tag must equal the tag the device sent, which is
  the check that catches a `,f` written to a `setint` register --
  accepted, dropped, device unchanged;
- bounds are asserted against upstream's node table with the `.scale`
  arithmetic written out, because a scale applied the wrong way makes
  every range ten times too wide;
- and the audible ones are measured: `gain = -10.0` on output 5 moved
  the device's own meter by exactly 10 dB.

A surviving mutant says "no assertion noticed". These say "the device
disagrees", which is the stronger statement.

## What this rules out

Mutation can no longer report a gap in the table, so a *logic* error
that creeps into `_eq_registers` or `_dynamics_registers` -- a loop
bound, a branch -- is caught only by the recording checks. That is
acceptable while those helpers are loops over a literal table whose
every output path is asserted. It stops being acceptable if either
grows a branch whose effect is not visible in the declared paths, and
that is the condition under which this ADR should be revisited rather
than a number adjusted.

## Amended in 0.7.0

The table moved out of `registers.py` into `devices.py` when the large
modules were split; the region, its markers and what it exempts are
unchanged. `registers.py` keeps the shape of a row and every function
that queries a table, and carries no exemption at all -- which the
architecture test now asserts, where it used to compare line numbers
inside one file. `device_for_name`, the one query that needs the list of
devices, lives below the region in `devices.py`.

## Related

[0005](0005-mutation-testing-scope.md) is the same kind of decision one
level up: the tests that cannot kill a mutant are excluded from the run.
This excludes the code that cannot be attributed to the tests that do
kill it. Both correct the measurement rather than the thing measured,
and both are recorded because a score whose scope is unstated is a
number nobody can re-derive.
