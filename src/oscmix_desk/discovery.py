"""Finding the device and the backend: ALSA sequencer, USB sysfs, UDP."""

from __future__ import annotations

import collections
import os
import re
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Mapping, Optional, Sequence, Set, Tuple

from .errors import DeviceAmbiguous
from .log import log

_CLIENT_RE = re.compile(r'^Client\s+(\d+)\s*:\s*"(.*)"', re.MULTILINE)


def parse_seq_clients(text: str) -> List[Tuple[int, str]]:
    """Parse /proc/asound/seq/clients into (client number, name) pairs."""
    return [(int(num), name) for num, name in _CLIENT_RE.findall(text)]


#: RME puts the number printed on the box in brackets at the end of the
#: product string, in the sequencer client name and in the card list.
_SERIAL_RE = re.compile(r"\((\d{4,})\)")


def serial_in(name: str) -> Optional[str]:
    """The serial in a device name such as ``Fireface UCX II (24216011)``."""
    match = _SERIAL_RE.search(name)
    return match.group(1) if match else None


#: One line of /proc/asound/seq/clients, read from its end: the client
#: type in brackets closes the line, and the name is everything between
#: the first quote and the last quote before it. A client name is chosen
#: by whoever opens the sequencer and may carry quotes and brackets of its
#: own -- `Fireface UCX II (99887766)" [Kernel` passed an unanchored match
#: and bound a user-space client (found by review, 0.6.9).
_CLIENT_LINE_RE = re.compile(
    r'^Client\s+(\d+)\s*:\s*"(.*)"\s*\[(\w+)[^\]\n]*\]\s*$', re.MULTILINE)
_CLIENT_NUMBER_RE = re.compile(r"^Client\s+(\d+)\s*:", re.MULTILINE)


def _kernel_clients(text: str) -> List[Tuple[int, str]]:
    """(number, name) of every client the kernel lists as its own.

    The kernel never lists a client number twice. A number that begins
    more than one line was forged -- a client name may contain a newline --
    and nothing in the file says which of the lines is real, so that is a
    refusal with its reason rather than a guess, or a silent drop that
    made the real interface vanish (found by review, 0.6.9).
    """
    numbers = collections.Counter(
        int(number) for number in _CLIENT_NUMBER_RE.findall(text))
    repeated = sorted(number for number, count in numbers.items() if count > 1)
    if repeated:
        raise DeviceAmbiguous(
            "sequencer client %s is listed more than once in "
            "/proc/asound/seq/clients, so a client name forges lines and "
            "no interface can be told from it"
            % ", ".join(str(number) for number in repeated))
    return [(int(number), name)
            for number, name, kind in _CLIENT_LINE_RE.findall(text)
            if kind == "Kernel"]


def _model(name: str) -> str:
    """A device name without the serial RME appends to it in brackets."""
    return _SERIAL_RE.sub("", name).strip()


def _named(names: Sequence[str], device_name: str) -> List[int]:
    """Indices of the names that are ``device_name``, exactly if any are.

    `[device] name` has always been a substring match, and stays one; but
    a model whose name is the start of another's -- `Fireface 802` beside
    a `Fireface 802 FS` -- is matched exactly first, so it is not two
    candidates (found by review, 0.6.9).
    """
    exact = [i for i, name in enumerate(names)
             if name == device_name or _model(name) == device_name]
    return exact or [i for i, name in enumerate(names) if device_name in name]


def select_seq_client(text: str, device_name: str, serial: str = "",
                      cards: Optional[Sequence[str]] = None) -> Optional[int]:
    """The one sequencer client this desk is for, or None if it is not there.

    ``serial`` narrows the name match to one box. Without it, more than
    one match is not a choice this function makes: the first client is
    whichever interface the kernel enumerated first, and a desk bound to
    it would configure an arbitrary box while its lock named another.
    DeviceAmbiguous says so and names the remedy (ADR 0024).

    Only kernel clients count. ``cards``, when given, is the product name
    of every card the kernel lists, and a client counts only when its name
    is one of them exactly: the kernel names a card's client after the
    card, and a client name is whatever the program that opened the
    sequencer chose. A line forged into the file for a box that is not
    plugged in, or under a name no card has, is not an interface.
    """
    clients = [(number, name) for number, name in _kernel_clients(text)
               if cards is None or name in cards]
    clients = [clients[i] for i in _named([n for _c, n in clients], device_name)]
    matches = [(number, name) for number, name in clients
               if not serial or serial_in(name) == serial]
    if len(matches) > 1:
        raise DeviceAmbiguous(
            "%d interfaces match %r and [device] serial does not say which "
            "one this desk is for: %s" % (
                len(matches), device_name,
                ", ".join("%s (client %d)" % (name, number)
                          for number, name in matches)))
    return matches[0][0] if matches else None


def _trigger_snd_seq_load() -> None:
    """Opening /dev/snd/seq makes the kernel autoload the snd-seq module."""
    device = os.environ.get("OSCMIX_SEQ_DEV", "/dev/snd/seq")
    try:
        os.close(os.open(device, os.O_RDONLY | os.O_NONBLOCK))
    except OSError:
        pass


def _usb_device_dir(usb_id: str, sysfs_usb: Path) -> Optional[Path]:
    """The sysfs directory of a USB device, by vendor:product, or None."""
    vendor, product = usb_id.lower().split(":")
    try:
        entries = list(sysfs_usb.iterdir())
    except OSError:
        return None
    for entry in entries:
        try:
            dev_vendor = (entry / "idVendor").read_text().strip().lower()
            dev_product = (entry / "idProduct").read_text().strip().lower()
        except OSError:
            continue
        if dev_vendor == vendor and dev_product == product:
            return entry
    return None


def usb_device_present(usb_id: str, sysfs_usb: Path) -> bool:
    """Check for a USB device by scanning sysfs (no lsusb dependency)."""
    return _usb_device_dir(usb_id, sysfs_usb) is not None


def usb_revision(usb_id: str, sysfs_usb: Path) -> Optional[str]:
    """The device release number USB reports, e.g. ``3.01``.

    ``bcdDevice`` in sysfs is the descriptor's release field, binary-coded
    decimal: ``0301`` is 3.01. It is the one version number the device
    offers without a dump, and it changes with firmware updates -- which
    is what makes it worth recording in every evidence artifact. A
    measurement that does not say which firmware it was taken against
    cannot be told apart from the next one, and a firmware that behaves
    differently would be invisible in the evidence.
    """
    entry = _usb_device_dir(usb_id, sysfs_usb)
    if entry is None:
        return None
    try:
        raw = (entry / "bcdDevice").read_text().strip()
    except OSError:
        return None
    # Binary-coded decimal has decimal digits only. The first version of
    # this accepted hex digits and then called int() on them, which the
    # mutation survivors pointed at: a release field of "0a01" would
    # have raised instead of being returned as it is.
    if len(raw) == 4 and raw.isdigit():
        return "%d.%s" % (int(raw[:2]), raw[2:])
    return raw or None


def device_firmware(usb_id: str, sysfs_usb: Path,
                    reports: Optional[Mapping[str, object]] = None
                    ) -> Dict[str, Optional[object]]:
    """The two version numbers the device offers, in one shape.

    ``usb_revision`` from sysfs, and ``dsp_version`` from the register
    ``/hardware/dspvers`` when the caller has a dump to read it from.
    One function so that every artifact -- the hardware evidence, the
    write sweep, the recorded dump, the snapshot header -- carries the
    same keys and a reader can compare two runs field by field.
    """
    dsp: Optional[object] = None
    if reports:
        args = reports.get("/hardware/dspvers")
        # The CLI and the fixture recorder keep every argument, the
        # write sweep keeps the first value only; both are one number.
        dsp = _first(args) if isinstance(args, (list, tuple)) else args
    return {"usb_revision": usb_revision(usb_id, sysfs_usb),
            "dsp_version": dsp}


def _first(args: Sequence[object]) -> Optional[object]:
    return args[0] if args else None


def udp_port_listening(port: int, proc_root: Path) -> bool:
    """Check /proc/net/udp{,6} for a socket bound to ``port``."""
    for name in ("udp", "udp6"):
        try:
            lines = (proc_root / "net" / name).read_text().splitlines()[1:]
        except OSError:
            continue
        for line in lines:
            fields = line.split()
            if len(fields) < 2 or ":" not in fields[1]:
                continue
            local_port = int(fields[1].rsplit(":", 1)[1], 16)
            if local_port == port:
                return True
    return False


def lock_key(usb_id: str, serial: str) -> str:
    """The lock file name for one interface, from what identifies it.

    Letters, digits and ``-._`` only, so no config value can put a path
    separator into a name that lives in a shared directory.
    """
    raw = "%s-%s" % (usb_id, serial or "unknown")
    return "".join(c if c.isalnum() or c in "-._" else "-" for c in raw)


@dataclass(frozen=True)
class Device:
    """The one interface a process is for, resolved once.

    Serial, sequencer client and lock key come from the same resolution,
    so they cannot describe different boxes. Until 0.6.9 each was worked
    out on its own: the unit bound the first matching client, pinned the
    first serial in the card list, and a switch keyed on a rule of its
    own -- three answers that agreed only while there was one interface
    (ADR 0024).
    """

    usb_id: str
    serial: str
    client: Optional[int]

    @property
    def key(self) -> str:
        return lock_key(self.usb_id, self.serial)


def resolve_device(usb_id: str, device_name: str, serial: str,
                   proc_root: Path) -> Device:
    """Which interface a desk is for, from its config and the machine.

    ``serial`` from `[device] serial` names the box outright. Without it
    neither the kernel's sequencer clients nor the card list may show more
    than one interface matching ``device_name``: the card list catches a
    second box whose client is not up yet, which the clients alone would
    miss while two interfaces enumerate one after the other. More than one
    raises DeviceAmbiguous rather than picking. Both lists are matched on
    the model name, so a Fireface of another model beside it is not a
    second candidate (ADR 0024).

    The serial comes from the client this desk binds, and from the card
    list only when no client is up, so a process and its lock describe the
    same box. One read of each file per call.
    """
    try:
        text = (proc_root / "asound" / "seq" / "clients").read_text(
            errors="replace")
    except OSError:
        text = ""
    cards_file = proc_root / "asound" / "cards"
    client = select_seq_client(text, device_name, serial,
                               card_products(cards_file))
    cards = device_serials(cards_file, device_name)
    if not serial and len(cards) > 1:
        raise DeviceAmbiguous(
            "%d interfaces match %r (%s) and [device] serial does not say "
            "which one this desk is for" % (len(cards), device_name,
                                            ", ".join(cards)))
    if not serial and client is not None:
        name = dict(_kernel_clients(text))[client]
        serial = serial_in(name) or ""
    if not serial and len(cards) == 1:
        serial = cards[0]
    return Device(usb_id=usb_id, serial=serial, client=client)


def wait_for_device(usb_id: str, device_name: str, serial: str,
                    timeout: float, proc_root: Path) -> Optional[Device]:
    """The resolved interface once its sequencer client is up, or None.

    The start binds the client and pins the serial from the same
    resolution, read once per poll, rather than finding a client and then
    working out the serial from a second look at the machine (ADR 0024).
    """
    clients_file = proc_root / "asound" / "seq" / "clients"
    deadline = time.monotonic() + timeout
    while True:
        if clients_file.is_file():
            device = resolve_device(usb_id, device_name, serial, proc_root)
            if device.client is not None:
                return device
        else:
            _trigger_snd_seq_load()
        if time.monotonic() >= deadline:
            return None
        time.sleep(1.0)

def udp_socket_inodes(port: int, proc_root: Path) -> Set[str]:
    """Inode numbers of the UDP sockets bound to ``port``.

    The inode is what ties a listening port to the process that holds
    it: ``/proc/<pid>/fd/<n>`` links to ``socket:[<inode>]``. Without
    that step, "something holds the port" and "this process holds the
    port" are the same statement, which is how a cleanup can terminate
    a process that never had the port.
    """
    inodes = set()
    for name in ("udp", "udp6"):
        try:
            lines = (proc_root / "net" / name).read_text().splitlines()[1:]
        except OSError:
            continue
        for line in lines:
            fields = line.split()
            if len(fields) < 10 or ":" not in fields[1]:
                continue
            try:
                local_port = int(fields[1].rsplit(":", 1)[1], 16)
            except ValueError:
                continue
            if local_port == port:
                inodes.add(fields[9])
    return inodes


def resolve_binary(name: str, env_var: str) -> Optional[str]:
    """Locate a binary: env override, then ~/.local/bin, then PATH.

    The pinned build lives in ~/.local/bin (install.sh puts it there),
    and the systemd user manager's PATH need not include that directory
    -- but it does include /usr/local/bin. With PATH consulted first, a
    stale copy there wins exactly when it matters most: the hotplug
    start right after boot, before the desktop session has imported the
    login PATH. Measured 2026-08-26: that start ran a February build
    from a pre-rename install for six hours, caught only by its enum
    warning missing the OSC address. So the pinned location is
    consulted before PATH, and the explicit override before everything.
    """
    override = os.environ.get(env_var)
    if override:
        if os.access(override, os.X_OK):
            return override
        log.error("%s=%s is not an executable file", env_var, override)
        return None
    pinned = os.path.join(os.path.expanduser("~/.local/bin"), name)
    if os.access(pinned, os.X_OK):
        return pinned
    found = shutil.which(name)
    if found:
        return found
    for directory in ("/usr/local/bin", "/usr/bin"):
        candidate = os.path.join(directory, name)
        if os.access(candidate, os.X_OK):
            return candidate
    return None


#: The first line of a card: `` 2 [II24216011 ]: USB-Audio - Fireface UCX
#: II (24216011)``. The driver id has no spaces; the product runs to the
#: serial. A card's second line repeats name and serial and is not read.
_CARD_LINE_RE = re.compile(
    r"^\s*\d+\s+\[[^\]]*\]:\s*\S+\s+-\s+(.*?)\s*$", re.MULTILINE)


def card_products(cards: Path) -> Optional[List[str]]:
    """The product name of every card the kernel lists, or None if unreadable."""
    try:
        return _CARD_LINE_RE.findall(cards.read_text(errors="replace"))
    except OSError:
        return None


def device_serials(cards: Path = Path("/proc/asound/cards"),
                   device_name: str = "") -> List[str]:
    """Every serial the ALSA card list shows for ``device_name``, in order.

    Matched on the model the way the sequencer clients are, so a Fireface
    802 beside a UCX II is not a second UCX II -- counting every "Fireface"
    line made that machine ambiguous for either desk (found by review,
    0.6.9). One entry per card: its second line repeats the serial, and
    counting lines made one interface look like two (measured, 0.6.8).

    More than one means the machine has more than one box, and nothing
    in the card list says which of them a given process is driving;
    ``resolve_device`` refuses to guess (ADR 0024).
    """
    products = card_products(cards) or []
    found: List[str] = []
    for index in _named(products, device_name):
        serial = serial_in(products[index])
        if serial and serial not in found:
            found.append(serial)
    return found


def built_backend_revision(repo_root: Path) -> Optional[str]:
    """The upstream oscmix commit built under ``repo_root``, or None.

    A measurement that does not say which backend produced it cannot be
    compared against the next one. ``install.sh`` pins the revision;
    recording what is actually checked out in ``build/oscmix`` is what
    makes an evidence artifact mean something, and reading the checkout
    rather than the pin keeps the two honest against each other.
    """
    build = repo_root / "build" / "oscmix"
    if not (build / ".git").exists():
        return None
    result = subprocess.run(["git", "-C", str(build), "rev-parse", "HEAD"],
                            capture_output=True, text=True, check=False)
    return result.stdout.strip() or None
