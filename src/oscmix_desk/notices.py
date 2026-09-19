"""What there is to say about a desk before it is written or shown.

Routes on a device nobody modelled: they are written as given, and said
so once, by whoever has the desk in hand. There were three notices in
0.6.11. A profile for another machine is refused since 0.7.0 (ADR 0026),
and a desk is validated for the interface ``--device`` names rather than
warned about afterwards, so this is the one that is left.
"""

from __future__ import annotations

from typing import Optional

from .devices import device_for_name, modelled_names
from .log import log
from .model import Config


def unchecked_routes_warning(config: "Config") -> Optional[str]:
    """What to say about routes on a device nobody modelled, or None.

    Still no opinion (ADR 0006), and no longer a silent one: a channel
    section on such a device has warned since 0.6.2, while its routes
    went to the hardware without a channel check and without a word.
    Asked by the paths that write or show a desk, about that desk
    (``log_desk_notices``) -- from the parser it fired on every load,
    and named routing.conf's routes while a profile was the desk being
    written (0.6.11).
    """
    if not config.routes or device_for_name(config.device_name) is not None:
        return None
    return ("no register model for %r: its %d route(s) are written as given, "
            "with no check that the device has those channels (modelled: %s)"
            % (config.device_name, len(config.routes), modelled_names()))


def log_desk_notices(config: "Config") -> None:
    """Warn, once, where a desk is about to be written or shown.

    A start, a dry run, a diff and the PipeWire sinks ask about the desk
    they have in hand; a switch, a restore and a SIGHUP reload about
    theirs. The first placement asked once in the CLI about the desk *in
    effect*, which for ``--profile`` and ``--no-profile`` is not the one
    being written, and a reload never passed it at all (found by review,
    0.6.11). A start asks again under the device lock when the desk it
    re-read there is another one (``reload._desk_under_the_lock``).
    """
    message = unchecked_routes_warning(config)
    if message:
        log.warning("%s", message)
