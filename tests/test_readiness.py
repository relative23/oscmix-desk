"""UDP port detection via /proc/net/udp (no ss/netstat dependency)."""

# Real /proc/net/udp format; 0x1C36 == 7222.
UDP_WITH_OSCMIX = """\
  sl  local_address rem_address   st tx_queue rx_queue tr tm->when retrnsmt   uid  timeout inode ref pointer drops
  100: 0100007F:1C36 00000000:0000 07 00000000:00000000 00:00000000 00000000  1000        0 123456 2 0000000000000000 0
  101: 00000000:0044 00000000:0000 07 00000000:00000000 00:00000000 00000000     0        0 654321 2 0000000000000000 0
"""

UDP_WITHOUT_OSCMIX = """\
  sl  local_address rem_address   st tx_queue rx_queue tr tm->when retrnsmt   uid  timeout inode ref pointer drops
  101: 00000000:0044 00000000:0000 07 00000000:00000000 00:00000000 00000000     0        0 654321 2 0000000000000000 0
"""

UDP6_WITH_OSCMIX = """\
  sl  local_address                         remote_address                        st tx_queue rx_queue tr tm->when retrnsmt   uid  timeout inode ref pointer drops
  200: 00000000000000000000000001000000:1C36 00000000000000000000000000000000:0000 07 00000000:00000000 00:00000000 00000000  1000        0 999999 2 0000000000000000 0
"""


def make_proc(tmp_path, udp=None, udp6=None):
    net = tmp_path / "proc" / "net"
    net.mkdir(parents=True)
    if udp is not None:
        (net / "udp").write_text(udp)
    if udp6 is not None:
        (net / "udp6").write_text(udp6)
    return tmp_path / "proc"


def test_detects_listening_port(session_mod, tmp_path):
    proc = make_proc(tmp_path, udp=UDP_WITH_OSCMIX)
    assert session_mod.udp_port_listening(7222, proc) is True


def test_ignores_other_ports(session_mod, tmp_path):
    proc = make_proc(tmp_path, udp=UDP_WITHOUT_OSCMIX)
    assert session_mod.udp_port_listening(7222, proc) is False


def test_detects_ipv6_socket(session_mod, tmp_path):
    proc = make_proc(tmp_path, udp=UDP_WITHOUT_OSCMIX, udp6=UDP6_WITH_OSCMIX)
    assert session_mod.udp_port_listening(7222, proc) is True


def test_missing_proc_files(session_mod, tmp_path):
    proc = tmp_path / "proc"
    proc.mkdir()
    assert session_mod.udp_port_listening(7222, proc) is False


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
    assert session_mod.find_stale_backends(proc) == [101, 103]


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
        assert session_mod.find_stale_backends(proc) == []
    finally:
        process.Path.stat = real_stat


def test_wait_for_seq_client_returns_the_client_number(session_mod, tmp_path):
    proc = tmp_path / "proc"
    (proc / "asound" / "seq").mkdir(parents=True)
    (proc / "asound" / "seq" / "clients").write_text(
        'Client info\n\nClient  24 : "Fireface UCX II (0)" [Kernel]\n')
    assert session_mod.wait_for_seq_client("Fireface UCX II", 1.0, proc) == 24


def test_wait_for_seq_client_gives_up_and_says_so(session_mod, tmp_path):
    # The timeout is the difference between "device is off" (exit 0) and
    # "driver problem" (exit 1), so it has to actually expire.
    import time

    proc = tmp_path / "proc"
    (proc / "asound" / "seq").mkdir(parents=True)
    (proc / "asound" / "seq" / "clients").write_text("Client info\n")
    started = time.monotonic()
    assert session_mod.wait_for_seq_client("Fireface UCX II", 0.5, proc) is None
    assert time.monotonic() - started >= 0.4


def test_wait_for_seq_client_tolerates_a_missing_proc_file(session_mod,
                                                           tmp_path):
    # snd_seq not loaded yet: the file simply is not there.
    assert session_mod.wait_for_seq_client("Fireface UCX II", 0.3,
                                           tmp_path / "nothing") is None


# --------------------------------------------------------------------------
# The inode behind the port (ADR 0021).
# --------------------------------------------------------------------------

def test_the_inode_of_the_bound_socket_is_found(session_mod, tmp_path):
    # Column ten of /proc/net/udp, and it is what ties the port to the
    # process that holds it.
    from oscmix_desk.discovery import udp_socket_inodes

    proc = make_proc(tmp_path, udp=UDP_WITH_OSCMIX, udp6=UDP6_WITH_OSCMIX)
    assert udp_socket_inodes(7222, proc) == {"123456", "999999"}
    assert udp_socket_inodes(68, proc) == {"654321"}
    assert udp_socket_inodes(9999, proc) == set()


def test_a_truncated_row_is_skipped_rather_than_believed(session_mod,
                                                         tmp_path):
    """A line without the inode column says nothing about ownership.

    Reading it as a match would hand the cleanup a name it cannot
    resolve, and the cleanup signals processes.
    """
    from oscmix_desk.discovery import udp_socket_inodes

    header = UDP_WITH_OSCMIX.splitlines()[0]
    proc = make_proc(tmp_path, udp=header + "\n  100: 0100007F:1C36 x\n")
    assert udp_socket_inodes(7222, proc) == set()


def test_a_local_address_that_is_not_hex_is_skipped(session_mod, tmp_path):
    from oscmix_desk.discovery import udp_socket_inodes

    header = UDP_WITH_OSCMIX.splitlines()[0]
    row = ("  100: 0100007F:ZZZZ 00000000:0000 07 00000000:00000000 "
           "00:00000000 00000000  1000        0 123456 2 0 0\n")
    proc = make_proc(tmp_path, udp=header + "\n" + row)
    assert udp_socket_inodes(7222, proc) == set()
