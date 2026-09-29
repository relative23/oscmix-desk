"""Where a desk is looked for: the config, its profiles, their names.

``--config``, ``$OSCMIX_CONFIG``, the user's config directory, the
system's -- in that order, the same for the session, the launcher and
the installer -- and ``profiles/`` beside whichever was found. A profile
name becomes a path here and nowhere else, which is why this is where a
name that is not a name is refused.
"""

from __future__ import annotations

import os
import pwd
import re
import stat
from pathlib import Path
from typing import List, Mapping, Optional

from .errors import ConfigError

#: The last place a desk is looked for, after the user's own.
SYSTEM_CONFIG = Path("/etc/oscmix/routing.conf")


def system_config(env: Mapping[str, str]) -> Path:
    """SYSTEM_CONFIG, or OSCMIX_SYSTEM_CONFIG from ``env`` for the test suite.

    The variable exists for the same reason OSCMIX_LOCK_DIR does: a test
    that runs the session as a subprocess cannot patch a module, and
    /etc is the machine's, not the test's.
    """
    return Path(env.get("OSCMIX_SYSTEM_CONFIG") or SYSTEM_CONFIG)


def regular_file(path: Path) -> bool:
    """Whether ``path`` is a regular file, answered alike on every Python.

    Before 3.14 Path.is_file() raised PermissionError for a file behind a
    directory the caller cannot search, and since 3.14 it returns False:
    a traceback on one version, "not found" on the other. Here an absent
    file is False and one that cannot be looked at is a ConfigError that
    names it.
    """
    try:
        return stat.S_ISREG(path.stat().st_mode)
    except (FileNotFoundError, NotADirectoryError):
        return False
    except OSError as exc:
        raise ConfigError("cannot read %s: %s" % (path, exc)) from None


def discover_config_path(
        environ: Optional[Mapping[str, str]] = None) -> Optional[Path]:
    """Return the first existing config file in the search order.

    ``environ`` is the environment to resolve in, this process's by
    default. The CLI passes the *unit's* environment to work out which
    desk the unit runs: OSCMIX_CONFIG, XDG_CONFIG_HOME and HOME there
    are not always this process's (0.6.10).
    """
    env = os.environ if environ is None else environ
    named = env.get("OSCMIX_CONFIG")
    if named:
        return Path(named)
    # A relative XDG_CONFIG_HOME is invalid and ignored (the spec's
    # words), which also keeps the answer independent of who asks.
    xdg: Optional[str] = env.get("XDG_CONFIG_HOME", "")
    if not xdg or not os.path.isabs(xdg):
        home = _home(env)
        xdg = home and os.path.join(home, ".config")
    candidates = [system_config(env)]
    if xdg:
        candidates.insert(0, Path(xdg) / "oscmix" / "routing.conf")
    for candidate in candidates:
        try:
            if regular_file(candidate):
                return candidate
        except ConfigError:
            # There, but not to be looked at: loading it says why, instead
            # of a desk further down the order being applied in its place.
            return candidate
    return None


def _home(env: Mapping[str, str]) -> Optional[str]:
    """HOME from ``env``, else the password database -- what ``~`` expands
    to -- or None for a uid without an entry, where ``~`` stays ``~``."""
    if env.get("HOME"):
        return env["HOME"]
    try:
        return pwd.getpwuid(os.getuid()).pw_dir
    except KeyError:
        return None


def profiles_dir(config_path: Optional[Path] = None) -> Optional[Path]:
    """Where profiles live: ``profiles/`` beside the routing config.

    Beside it rather than inside it, because a profile *is* a
    ``routing.conf`` -- complete, parsed by the same code, subject to
    the same compatibility rule. A new section type for them
    would have meant a second format with a second set of promises, and
    ``--dump-config > profiles/tracking.conf`` would not compose.
    """
    base = config_path or discover_config_path()
    if base is None:
        return None
    return base.parent / "profiles"


def list_profiles(config_path: Optional[Path] = None) -> List[str]:
    """Profile names, sorted. Missing directory is empty, not an error."""
    directory = profiles_dir(config_path)
    # os.path, not pathlib: False rather than PermissionError before 3.14.
    if directory is None or not os.path.isdir(directory):
        return []
    return sorted(p.stem for p in directory.glob("*.conf") if os.path.isfile(p))


def profile_path(name: str, config_path: Optional[Path] = None) -> Path:
    """The file a profile name refers to.

    Refuses a name that is not a plain identifier: profiles are selected
    on a command line and a path separator would let one escape the
    directory. Checked here rather than at each call site.
    """
    if not name or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", name):
        raise ConfigError(
            "%r is not a profile name -- letters, digits, dot, dash and "
            "underscore, and it may not start with punctuation" % name)
    directory = profiles_dir(config_path)
    if directory is None:
        raise ConfigError("no config directory, so no profiles either")
    return directory / ("%s.conf" % name)
