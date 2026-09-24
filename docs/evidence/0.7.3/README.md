# 0.7.3 qualification

**Locally qualified.** The software candidate is
`384352a`; installation and desktop checks use `6096d15`.
Hardware was measured on `af89b03`; all 36 runtime files
are byte-identical. Each record retains its actual source identity.

## Software

[software-qualification.json](software-qualification.json) records all 27
release gates, file/log hashes and durations: 1,933 passed tests with two
regular skips, Python 3.9–3.14, five full repeats, 200 restart cycles and
fifteen fault-suite repeats. Python 3.9 additionally skips 36 standard-library
introspection cases covered by the later interpreters.

Runtime/entry-point statement-plus-branch coverage is **97.8062%**.
The complete **9,867-mutant** result is **7,803 killed,
2,048 survived, 16 timeouts and 0 uncovered**;
score **0.792102** excludes timeouts. The initial cache was empty.
Tests added during qualification receive fresh stats and explicit function
rejudgments; both phases and their selected functions remain in the record.

The contradictory-report regression exercises both report orders, repeated
toggles, invalid values and listener batches. Startup PIN repair, REMEMBER
preservation and strict profile outcomes use the latest report received in
the open window. This is a bounded observation, not an atomic snapshot.

## Installation and upstream mixer

[installation.json](installation.json) records seven source/manual targets:
Debian 13, Ubuntu 24.04/26.04, Fedora 44, openSUSE Leap 16, Arch and Alpine
3.22. Native core and GTK packages pass on Ubuntu 24.04, Fedora 44,
openSUSE Leap 16 and Arch. All are x86_64 container runs with isolated
configuration and a simulated backend. Repeat builds are byte-identical in
each recorded environment. Actual 0.7.2 transitions, paired upgrades and
rollback, dependency refusal, independent maintenance fences and removal
preserve the desk and activation. The source archive passes 55 extracted
installer tests and both isolated 0.7.2 upgrade/rollback layouts.

[desktop-integration.json](desktop-integration.json) uses the actual packaged
GTK executable. GNOME Shell 46.0 and KWin 5.27.11 run nested Wayland sessions;
Xfce 4.18.3 runs X11. Rendering is software in Xvfb, with private D-Bus and
runtime directories. All four native targets also pass isolated X11 checks.
AT-SPI observes **96000 Hz**, then **88200 Hz after reopening**, proving fresh
backend receipt rather than process liveness alone. Shutdown uses SIGTERM.

GUI-first operation makes desk read-back busy: reconcile sends nothing,
while an explicit profile switch reports applied/unverified. Closing GTK
allows a deliberate reconcile which retains a REMEMBER fader. Desk-first
operation is refused by the launcher; reopening works after port release.

## UCX II and restoration

[write-sweep-ucx2.json](write-sweep-ucx2.json) confirms **1,888 entries** and
skips **14 protected entries**. A report missing on the first even-channel
pass is confirmed on retry. The complete before/after comparison restores
all **2,252 readable messages**, including type tags and every argument.

[hardware-evidence.json](hardware-evidence.json) measures all five declared
routes through their named stereo PipeWire sinks at 48 kHz, using a
**−40 dBFS** stimulus and device meters. The device is UCX II **24216011**,
USB **3.01** / DSP **36**; the backend remains
`f2fdd5ec78338848754aad32cc07f3440de63395`. KRK power was off and headphones
were unplugged. The BETA 58A remained on input 1 with phantom power off.
Two full post-test refreshes match the original state exactly.

[lifecycle.json](lifecycle.json) records the actual 0.7.2 → 0.7.3 per-user
installation, preserved configuration/system integration, startup and
SIGHUP. The physical off test passes **1,933 tests, two skipped**, with
continuous USB-absence checks. Power-on automatically starts the installed
service; two complete refreshes again match all 2,252 original states.
No tone is played after power-on. Installed `--status --json` correctly
distinguishes offline and running states and never claims verification.

Higher-rate measurements remain linked to their original
[0.7.1](../0.7.1/sample-rates.md) and [0.7.2](../0.7.2/) recordings. They are
not relabelled as new 0.7.3 measurements. The new verifier changes no rate,
route syntax, register domain or backend pin.
