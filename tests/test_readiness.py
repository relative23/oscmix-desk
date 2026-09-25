"""Device readiness and read-only endpoint ownership."""

from oscmix_desk import discovery, process


def test_readiness_finds_the_listening_control_owner(endpoint):
    _config, path, proc = endpoint
    assert process.control_socket_owner(path, proc) == 101
    assert process.control_socket_owner(path.with_name("other.control"), proc) is None


def test_find_stale_backends_matches_only_oscmix(session_mod, tmp_path):
    proc = tmp_path / "proc"
    for pid, argv0 in ((101, b"/usr/local/bin/oscmix"),
                       (102, b"/usr/local/bin/alsaseqio"),
                       (103, b"oscmix"),
                       (104, b"oscmix-gtk")):
        entry = proc / str(pid)
        entry.mkdir(parents=True)
        (entry / "cmdline").write_bytes(argv0 + b"\x00")
    (proc / "self").mkdir()  # non-numeric entries are skipped
    (proc / "105").mkdir()   # missing cmdline is skipped
    assert process.find_stale_backends(proc) == [101, 103]


def test_find_stale_backends_skips_unreadable_entries(session_mod, tmp_path):
    # An unreadable /proc entry must be skipped, not fall through with the
    # ownership check silently missed: matching argv0 afterwards would put
    # a process nobody verified onto the kill list.
    proc = tmp_path / "proc"
    (proc / "200").mkdir(parents=True)
    (proc / "200" / "comm").write_text("oscmix\n")
    (proc / "200" / "cmdline").write_bytes(b"oscmix\x00")
    from oscmix_desk import process

    real_stat = process.Path.stat

    def failing_stat(self, *args, **kwargs):
        if self.name == "200":
            raise PermissionError("ownership unreadable")
        return real_stat(self, *args, **kwargs)

    process.Path.stat = failing_stat
    try:
        assert process.find_stale_backends(proc) == []
    finally:
        process.Path.stat = real_stat


def test_wait_for_device_returns_the_resolved_interface(session_mod, tmp_path):
    from support import fake_proc

    proc = fake_proc(tmp_path / "proc", boxes=[(24, "24216011")])
    found = discovery.wait_for_device("2a39:3fd9", "Fireface UCX II", "",
                                        1.0, proc)
    assert (found.client, found.serial) == (24, "24216011")


def test_wait_for_device_gives_up_and_says_so(session_mod, tmp_path):
    # The timeout is the difference between "device is off" (exit 0) and
    # "driver problem" (exit 1), so it has to actually expire.
    import time

    from support import fake_proc

    proc = fake_proc(tmp_path / "proc")
    started = time.monotonic()
    assert discovery.wait_for_device("2a39:3fd9", "Fireface UCX II", "",
                                       0.5, proc) is None
    assert time.monotonic() - started >= 0.4


def test_wait_for_device_tolerates_a_missing_proc_file(session_mod, tmp_path):
    # snd_seq not loaded yet: the file simply is not there.
    assert discovery.wait_for_device("2a39:3fd9", "Fireface UCX II", "",
                                       0.3, tmp_path / "nothing") is None
