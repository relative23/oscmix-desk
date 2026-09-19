"""`--diff`: the plan printed instead of sent.

The reconciler already answers "what would an apply write" -- `plan()`
is what the session runs on every start. This prints its result. The
test that matters most is the one asserting the device is never written
to: a diagnostic that changes what it inspects is worse than none.

Shares the FakeBackend from the dump-config tests, because the two
commands ask the device the same question and differ only in what they
do with the answer.
"""

import socket
from pathlib import Path

from support import fake_proc, free_udp_port, osc_bundle
from test_dump_config_cli import FakeBackend, dump_of
from two_boxes import A, B

from oscmix_desk import cli
from oscmix_desk import reads as reads_mod
from oscmix_desk.discovery import Device

CONFIG = ("[device]\nname = Fireface UCX II\n\n"
          "[route:main]\nplayback = 1/2\noutput = 5/6\nlevel = 0.0\n\n"
          "[input:3]\ngain = 12.0\n")


def run_diff(session_mod, capsys, tmp_path, registers, *, hold_port=False):
    """Returns (exit code, stdout, every message the backend received)."""
    send_port, recv_port = free_udp_port(), free_udp_port()
    path = tmp_path / "routing.conf"
    path.write_text(CONFIG + "\n[osc]\nport = %d\nrecv-port = %d\n"
                    % (send_port, recv_port))
    config = session_mod.load_config(path)

    blocker = None
    if hold_port:
        blocker = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        blocker.bind(("127.0.0.1", recv_port))

    backend = RecordingBackend(session_mod, send_port, recv_port,
                               dump_of(session_mod, registers))
    backend.start()
    try:
        code = cli._diff(config)
    finally:
        backend.stop()
        backend.join(timeout=3)
        backend.sock.close()
        if blocker is not None:
            blocker.close()
    return code, capsys.readouterr().out, backend.received


class RecordingBackend(FakeBackend):
    """FakeBackend that also keeps every address it was sent."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.received = []

    def run(self):
        while not self.stopping.is_set():
            try:
                data, _ = self.sock.recvfrom(65536)
            except (socket.timeout, OSError):
                if self.stopping.is_set():
                    return
                continue
            for message in self.session_mod.iter_osc_messages(data):
                try:
                    path, _t, _a = self.session_mod.decode_osc(message)
                except ValueError:
                    continue
                self.received.append(path)
                if path == "/refresh":
                    self.sock.sendto(osc_bundle(self.dump),
                                     ("127.0.0.1", self.recv_port))


#: What a device would report if it already held the config above.
IN_SYNC = [
    ("/output/5/stereo", "i", (1,)),
    ("/playback/1/stereo", "i", (1,)),
    ("/mix/5/input/1", "fi", (0.0, 0)),
    ("/input/3/gain", "f", (12.0,)),
    ("/output/5/volume", "f", (0.0,)),
    ("/output/6/volume", "f", (0.0,)),
]

DRIFTED = [(path, tags, (3.0,) if path == "/input/3/gain" else args)
           for path, tags, args in IN_SYNC]


# --------------------------------------------------------------------------
# The property the whole command rests on.
# --------------------------------------------------------------------------

def test_it_writes_nothing_to_the_device(session_mod, capsys, tmp_path):
    """A diagnostic that changes what it inspects is worse than none.

    Asserted against every address the backend saw, not against a mock's
    call list -- the question is what reached the wire.
    """
    _code, _out, received = run_diff(session_mod, capsys, tmp_path, DRIFTED)
    assert received, "the backend saw nothing at all -- the test proves nothing"
    assert set(received) == {"/refresh"}


def test_a_drifted_register_is_reported_with_both_values(session_mod, capsys,
                                                         tmp_path):
    code, out, _ = run_diff(session_mod, capsys, tmp_path, DRIFTED)
    assert code == 3
    assert "/input/3/gain" in out
    assert "12.0" in out          # what the config asks for
    assert "3.0" in out           # what the device holds
    assert "mismatched" in out


def test_a_device_that_matches_says_so_plainly(session_mod, capsys, tmp_path):
    code, out, _ = run_diff(session_mod, capsys, tmp_path, IN_SYNC)
    assert code == 0
    assert "the device matches the config" in out
    assert "mismatched" not in out


# --------------------------------------------------------------------------
# A rewrite is not a difference.
# --------------------------------------------------------------------------

def test_the_playback_matrix_is_counted_apart_from_real_differences():
    """`/mix/<out>/playback/<pb>` is never reported (ADR 0002), so it is
    written on every apply whatever the device holds. Listing it as a
    difference would answer "has the desk drifted?" with a number that
    is never zero.
    """
    from oscmix_desk.devices import UCX2
    from oscmix_desk.reconcile import REWRITE, desired, plan

    config_paths = {"/mix/5/playback/1"}
    entries = [e for e in desired(_config()) if e.path in config_paths]
    assert entries, "no playback matrix entry to judge"
    result = plan(entries, {}, UCX2)
    assert all(w.reason == REWRITE for w in result.writes)


def _config():
    from oscmix_desk.model import Config, Route
    return Config(device_name="Fireface UCX II",
                  routes=[Route(name="main", playback=(1, 2), output=(5, 6),
                                level=0.0)])


def test_the_rewrites_are_named_in_the_output(session_mod, capsys, tmp_path):
    _code, out, _ = run_diff(session_mod, capsys, tmp_path, IN_SYNC)
    assert "rewritten regardless" in out
    assert "ADR 0002" in out


# --------------------------------------------------------------------------
# Failure paths, which are the ones a user meets first.
# --------------------------------------------------------------------------

def test_a_held_receive_port_is_refused_rather_than_half_read(session_mod,
                                                              capsys,
                                                              tmp_path):
    code, out, _ = run_diff(session_mod, capsys, tmp_path, IN_SYNC,
                            hold_port=True)
    assert code == 1
    assert out == ""


def test_silence_is_an_error_not_an_empty_diff(session_mod, capsys, tmp_path,
                                               monkeypatch):
    """"nobody answered" and "nothing differs" are opposite situations and
    must not print the same thing.

    The window is shortened because this test's whole point is that it
    expires -- paying the real 8 s to learn that is what pushed the CI
    mutation job past its limit once already.
    """
    monkeypatch.setattr(reads_mod, "DUMP_READ_SECONDS", 0.6)
    code, out, _ = run_diff(session_mod, capsys, tmp_path, [])
    assert code == 1
    assert "matches the config" not in out


# --------------------------------------------------------------------------
# `--snapshot`: what a dump cannot show.
# --------------------------------------------------------------------------

def run_snapshot(session_mod, capsys, tmp_path, registers):
    send_port, recv_port = free_udp_port(), free_udp_port()
    path = tmp_path / "routing.conf"
    path.write_text(CONFIG + "\n[osc]\nport = %d\nrecv-port = %d\n"
                    % (send_port, recv_port))
    config = session_mod.load_config(path)
    backend = RecordingBackend(session_mod, send_port, recv_port,
                               dump_of(session_mod, registers))
    backend.start()
    try:
        code = cli._snapshot(config)
    finally:
        backend.stop()
        backend.join(timeout=3)
        backend.sock.close()
    return code, capsys.readouterr().out


#: A device that reports a link flag, a level meter and a setting.
MIXED = [
    ("/output/9/stereo", "i", (1,)),          # no value domain: no dump line
    ("/output/5/volume", "f", (0.0,)),        # a setting: dumped
    ("/output/5/level", "f", (-42.0,)),       # streams: excluded
    ("/input/1/48v", "i", (0,)),              # withheld from configs
]


def test_it_carries_what_a_config_cannot_express(session_mod, capsys, tmp_path):
    """The reason this exists, and it was found the hard way.

    A measurement left `/output/9/stereo` unlinked on a working desk and
    two `--dump-config` runs compared equal, because `stereo` has no
    value domain and no dump ever carried it. The link flags are the
    register class that produced every defect in 0.1.3, so a restoration
    proof blind to them is not one.
    """
    code, out = run_snapshot(session_mod, capsys, tmp_path, MIXED)
    assert code == 0
    assert "/output/9/stereo 1" in out
    assert "/input/1/48v 0" in out
    assert "/output/5/volume 0.0" in out


def test_streaming_registers_are_left_out(session_mod, capsys, tmp_path):
    """A meter changes between any two reads. Including it would make
    every comparison differ and none of them mean anything."""
    _code, out = run_snapshot(session_mod, capsys, tmp_path, MIXED)
    assert "/output/5/level" not in out
    assert "streaming and left out" not in out      # that line goes to the log


def test_the_output_is_sorted_so_two_runs_can_be_diffed(session_mod, capsys,
                                                        tmp_path):
    _code, out = run_snapshot(session_mod, capsys, tmp_path, MIXED)
    paths = [line.split(" ")[0] for line in out.splitlines()
             if not line.startswith("#")]
    assert paths == sorted(paths)


def test_silence_is_an_error_not_an_empty_snapshot(session_mod, capsys,
                                                   tmp_path, monkeypatch):
    monkeypatch.setattr(reads_mod, "DUMP_READ_SECONDS", 0.6)
    code, out = run_snapshot(session_mod, capsys, tmp_path, [])
    assert code == 1
    assert out == ""


# --------------------------------------------------------------------------
# The exit-code contract, which is the reason --diff is scriptable at all.
# --------------------------------------------------------------------------

def test_the_three_outcomes_have_three_codes(session_mod, capsys, tmp_path,
                                             monkeypatch):
    """0 matches, 3 differs, 1 nothing was learned.

    `diff(1)` uses 1 for "differing" and that is not available: 1 already
    means EXIT_FAILURE here. Conflating them makes a monitoring check
    report healthy silence while the backend is down, which is the one
    outcome such a check exists to prevent.
    """
    from oscmix_desk.constants import EXIT_CONFIG, EXIT_DIFFERS, EXIT_FAILURE, EXIT_OK

    assert (EXIT_OK, EXIT_FAILURE, EXIT_CONFIG, EXIT_DIFFERS) == (0, 1, 2, 3)

    # mutmut re-runs a covering test once per mutant, so a second spent
    # waiting here is paid thousands of times. The read window and the
    # quiet detection are what this test would otherwise sit through
    # three times over, and neither is what it is about.
    monkeypatch.setattr(reads_mod, "DUMP_QUIET_SECONDS", 0.15)
    matched, _out, _ = run_diff(session_mod, capsys, tmp_path, IN_SYNC)
    differed, _out, _ = run_diff(session_mod, capsys, tmp_path, DRIFTED)
    monkeypatch.setattr(reads_mod, "DUMP_READ_SECONDS", 0.6)
    silent, _out, _ = run_diff(session_mod, capsys, tmp_path, [])

    assert (matched, differed, silent) == (EXIT_OK, EXIT_DIFFERS, EXIT_FAILURE)


def test_a_rewrite_alone_does_not_make_it_differ(session_mod, capsys,
                                                 tmp_path):
    """`/mix/<out>/playback/<pb>` is never reported and is written on
    every apply. Counting it would pin the exit code at 3 for ever and
    make it worth nothing."""
    code, out, _ = run_diff(session_mod, capsys, tmp_path, IN_SYNC)
    assert "rewritten regardless" in out      # there are some
    assert code == 0                          # and they do not count


def test_only_diff_can_return_it(session_mod, capsys, tmp_path):
    """The service runs `oscmix-session` with no flag, so systemd never
    sees a 3. If it ever did, `Restart=on-failure` would treat it as a
    failure, which is the safe direction for "the state is not what was
    asked for"."""
    code, _out = run_snapshot(session_mod, capsys, tmp_path, MIXED)
    assert code == 0


def test_two_port_draws_never_collide():
    """send == recv is a backend answering itself and a CLI reading
    silence; free_udp_port now remembers its recent draws, and this
    holds it to that."""
    from support import free_udp_port

    for _ in range(500):
        assert free_udp_port() != free_udp_port()


def test_a_snapshot_names_the_resolved_box(tmp_path, monkeypatch):
    from oscmix_desk import Config

    one = fake_proc(tmp_path / "one", boxes=[B])
    monkeypatch.setenv("OSCMIX_PROC_ROOT", str(one))
    assert reads_mod._snapshot_serial(Config()) == B[1]
    two = fake_proc(tmp_path / "two", boxes=[A, B])
    monkeypatch.setenv("OSCMIX_PROC_ROOT", str(two))
    assert reads_mod._snapshot_serial(Config()) == "ambiguous"
    assert reads_mod._snapshot_serial(Config(serial=A[1])) == A[1]
    monkeypatch.setenv("OSCMIX_PROC_ROOT", str(fake_proc(tmp_path / "none")))
    assert reads_mod._snapshot_serial(Config()) == "?"

def test_a_snapshot_names_the_box_its_backend_drives(tmp_path, monkeypatch):
    from oscmix_desk import Config

    port = free_udp_port()
    proc = fake_proc(tmp_path, boxes=[A, B], bound=[(port, "oscmix", A[0])])
    monkeypatch.setenv("OSCMIX_PROC_ROOT", str(proc))
    assert reads_mod._snapshot_serial(Config(serial=B[1], osc_port=port)) == A[1]

def test_a_snapshot_reads_the_real_proc_by_default(monkeypatch):

    from oscmix_desk import Config

    seen = []
    monkeypatch.delenv("OSCMIX_PROC_ROOT", raising=False)
    monkeypatch.setattr(reads_mod, "port_holder",
                        lambda port, proc: seen.append(proc) or None)
    monkeypatch.setattr(reads_mod, "resolve_device",
                        lambda usb, name, serial, proc: seen.append(proc)
                        or Device(usb, B[1], B[0]))
    assert reads_mod._snapshot_serial(Config()) == B[1]
    assert seen == [Path("/proc"), Path("/proc")]
