"""The `[desk]` section: what a desk is beneath its routes, and how the session keeps it.

``base`` belongs to the desk, so a profile may state its own. ``enforce`` and ``peak-safe`` say
how the running session and its switches treat any desk; they are routing.conf's alone, and
every profile inherits them (``profiles.load_profile``).
"""

from __future__ import annotations

import configparser
from typing import Dict, Union

from .errors import ConfigError
from .sections import _parse_bool

#: `[desk] enforce`: ``apply`` keeps the desk on start, switch and reload; ``watch`` also
#: reconciles when the device contradicts a pinned value (watch.py).
ENFORCE = ("apply", "watch")
#: Options of the session rather than of the desk.
SESSION_OPTIONS = ("enforce", "peak-safe")

Value = Union[str, bool]


def parse_desk(parser: "configparser.ConfigParser", section: str, *,
               profile: bool) -> Dict[str, Value]:
    """The `[desk]` options the file states, by Config attribute."""
    stated: Dict[str, Value] = {}
    if parser.has_option(section, "base"):
        stated["base"] = parser.get(section, "base").strip()
    session = [option for option in SESSION_OPTIONS if parser.has_option(section, option)]
    if session and profile:
        raise ConfigError("[desk] %s: how the running session keeps the desk is set in "
                          "routing.conf, and every profile inherits it; take it out of the "
                          "profile" % session[0])
    if "enforce" in session:
        enforce = parser.get(section, "enforce").strip()
        if enforce not in ENFORCE:
            raise ConfigError("[desk] enforce: expected one of %s, got %r"
                              % (", ".join(ENFORCE), enforce))
        stated["enforce"] = enforce
    if "peak-safe" in session:
        stated["peak_safe"] = _parse_bool(parser.get(section, "peak-safe"), section,
                                          "peak-safe")
    return stated
