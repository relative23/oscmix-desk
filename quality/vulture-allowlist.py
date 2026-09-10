"""Names vulture would report at 60 % confidence that are used on purpose.

The dead-code gate ran at `--min-confidence 80` until 0.6.2, and 80 is
the level at which vulture reports only what it is nearly certain of --
unreachable code after a return, an unused import. Everything below it
was invisible, and the audit that followed an outside review found
three functions there with no caller at all (`routes_of`,
`unreachable`, `channel_limit`) and three backend traits the docstring
promised the control flow would read and nothing read.

The gate runs at 60 now, and this file is the list of what it may see
there without failing: names that exist as a contract for the tests, or
as data a reader is meant to consult, rather than as code the runtime
calls. Each line says which. A name added here without a reason next to
it is the old blind spot coming back.

vulture reads this file as code: a name that appears in it counts as
used. `_.x` marks an attribute, a bare name a function or constant.
"""

# backend.Traits: two of three fields are documented facts, checked by
# tests/test_backend.py against recordings, and deliberately not read
# by the runtime -- the register table encodes what they state.
_.dumps_playback_matrix          # noqa: F821  -- ADR 0002, REESTABLISHED class
_.reports_unchanged_registers    # noqa: F821  -- why the barrier is opportunistic

# constants.startup_budget: the arithmetic tests/test_unit_file.py holds
# the unit's TimeoutStartSec against. Runtime code has no use for the
# sum; the gate does.
startup_budget                   # noqa: F821

# profiles.STATES and Outcome.applied: the public contract of a switch.
# STATES is what tests/test_profiles.py asserts is exhaustive; applied is
# the field a script branches on (README, *Profiles*).
STATES                           # noqa: F821
_.applied                        # noqa: F821

# registers.VERIFY_CLASSES and Device.supported: data the tests read.
# VERIFY_CLASSES is the closed set tests/test_registers.py checks every
# row against; supported is the "may work" bar stated in the data
# rather than in a README, and the same test holds it to its evidence.
VERIFY_CLASSES                   # noqa: F821
_.supported                      # noqa: F821
