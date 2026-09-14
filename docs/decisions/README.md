# Decision records

Short notes on the choices that are not obvious from the code, and that
cost a measurement session to arrive at. Each one states what was decided,
what it rules out, and what evidence produced it.

They exist because the reasoning behind the routing behaviour lived only
in commit messages. Anyone changing that code needs to know why it looks
the way it does, or they will "simplify" it straight back into a bug that
silences half the outputs.

| # | Decision |
|---|---|
| [0001](0001-two-phase-routing-apply.md) | Channel links are written before the mix matrix |
| [0002](0002-one-dump-for-verify-and-reapply.md) | Verification and the mix re-apply share one `/refresh` |
| [0003](0003-declared-registers-only.md) | A route rewrites only the registers it declares |
| [0004](0004-package-with-stdlib-only-runtime.md) | The runtime is a package, and imports only the standard library |
| [0005](0005-mutation-testing-scope.md) | Mutation testing runs against the in-process tests only |
| [0006](0006-routing-conf-compatibility.md) | An unknown section warns, an unknown option fails |
| [0007](0007-growth-order-not-wall-clock.md) | Performance gates measure growth order, not wall-clock time |
| [0008](0008-pinned-upstream-revision.md) | The upstream backend is pinned, and the pin moves only after a measurement |
| [0009](0009-verifier-stop-contract.md) | The background verifier stops between phases, and the session waits for it |
| [0010](0010-timing-constants-need-a-recording.md) | A timing constant needs a recording, not a recollection |
| [0011](0011-a-profile-switch-states-its-outcome.md) | A profile switch states its outcome; it never raises |
| [0012](0012-pin-and-remember.md) | Pin and remember are a column in the register table |
| [0013](0013-reconcile-triggers.md) | Reconcile on events, never on a clock |
| [0014](0014-nested-config-sections.md) | Nested settings go in `[<family>:<channel-family>:<n>]` |
| [0015](0015-the-register-table-is-not-mutated.md) | The register table is exempt from mutation, and checked against recordings instead |
| [0016](0016-no-register-is-declared-dangerous.md) | No 0.4.0 register is declared dangerous, and the clock source stays pinned |
| [0017](0017-the-osc-port-is-the-trust-boundary.md) | The OSC port is the trust boundary, and it has none |
| [0018](0018-the-active-profile-survives-a-start.md) | The active profile is remembered beside the config, and a start applies it |
| [0019](0019-one-lock-for-every-writer.md) | One lock for every writer of the device: the unit takes it too |
| [0020](0020-the-desk-is-read-under-the-lock.md) | The desk is read under the lock that writes it |
| [0021](0021-a-start-that-cannot-be-heard-is-not-a-start.md) | A start that cannot be heard is not a start: port readiness and socket-owner cleanup |
| [0022](0022-the-lock-names-the-device.md) | The lock names the device, and a writer without it does not write |
| [0023](0023-one-lock-path-for-every-writer.md) | One lock path for every writer, and no writing to an absent device |
| [0024](0024-one-interface-resolved-once.md) | One interface, resolved once, and a lock only its group can touch |
