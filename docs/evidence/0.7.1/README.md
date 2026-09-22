# 0.7.1 candidate evidence

**Unreleased. Physical qualification remains open.** The
[software record](software-qualification.json) identifies the tested
candidate files by SHA-256 and records the offline gates. Its base commit
alone does not identify the candidate's uncommitted changes.

The final check passes 1,653 tests with two empty-parameter skips. The
core passes Python 3.9–3.14, five repetitions, a 200-cycle soak and fifteen
fault repetitions. Coverage is 97.58% including branches, displayed as 98%.
The full fresh mutation run covers 8,263 mutants; after additional numeric
boundary tests and a targeted recheck, the score is 0.7983. An independent
C calculation of the pinned scalar conversions agrees in 34,930 cases.
The record distinguishes the successive test runs and their exact scope.

This is software evidence. It does not establish physical gain/delay,
fresh register write verification, higher-rate behavior or restoration of
the attached interface. Historic 0.7.0 sweeps remain annotated with their
method's limits; they are not relabelled as a 0.7.1 pass. The corrected
hardware qualification and the release checklist must finish before
publication. Native-package experiments belong to the following milestone.
