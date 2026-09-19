"""What a switch did, as a value.

Four states:

``APPLIED_VERIFIED``
    Written, and the device reported the values back.

``APPLIED_UNVERIFIED``
    Written, and the read-back could not confirm it -- normally because
    the mixer GUI holds UDP 8222, which is the common desktop case, not
    a fault. Carries the list of what went unconfirmed, because
    "unverified" without the list is not an outcome a person can act on.

``REFUSED``
    Nothing was written. The config did not parse, the profile does not
    exist, the name was not a name, or the backend could not be written
    to at all.

``WRITTEN_IN_PART``
    Some registers went out and then the wire gave out. Carries both
    lists; the marker is left alone, so the desk in effect is still the
    one a reload or a start writes back (ADR 0027).

A value rather than an exception or an exit code: what it means for a
process is the CLI's business, and a caller that switches from code can
read every part of it (ADR 0011).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List


def _short(paths: List[str], limit: int = 6) -> str:
    """A register list a person can read at the end of a sentence."""
    shown = ", ".join(paths[:limit])
    return shown if len(paths) <= limit else "%s and %d more" % (
        shown, len(paths) - limit)

#: Written, and the device reported it back.
APPLIED_VERIFIED = "applied-verified"
#: Written; the read-back could not confirm it. ``unverified`` says what.
APPLIED_UNVERIFIED = "applied-unverified"
#: Nothing was written. ``reason`` says why.
REFUSED = "refused"
#: Some of it was written and then the wire gave out. ``written`` and
#: ``unwritten`` say which; the marker was left alone (ADR 0027).
WRITTEN_IN_PART = "written-in-part"

#: The ``reason`` on an outcome where the read-back was never attempted,
#: because the caller asked for none. Distinct wording from a read-back
#: that ran and came up short: the register list means "unknown" here
#: and "looked for and absent" there, and the state cannot say so.
NOT_CHECKED = "verification not requested"

#: The complete set. A member more is a design change, and
#: ``tests/test_profiles.py`` asserts this is exhaustive so it cannot
#: arrive by accretion. The fourth arrived in 0.7.0 as one: "partly
#: applied" was the state this value was meant to make unrepresentable,
#: and a wire that fails half-way made it real all the same -- as a
#: traceback (ADR 0027).
STATES = (APPLIED_VERIFIED, APPLIED_UNVERIFIED, REFUSED, WRITTEN_IN_PART)


@dataclass(frozen=True)
class Outcome:
    """What a switch did. Every field answerable without a traceback."""

    state: str
    name: str
    reason: str = ""
    #: Everything not confirmed at its expected value.
    unverified: List[str] = field(default_factory=list)
    #: The subset of ``unverified`` this backend never reports at all --
    #: the playback mix matrix, and anything write-only. Kept separate
    #: because "I could not check it" and "it cannot be checked" are
    #: different facts, and on a real routing the second is the normal
    #: case: every switch leaves /mix/<out>/playback/<pb> unconfirmed,
    #: measured, by design (backend.Traits.dumps_playback_matrix).
    unverifiable: List[str] = field(default_factory=list)
    #: Whether the marker now says what the device does. False when the
    #: switch landed but the marker could not be written, or the restore
    #: could not remove it: the desk holds only until the next reload or
    #: start, and the caller must not send that reload itself (ADR 0019).
    persisted: bool = True
    #: False when the marker change is in effect but its directory could
    #: not be synced, so a power cut may bring the previous state back.
    #: ``persisted`` decides the reload; this is said in the line only.
    durable: bool = True
    #: Whether a read-back ran, for an applied outcome (a refusal wrote
    #: nothing, so there was nothing to read). False when the caller
    #: asked for none, or the receive port was held or could not be
    #: bound: ``unverified`` then means "unknown" rather than "looked for
    #: and absent", and ``reason`` says why nobody looked. Not derivable
    #: from ``reason``, which is free text for an unbindable port. Until
    #: 0.6.11 a held port was worded "N register(s) unconfirmed", which
    #: is what a read-back that ran and came up short says.
    read_back: bool = True
    #: For ``WRITTEN_IN_PART``: the registers that had gone out when the
    #: wire gave out, in order, and the ones that had not. Handed to the
    #: kernel is what "gone out" means; a datagram socket knows no more.
    written: List[str] = field(default_factory=list)
    unwritten: List[str] = field(default_factory=list)

    @property
    def applied(self) -> bool:
        """Whether anything reached the device.

        The field a script branches on, derived from the state rather
        than stored beside it: two sources for one fact is how "applied
        but the flag says otherwise" happens.
        """
        return self.state != REFUSED

    def describe(self) -> str:
        """One line, for a person."""
        line = self._describe_state()
        if self.state == WRITTEN_IN_PART:
            return line + ("; the desk in effect has not changed, and a "
                           "reload or start writes it back")
        if not self.persisted:
            return line + ("; not remembered, so the next reload or start "
                           "undoes it")
        if not self.durable:
            return line + "; remembered, but it may not survive a power cut"
        return line

    def _describe_state(self) -> str:
        if self.state == REFUSED:
            return "refused %r, nothing written: %s" % (self.name, self.reason)
        if self.state == WRITTEN_IN_PART:
            return ("wrote %d of %d register(s) of %r and then could not: %s "
                    "-- written: %s; not written: %s"
                    % (len(self.written),
                       len(self.written) + len(self.unwritten), self.name,
                       self.reason, _short(self.written),
                       _short(self.unwritten)))
        if self.state == APPLIED_VERIFIED:
            return "applied %r and verified it at the device" % self.name
        if self.reason == NOT_CHECKED:
            return ("applied %r; not checked, so none of its %d register(s) "
                    "is confirmed" % (self.name, len(self.unverified)))
        if not self.read_back:
            return ("applied %r; not read back (%s), so none of its %d "
                    "register(s) is confirmed"
                    % (self.name, self.reason, len(self.unverified)))
        missed = [p for p in self.unverified if p not in self.unverifiable]
        if not missed:
            return ("applied %r; %d register(s) this backend cannot report: %s"
                    % (self.name, len(self.unverifiable),
                       _short(self.unverifiable)))
        return ("applied %r; %d register(s) unconfirmed: %s%s"
                % (self.name, len(missed), _short(missed),
                   "" if not self.unverifiable
                   else " (plus %d this backend cannot report)"
                        % len(self.unverifiable)))
