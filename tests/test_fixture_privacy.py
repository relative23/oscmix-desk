"""Recorded fixtures carry device evidence, never material of the recording host.

Every JSON file in tests/data ships with the source. The evidence validators
check what a fixture proves; this checks what no fixture may carry, whatever
its schema: host paths, systemd unit names, invocation or machine identities,
e-mail addresses and kernel addresses.

usbmon tags each URB with the kernel address of its struct. Those tags are
allowed as integers where usbmon puts them -- `urb`, `urb_id`, and `id` of a
packet header -- because they only identify transfers within the boot that
recorded them; a kernel address anywhere else fails.
"""

# The refused examples spell out host temp paths on purpose.
# ruff: noqa: S108

import json
import re
from pathlib import Path

import pytest

DATA = Path(__file__).resolve().parent / "data"
FIXTURES = sorted(DATA.glob("*.json"))

HOST_PATH = re.compile(
    r"(?<![\w./-])/(?:home|root|run|var|tmp|etc|usr|proc|sys|dev|media|mnt|snap|opt|srv|boot)(?:/|$)")
UNIT = re.compile(r"[\w@-]\.(?:service|scope|socket|timer|slice|mount)\b")
EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)*\.[A-Za-z]{2,}")
KERNEL_TEXT = re.compile(r"\b(?:0x)?ffff[89a-f][0-9a-f]{11}\b", re.IGNORECASE)
KERNEL_HALF = 0xFFFF800000000000
IDENTITY_KEYS = {"invocation", "invocation_id", "machine_id", "boot_id",
                 "hostname", "username", "user", "home", "cwd"}
URB_KEYS = {"urb", "urb_id"}
USBMON_HEADER = {"id", "kind", "transfer", "endpoint", "device", "bus"}


def leaks(value, where="$"):
    """Yield (where, reason) for every host detail in one decoded JSON value."""
    if isinstance(value, dict):
        for key, item in value.items():
            here = where + "." + key
            if key in IDENTITY_KEYS:
                yield here, "identity key"
            yield from text_leaks(key, here + " (key)")
            tag = key in URB_KEYS or (key == "id" and value.keys() >= USBMON_HEADER)
            if tag and type(item) is int:
                continue
            yield from leaks(item, here)
    elif isinstance(value, list):
        for index, item in enumerate(value):
            yield from leaks(item, f"{where}[{index}]")
    elif isinstance(value, str):
        yield from text_leaks(value, where)
    elif type(value) is int and value >= KERNEL_HALF:
        yield where, "kernel address"


def text_leaks(text, where):
    for pattern, reason in ((HOST_PATH, "host path"), (UNIT, "systemd unit"),
                            (EMAIL, "e-mail address"), (KERNEL_TEXT, "kernel address")):
        if pattern.search(text):
            yield where, reason


def test_there_are_fixtures_to_check():
    assert len(FIXTURES) >= 10


@pytest.mark.parametrize("fixture", FIXTURES, ids=[path.name for path in FIXTURES])
def test_fixture_carries_no_host_material(fixture):
    found = list(leaks(json.loads(fixture.read_text())))
    assert found == [], found[:5]


@pytest.mark.parametrize(("value", "reason"), [
    ({"note": "/home/someone/build/x.json"}, "host path"),
    ({"socket": "/run/oscmix-desk/control"}, "host path"),
    ({"scratch": "/var/tmp/c91/x"}, "host path"),
    ({"cases": [{"unit": "oscmix-desk-m2-run.service"}]}, "systemd unit"),
    ({"invocation_id": "0123456789abcdef0123456789abcdef"}, "identity key"),
    ({"hostname": "desk"}, "identity key"),
    ({"by": "someone@example.org"}, "e-mail address"),
    ({"pointer": "0xffff8ecf12345678"}, "kernel address"),
    ({"pointer": 18446619618511426624}, "kernel address"),
    ({"urb": "0xffff8ecf12345678"}, "kernel address"),
    ({"usb": {"tag": 18446619618511426624}}, "kernel address"),
    ({"event": {"id": 18446619618511426624, "kind": "S"}}, "kernel address"),
    ({"/tmp/key": 1}, "host path"),
])
def test_each_kind_of_host_material_is_refused(value, reason):
    assert reason in {found for _, found in leaks(value)}


@pytest.mark.parametrize("value", [
    {"path": "/output/1/volume", "args": [-18.0]},
    {"path": "/mix/5/input/3", "args": ["-inf"]},
    {"path": "/playback/12/mute"},
    {"usb": {"urb": 18446619618511426624, "event": {"urb_id": 18446619618459210048}}},
    {"packet": {"id": 18446613905769459456, "kind": "S", "transfer": 3, "endpoint": 7,
                "device": 2, "bus": 5}},
    {"sha256": "6e814f4eb4233956175356dd05d44767d7146356f2cc9d18183caccfc91764fc"},
    {"sequence": 7, "code": "OK"},
])
def test_device_evidence_passes(value):
    assert list(leaks(value)) == []
