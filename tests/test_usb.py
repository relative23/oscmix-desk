"""USB presence detection via sysfs (no lsusb dependency)."""


def test_device_found(session_mod, fake_sysfs):
    assert session_mod.usb_device_present("2a39:3fd9", fake_sysfs) is True


def test_case_insensitive_match(session_mod, fake_sysfs):
    assert session_mod.usb_device_present("2A39:3FD9", fake_sysfs) is True


def test_device_absent(session_mod, empty_sysfs):
    assert session_mod.usb_device_present("2a39:3fd9", empty_sysfs) is False


def test_missing_sysfs_dir(session_mod, tmp_path):
    assert session_mod.usb_device_present("2a39:3fd9", tmp_path / "nope") is False


def test_launcher_uses_same_detection(launch_mod, fake_sysfs, empty_sysfs):
    assert launch_mod.usb_device_present("2a39:3fd9", fake_sysfs) is True
    assert launch_mod.usb_device_present("2a39:3fd9", empty_sysfs) is False


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
    from oscmix_desk import discovery

    assert launch_mod.usb_device_present is discovery.usb_device_present
    assert launch_mod.udp_port_listening is discovery.udp_port_listening


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
