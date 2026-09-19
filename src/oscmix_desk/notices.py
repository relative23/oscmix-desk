"""What there is to say about a desk before it is written or shown.

Routes on a device nobody modelled, a desk checked for one interface and
used for another. Neither refuses anything; each is said once, by
whoever has the desk in hand.
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
    for message in (unchecked_routes_warning(config),
                    replaced_device_warning(config)):
        if message:
            log.warning("%s", message)


def replaced_device_warning(config: "Config") -> Optional[str]:
    """What to say about a desk checked for one interface and used for
    another, or None.

    A config is validated for the device its file names
    (``loaded.device_name``), and ``--device`` replaces the name
    afterwards. When the two are different models -- or one is no model
    at all -- the channel and section checks said nothing about the
    interface the routes go to: measured, outputs 41/42 reached a UCX
    II, which has twenty, in silence (0.6.11). Validating for the
    override itself needs the parser to know it, which is the
    frozen-config work of 0.7.0; until then this is the notice.
    """
    if config.loaded is None or not (config.routes or config.channels
                                     or config.globals):
        return None                 # nothing in it was checked for a device
    if device_for_name(config.loaded.device_name) is device_for_name(
            config.device_name):
        return None
    return ("--device replaces [device] name after validation: this config "
            "was checked for %r and is used for %r, so its channels and "
            "sections were validated against the wrong interface"
            % (config.loaded.device_name, config.device_name))
