# 0007 -- Performance gates measure growth order, not wall-clock time

**Status:** accepted (0.2.0)

## Decision

`tests/test_performance.py` asserts how the runtime *scales* with the
input, plus an absurd absolute bound that only a hang can cross. It does
not assert milliseconds, and there is no benchmark job in CI.

## Why

The original roadmap entry planned a wall-clock budget. It measured the
wrong thing twice over.

**The number would be dominated by things a benchmark cannot see.**
Time-to-`READY=1` on real hardware is the device wait, the 1.5 s link
barrier and the 15-20 s `/refresh` dump. The Python is noise beside
that. A benchmark against a stub removes precisely the three components
that make up the duration and measures what is left.

**A wall-clock gate on a shared runner mostly measures the runner.**
GitHub's hosted runners vary by more than the margin any useful budget
would have. The failures that would produce are indistinguishable from
real regressions, and this project's actual defects have all been timing
bugs -- a new flake source here is worse than no gate.

**Growth order survives a busy machine.** Doubling the input must not
quadruple the time. That rejects an accidental quadratic in the dump
parser, which is the regression worth catching: the dump is thousands of
registers and the parser walks it once per verification.

## What this rules out

Any assertion of the form "this must complete in N milliseconds", and a
benchmarking dependency. If a number is ever wanted, it is a
*measurement* on real hardware recorded in the release evidence
artifact, not a gate in CI.

## What replaces the question this did not answer

The performance question with actual value is not benchmarkable here at
all: whether the handful of `/output/<n>/stereo` registers can be
queried directly instead of waiting out a full `/refresh`. That is a
feature request to upstream, tracked in the roadmap, not a number.

## Enforced by

`tests/test_performance.py`, and the absence of a benchmark job in
`.github/workflows/ci.yml`.

## Amended in 0.6.10

The "15-20 s" dump was an unrecorded observation; the recorded one is
1.9 s for the 2252 registers a dump reports on a warm device (2322 with
the 70 streamed meters, `tests/data/refresh-dump.json` at the pin), and
the cold replug timeline is in `tests/data/cold-plug-timeline.json`
(ADR 0010, OSC-PROTOCOL.md).
