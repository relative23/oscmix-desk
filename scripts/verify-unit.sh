#!/bin/sh
# Run `systemd-analyze verify` on the unit and fail on anything it says.
#
# Roadmap item J. tests/test_unit_file.py reads the unit as text: it
# catches the directives known to break a *user* unit, and it asserts the
# timing budget against the constants. What it cannot catch is a typo in
# a directive *name* -- systemd ignores unknown keys, so
# `NoNewPrivilegs=yes` silently disables the hardening it looks like it
# enables, and every string-matching test still passes.
#
# systemd-analyze knows the key names. It does not, however, exit
# non-zero for them: an unknown key is reported on stderr and the exit
# status stays 0. So the gate is the output, not the status -- and only
# the lines about *this* unit, because the tool also reports on whatever
# else is installed on the machine running it.
#
# --user matters: the unit is a user unit, and the specifiers (%h) and
# the permitted directives differ from the system manager's.

set -eu

UNIT="${1:-$(dirname "$0")/../systemd/oscmix.service}"

if ! command -v systemd-analyze >/dev/null 2>&1; then
    echo "verify-unit: systemd-analyze not found; skipping" >&2
    exit 77
fi

if [ ! -f "$UNIT" ]; then
    echo "verify-unit: no such unit: $UNIT" >&2
    exit 1
fi

BASENAME="$(basename "$UNIT")"
# LC_ALL=C: systemd translates its diagnostics, and the filter below
# matches on their English text. A German runner reports "Datei oder
# Verzeichnis nicht gefunden" and the exception would stop matching --
# turning an environmental note back into a failing gate.
VERIFY_STATUS=0
OUTPUT="$(LC_ALL=C systemd-analyze verify --user "$UNIT" 2>&1)" || VERIFY_STATUS=$?

# Anything naming our unit is a finding: unknown keys, bad values,
# unresolvable specifiers. Other units on the host are not our problem.
MINE="$(printf '%s\n' "$OUTPUT" | grep -F "$BASENAME" || true)"

# A manager that could not even start reports no unit name. Filtering
# that diagnostic used to turn a failed verification into "is clean".
if [ "$VERIFY_STATUS" -ne 0 ] && [ -z "$MINE" ]; then
    echo "systemd-analyze could not verify $BASENAME (exit $VERIFY_STATUS):" >&2
    printf '%s\n' "$OUTPUT" >&2
    exit 1
fi

# One exception, and only one. systemd-analyze also resolves ExecStart=
# and reports the binary as missing when it is not installed -- which is
# the normal state of a CI checkout, and says nothing about the unit
# being well-formed. It is reported rather than dropped, because a check
# that hides findings is worse than no check.
#
# The property this gives up is covered better elsewhere:
# tests/test_install_sh.py installs into a throwaway HOME and *runs* the
# result, so whether ExecStart points at something that exists and works
# is proven by executing it, not by resolving a path.
NOT_INSTALLED="$(printf '%s\n' "$MINE" \
    | grep -E 'Command .* is not executable' || true)"
FINDINGS="$(printf '%s\n' "$MINE" \
    | grep -vE 'Command .* is not executable' | grep -F "$BASENAME" || true)"

if [ -n "$NOT_INSTALLED" ]; then
    echo "note: not installed on this machine, so ExecStart was not resolved:" >&2
    printf '%s\n' "$NOT_INSTALLED" >&2
fi

if [ -n "$FINDINGS" ]; then
    echo "systemd-analyze rejected $BASENAME:" >&2
    printf '%s\n' "$FINDINGS" >&2
    exit 1
fi

echo "systemd-analyze verify: $BASENAME is clean"
