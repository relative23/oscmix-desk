"""Latest decoded values within one expectation set and observation window.

This primitive owns no transport, timing or write permission. A caller must
finish a decoded delivery before acting on its classification. A new window
starts without any confirmations, including when retrying the same request.
"""

from __future__ import annotations

from typing import Mapping, Optional, Set, Tuple

from .numeric import float32, integer, report_value
from .osc import Args, Message
from .reconcile import matches
from .registers import Device, register_at


class Observation:
    """A match can be revoked and restored; an absent report is neither."""

    def __init__(self, expected: Mapping[str, Tuple[str, Args]],
                 device: Optional[Device] = None) -> None:
        self.expected = dict(expected)
        self.device = device
        self.confirmed: Set[str] = set()
        self.mismatched: Set[str] = set()
        self.invalid: Set[str] = set()

    def absorb(self, report: Message) -> None:
        path, _tags, args = report
        expected = self.expected.get(path)
        if expected is None:
            return
        valid = self._valid(path, expected[0], args)
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
            for index, (tag, value) in enumerate(zip(tags, args)):
                if register is not None and register.mix_level:
                    if index == 0 and value == float("-inf"):
                        continue
                    if index == 1 and not -100 <= integer(value) <= 100:
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
    def unobserved(self) -> Set[str]:
        return self.expected.keys() - self.confirmed - self.mismatched

    @property
    def complete(self) -> bool:
        return len(self.confirmed) == len(self.expected)
