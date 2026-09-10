"""Switching the whole desk to a named config, with a stated outcome.

A profile is a complete ``routing.conf`` in ``profiles/`` beside the
main one. Not a new section type: a profile *is* a config, parsed by the
same code, subject to the same compatibility rule (ADR 0006), and
``--dump-config > profiles/tracking.conf`` composes for free.

The design constraint is the desk, not the file format. Switching
happens while someone is listening, and there is no rollback for a
mixer: once ``/output/1/volume`` is on the wire, the monitors are loud.
So the order is fixed and the whole module is arranged around it --
**parse and validate everything, then write, then check.** A config that
cannot be understood costs an error message and not one datagram.

That is why this states an outcome rather than raising. Three, and only
three:

``APPLIED_VERIFIED``
    Written, and the device reported the values back.

``APPLIED_UNVERIFIED``
    Written, and the read-back could not confirm it -- normally because
    the mixer GUI holds UDP 8222, which is the common desktop case, not
    a fault. Carries the list of what went unconfirmed, because
    "unverified" without the list is not an outcome a person can act on.

``REFUSED``
    Nothing was written. The config did not parse, the profile does not
    exist, or the name was not a name.

There is deliberately no fourth. "Partly applied, and here is a
traceback" is the state this module exists to make unrepresentable.
"""

from __future__ import annotations

import configparser
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional, Sequence

from .backend import Backend, loopback
from .config import Config, load_config, profile_path
from .constants import VERIFY_TIMEOUT
from .errors import ConfigError
from .log import log
from .registers import device_for_name
from .routing import apply_routing
from .verify import expected_registers, register_ever_reported, verify_routing


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

#: The ``reason`` on an outcome where the read-back was never attempted,
#: because the caller asked for none. Distinct wording from a read-back
#: that ran and came up short: the register list means "unknown" here
#: and "looked for and absent" there, and the state cannot say so.
NOT_CHECKED = "verification not requested"

#: The complete set. A fourth member is a design change, and
#: ``tests/test_profiles.py`` asserts this is exhaustive so it cannot
#: arrive by accretion.
STATES = (APPLIED_VERIFIED, APPLIED_UNVERIFIED, REFUSED)


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
        if self.state == REFUSED:
            return "refused %r, nothing written: %s" % (self.name, self.reason)
        if self.state == APPLIED_VERIFIED:
            return "applied %r and verified it at the device" % self.name
        if self.reason == NOT_CHECKED:
            return ("applied %r; not checked, so none of its %d register(s) "
                    "is confirmed" % (self.name, len(self.unverified)))
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


def load_profile(name: str, config_path: Optional[Path] = None) -> Config:
    """Parse a profile, or raise ``ConfigError``.

    Separate from :func:`switch_profile` so the refusal path can be
    tested, and used, without a device anywhere near it -- that is what
    makes ``--dry-run`` on a profile honest.

    **Transport settings come from the main config, not the profile.**
    A profile describes the desk: what is routed where, and at what
    level. The OSC ports and the device name describe the *machine*, and
    a profile that had to restate them would be wrong the moment it was
    copied between two machines -- or, worse, silently right. That is
    not hypothetical: a profile with no ``[osc]`` section fell back to
    the compiled-in default 7222 during development and wrote to a live
    Fireface from a unit test, because the default happened to match.

    A profile may still set them, and then it wins: the machine that
    needs a second backend on another port is exactly the machine whose
    profiles are per-backend.
    """
    path = profile_path(name, config_path)
    if not path.is_file():
        raise ConfigError("no profile %r (looked in %s)" % (name, path.parent))
    profile = load_config(path)
    if config_path is not None and Path(config_path).is_file():
        _inherit_transport(profile, load_config(config_path), path)
    return profile


#: Everything in a config that describes the *machine* rather than the
#: desk, as (section, option, Config attribute). A profile inherits each
#: of these unless it states it itself.
#:
#: A table rather than a list of ifs, because the failure mode is
#: forgetting one: `usb-id` was left out of the first version and would
#: have silently reverted to the compiled-in default on any machine that
#: sets it. `tests/test_profiles.py` holds this table against the set of
#: fields on Config that no `[route]` or channel section can write, so a
#: new machine-level setting cannot be added without landing here too.
MACHINE_SETTINGS = (
    ("osc", "port", "osc_port"),
    ("osc", "recv-port", "osc_recv_port"),
    ("device", "name", "device_name"),
    ("device", "usb-id", "usb_id"),
)


def _inherit_transport(profile: Config, main: Config, path: Path) -> None:
    """Fill the machine-level settings a profile did not state itself."""
    text = path.read_text()
    for section, option, attr in MACHINE_SETTINGS:
        if not _states(text, section, option):
            setattr(profile, attr, getattr(main, attr))


def _states(text: str, section: str, option: str) -> bool:
    """Whether the profile file actually names this option.

    Reads the file rather than comparing against the default, because a
    profile that deliberately sets the default is stating it, and
    "equals the default" cannot tell those apart.
    """
    # Same parser settings as load_config. A second parser with
    # different rules reading the same file is the shape of three
    # separate defects in 0.3.0; matching them costs one line.
    parser = configparser.ConfigParser(
        interpolation=None, inline_comment_prefixes=("#", ";"))
    try:
        parser.read_string(text)
    except configparser.Error:
        return False
    return parser.has_option(section, option)


def switch_profile(name: str, config_path: Optional[Path] = None,
                   backend: Optional[Backend] = None,
                   verify: bool = True) -> Outcome:
    """Switch the desk to a profile and say what happened.

    Never raises for a bad config: an unparseable profile is an
    ``Outcome``, because the caller has to distinguish "your typo cost
    you nothing" from "it is applied but I could not check" and an
    exception collapses those into the same thing.
    """
    try:
        config = load_profile(name, config_path)
    except ConfigError as exc:
        # Before any socket exists. The ordering is the promise.
        # The message names the profile: on a desk with five of them,
        # "channel 99 out of range" without a name is a search.
        log.error("profile %r refused, nothing written: %s", name, exc)
        return Outcome(state=REFUSED, name=name, reason=str(exc))

    device = backend if backend is not None else loopback(
        config.osc_port, config.osc_recv_port)
    _write(config, device)

    if not verify:
        # Not confirmed, because nobody looked -- which is a different
        # fact from "looked and did not see it", and the state is the
        # same either way. Everything expected goes in the list.
        return Outcome(state=APPLIED_UNVERIFIED, name=name,
                       reason=NOT_CHECKED,
                       unverified=sorted(expected_registers(config)))
    return _check(name, config, device)


def _write(config: Config, device: Backend) -> None:
    """Apply the profile, barrier and all.

    Delegates to ``routing.apply_routing`` rather than sending the three
    phases itself. The first version did send them itself -- links, mix,
    channel state, one after the other with nothing in between -- and
    dropped the link barrier in the process. On a live UCX II the whole
    switch, apply and read-back, took 48 ms; the barrier alone is
    measured in seconds. That is the stereo-link race the 0.2.0 release
    was about, reintroduced on a new write path.

    Three defects in 0.3.0 had the same shape: a second
    implementation of something that already existed, correct in
    everything it did and missing something the original had. This is
    the third and last of them.
    """
    apply_routing(config, config.osc_port, config.osc_recv_port,
                  backend=device)


def _check(name: str, config: Config, device: Backend) -> Outcome:
    """Read the state back and classify the switch.

    Delegates the whole read-back to ``verify.verify_routing`` against
    the caller's backend. It deliberately does not reimplement the loop:
    the first version of this function read a single datagram and gave
    up, so a register that was demonstrably correct at the device came
    back unconfirmed -- measured on a UCX II, where ``/output/1/volume``
    went 0.0 -> -6.0 and the switch reported it unverified anyway.

    Safe in the direction it failed, and useless: APPLIED_VERIFIED was
    unreachable on real hardware.
    """
    model = device_for_name(config.device_name)
    registers = expected_registers(config)
    result = verify_routing(registers, config.osc_port, config.osc_recv_port,
                            VERIFY_TIMEOUT, device_model=model,
                            backend=device)
    if result is None:
        # The mixer GUI holds the port. Applied, blind, and said so.
        log.info("profile %r applied; read-back port in use, cannot verify",
                 name)
        return Outcome(state=APPLIED_UNVERIFIED, name=name,
                       reason="receive port in use (mixer GUI running?)",
                       unverified=sorted(registers))

    unverified = sorted(result.mismatched + result.unobserved)
    if not unverified:
        return Outcome(state=APPLIED_VERIFIED, name=name)
    # Registers this backend never reports are not evidence of a problem,
    # but they are still not confirmation -- so they stay in the list
    # rather than being quietly dropped from it, and are named as the
    # separate thing they are.
    blind = sorted(p for p in unverified
                   if not register_ever_reported(p, model))
    reason = ("%d unconfirmed, %d of them never reported by this backend"
              % (len(unverified), len(blind)))
    return Outcome(state=APPLIED_UNVERIFIED, name=name, reason=reason,
                   unverified=unverified, unverifiable=blind)


def describe_profiles(config_path: Optional[Path] = None) -> Sequence[str]:
    """Profile names with a one-line summary each, for ``--list-profiles``."""
    from .config import list_profiles

    lines = []
    for name in list_profiles(config_path):
        try:
            config = load_profile(name, config_path)
        except ConfigError as exc:
            lines.append("%-16s  BROKEN: %s" % (name, exc))
            continue
        lines.append("%-16s  %d route(s), %d channel section(s)"
                     % (name, len(config.routes), len(config.channels)))
    return lines
