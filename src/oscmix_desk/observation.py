"""Latest decoded values within one expectation set and observation window.

This primitive owns no transport, timing or write permission. A caller must
finish a decoded delivery before acting on its classification. A new window
starts without any confirmations, including when retrying the same request.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from .autogain import flag_value
from .numeric import float32, integer, report_value
from .osc import Args, Message
from .reconcile import matches
from .registers import VERIFIABLE, Device, register_at


def boolean_dependency(report: tuple[str, Args] | None) -> int | None:
    """A last typed Boolean report, without accepting numeric coercions.

    Missing or malformed feedback cannot authorize a dependent write. Both
    0 and 1 are valid observations; neither implies a desired value.
    """
    return flag_value(report)


class Observation:
    """A match can be revoked and restored; an absent report is neither."""

    def __init__(self, expected: Mapping[str, tuple[str, Args]],
                 device: Device | None = None,
                 dependency_paths: Sequence[str] = (),
                 noted: Sequence[str] = ()) -> None:
        self.expected = {path: (tags, tuple(args))
                         for path, (tags, args) in expected.items()}
        self.device = device
        self.dependency_paths = frozenset(dependency_paths)
        #: Kept in ``latest`` as reported, neither expected nor waited for.
        self.noted = frozenset(noted)
        self.latest: dict[str, tuple[str, Args]] = {}
        self.confirmed: set[str] = set()
        self.mismatched: set[str] = set()
        self.invalid: set[str] = set()

    def absorb(self, report: Message) -> None:
        path, tags, args = report
        if path in self.expected or path in self.dependency_paths or path in self.noted:
            # Retain malformed last reports too: an older valid value must
            # never survive its contradiction as write permission.
            self.latest[path] = (tags, tuple(args))
        expected = self.expected.get(path)
        if expected is None:
            return
        register = register_at(self.device, path)
        if (register is not None and register.verify != VERIFIABLE
                and register.domain is not None):
            # An echo/cache value cannot turn a declared unobservable path
            # into DEVICE confirmation. A future readable backend needs a
            # measured table change, not accidental acceptance of an echo.
            # Existing structural link hints keep their legacy comparison;
            # they have no settable scalar domain and are not mute feedback.
            return
        valid = tags.startswith(expected[0]) and self._valid(path, expected[0], args)
        if path.endswith("/autoset"):
            valid = valid and flag_value((tags, args)) is not None
        if valid and matches(expected[0], expected[1], args,
                   register=register_at(self.device, path)):
            self.confirmed.add(path)
            self.mismatched.discard(path)
        else:
            self.confirmed.discard(path)
            self.mismatched.add(path)
        if valid:
            self.invalid.discard(path)
        else:
            self.invalid.add(path)

    def _valid(self, path: str, tags: str, args: Args) -> bool:
        """Invalid feedback is not an observed user adjustment."""
        if not tags or len(args) < len(tags):
            return False
        register = register_at(self.device, path)
        try:
            for index, (tag, value) in enumerate(zip(tags, args, strict=False)):
                if register is not None and register.mix_level:
                    if index == 0 and value == float("-inf"):
                        continue
                    if index == 1 and (type(value) is not int or not -100 <= value <= 100):
                        return False
                elif register is not None and register.domain is not None:
                    report_value(value, register)
                if tag == "i":
                    number = integer(value)
                    if path.endswith("/stereo") and number not in (0, 1):
                        return False
                elif tag == "f":
                    float32(value)
                else:
                    return False
        except (TypeError, ValueError, OverflowError):
            return False
        return True

    @property
    def unobserved(self) -> set[str]:
        return self.expected.keys() - self.confirmed - self.mismatched

    @property
    def complete(self) -> bool:
        return len(self.confirmed) == len(self.expected)

    @property
    def dependencies_complete(self) -> bool:
        """Every requested Boolean dependency has a valid last report."""
        return all(boolean_dependency(self.latest.get(path)) is not None
                   for path in self.dependency_paths)
