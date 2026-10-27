"""The supported Python API for declarative RME mixer state.

Use open_desk for one coordinated apply, verification or repair. Named
profile operations additionally commit the selection; outcomes state what
was written, read back and persisted. The runtime needs only the standard
library. Architecture tests enforce the module graph and this public surface;
implementation modules remain separately importable.
"""

from __future__ import annotations

from .backend import Control
from .config import load_config
from .constants import (
    EXIT_CONFIG,
    EXIT_DIFFERS,
    EXIT_FAILURE,
    EXIT_NOT_PERSISTED,
    EXIT_OK,
    EXIT_RELOAD_FAILED,
    __version__,
)
from .desk import Desk, open_desk
from .errors import (
    BackendControlError,
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
    Overrides,
    Route,
)
from .outcome import (
    APPLIED_MISMATCHED,
    APPLIED_UNVERIFIED,
    APPLIED_VERIFIED,
    REFUSED,
    WRITTEN_IN_PART,
    Outcome,
    OutcomeState,
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
from .reconcile import ApplyIntent
from .registers import Policy
from .verify import VerifyResult, expected_registers

__all__ = [
    "APPLIED_MISMATCHED",
    "APPLIED_UNVERIFIED",
    "APPLIED_VERIFIED",
    "EXIT_CONFIG",
    "EXIT_DIFFERS",
    "EXIT_FAILURE",
    "EXIT_NOT_PERSISTED",
    "EXIT_OK",
    "EXIT_RELOAD_FAILED",
    "REFUSED",
    "WRITTEN_IN_PART",
    "ApplyIntent",
    "BackendControlError",
    "ChannelSetting",
    "CommandLine",
    "Config",
    "ConfigError",
    "Control",
    "Desk",
    "DeviceAmbiguous",
    "DeviceLockUnavailable",
    "GlobalSetting",
    "Machine",
    "Outcome",
    "OutcomeState",
    "Overrides",
    "Policy",
    "ReceivePortError",
    "Route",
    "VerifyResult",
    "WriteFailed",
    "__version__",
    "active_profile",
    "describe_profiles",
    "discover_config_path",
    "effective_config",
    "expected_registers",
    "generate_pipewire_conf",
    "launch_mixer",
    "list_profiles",
    "load_config",
    "load_profile",
    "open_desk",
    "profile_path",
    "restore_main",
    "switch_profile",
]
