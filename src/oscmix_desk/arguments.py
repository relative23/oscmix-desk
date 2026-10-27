"""The session's argument grammar, shared with service command-line inspection."""

from argparse import ArgumentParser
from pathlib import Path

from .constants import DEFAULT_DEVICE_TIMEOUT, __version__


def build_arg_parser() -> ArgumentParser:
    parser = ArgumentParser(
        prog="oscmix-session",
        description="Supervise the oscmix backend for an RME Fireface interface.",
    )
    parser.add_argument("--config", type=Path, metavar="FILE",
                        help="routing config (default: $OSCMIX_CONFIG, then "
                             "$XDG_CONFIG_HOME/oscmix/routing.conf or "
                             "~/.config/oscmix/routing.conf, then "
                             "/etc/oscmix/routing.conf)")
    parser.add_argument("--device", metavar="NAME",
                        help="ALSA client name to wait for (overrides config)")
    parser.add_argument("--timeout", type=float, default=DEFAULT_DEVICE_TIMEOUT,
                        metavar="SECONDS", help="how long to wait for the device")
    parser.add_argument("--osc-port", type=int, metavar="PORT",
                        help="legacy port value (overrides config; "
                             "control uses a local Unix socket)")
    parser.add_argument("--dry-run", action="store_true",
                        help="show what would be started and sent, then exit")
    parser.add_argument("--strict", action="store_true",
                        help="reject ignored config sections and an unusable "
                             "active profile instead of falling back")
    parser.add_argument("--check", action="store_true",
                        help="strictly validate the main config and every "
                             "profile offline, then exit")
    parser.add_argument("--status", action="store_true",
                        help="inspect installation, device and control endpoint "
                             "without connecting or writing")
    parser.add_argument("--json", action="store_true",
                        help="versioned JSON output for --status")
    parser.add_argument("--snapshot", action="store_true",
                        help="print every register the device reports, for "
                             "comparing two moments; unlike --dump-config "
                             "this is not a config and holds nothing back")
    parser.add_argument("--diff", action="store_true",
                        help="compare the running device against the config "
                             "and print what an apply would write, without "
                             "writing it")
    parser.add_argument("--dump-config", action="store_true",
                        help="ask the running device for its state and print "
                             "a routing.conf that reproduces what it reports")
    parser.add_argument("--pipewire-sinks", action="store_true",
                        help="print a PipeWire config with one named sink "
                             "per stereo output pair, then exit")
    parser.add_argument("--pipewire-target", metavar="NODE",
                        help="Fireface sink node.name for --pipewire-sinks "
                             "(default: auto-detect via pw-dump)")
    parser.add_argument("--profile", metavar="NAME",
                        help="switch the desk to profiles/NAME.conf and "
                             "report the outcome, then exit")
    parser.add_argument("--list-profiles", action="store_true",
                        help="list the profiles found beside the config; "
                             "the active one is marked")
    parser.add_argument("--no-profile", action="store_true",
                        help="apply routing.conf again and forget the "
                             "active profile, then exit")
    parser.add_argument("--verbose", action="store_true", help="debug logging")
    parser.add_argument("--version", action="version", version=__version__)
    return parser
