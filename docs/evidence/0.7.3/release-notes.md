# oscmix-desk 0.7.3

This release fixes stale verifier confirmations and improves daily use of
the CLI and the existing upstream GTK mixer.

- A later contradictory report now revokes an earlier matching observation.
  PIN repair and REMEMBER preservation use that latest received value;
  explicit profile confirmation remains strict.
- Read-back summaries separate matching values, retained REMEMBER values,
  PIN differences, missing reports and backend-unreportable state. Profiles
  are documented as partial configurations.
- `oscmix-session --status [--json]` diagnoses installation, config/profile,
  device/backend identity, service, playback mode, reply port and GTK
  prerequisites without changing hardware or starting a session.
- `--dry-run --profile NAME` shows planned writes and previously declared
  route crosspoints that the target profile leaves undeclared.
- The launcher checks the real backend/device and GTK connection before
  opening the mixer, reuses matching manual sessions and respects explicit
  service activation and package-maintenance fences.
- Optional `oscmix-desk-gtk` companion packages accompany the core on
  Ubuntu 24.04, Fedora 44, openSUSE Leap 16 and Arch, all x86_64. Install or
  upgrade the matching pair together. Source installation remains available.

The configuration format and backend pin are unchanged. See
[upgrade instructions](https://github.com/relative23/oscmix-desk/blob/v0.7.3/docs/UPGRADING.md)
and [runtime status/mixer workflow](https://github.com/relative23/oscmix-desk/blob/v0.7.3/docs/STATUS.md).
The close/read-back/reopen workflow is tested; the backend still has one
reply destination, and upstream GTK writes do not take the desk lock.
ADR 0028 defines requirements for a future shared reply path. The separate
desk GUI remains deferred.

## Qualification

- Code candidate `384352acd984936761206e6b5712d0af173d62e2`; every runtime, test, script and installation
  file is identified in the attached software evidence.
- 1,933 tests pass with two regular skips on Python 3.10–3.14. Python 3.9
  passes 1,897 with the same two skips plus 36 import-introspection checks
  supported by the later interpreters. All five repeat runs, the 200-cycle
  soak and fifteen fault-suite repeats pass.
- Combined statement/branch coverage: **97.8062%** for the runtime
  and entry points. Installation/packaging has separate behavioral checks.
- Mutation: **0.792102**, calculated as 7803 / (7803 +
  2048); **9,867** total, **16** timeouts and
  **0** uncovered. A fresh complete run and named rejudgments
  for subsequently strengthened tests are recorded separately.
- Seven source/manual targets and four native core/GTK targets pass,
  including actual 0.7.2 upgrade/rollback and reproducible repeat builds.
  The extracted source archive passes 55 installer tests.
- Real GTK receives fresh state in isolated GNOME 46/KDE 5.27 Wayland and
  Xfce 4.18 X11 sessions. GUI/desk contention, deliberate recovery and
  reopening pass with a simulated backend and software rendering.
- Hardware measured **2026-09-24** on UCX II **24216011**, USB **3.01** /
  DSP **36**, backend **`f2fdd5ec78338848754aad32cc07f3440de63395`**.
  Hardware candidate `af89b03` has byte-identical runtime files to the final
  code. Fresh sweep: **1,888 confirmed, 14 protected skips**. All five
  declared routes pass a −40 dBFS device-meter stimulus; **2,252 readable
  states** are restored exactly. Startup, SIGHUP, physical off/on and the
  complete test run without the interface pass.
- Gate durations: check 160.79s, coverage 161.51s,
  five repeats 793.69s, soak 263.63s,
  mutation including named rejudgments 12246.29s.
  Per-interpreter and per-repeat durations are attached in the JSON.

Support remains measured UCX II operation. Multi-device identity and writer
cases use simulated interfaces and real CLI processes. The pinned backend
does not support the Fireface 802. Playback-matrix writes remain
unverifiable through register read-back; route tests observe their effect
at device meters.

The release includes the source archive, eight native packages, build
metadata, checksums, GitHub attestations and the qualification evidence.
Follow the [artifact verification recipe](https://github.com/relative23/oscmix-desk/blob/v0.7.3/docs/RELEASE-ARTIFACTS.md)
before installation.
