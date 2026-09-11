"""The restart soak: apply the routing N times, assert the result N times.

Roadmap item A. *Proven by* has said "soak on main" since the first
draft of the roadmap and nothing in the repository ran one, so the claim
was carried by a Makefile target that did not exist.

Every failure mode this project has actually found was a timing bug --
the link race, two teardown races, the stub signal race -- and each one
survived a green single run. A soak is the only gate shaped like those
defects: it does not test a new path, it runs the same path until
something that leaks a socket, a thread or a stale barrier state falls
over on a later cycle.

The cycle count comes from OSCMIX_SOAK_CYCLES so the scheduled workflow
can turn it up without a second copy of the test. It defaults to a small
number rather than to zero: a soak that skips itself by default is the
silent-skip problem again, and two cycles still catch anything that
cannot survive being started twice.
"""

import json
import os
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest
from conftest import free_udp_port, read_until_ready
from test_session_integration import (
    ROUTING_CONF,
    SESSION_BIN,
    make_env,
    terminate,
    wait_for,
)

pytestmark = pytest.mark.skipif(
    bool(os.environ.get("MUTANT_UNDER_TEST")),
    reason="subprocess tests cannot observe mutants",
)

DEFAULT_CYCLES = 2


def soak_cycles():
    """How many restarts this run performs.

    Kept a function rather than a module constant so a mis-set value
    fails loudly at collection with the value it saw, instead of
    silently soaking zero times.
    """
    raw = os.environ.get("OSCMIX_SOAK_CYCLES")
    if raw is None:
        return DEFAULT_CYCLES
    try:
        cycles = int(raw)
    except ValueError as exc:
        raise ValueError(
            "OSCMIX_SOAK_CYCLES is not an integer: %r" % raw) from exc
    if cycles < 1:
        raise ValueError("OSCMIX_SOAK_CYCLES must be at least 1, got %d"
                         % cycles)
    return cycles


def one_startup(tmp_path, session_mod, cycle):
    """One full start -> READY -> verify -> SIGTERM -> exit 0 cycle.

    Returns nothing and asserts everything: the point of a soak is that
    cycle 40 is held to the same standard as cycle 1, so the assertions
    live here rather than in the caller.
    """
    work = tmp_path / ("cycle-%03d" % cycle)
    work.mkdir()
    port, recv_port = free_udp_port(), free_udp_port()
    env, stub_dir, backend = make_env(
        work, with_client=True, with_usb=True, port=port, reply_port=recv_port,
    )
    config = work / "routing.conf"
    config.write_text(ROUTING_CONF.format(port=port, recv_port=recv_port))

    notify = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    notify.bind(str(work / "notify.sock"))
    notify.settimeout(15)
    env["NOTIFY_SOCKET"] = str(work / "notify.sock")

    proc = subprocess.Popen(
        [sys.executable, str(SESSION_BIN), "--config", str(config),
         "--timeout", "5"],
        env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    try:
        datagram_log = stub_dir / "datagrams.hex"
        assert wait_for(
            lambda: datagram_log.exists()
            and len(datagram_log.read_text().splitlines()) >= 9,
            timeout=20.0,
        ), "cycle %d: routing + verification traffic did not arrive" % cycle

        # The whole point: the same registers, in the same order, every
        # cycle. A leak that leaves the barrier in a stale state shows up
        # here as reordering rather than as a crash.
        mix = [
            session_mod.encode_osc("/mix/5/playback/1", "fi", 0.0, 0),
            session_mod.encode_osc("/output/5/volume", "f", 0.0),
            session_mod.encode_osc("/output/6/volume", "f", 0.0),
        ]
        expected = [
            session_mod.encode_osc("/playback/1/stereo", "i", 1),
            session_mod.encode_osc("/output/5/stereo", "i", 1),
        ] + mix + [session_mod.encode_osc("/refresh")] + mix
        received = [bytes.fromhex(line)
                    for line in datagram_log.read_text().splitlines()]
        assert received[:9] == expected, "cycle %d: routing diverged" % cycle

        assert read_until_ready(notify) == b"READY=1", \
            "cycle %d: readiness was not signalled" % cycle

        # The ports reaching the backend are part of the result, not a
        # detail: a cycle that resolved a stale port would still route.
        argv = json.loads((stub_dir / "argv.json").read_text())
        assert argv == ["42:1", str(backend),
                        "-r", "udp!127.0.0.1!%d" % port,
                        "-s", "udp!127.0.0.1!%d" % recv_port], \
            "cycle %d: backend arguments diverged" % cycle

        proc.send_signal(signal.SIGTERM)
        assert proc.wait(timeout=15) == 0, \
            "cycle %d: unclean exit" % cycle
        stderr = proc.stderr.read()
        assert "routing verified against device state" in stderr, \
            "cycle %d: verification did not confirm the routing" % cycle
    finally:
        notify.close()
        terminate(proc)


def test_the_routing_survives_being_applied_over_and_over(tmp_path,
                                                          session_mod):
    cycles = soak_cycles()
    started = time.monotonic()
    for cycle in range(1, cycles + 1):
        one_startup(tmp_path, session_mod, cycle)
    elapsed = time.monotonic() - started
    # Not a performance gate -- see docs/decisions on why this project
    # asserts growth order rather than wall-clock time. It is a hang
    # detector: a cycle that waits out a real timeout instead of the
    # stubbed one takes 30 s+, and averaging that away over many cycles
    # is exactly what a soak must not do.
    assert elapsed / cycles < 25.0, (
        "%d cycles took %.1fs (%.1fs each) -- a cycle is waiting out a "
        "timeout rather than converging" % (cycles, elapsed, elapsed / cycles)
    )


def test_the_cycle_count_is_configurable_and_never_silently_zero(monkeypatch):
    # The scheduled workflow turns this up; a typo in that workflow must
    # not quietly reduce the soak to nothing.
    monkeypatch.delenv("OSCMIX_SOAK_CYCLES", raising=False)
    assert soak_cycles() == DEFAULT_CYCLES
    monkeypatch.setenv("OSCMIX_SOAK_CYCLES", "40")
    assert soak_cycles() == 40
    for bad in ("0", "-1"):
        monkeypatch.setenv("OSCMIX_SOAK_CYCLES", bad)
        with pytest.raises(ValueError, match="at least 1"):
            soak_cycles()
    for bad in ("", "many", "2.5"):
        monkeypatch.setenv("OSCMIX_SOAK_CYCLES", bad)
        with pytest.raises(ValueError, match="not an integer"):
            soak_cycles()


def test_the_soak_drives_the_real_entry_point():
    # A soak against an in-process fake would miss every teardown race,
    # which is the class of defect it exists for.
    assert Path(SESSION_BIN).is_file()
    assert os.access(SESSION_BIN, os.X_OK)
