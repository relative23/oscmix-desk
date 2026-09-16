"""oscmix-desk -- declarative, verified mixer state for RME Fireface.

The public surface of this package is what ``__all__`` lists below.
Anything else is an implementation detail and may change without notice.

The runtime deliberately imports nothing outside the standard library, so
the package runs from a checkout on a bare system. ``tests/test_architecture.py``
enforces that, along with the layering the modules are arranged in:

    constants, errors, log      no internal imports
    osc, notify, discovery      leaves
    config                      constants, errors
    routing                     config, constants, log, osc
    verify                      routing, ...
    pipewire, process           leaves plus config/discovery
    profiles                    routing, verify; the desk in effect
    session                     composes everything below it
    cli                         the only entry point
"""

from __future__ import annotations

from .config import (
                        ChannelSetting,
                        Config,
                        Route,
                        discover_config_path,
                        list_profiles,
                        load_config,
                        profile_path,
)
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
from .errors import ConfigError, DeviceAmbiguous, DeviceLockUnavailable
from .launcher import main as launch_mixer
from .log import log
from .notify import sd_notify
from .osc import decode_osc, encode_osc, iter_osc_messages
from .pipewire import generate_pipewire_conf, pipewire_positions, pw_sink_info
from .process import find_stale_backends, port_holder, supervise
from .profiles import (
                        APPLIED_UNVERIFIED,
                        APPLIED_VERIFIED,
                        REFUSED,
                        Outcome,
                        active_profile,
                        describe_profiles,
                        effective_config,
                        load_profile,
                        restore_main,
                        switch_profile,
                        take_device_lock,
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
                        "Config",
                        "ConfigError",
                        "Device",
                        "DeviceAmbiguous",
                        "DeviceLockUnavailable",
                        "Outcome",
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
