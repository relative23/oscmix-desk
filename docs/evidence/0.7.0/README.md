# 0.7.0 candidate measurements

**Historical-method limit (identified during 0.7.1 work):** the sweep's
universal tolerance could confirm a changed but incorrect scalar value;
confirmed findings did not retain that report. Snapshot rendering also
rounded floats to one decimal place, so equality could hide smaller
changes. The results below retain their original method and counts; they
do not establish equality under the corrected numeric contract. The
route/meter checks have a separate method and remain separate evidence.
See [numeric contracts](../../NUMERIC-CONTRACT.md).

Collected locally on 2026-09-22 against desk commit
`86fb4e45d6ca342fbd7744b5312239f8a9103994`. The installed service used
that runtime. `measurement.json` records the backend commit, binary hash,
firmware and SHA-256 digests of the recorded artifacts.

- `hardware-evidence.json`: five linked routes, complete and passing.
- `hardware-evidence-unlinked.json`: the same five routes with stereo
  links temporarily disabled, complete and passing; original routing
  reapplied in the measurement wrapper's `finally` block.
- `write-sweep-ucx2.json`: 1888 confirmed, 14 deliberately skipped,
  no registers left unrestored.
- `refresh-dump.json`: 2322 paths, 70 streamed, USB 3.01 / DSP 36.
- `power-cycle.json`: user-confirmed physical power-off/on, successful
  full check while off, automatic hotplug start and verifier completion.
  All 2252 non-streaming values still match the pre-test snapshot.
- `hardware-evidence-powercycle.json`: all five routes pass after that
  cold start, using quiet two-second tones at amplitude 0.001 (about
  -60 dBFS peak). The original harness was imported with only
  `TONE_AMPLITUDE` and `TONE_SECONDS` overridden. Generated 16-bit WAVs
  were checked for a peak no greater than 32/32767 before playback;
  the 12 dB verdict thresholds were unchanged.

Route timestamps are local Europe/Berlin; the sweep timestamp is UTC.
The dump tool's explanatory notes retain the historical measurements
that motivated it; the top-level date, firmware and arrival data identify
this new recording. The source and tests behind the measurements are
unchanged by adding these artifacts.

The tones are checked with device meters, not an external analog capture.
Snapshot equality excludes streamed values and cannot verify the playback
matrix. These artifacts measure one UCX II, serial 24216011. They say
nothing about two physical devices or another model.
