"""`[desk] peak-safe = true`: an explicit switch pulls the outputs it changes down first.

TotalMix 2.0 can reduce volume peaks while it loads a snapshot. A desk switch writes links, the
barrier, the matrix and channel state one register at a time, and the interim states between
them can be louder than either end -- a route at its new level before the old one is silenced,
a pair heard unlinked for a moment. With ``peak-safe`` an explicit apply (a switch, a restore)
first sets every output it changes to the backend's explicit zero, then writes the plan, then
gives each output its volume back: the one the desk declares, else the one the device reported
before the switch.

An output counts as changed, with its pair partner, when the plan writes one of its mix cells or
output settings; any
input or playback setting other than a link changes every output, since monitoring can carry it
anywhere. An
output whose volume the device did not report is left alone -- there would be nothing to give
back. The volumes go back on every path out, a failed switch included. A start, a repair and a
reload write the same desk again and are not quieted.
"""

from __future__ import annotations

import math
from dataclasses import replace
from typing import Callable, Dict, List, Mapping, Optional, Sequence, Set, Tuple

from .backend import Control
from .constants import LEVEL_MIN
from .devices import device_for_name
from .log import log
from .model import Config
from .numeric import float32
from .osc import Args, Message
from .reconcile import ApplyIntent, Plan
from .registers import Device

VOLUME = "/output/%d/volume"


def changed_outputs(paths: Sequence[str], device: Device) -> Tuple[int, ...]:
    """The outputs a plan that writes ``paths`` changes."""
    outputs = device.channels_for("output")
    changed: Set[int] = set()
    for path in paths:
        parts = path.strip("/").split("/")
        # A link sounds through the cells the same plan writes; any other input or playback
        # setting is heard wherever that channel is routed.
        if parts[0] in ("input", "playback") and len(parts) >= 3 and parts[-1] != "stereo":
            return tuple(outputs)
        if parts[0] in ("mix", "output") and len(parts) >= 2 and parts[1].isdigit():
            # A linked pair answers on its odd channel: its partner changes as well.
            left = int(parts[1]) - (int(parts[1]) - 1) % 2
            changed.update((left, left + 1))
    return tuple(sorted(changed & set(outputs)))


def reported_volumes(outputs: Sequence[int],
                     observed: Mapping[str, Tuple[str, Args]]) -> Dict[int, float]:
    """The volume each output reported, for the ones that reported a usable one."""
    volumes: Dict[int, float] = {}
    for output in outputs:
        report = observed.get(VOLUME % output)
        if not report:
            continue
        try:
            value = report[1][0]
            volume = LEVEL_MIN if value == -math.inf else float32(value)
        except (IndexError, TypeError, ValueError):
            continue
        volumes[output] = volume
    return volumes


def quieting(volumes: Mapping[int, float]) -> List[Message]:
    """The writes that pull every quieted output down to the explicit zero."""
    return [(VOLUME % output, "f", (LEVEL_MIN,)) for output in sorted(volumes)]


def giving_back(volumes: Mapping[int, float],
                declared: Mapping[str, Args]) -> List[Message]:
    """Each quieted output's volume after the switch: declared, else what it was."""
    back: List[Message] = []
    for output, before in sorted(volumes.items()):
        wanted: Optional[Args] = declared.get(VOLUME % output)
        back.append((VOLUME % output, "f", wanted if wanted is not None else (before,)))
    return back


def send_quietly(config: Config, wanted: Plan, backend: Control, intent: ApplyIntent,
                 latest: Mapping[str, Tuple[str, Args]],
                 read: Callable[[Sequence[str]], Mapping[str, Tuple[str, Args]]],
                 send: Callable[[Plan], None]) -> None:
    """Send ``wanted`` with ``send``; for a peak-safe explicit apply, between quieted outputs.

    ``read`` observes the registers ``latest`` lacks. The volumes go back even when ``send``
    raises; a failure to give them back is logged, and the original failure is the one raised.
    """
    device = device_for_name(config.device_name)
    if not config.peak_safe or intent != ApplyIntent.EXPLICIT or device is None \
            or not wanted.writes:
        send(wanted)
        return
    outputs = changed_outputs([write.path for write in wanted.writes], device)
    seen = dict(latest)
    missing = [VOLUME % output for output in outputs if VOLUME % output not in seen]
    if missing:
        seen.update(read(missing))
    volumes = reported_volumes(outputs, seen)
    if not volumes:
        send(wanted)
        return
    quieted = {VOLUME % output for output in volumes}
    declared = {write.path: write.args for write in wanted.writes if write.path in quieted}
    rest = replace(wanted, writes=tuple(w for w in wanted.writes if w.path not in quieted))
    log.info("peak-safe: outputs %s down for the switch", ", ".join(map(str, sorted(volumes))))
    backend.send(quieting(volumes))
    try:
        send(rest)
    except BaseException:
        try:
            backend.send(giving_back(volumes, declared))
        except OSError as exc:
            log.error("peak-safe: outputs %s stay at %.0f dB until the next apply (%s)",
                      ", ".join(map(str, sorted(volumes))), LEVEL_MIN, exc)
        raise
    backend.send(giving_back(volumes, declared))
