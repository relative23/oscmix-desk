"""Latest decoded values within one expectation set and observation window.

This primitive owns no transport, timing or write permission. A caller must
finish a decoded delivery before acting on its classification. A new window
starts without any confirmations, including when retrying the same request.
"""

from __future__ import annotations

from typing import Mapping, Optional, Set, Tuple

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

    def absorb(self, report: Message) -> None:
        path, _tags, args = report
        expected = self.expected.get(path)
        if expected is None:
            return
        if matches(expected[0], expected[1], args,
                   register=register_at(self.device, path)):
            self.confirmed.add(path)
            self.mismatched.discard(path)
        else:
            self.confirmed.discard(path)
            self.mismatched.add(path)

    @property
    def unobserved(self) -> Set[str]:
        return self.expected.keys() - self.confirmed - self.mismatched

    @property
    def complete(self) -> bool:
        return len(self.confirmed) == len(self.expected)
