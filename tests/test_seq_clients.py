"""Parsing /proc/asound/seq/clients (fixture captured from a real system)."""

from oscmix_desk import discovery

# Trimmed real output: the Fireface name also appears in port lines and
# there is a "Midi Through" client before it -- both historic footguns.
REAL_OUTPUT = """\
Client info
  cur  clients : 6
  peak clients : 21
  max  clients : 192

Client   0 : "System" [Kernel Legacy]
  Port   0 : "Timer" (Rwe-) [In/Out]
    Connecting To: 144:0
Client  14 : "Midi Through" [Kernel Legacy]
  Port   0 : "Midi Through Port-0" (RWe-) [In/Out]
Client  24 : "Fireface UCX II (24216011)" [Kernel Legacy]
  Port   0 : "Fireface UCX II (24216011) Port" (RWeX) [In/Out]
  Port   1 : "Fireface UCX II (24216011) Port" (RWeX) [In/Out]
    Connecting To: 128:0
Client 128 : "alsaseq" [User Legacy]
  Port   0 : "alsaseq" (rwe-) [In/Out]
Client 144 : "PipeWire-System" [User UMP MIDI2]
  Port   0 : "input" (rwe-) [In/Out]
"""


def test_parses_all_clients(session_mod):
    clients = discovery.parse_seq_clients(REAL_OUTPUT)
    assert clients == [
        (0, "System"),
        (14, "Midi Through"),
        (24, "Fireface UCX II (24216011)"),
        (128, "alsaseq"),
        (144, "PipeWire-System"),
    ]


def test_finds_fireface_client_number(session_mod):
    assert discovery.select_seq_client(REAL_OUTPUT, "Fireface UCX II") == 24


def test_port_lines_do_not_shadow_client_line(session_mod):
    # The device name appears in "Port 0/1" lines too; only the Client
    # line may match (the old grep -B1 approach picked "Midi Through").
    result = discovery.select_seq_client(REAL_OUTPUT, "Fireface UCX II")
    assert result == 24
    assert result != 14


def test_absent_device_returns_none(session_mod):
    assert discovery.select_seq_client(REAL_OUTPUT, "Babyface Pro") is None


def test_empty_input(session_mod):
    assert discovery.parse_seq_clients("") == []
    assert discovery.select_seq_client("", "Fireface UCX II") is None


def test_device_serials_read_the_product_string(tmp_path):
    """The number RME prints on the box, not the USB iSerial.

    An evidence artifact that cannot name its box stops being evidence
    the moment there is a second one; which box a process is for is
    decided by discovery.resolve_device on top of this (ADR 0024).
    """
    from oscmix_desk.discovery import device_serials

    cards = tmp_path / "cards"
    cards.write_text(
        " 0 [NVidia       ]: HDA-Intel - HDA NVidia\n"
        " 2 [II24216011   ]: USB-Audio - Fireface UCX II (24216011)\n")
    assert device_serials(cards) == ["24216011"]

    cards.write_text(" 0 [NVidia ]: HDA-Intel - HDA NVidia\n")
    assert device_serials(cards) == []

    assert device_serials(tmp_path / "missing") == []

def test_built_backend_revision_reads_the_checkout(tmp_path):
    """The revision comes from the checkout, not from the pin.

    Reading what is actually built keeps install.sh's pin honest: if the
    two ever disagree, the artifact names the binary that ran, which is
    the one the measurement is about.
    """
    import subprocess

    from oscmix_desk.discovery import built_backend_revision

    # No build/oscmix at all: no answer, not a crash.
    assert built_backend_revision(tmp_path) is None

    build = tmp_path / "build" / "oscmix"
    build.mkdir(parents=True)
    subprocess.run(["git", "init", "-q"], cwd=build, check=True)
    subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t",
                    "commit", "-q", "--allow-empty", "-m", "x"],
                   cwd=build, check=True)
    revision = built_backend_revision(tmp_path)
    assert revision is not None
    assert len(revision) == 40
    # str, not bytes: read from survivors -- without text=True the
    # length check still passes on b"..." and json.dumps of the sweep
    # artifact crashes instead.
    assert isinstance(revision, str)


def test_a_broken_checkout_answers_none_not_an_exception(tmp_path):
    """A .git that is not a repository must not raise.

    check=False is deliberate: this runs inside evidence collection, and
    a half-cloned or corrupted build directory should degrade to
    "revision unknown" in the artifact, not abort the measurement.
    """
    import subprocess

    from oscmix_desk.discovery import built_backend_revision

    # A nested invalid checkout must not fall back to the containing
    # oscmix-desk repository and attribute its HEAD to the backend.
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "-c", "user.email=t@t",
                    "-c", "user.name=t", "commit", "-q", "--allow-empty",
                    "-m", "containing desk repository"], check=True)
    (tmp_path / "build" / "oscmix" / ".git").mkdir(parents=True)
    assert built_backend_revision(tmp_path) is None


def test_an_unborn_backend_checkout_has_no_revision(tmp_path):
    import subprocess

    from oscmix_desk.discovery import built_backend_revision

    build = tmp_path / "build" / "oscmix"
    build.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(build)], check=True)
    assert built_backend_revision(tmp_path) is None


def test_backend_revision_accepts_a_linked_worktree(tmp_path):
    import subprocess

    from oscmix_desk.discovery import built_backend_revision

    source = tmp_path / "source"
    subprocess.run(["git", "init", "-q", str(source)], check=True)
    subprocess.run(["git", "-C", str(source), "-c", "user.email=t@t",
                    "-c", "user.name=t", "commit", "-q", "--allow-empty",
                    "-m", "backend"], check=True)
    expected = subprocess.run(["git", "-C", str(source), "rev-parse", "HEAD"],
                              capture_output=True, text=True, check=True).stdout.strip()
    build = tmp_path / "build" / "oscmix"
    subprocess.run(["git", "-C", str(source), "worktree", "add", "--detach",
                    str(build)], capture_output=True, check=True)
    assert (build / ".git").is_file()
    assert built_backend_revision(tmp_path) == expected


def test_device_serials_lists_every_box(tmp_path):
    """More than one line means more than one interface."""
    from oscmix_desk.discovery import device_serials

    cards = tmp_path / "cards"
    cards.write_text(
        " 0 [NVidia     ]: HDA-Intel - HDA NVidia\n"
        " 2 [II24216011 ]: USB-Audio - Fireface UCX II (24216011)\n"
        " 3 [II99887766 ]: USB-Audio - Fireface UCX II (99887766)\n")
    assert device_serials(cards) == ["24216011", "99887766"]
    assert device_serials(tmp_path / "missing") == []


def test_the_lock_key_names_the_box_and_nothing_a_path_can_abuse():
    from oscmix_desk.discovery import lock_key

    assert lock_key("2a39:3fd9", "24216011") == "2a39-3fd9-24216011"
    assert lock_key("2a39:3fd9", "") == "2a39-3fd9-unknown"
    assert lock_key("2a39:3fd9", "../x/y") == "2a39-3fd9-..-x-y"


def test_one_card_spanning_two_lines_is_one_interface(tmp_path):
    """`/proc/asound/cards` gives every card a continuation line.

    Both lines carry the name and the serial in brackets. Counting
    matching lines rather than distinct serials made a single UCX II
    look like two interfaces, and every writer on the machine went to
    the `ambiguous` key -- measured against the real file the first time
    the new key ran on hardware.
    """
    from oscmix_desk.discovery import device_serials

    cards = tmp_path / "cards"
    cards.write_text(
        " 0 [NVidia         ]: HDA-Intel - HDA NVidia\n"
        "                      HDA NVidia at 0xdc080000 irq 169\n"
        " 2 [II24216011     ]: USB-Audio - Fireface UCX II (24216011)\n"
        "                      RME Fireface UCX II (24216011) at "
        "usb-0000:77:00.0-2, high speed\n")
    assert device_serials(cards) == ["24216011"]
