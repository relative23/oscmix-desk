"""oscmix-desk -- declarative, verified mixer state for RME Fireface.

The public surface of this package is what ``__all__`` lists below.
Anything else is an implementation detail and may change without notice.

The runtime deliberately imports nothing outside the standard library, so
the package runs from a checkout on a bare system. ``tests/test_architecture.py``
enforces that, along with the layering the modules are arranged in:

    constants, errors, log, osc no internal imports
    notify, discovery, registers the leaves above, nothing else
    devices                     constants, registers
    model, paths                constants, registers; errors
    sections, notices           model, devices, registers, log
    config                      model, sections, devices, ...
    backend                     errors, osc
    reconcile                   model, constants, registers, devices
    dump                        reconcile, model, registers
    routing                     backend, model, reconcile, ...
    verify                      routing, ...
    pipewire, process, launcher leaves plus config/discovery
    locking, marker, outcome    near-leaves: constants, config, log
    profiles                    routing, verify, process, locking, ...
    reload                      profiles, locking, verify, ...
    session                     reload, profiles, locking, verify, routing, ...
    reads                       backend, reconcile, dump, discovery, ...
    cli                         session, profiles, reads, ...; oscmix-session
    launcher                    (above) oscmix-launch, the desktop entry

This is the shape, not the list. The exact edges are ``ALLOWED_IMPORTS``
in that test, which holds each module to what it really imports, in
both directions.
"""

from __future__ import annotations

from .config import load_config
from .constants import (
                        CHANNEL_MAX,
                        CHANNEL_MIN,
                        DEFAULT_DEVICE_NAME,
                        DEFAULT_DEVICE_TIMEOUT,
                        DEFAULT_OSC_PORT,
                        DEFAULT_OSC_RECV_PORT,
                        DEFAULT_USB_ID,
                        EXIT_CONFIG,
                        EXIT_FAILURE,
                        EXIT_OK,
                        LEVEL_MAX,
                        LEVEL_MIN,
                        UNLINKED_GAIN_OFFSET,
                        __version__,
)
from .discovery import (
                        Device,
                        lock_key,
                        parse_seq_clients,
                        resolve_binary,
                        resolve_device,
                        select_seq_client,
                        udp_port_listening,
                        usb_device_present,
                        wait_for_device,
)
from .errors import (
                        ConfigError,
                        DeviceAmbiguous,
                        DeviceLockUnavailable,
                        ReceivePortError,
)
from .launcher import main as launch_mixer
from .locking import take_device_lock
from .log import log
from .marker import active_profile
from .model import ChannelSetting, CommandLine, Config, Machine, Route
from .notify import sd_notify
from .osc import decode_osc, encode_osc, iter_osc_messages
from .outcome import APPLIED_UNVERIFIED, APPLIED_VERIFIED, REFUSED, Outcome
from .paths import discover_config_path, list_profiles, profile_path
from .pipewire import generate_pipewire_conf, pipewire_positions, pw_sink_info
from .process import find_stale_backends, port_holder, supervise
from .profiles import (
                        describe_profiles,
                        effective_config,
                        load_profile,
                        restore_main,
                        switch_profile,
)
from .reconcile import link_messages, mix_messages, policy_for
from .registers import PIN, REMEMBER
from .routing import (
                        apply_routing,
                        await_link_echo,
                        blind_reapply_mix,
                        output_link_state,
                        send_mix,
)
from .session import run_session
from .verify import (
                        VerifyResult,
                        expected_registers,
                        register_promptly_reported,
                        verify_and_repair,
                        verify_routing,
)

__all__ = [
                        "APPLIED_UNVERIFIED",
                        "APPLIED_VERIFIED",
                        "CHANNEL_MAX",
                        "CHANNEL_MIN",
                        "DEFAULT_DEVICE_NAME",
                        "DEFAULT_DEVICE_TIMEOUT",
                        "DEFAULT_OSC_PORT",
                        "DEFAULT_OSC_RECV_PORT",
                        "DEFAULT_USB_ID",
                        "EXIT_CONFIG",
                        "EXIT_FAILURE",
                        "EXIT_OK",
                        "LEVEL_MAX",
                        "LEVEL_MIN",
                        "PIN",
                        "REFUSED",
                        "REMEMBER",
                        "UNLINKED_GAIN_OFFSET",
                        "ChannelSetting",
                        "CommandLine",
                        "Config",
                        "ConfigError",
                        "Device",
                        "DeviceAmbiguous",
                        "DeviceLockUnavailable",
                        "Machine",
                        "Outcome",
                        "ReceivePortError",
                        "Route",
                        "VerifyResult",
                        "__version__",
                        "active_profile",
                        "apply_routing",
                        "await_link_echo",
                        "blind_reapply_mix",
                        "decode_osc",
                        "describe_profiles",
                        "discover_config_path",
                        "effective_config",
                        "encode_osc",
                        "expected_registers",
                        "find_stale_backends",
                        "generate_pipewire_conf",
                        "iter_osc_messages",
                        "launch_mixer",
                        "link_messages",
                        "list_profiles",
                        "load_config",
                        "load_profile",
                        "lock_key",
                        "log",
                        "mix_messages",
                        "output_link_state",
                        "parse_seq_clients",
                        "pipewire_positions",
                        "policy_for",
                        "port_holder",
                        "profile_path",
                        "pw_sink_info",
                        "register_promptly_reported",
                        "resolve_binary",
                        "resolve_device",
                        "restore_main",
                        "run_session",
                        "sd_notify",
                        "select_seq_client",
                        "send_mix",
                        "supervise",
                        "switch_profile",
                        "take_device_lock",
                        "udp_port_listening",
                        "usb_device_present",
                        "verify_and_repair",
                        "verify_routing",
                        "wait_for_device",
]
