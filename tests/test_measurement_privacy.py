"""The measurement tools record which box they measured without its serial.

Since 2026-11-14 the serial of the measuring box is private. The tools still
need it while they run: to pick the box, find its ALSA card and PipeWire nodes
and notice another one in the middle of a run. What they write goes through
`without_serial`: every serial the record names becomes the placeholder, and
`device_pseudonym` -- a keyed hash with the machine ID -- shows that two records
measured the same box without giving the number away.
"""

import importlib.util
import json
import re

import pytest
from support import repo_file

SERIAL = "13572468"
TOOLS = ("verify-hardware", "sweep-writes", "record-dump", "probe-loopback", "probe-fx",
         "probe-phase", "probe-playback-mute", "probe-refresh-echo")


def load(name, module):
    spec = importlib.util.spec_from_file_location(module, repo_file("scripts", name + ".py"))
    loaded = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(loaded)
    return loaded


@pytest.fixture
def measurement(tmp_path, monkeypatch):
    module = load("measurement", "measurement_privacy")
    machine_id = tmp_path / "machine-id"
    machine_id.write_text("0123456789abcdef0123456789abcdef\n")
    monkeypatch.setattr(module, "MACHINE_ID_FILES", (machine_id,))
    return module


def test_every_form_of_the_serial_becomes_the_placeholder(measurement):
    digest = "ab" + SERIAL + "cd" * 27
    record = {"serial": SERIAL, "device": "Fireface UCX II, serial " + SERIAL,
              "running_backend": {"device": "Fireface UCX II (%s)" % SERIAL, "sha256": digest},
              "lock": "/run/oscmix-desk/2a39-3fd9-%s.lock" % SERIAL,
              "card": " 2 [II%s       ]: USB-Audio" % SERIAL,
              "sink": "alsa_output.usb-RME_Fireface_UCX_II__%s-00.pro-output-0" % SERIAL,
              "level": float("-inf")}
    clean = measurement.without_serial(record, SERIAL)
    text = json.dumps(clean, allow_nan=True)
    assert SERIAL not in text.replace(digest, "")
    assert clean["running_backend"]["sha256"] == digest
    assert clean["running_backend"]["device"] == "Fireface UCX II (12345678)"
    assert clean["sink"] == "alsa_output.usb-RME_Fireface_UCX_II__12345678-00.pro-output-0"
    assert clean["level"] == float("-inf")
    assert re.fullmatch(r"box-[0-9a-f]{16}", clean["device_pseudonym"])
    assert record["serial"] == SERIAL, "the tool keeps its own record for the run"


def test_a_serial_in_a_known_context_is_found_without_being_named(measurement):
    clean = measurement.without_serial({"device": "Fireface UCX II (%s)" % SERIAL})
    assert clean["device"] == "Fireface UCX II (12345678)"
    assert clean["device_pseudonym"] == measurement.device_pseudonym(SERIAL)


def test_the_pseudonym_is_stable_tells_boxes_apart_and_hides_the_serial(measurement, tmp_path,
                                                                     monkeypatch):
    first = measurement.device_pseudonym(SERIAL)
    assert first == measurement.device_pseudonym(SERIAL)
    assert first != measurement.device_pseudonym("24681357")
    assert SERIAL not in first
    other = tmp_path / "other-machine-id"
    other.write_text("fedcba9876543210fedcba9876543210\n")
    monkeypatch.setattr(measurement, "MACHINE_ID_FILES", (tmp_path / "absent", other))
    assert measurement.device_pseudonym(SERIAL) not in (None, first)


def test_without_a_machine_id_a_record_names_no_box(measurement, tmp_path, monkeypatch):
    empty = tmp_path / "empty-machine-id"
    empty.write_text("")
    monkeypatch.setattr(measurement, "MACHINE_ID_FILES", (tmp_path / "absent", empty))
    clean = measurement.without_serial({"serial": SERIAL}, SERIAL)
    assert clean == {"serial": "12345678", "device_pseudonym": None}


def test_a_record_already_without_a_serial_keeps_its_pseudonym(measurement):
    once = measurement.without_serial({"serial": SERIAL}, SERIAL)
    assert measurement.without_serial(once) == once


@pytest.mark.parametrize("serial", [None, "", "unknown", "123", "?"])
def test_a_value_that_is_no_serial_changes_nothing(measurement, serial):
    record = {"key": "2a39-3fd9-unknown", "seconds": 123, "tag": "?"}
    assert measurement.without_serial(record, serial) == dict(record, device_pseudonym=None)


def test_the_fx_probe_refuses_an_inventory_of_another_box(monkeypatch):
    probe = load("probe-fx", "fx_probe_privacy")
    monkeypatch.setattr(probe, "device_pseudonym", lambda serial: "box-" + serial)
    evidence = {"pid": 1, "epoch": "e", "sha256": "a", "series_sha256": "b"}
    inventory = {"device_pseudonym": "box-" + SERIAL, "running_backend": dict(evidence)}
    probe.require_inventory_peer(inventory, evidence, SERIAL)
    with pytest.raises(ValueError, match="different peer, build or device"):
        probe.require_inventory_peer(inventory, evidence, "24681357")
    with pytest.raises(ValueError, match="different peer, build or device"):
        probe.require_inventory_peer({"serial": SERIAL, "running_backend": evidence},
                                     evidence, SERIAL)


@pytest.mark.parametrize("tool", TOOLS)
def test_every_tool_writes_its_record_through_the_scrub(tool):
    """Each file write takes the scrubbed record, directly or by a name bound to it."""
    source = repo_file("scripts", tool + ".py").read_text()
    written = re.findall(r"(?:write_text|json\.dump)\((?:json\.dumps\(|_render\()?(\w+)", source)
    assert written, tool
    for name in written:
        assert name == "without_serial" or re.search(
            r"^\s*%s = .*without_serial\(" % name, source, re.MULTILINE), (tool, name)
