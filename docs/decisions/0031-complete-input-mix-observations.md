# 0031 — Compound input-mix reports need complete device dependencies

**Status:** implemented for 0.8.0; final release qualification pending.
Complements [0030](0030-backend-owned-control.md).

Upstream `newmix()` receives either the level or pan of one physical input
crosspoint. It reconstructs the other component through `calclevel()` from
`out->mix`. Commands also change that array. A MIDI-handler origin therefore
does not by itself establish that the reported pair came from the device.
After sending −6 dB, receiving only pan could previously emit −6 dB as though
the device had reported both. Linked input pairs also depend on their partner.

The versioned backend patch records which level/pan components have actually
arrived, and whether the relevant input/output link state has been observed.
Every command or refresh invalidates this completeness record. An observed
link change invalidates the matrix completeness record because it changes the
representation's dependencies. A compound report is emitted only when both
components and every required partner are observed. A later observation still
replaces the prior value; completeness is not an immutable confirmation.

Incomplete compound reports are suppressed. They are neither fabricated from
desired values nor emitted under a device-origin label. Suppression leaves
the enclosing OSC delivery intact, so it cannot split a link contradiction
across an artificial delivery boundary. Existing scalar/link reports remain
available, and a partial dump remains partial. The client supplies no fallback
confirmation. No persistent hardware-value cache or second decoder is added.

The actual C backend regressions reproduce both defects before the patch:
sent level plus device pan, and one observed member of a linked input pair.
They require an independent device level before accepting the compound result.
Two UCX-II refresh windows on USB 3.01/DSP 36 retain 100 input-mix paths each;
the saved 2,252 readable values match after restoring the installed service.
The [development observation record](../evidence/0.8.0/backend-observations.json)
identifies the exact build and limits of these measurements.

This corrects report provenance. It does not add a targeted register request
or playback-matrix read-back. Upstream's input-mix handler still computes and
writes physical coefficients while handling reports; a refresh is therefore
not a promise of zero MIDI writes. Its routing behavior requires the final
hardware qualification. Read-only status never asks for a refresh.
