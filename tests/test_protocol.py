"""The ODK1 values in oscmix_desk.protocol are the ones control.h defines.

The client and the backend are built from different trees: a number
changed on one side only would still pass every test that uses the
Python values on both ends of a socket. So the header is read out of the
patch that ships it, and every enumerator and limit is compared here.
"""

import json
import re

from support import repo_file

from oscmix_desk import protocol
from oscmix_desk.protocol import Event, Request, Role, Source, Status


def added_file(patch, name):
    """The full text of a file that ``patch`` creates."""
    text = repo_file("patches", patch).read_text()
    start = text.index("diff --git a/%s b/%s" % (name, name))
    hunk = text.index("\n@@ ", start)
    lines = []
    for line in text[hunk + 1:].splitlines()[1:]:
        if not line.startswith("+"):
            break
        lines.append(line[1:])
    return "\n".join(lines)


def enumerators(header):
    """Every ``NAME = value`` of every enum, with C's implicit increments."""
    values = {}
    for body in re.findall(r"enum\s+\w+\s*\{([^}]*)\}", header):
        following = 0
        for item in filter(None, (part.strip() for part in body.split(","))):
            name, _, value = (part.strip() for part in item.partition("="))
            following = int(value, 0) if value else following
            values[name] = following
            following += 1
    return values


def defines(header):
    return dict(re.findall(r"^#define\s+(\w+)\s+(.+)$", header, re.MULTILINE))


HEADER = added_file("0003-coordinated-backend-and-gtk.patch", "control.h")
SOURCE = added_file("0003-coordinated-backend-and-gtk.patch", "control.c")


def test_only_the_first_patch_of_the_series_defines_control_h():
    # A later patch that edits the header would make the text read above
    # stale without failing anything, so that is refused outright.
    series = json.loads(repo_file("patches", "backend-series.json").read_text())
    files = [entry["file"] for entry in series["patches"]]
    assert files[0] == "0003-coordinated-backend-and-gtk.patch"
    for name in files[1:]:
        assert "diff --git a/control.h" not in repo_file("patches", name).read_text(), name


def test_every_enumerator_matches():
    expected = {}
    for enum in (Request, Status, Role, Source):
        expected.update(("CONTROL_" + member.name, int(member)) for member in enum)
    # Event.LEASE is the lease-state packet control.h calls CONTROL_EVENT.
    names = {Event.LEASE: "CONTROL_EVENT"}
    expected.update((names.get(member, "CONTROL_" + member.name), int(member))
                    for member in Event)
    assert enumerators(HEADER) == expected


def test_the_framing_and_limits_match():
    values = defines(HEADER)
    assert int(values["CONTROL_PAYLOAD"]) == protocol.PAYLOAD
    assert int(values["CONTROL_HEADER"]) == protocol.HEADER.size
    assert values["CONTROL_REPLY"] == "UINT32_C(0x%08X)" % protocol.REPLY
    assert 'memcpy(p->data, "%s", 4);' % protocol.MAGIC.decode() in SOURCE


def test_the_backend_clocks_match():
    # From the source 0003 creates; no later patch may redefine them.
    values = defines(SOURCE)
    for name in ("HELLO_TIMEOUT", "LEASE_IDLE", "LEASE_TOTAL", "REFRESH_WINDOW"):
        assert float(values[name]) == getattr(protocol, name), name
    assert int(defines(HEADER)["CONTROL_CLIENTS"]) == protocol.CLIENTS
    series = json.loads(repo_file("patches", "backend-series.json").read_text())
    for entry in series["patches"][1:]:
        text = repo_file("patches", entry["file"]).read_text()
        assert not re.search(r"^[-+]#define (HELLO_TIMEOUT|LEASE_|REFRESH_WINDOW|CONTROL_CLIENTS)",
                             text, re.MULTILINE), entry["file"]


def test_the_client_keeps_its_lease_with_room_to_spare():
    # Between two KEEPALIVEs the client waits at most one heartbeat and one
    # acknowledgement; the backend's idle check has to stay well clear.
    from oscmix_desk import backend, constants

    assert backend.CONTROL_HEARTBEAT + constants.CONTROL_ACK_TIMEOUT <= protocol.LEASE_IDLE - 2
    assert constants.CONTROL_LEASE_TOTAL == protocol.LEASE_TOTAL


def test_the_hello_reply_layout_matches():
    epoch = protocol.HELLO_REPLY.size - 8
    assert "memcpy(payload, epoch, %d);" % epoch in SOURCE
    assert "put32(payload + %d, getpid());" % epoch in SOURCE
    assert "put32(payload + %d, %d);" % (epoch + 4, protocol.VERSION) in SOURCE
    assert "memcpy(payload + %d, device_name" % protocol.HELLO_REPLY.size in SOURCE
