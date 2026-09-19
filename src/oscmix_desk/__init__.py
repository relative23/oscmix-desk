"""oscmix-desk -- declarative, verified mixer state for RME Fireface.

The supported surface of this package is what ``__all__`` lists below:
reading a config, applying and verifying it, switching profiles, the
errors and outcomes those produce, and the two entry points. Until 0.7.0
it listed 78 names, most of them internals -- the OSC codec, the message
shapes, the sequencer search, the process scan -- which made every one of
them something a caller could come to depend on (third outside review).
The modules are importable as they always were, and are implementation:
they change without notice.

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
from .constants import EXIT_CONFIG, EXIT_FAILURE, EXIT_OK, __version__
from .errors import (
                        ConfigError,
                        DeviceAmbiguous,
                        DeviceLockUnavailable,
                        ReceivePortError,
                        WriteFailed,
)
from .launcher import main as launch_mixer
from .marker import active_profile
from .model import (
                        ChannelSetting,
                        CommandLine,
                        Config,
                        GlobalSetting,
                        Machine,
                        Route,
)
from .outcome import (
                        APPLIED_UNVERIFIED,
                        APPLIED_VERIFIED,
                        REFUSED,
                        WRITTEN_IN_PART,
                        Outcome,
)
from .paths import discover_config_path, list_profiles, profile_path
from .pipewire import generate_pipewire_conf
from .profiles import (
                        describe_profiles,
                        effective_config,
                        load_profile,
                        restore_main,
                        switch_profile,
)
from .routing import apply_routing
from .session import run_session
from .verify import (
                        VerifyResult,
                        expected_registers,
                        verify_and_repair,
                        verify_routing,
)

__all__ = [
                        "APPLIED_UNVERIFIED",
                        "APPLIED_VERIFIED",
                        "EXIT_CONFIG",
                        "EXIT_FAILURE",
                        "EXIT_OK",
                        "REFUSED",
                        "WRITTEN_IN_PART",
                        "ChannelSetting",
                        "CommandLine",
                        "Config",
                        "ConfigError",
                        "DeviceAmbiguous",
                        "DeviceLockUnavailable",
                        "GlobalSetting",
                        "Machine",
                        "Outcome",
                        "ReceivePortError",
                        "Route",
                        "VerifyResult",
                        "WriteFailed",
                        "__version__",
                        "active_profile",
                        "apply_routing",
                        "describe_profiles",
                        "discover_config_path",
                        "effective_config",
                        "expected_registers",
                        "generate_pipewire_conf",
                        "launch_mixer",
                        "list_profiles",
                        "load_config",
                        "load_profile",
                        "profile_path",
                        "restore_main",
                        "run_session",
                        "switch_profile",
                        "verify_and_repair",
                        "verify_routing",
]
