"""USB presence detection via sysfs (no lsusb dependency)."""

import pytest

from oscmix_desk import discovery


@pytest.fixture
def usb_pair(fake_sysfs):
    first = fake_sysfs / '5-2'
    (first / 'product').write_text('Fireface UCX II (24216011)\n')
    (first / 'serial').write_text('opaque-usb-a\n')
    second = fake_sysfs / '5-3'
    second.mkdir()
    for name, value in {'idVendor': '2a39', 'idProduct': '3fd9',
                        'product': 'Fireface UCX II (99887766)',
                        'serial': 'opaque-usb-b', 'bcdDevice': '0302'}.items():
        (second / name).write_text(value + '\n')
    return fake_sysfs


@pytest.mark.parametrize(('serial', 'expected'), [
    ('24216011', '3.01'), ('99887766', '3.02'), ('', None),
    ('00000000', None), ('opaque-usb-b', None),
])
def test_firmware_belongs_to_the_selected_rme_identity(usb_pair, serial, expected):
    assert discovery.usb_revision('2a39:3fd9', usb_pair, serial=serial) == expected
    assert discovery.device_firmware('2a39:3fd9', usb_pair,
        {'/hardware/dspvers': (36,)}, serial=serial) == {
            'usb_revision': expected, 'dsp_version': 36}


@pytest.mark.parametrize('change', ['missing-product', 'unreadable-product', 'duplicate-product'])
def test_firmware_with_uncertain_product_identity_is_unknown(usb_pair, change):
    product = usb_pair / '5-3/product'
    if change == 'duplicate-product':
        (usb_pair / '5-2/product').write_text(product.read_text())
    else:
        product.unlink()
        if change == 'unreadable-product':
            product.mkdir()
    assert discovery.usb_revision('2a39:3fd9', usb_pair, serial='99887766') is None


@pytest.mark.parametrize(('serial', 'revision'), [('24216011', '3.01'), ('99887766', '3.02')])
def test_snapshot_uses_the_reported_device_identity_for_its_firmware(
        usb_pair, monkeypatch, capsys, serial, revision):
    from oscmix_desk import reads
    from oscmix_desk.model import Config

    monkeypatch.setenv('OSCMIX_SYSFS_USB', str(usb_pair))
    monkeypatch.setattr(reads, '_read_device', lambda *_: reads.DeviceRead(
        {'/hardware/dspvers': (36,), '/mix/5/input/1': (-6.0, 0)}, serial, 'ab' * 16))
    assert reads._snapshot(Config()) == 0
    text = capsys.readouterr().out
    assert 'serial %s, usb %s, dsp 36;' % (serial, revision) in text
    assert '/mix/5/input/1 -6.0 0\n' in text


@pytest.mark.parametrize(('serial', 'warning'), [('24216011', False), ('99887766', True)])
def test_firmware_notice_uses_the_configured_device(
        usb_pair, caplog, session_module, serial, warning):
    from oscmix_desk.model import Config

    with caplog.at_level('WARNING'):
        session_module._firmware_notice(Config(serial=serial), usb_pair)
    assert bool(caplog.text) is warning
    if warning:
        assert 'USB release 3.02' in caplog.text
        assert 'recorded on 3.01' in caplog.text


def test_device_found(session_mod, fake_sysfs):
    assert discovery.usb_device_present("2a39:3fd9", fake_sysfs) is True


def test_case_insensitive_match(session_mod, fake_sysfs):
    assert discovery.usb_device_present("2A39:3FD9", fake_sysfs) is True


def test_device_absent(session_mod, empty_sysfs):
    assert discovery.usb_device_present("2a39:3fd9", empty_sysfs) is False


def test_missing_sysfs_dir(session_mod, tmp_path):
    assert discovery.usb_device_present("2a39:3fd9", tmp_path / "nope") is False


def test_a_device_the_kernel_has_not_authorized_is_present_and_named(
        fake_sysfs, empty_sysfs):
    """`authorized=0` keeps the entry and unbinds every driver. Still
    present -- the start retries it, which is what brings the desk up once
    it is allowed -- but the reason it gives has to say so (0.6.10)."""
    from oscmix_desk import discovery

    device = fake_sysfs / "5-2"
    assert discovery.usb_device_authorized("2a39:3fd9", fake_sysfs) is True
    (device / "authorized").write_text("0\n")
    assert discovery.usb_device_present("2a39:3fd9", fake_sysfs) is True
    assert discovery.usb_device_authorized("2a39:3fd9", fake_sysfs) is False
    (device / "authorized").write_text("1\n")
    assert discovery.usb_device_authorized("2a39:3fd9", fake_sysfs) is True
    (device / "authorized").unlink()
    (device / "authorized").mkdir()        # unreadable as a file
    assert discovery.usb_device_authorized("2a39:3fd9", fake_sysfs) is True
    assert discovery.usb_device_authorized("2a39:3fd9", empty_sysfs) is True


def test_the_launcher_entry_point_is_exported(session_mod, launch_mod):
    # The launcher moved into the package in 0.2.0; it was the last file
    # outside the architecture test, the mutation scope and the coverage
    # everything else is held to.
    assert session_mod.launch_mixer is launch_mod.main


def test_the_launcher_no_longer_duplicates_device_detection(launch_mod):
    # It used to carry its own copies of usb_device_present and
    # udp_port_listening so it could stand alone. The package is
    # installed beside it now, so the copies bought nothing but a second
    # place for the same bug.
    from oscmix_desk import diagnostics, discovery

    assert launch_mod.backend_status is diagnostics.backend_status
    assert diagnostics.resolve_device is discovery.resolve_device
    from oscmix_desk import process

    assert diagnostics.control_holder is process.control_holder


# --------------------------------------------------------------------------
# The firmware the evidence was taken against
# --------------------------------------------------------------------------

def test_the_usb_revision_is_read_as_a_version(fake_sysfs):
    # bcdDevice is binary-coded decimal: 0301 is release 3.01. Recorded
    # in every artifact, because a measurement that cannot say which
    # firmware it saw cannot be told apart from the next one.
    from oscmix_desk import discovery

    assert discovery.usb_revision("2a39:3fd9", fake_sysfs) == "3.01"
    assert discovery.usb_revision("2A39:3FD9", fake_sysfs) == "3.01"


def test_the_usb_revision_formats_bcd_and_returns_anything_else_as_is(
        tmp_path):
    from oscmix_desk import discovery

    def sysfs(raw):
        root = tmp_path / ("sysfs-" + (raw or "empty"))
        (root / "1-1").mkdir(parents=True)
        (root / "1-1" / "idVendor").write_text("2a39\n")
        (root / "1-1" / "idProduct").write_text("3fd9\n")
        (root / "1-1" / "bcdDevice").write_text(raw + "\n")
        return root

    assert discovery.usb_revision("2a39:3fd9", sysfs("1000")) == "10.00"
    assert discovery.usb_revision("2a39:3fd9", sysfs("0000")) == "0.00"
    # Not BCD: handed back untouched rather than parsed wrongly or
    # crashed on -- the first version called int() on hex digits.
    assert discovery.usb_revision("2a39:3fd9", sysfs("0a01")) == "0a01"
    assert discovery.usb_revision("2a39:3fd9", sysfs("301")) == "301"
    assert discovery.usb_revision("2a39:3fd9", sysfs("")) is None


def test_a_device_matching_only_half_the_id_is_not_the_device(tmp_path):
    # vendor right, product wrong, and the other way round. The lookup
    # is an AND, and nothing asserted that until a mutant flipped it.
    from oscmix_desk import discovery

    root = tmp_path / "sysfs-halves"
    for name, vendor, product in (("1-1", "2a39", "0000"), ("1-2", "0000", "3fd9")):
        (root / name).mkdir(parents=True)
        (root / name / "idVendor").write_text(vendor + "\n")
        (root / name / "idProduct").write_text(product + "\n")
        (root / name / "bcdDevice").write_text("0301\n")
    assert discovery.usb_device_present("2a39:3fd9", root) is False
    assert discovery.usb_revision("2a39:3fd9", root) is None


def test_the_usb_revision_is_none_without_the_device_or_the_file(
        empty_sysfs, tmp_path):
    from oscmix_desk import discovery

    assert discovery.usb_revision("2a39:3fd9", empty_sysfs) is None
    assert discovery.usb_revision("2a39:3fd9", tmp_path / "nope") is None
    bare = tmp_path / "sysfs-no-bcd"
    (bare / "1-1").mkdir(parents=True)
    (bare / "1-1" / "idVendor").write_text("2a39\n")
    (bare / "1-1" / "idProduct").write_text("3fd9\n")
    assert discovery.usb_revision("2a39:3fd9", bare) is None


def test_device_firmware_has_one_shape_for_every_artifact(fake_sysfs,
                                                          empty_sysfs):
    from oscmix_desk import discovery

    # From a dump keyed by path, whether the reader kept a tuple of
    # arguments (the CLI, the fixture recorder) or the first value only
    # (the write sweep).
    assert discovery.device_firmware("2a39:3fd9", fake_sysfs,
                                     {"/hardware/dspvers": (36,)}) == {
        "usb_revision": "3.01", "dsp_version": 36}
    assert discovery.device_firmware("2a39:3fd9", fake_sysfs,
                                     {"/hardware/dspvers": 36}) == {
        "usb_revision": "3.01", "dsp_version": 36}
    # Nothing known is None, not a guess and not an omitted key.
    assert discovery.device_firmware("2a39:3fd9", empty_sysfs, None) == {
        "usb_revision": None, "dsp_version": None}


@pytest.mark.parametrize(("release", "name", "said"), [
    ("0301", "Fireface UCX II", False),     # the firmware that was measured
    ("0305", "Fireface UCX II", True),
    ("0305", "Some Box", False),            # nobody measured anything on it
    (None, "Fireface UCX II", False),       # not readable: nothing to say
])
def test_a_start_says_when_the_firmware_is_not_the_one_that_was_measured(
        tmp_path, caplog, release, name, said):
    """The register table, the hardware evidence and the write sweep were
    recorded on USB release 3.01. Until 0.7.0 nothing said that the box on
    the desk was not the box that was measured. A notice, not a refusal:
    every register is still read back, and a firmware update must not take
    the desk down."""
    from oscmix_desk import Config
    from oscmix_desk import session as session_module

    dev = tmp_path / "1-1"
    dev.mkdir()
    (dev / "idVendor").write_text("2a39\n")
    (dev / "idProduct").write_text("3fd9\n")
    if release:
        (dev / "bcdDevice").write_text(release + "\n")
    with caplog.at_level("WARNING"):
        session_module._firmware_notice(Config(device_name=name), tmp_path)
    expected = ("the interface reports USB release 3.05, and the register "
                "table for 'Fireface UCX II' was recorded on 3.01")
    assert (expected in caplog.text) is said
    assert bool(caplog.text) is said


def test_interface_entries_without_identity_do_not_end_the_device_search(fake_sysfs):
    # Real sysfs lists interfaces beside devices, in no particular order.
    for number in range(8):
        (fake_sysfs / ("5-2:1.%d" % (number + 1))).mkdir()
        (fake_sysfs / ("usb%d" % number)).mkdir()
    assert discovery.usb_device_present("2a39:3fd9", fake_sysfs)
    assert discovery.usb_revision("2a39:3fd9", fake_sysfs) == "3.01"


def test_an_unnamed_device_does_not_end_the_serial_selection(usb_pair):
    for number in range(8):
        unnamed = usb_pair / ("6-%d" % number)
        unnamed.mkdir()
        (unnamed / "idVendor").write_text("2a39\n")
        (unnamed / "idProduct").write_text("3fd9\n")
    assert discovery.usb_revision("2a39:3fd9", usb_pair, serial="99887766") == "3.02"


def test_an_interface_named_without_a_serial_has_no_invented_serial(tmp_path):
    from support import fake_proc

    proc = fake_proc(tmp_path / "proc")
    (proc / "asound/seq/clients").write_text('Client  24 : "Fireface UCX II" [Kernel]\n')
    (proc / "asound/cards").write_text(
        " 2 [II             ]: USB-Audio - Fireface UCX II\n"
        "      RME Fireface UCX II at usb-0000:77:00.0-1\n")
    device = discovery.resolve_device("2a39:3fd9", "Fireface UCX II", "", proc)
    assert (device.serial, device.client) == ("", 24)
