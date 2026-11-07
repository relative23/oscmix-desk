"""Actual prepared mixer callbacks and write deadlines with substituted I/O.

Run beside backend_control.py with OSCMIX_CONTROL_BINARY set. These compile
full prepared C translation units; only transport/syscall sinks are replaced.
They do not open ALSA, USB or device sockets.
"""

import os
import subprocess
from pathlib import Path

import pytest


@pytest.fixture(scope="module")
def mixer_source():
    binary = Path(os.environ["OSCMIX_CONTROL_BINARY"]).resolve()
    assert binary.is_file()
    assert os.access(binary, os.X_OK)
    source = binary.parent
    assert (source / "oscmix.c").is_file()
    assert (source / "main.c").is_file()
    return source


@pytest.mark.parametrize(("probe", "contract"), [
    ("playback_mute", "total=69059"),
    ("mix_bulk", "actual_callback_cases=67200"),
    ("mix_invalid", "actual_mapping_value_capacity_reject_cases=12"),
    ("mix_partial", "partial_transport_cases=9"),
    ("pan_restore", "actual_combined_callback_cases=242"),
    ("midi_deadline", "actual_writemidi_deadline_cases=20"),
    ("mix_geometry", "actual_geometry_cases=15491"),
    ("readback_freshness", "actual_observation_lifecycle_cases=100"),
    ("mix_phase", "actual_phase_sign_cases=12"),
])
def test_actual_mixer_regressions(mixer_source, tmp_path, probe, contract):
    fixture = Path(__file__).parent / "backend_mixer" / (probe + ".c")
    target = tmp_path / probe
    command = ["cc", "-std=c11", "-O2", "-g", "-I", str(mixer_source),
               "-ffunction-sections", "-fdata-sections", "-Wl,--gc-sections",
               "-DCANDIDATE", str(fixture)]
    if probe != "midi_deadline":
        command.extend(str(mixer_source / name) for name in
                       ("osc.c", "sysex.c", "device_ffucxii.c", "util.c"))
    command.extend(["-lm", "-o", str(target)])
    subprocess.run(command, check=True, capture_output=True, timeout=30)
    result = subprocess.run([str(target)], capture_output=True, text=True,
                            check=False, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout.startswith("PASS ")
    assert contract in result.stdout
