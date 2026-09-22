"""Two real processes switching at once (second outside review).

The lock, the marker's temporary file and the path both are found under
are each computed by the process that uses them. Threads in one process
share an environment and an interpreter; two `oscmix-session --profile`
started together do not, and that is the case the lock exists for.
"""

import os
import socket
import subprocess
import sys
import threading

import pytest
from support import free_udp_port, proc_with_ports, write_config
from test_session_integration import (
    SESSION_BIN,
    _enable_subprocess_coverage,
    _guard_systemctl,
)

from oscmix_desk import osc

# A subprocess loads the checked-out source, never a mutant.
pytestmark = pytest.mark.skipif(
    bool(os.environ.get("MUTANT_UNDER_TEST")),
    reason="subprocess tests cannot observe mutants",
)

PROFILES = {"a": "[route:a]\nplayback = 1/2\noutput = 3/4\nvolume = -3.0\n",
            "b": "[route:b]\nplayback = 5/6\noutput = 7/8\nvolume = -9.0\n"}
#: Which profile a datagram belongs to, by the channels it names.
OWNER = {"/playback/1/stereo": "a", "/output/3/stereo": "a",
         "/mix/3/playback/1": "a", "/output/3/volume": "a",
         "/output/4/volume": "a",
         "/playback/5/stereo": "b", "/output/7/stereo": "b",
         "/mix/7/playback/5": "b", "/output/7/volume": "b",
         "/output/8/volume": "b"}


def test_two_switches_from_two_processes_do_not_interleave(tmp_path):
    send_port, recv_port = free_udp_port(), free_udp_port()
    path = write_config(tmp_path / "routing.conf",
                        "[osc]\nport = %d\nrecv-port = %d\n"
                        % (send_port, recv_port))
    for name, text in PROFILES.items():
        write_config(tmp_path / "profiles" / ("%s.conf" % name), text)
    sysfs = tmp_path / "sysfs" / "5-2"
    sysfs.mkdir(parents=True)
    (sysfs / "idVendor").write_text("2a39\n")
    (sysfs / "idProduct").write_text("3fd9\n")
    runtime = tmp_path / "runtime"
    runtime.mkdir(mode=0o700)

    env = dict(os.environ)
    env.pop("NOTIFY_SOCKET", None)
    env.update({
        "XDG_CONFIG_HOME": str(tmp_path / "xdg-config"),
        "XDG_RUNTIME_DIR": str(runtime),
        "OSCMIX_LOCK_DIR": str(tmp_path / "no-shared-lock-dir"),
        "OSCMIX_SYSTEM_CONFIG": str(tmp_path / "no-system-config"),
        "OSCMIX_PROC_ROOT": str(proc_with_ports(tmp_path / "proc", send_port)),
        "OSCMIX_SYSFS_USB": str(tmp_path / "sysfs"),
        "OSCMIX_SEQ_DEV": str(tmp_path / "no-such-seq-device"),
        # The barrier is what gives two unserialised switches the room to
        # interleave: links, a wait, then the mix.
        "OSCMIX_LINK_TIMEOUT": "0.2",
        "OSCMIX_LINK_SETTLE": "0.3",
    })
    _enable_subprocess_coverage(env)
    _guard_systemctl(tmp_path, env)

    # The backend's port, and the receive port held the way the mixer GUI
    # holds it: both switches are applied and not read back, at once.
    wire = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    wire.bind(("127.0.0.1", send_port))
    wire.settimeout(0.2)
    held = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    held.bind(("127.0.0.1", recv_port))
    seen, stop = [], threading.Event()

    def record():
        while not stop.is_set():
            try:
                datagram, _ = wire.recvfrom(65536)
            except socket.timeout:
                continue
            for message in osc.iter_osc_messages(datagram):
                seen.append(osc.decode_osc(message)[0])

    recorder = threading.Thread(target=record, daemon=True)
    recorder.start()
    try:
        switches = [subprocess.Popen(
            [sys.executable, str(SESSION_BIN), "--config", str(path),
             "--profile", name],
            env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            for name in PROFILES]
        results = [(p.wait(timeout=60), p.stdout.read(), p.stderr.read())
                   for p in switches]
    finally:
        stop.set()
        recorder.join(timeout=5)
        wire.close()
        held.close()

    assert [code for code, _out, _err in results] == [0, 0], results
    owners = [OWNER[path] for path in seen if path in OWNER]
    assert sorted(set(owners)) == ["a", "b"], seen
    assert len(owners) == len(OWNER), "every register of both, once"
    first = owners[0]
    boundary = owners.index("b" if first == "a" else "a")
    assert set(owners[:boundary]) == {first}
    assert first not in owners[boundary:], \
        "one switch's registers sit inside the other's: %s" % seen
    # The marker names whichever wrote last, whole, and nothing is left of
    # the temporary files the two wrote it through.
    assert (tmp_path / "active-profile").read_text() == owners[-1] + "\n"
    assert sorted(p.name for p in tmp_path.iterdir()
                  if p.name.startswith("active-profile")) == ["active-profile"]
