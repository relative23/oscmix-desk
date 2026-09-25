"""Two real processes switching at once (second outside review).

The lock, the marker's temporary file and the path both are found under
are each computed by the process that uses them. Threads in one process
share an environment and an interpreter; two `oscmix-session --profile`
started together do not, and that is the case the lock exists for.
"""

import os
import subprocess
import sys
from pathlib import Path

import pytest
from support import fake_proc, write_config
from test_session_integration import SESSION_BIN, _enable_subprocess_coverage, _guard_systemctl

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


PROFILE_PROCESS = """\
import os, runpy, sys, time
sys.path[:0] = [os.environ['STUB_SOURCE'], os.environ['STUB_TESTS']]
from backend_doubles import RecordingBackend
from oscmix_desk import profiles, osc
from oscmix_desk.backend import OSCMIX

class IndependentRecorder(RecordingBackend):
    traits = OSCMIX
    def __init__(self):
        super().__init__(reports=lambda sent: iter(sent))
    def send(self, messages):
        for message in messages:
            super().send([message])
            with open(os.environ['STUB_TRAFFIC'], 'a') as log:
                log.write(osc.encode_osc(message[0], message[1], *message[2]).hex() + '\\n')
            time.sleep(.05)

profiles.connect_backend = lambda *a, **k: IndependentRecorder()
runpy.run_path(os.environ['STUB_ENTRY'], run_name='__main__')
"""


def test_two_switches_from_two_processes_do_not_interleave(tmp_path):
    path = write_config(tmp_path / "routing.conf", "")
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
        "OSCMIX_PROC_ROOT": str(fake_proc(tmp_path / "proc", boxes=[(24, "24216011")])),
        "OSCMIX_SYSFS_USB": str(tmp_path / "sysfs"),
        "OSCMIX_SEQ_DEV": str(tmp_path / "no-such-seq-device"),
        # The barrier is what gives two unserialised switches the room to
        # interleave: links, a wait, then the mix.
        "OSCMIX_LINK_TIMEOUT": "0.2",
    })
    _enable_subprocess_coverage(env)
    _guard_systemctl(tmp_path, env)

    # Isolate the device file lock from the backend lease. The two processes
    # use the real CLI, planner, verifier and marker, but each has an independent
    # recording connection. A coordinating fake could otherwise mask a broken
    # file lock by serialising the clients itself. Real backend lease exclusion
    # is checked separately against the C backend in backend_control.py.
    traffic = tmp_path / "traffic.hex"
    env.update(STUB_TRAFFIC=str(traffic), STUB_SOURCE=str(SESSION_BIN.parent.parent / "src"),
               STUB_TESTS=str(Path(__file__).parent), STUB_ENTRY=str(SESSION_BIN))
    wrapper = tmp_path / "profile-process.py"
    wrapper.write_text(PROFILE_PROCESS)
    switches = []
    try:
        switches = [subprocess.Popen(
            [sys.executable, str(wrapper), "--config", str(path), "--profile", name],
            env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            for name in PROFILES]
        results = [p.communicate(timeout=30) for p in switches]
        results = [(p.returncode, out, err) for p, (out, err) in zip(switches, results)]
    finally:
        for child in switches:
            if child.poll() is None:
                child.kill()
            child.wait(timeout=3)
    seen = [osc.decode_osc(bytes.fromhex(line))[0] for line in traffic.read_text().splitlines()]

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
