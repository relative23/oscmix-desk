"""What a switch did, as a value.

Five states:

``APPLIED_VERIFIED``
    Written, and the device reported the values back.

``APPLIED_UNVERIFIED``
    Written, but hardware-origin reports were missing for some values,
    verification was not requested, or the coordinated operation failed
    to finish. Carries the unconfirmed registers and whether the marker
    was committed. Shared replies let GTK and desk observe the same
    backend without competing for a receive port.

``APPLIED_MISMATCHED``
    Written, but a completed read-back reported differing or invalid values.
    Carries those paths separately from missing and unavailable feedback;
    a mismatch does not mean nothing was written or permit blind replay.

``REFUSED``
    Nothing was written. The config did not parse, the profile does not
    exist, the name was not a name, or the backend could not be written
    to at all.

``WRITTEN_IN_PART``
    Some registers went out and then the wire gave out. Carries both
    lists; the marker is left alone, so the desk in effect is still the
    previous one: a reload restores its pinned values and mix, a start
    or an explicit selection all of its declared values.

A value rather than an exception or an exit code: what it means for a
process is the CLI's business, and a caller that switches from code can
read every part of it.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Sequence, Tuple


def _short(paths: Sequence[str], limit: int = 6) -> str:
    """A register list a person can read at the end of a sentence."""
    shown = ", ".join(paths[:limit])
    return shown if len(paths) <= limit else "%s and %d more" % (
        shown, len(paths) - limit)

class OutcomeState(str, Enum):
    """The five results of selecting a desk."""

    APPLIED_VERIFIED = "applied-verified"
    APPLIED_UNVERIFIED = "applied-unverified"
    APPLIED_MISMATCHED = "applied-mismatched"
    REFUSED = "refused"
    WRITTEN_IN_PART = "written-in-part"

    def __str__(self) -> str:
        return self.value


APPLIED_VERIFIED = OutcomeState.APPLIED_VERIFIED
APPLIED_UNVERIFIED = OutcomeState.APPLIED_UNVERIFIED
APPLIED_MISMATCHED = OutcomeState.APPLIED_MISMATCHED
REFUSED = OutcomeState.REFUSED
WRITTEN_IN_PART = OutcomeState.WRITTEN_IN_PART

#: The ``reason`` on an outcome where the read-back was never attempted,
#: because the caller asked for none. Distinct wording from a read-back
#: that ran and came up short: the register list means "unknown" here
#: and "looked for and absent" there, and the state cannot say so.
NOT_CHECKED = "verification not requested"

STATES = tuple(OutcomeState)


@dataclass(frozen=True)
class Outcome:
    """A validated, immutable switch result.

    Read-back and persistence default to False; the operation must state
    what it completed. Unknown state types raise TypeError, contradictory
    combinations ValueError. Incoming path sequences are copied to tuples.
    """

    state: OutcomeState
    name: str
    reason: str = ""
    #: Everything not confirmed at its expected value.
    unverified: Tuple[str, ...] = ()
    #: The subset of ``unobserved`` this backend never reports at all --
    #: the playback mix matrix, and anything write-only. Kept separate
    #: because "I could not check it" and "it cannot be checked" are
    #: different facts, and on a real routing the second is the normal
    #: case: every switch leaves /mix/<out>/playback/<pb> unconfirmed,
    #: measured, by design (backend.Traits.dumps_playback_matrix).
    unverifiable: Tuple[str, ...] = ()
    #: Whether the marker records the intended selection. It does not
    #: confirm that every device value matches it. False when the
    #: switch landed but the marker could not be written, or the restore
    #: could not remove it: a later reconcile uses the previous PIN values
    #: and a new session uses its starting values. Do not send a reload
    #: from the failed persistence operation.
    persisted: bool = False
    #: False when the marker change is in effect but its directory could
    #: not be synced, so a power cut may bring the previous state back.
    #: ``persisted`` decides the reload; this is said in the line only.
    durable: bool = False
    #: Whether a completed read-back remains valid for this operation.
    #: False when verification was skipped, reading failed, or the lease
    #: could not finish: ``unverified`` then means unknown rather than
    #: observed to differ or absent from a completed observation window.
    #: ``reason`` explains the failure but does not determine this flag.
    read_back: bool = False
    #: For ``WRITTEN_IN_PART``: the registers that had gone out when the
    #: wire gave out, in order, and the ones that had not. Submission or
    #: a lost acknowledgment does not confirm a hardware value. The
    #: uncertain in-flight write stays in ``written`` to prevent replay.
    written: Tuple[str, ...] = ()
    unwritten: Tuple[str, ...] = ()
    #: The running service refused the hand-over after a persisted or
    #: partial write. Device and marker facts still stand; False also covers
    #: a stopped service or one that runs another config and needs no reload.
    reload_failed: bool = False
    #: Differing or invalid hardware-origin reports from a completed read-back.
    mismatched: Tuple[str, ...] = ()
    #: Expected paths absent from that read-back, including write-only paths.
    #: Empty when no valid read-back completed; unverified then holds unknowns.
    unobserved: Tuple[str, ...] = ()
    #: The subset of mismatched whose reports could not represent a valid value.
    invalid: Tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.state, OutcomeState):
            raise TypeError("state must be an OutcomeState")
        for field_name in ("unverified", "unverifiable", "written", "unwritten",
                           "mismatched", "unobserved", "invalid"):
            paths = getattr(self, field_name)
            if isinstance(paths, str):
                raise TypeError("%s must contain register paths" % field_name)
            paths = tuple(paths)
            if any(not isinstance(p, str) for p in paths):
                raise TypeError("%s must contain register paths" % field_name)
            object.__setattr__(self, field_name, paths)
        self._validate()

    def _validate(self) -> None:
        if any(not isinstance(flag, bool)
               for flag in (self.persisted, self.durable, self.read_back,
                            self.reload_failed)):
            raise TypeError("outcome flags must be bools")
        if self.durable and not self.persisted:
            raise ValueError("a durable marker must be in effect")
        if self.reload_failed and self.state != WRITTEN_IN_PART and not self.persisted:
            raise ValueError("a reload failure requires a persisted or partial operation")
        if not set(self.unverifiable) <= set(self.unverified):
            raise ValueError("unverifiable must be a subset of unverified")
        if self.unverifiable and not self.read_back:
            raise ValueError("an incomplete read-back cannot classify missing registers")
        if (self.mismatched or self.unobserved or self.invalid) and not self.read_back:
            raise ValueError("an incomplete read-back cannot classify register reports")
        if not set(self.invalid) <= set(self.mismatched):
            raise ValueError("invalid must be a subset of mismatched")
        if set(self.mismatched) & set(self.unobserved):
            raise ValueError("mismatched and unobserved must be disjoint")
        if self.reason == NOT_CHECKED and (self.read_back or self.state != APPLIED_UNVERIFIED):
            raise ValueError("verification not requested requires an unread applied outcome")
        if self.state != WRITTEN_IN_PART and (self.written or self.unwritten):
            raise ValueError("written and unwritten belong only to WRITTEN_IN_PART")
        if self.state in (REFUSED, WRITTEN_IN_PART):
            if not self.reason:
                raise ValueError("a refused or partial operation needs a reason")
            if self.read_back or self.persisted:
                raise ValueError("a refused or partial operation cannot be read back "
                                 "or persisted")
            if self.state == REFUSED and self.unverified:
                raise ValueError("a refused operation has no unverified writes")
            if self.state == WRITTEN_IN_PART:
                if not self.written:
                    raise ValueError("a partial operation must have submitted a write")
                if set(self.unverified) != set(self.written + self.unwritten):
                    raise ValueError("every submitted or pending path remains unverified "
                                     "after a partial operation")
        elif self.state == APPLIED_VERIFIED:
            if (not self.read_back or self.unverified or self.mismatched
                    or self.unobserved):
                raise ValueError("a verified operation needs a complete, matching read-back")
        elif self.state == APPLIED_MISMATCHED:
            if not self.read_back or not self.mismatched:
                raise ValueError("a mismatched operation needs a completed, differing read-back")
        elif self.mismatched:
            raise ValueError("known mismatches require APPLIED_MISMATCHED")
        elif self.read_back:
            if not self.unverified:
                raise ValueError("an unverified read-back must name its missing "
                                 "or differing paths")
        elif not self.reason:
            raise ValueError("an unread applied operation needs a reason")
        if self.read_back:
            if set(self.unverified) != set(self.mismatched + self.unobserved):
                raise ValueError("unverified must include exactly mismatched and unobserved")
            if not set(self.unverifiable) <= set(self.unobserved):
                raise ValueError("unverifiable must be a subset of unobserved")

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
        if self.state == REFUSED:
            return line
        if self.state == WRITTEN_IN_PART:
            line += ("; the declared desk in effect has not changed; "
                     "apply it explicitly or restart to restore its starting values")
        elif not self.persisted:
            line += ("; not remembered; a reload restores the previous PIN values, "
                     "and a new session restores the previous starting values")
        elif not self.durable:
            line += "; remembered, but it may not survive a power cut"
        if self.reload_failed:
            line += "; the running service refused the reload and may still use the previous desk"
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
        if self.state == APPLIED_MISMATCHED:
            line = ("applied %r; read-back mismatched for %d register(s): %s"
                    % (self.name, len(self.mismatched), _short(self.mismatched)))
            if self.invalid:
                line += " (invalid reports: %s)" % _short(self.invalid)
            if self.unobserved:
                line += "; %d not observed: %s" % (len(self.unobserved), _short(self.unobserved))
            if self.unverifiable:
                line += " (%d this backend cannot report)" % len(self.unverifiable)
            return line
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
