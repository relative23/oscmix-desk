"""`--diff`: the plan printed instead of sent.

The reconciler already answers "what would an apply write" -- `plan()`
is what the session runs on every start. This prints its result. The
test that matters most is the one asserting the device is never written
to: a diagnostic that changes what it inspects is worse than none.

Shares a scripted ODK1 peer with the dump-config tests. Requests and
observations cross the real client, including the read-only reader role.
"""

from control_peer import HELLO, REFRESH
from support import free_udp_port

from oscmix_desk import cli
from oscmix_desk import reads as reads_mod

CONFIG = ("[device]\nname = Fireface UCX II\n\n"
          "[route:main]\nplayback = 1/2\noutput = 5/6\nlevel = 0.0\n\n"
          "[input:3]\ngain = 12.0\n")


def run_diff(session_mod, capsys, tmp_path, registers, *, read_peer, disconnect=False):
    """Returns exit code, stdout and every request the real client emitted."""
    path = tmp_path / "routing.conf"
    path.write_text(CONFIG)
    config = session_mod.load_config(path)
    with read_peer(registers, lose_reply=REFRESH if disconnect else 0) as peer:
        code = cli._diff(config, path)
    assert peer.writes == []
    return code, capsys.readouterr().out, peer.requests


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

def test_it_writes_nothing_to_the_device(session_mod, capsys, tmp_path, read_peer):
    """A diagnostic that changes what it inspects is worse than none.

    Asserted against every address the backend saw, not against a mock's
    call list -- the question is what reached the wire.
    """
    _code, _out, received = run_diff(session_mod, capsys, tmp_path, DRIFTED, read_peer=read_peer)
    assert received, "the backend saw nothing at all -- the test proves nothing"
    assert received == [HELLO, REFRESH]


def test_a_drifted_register_is_reported_with_both_values(session_mod, capsys,
                                                         tmp_path, read_peer):
    code, out, _ = run_diff(session_mod, capsys, tmp_path, DRIFTED, read_peer=read_peer)
    assert code == 3
    assert "/input/3/gain" in out
    assert "12.0" in out          # what the config asks for
    assert "3.0" in out           # what the device holds
    assert "mismatched" in out


def test_a_device_that_matches_says_so_plainly(session_mod, capsys, tmp_path, read_peer):
    code, out, _ = run_diff(session_mod, capsys, tmp_path, IN_SYNC, read_peer=read_peer)
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


def test_the_rewrites_are_named_in_the_output(session_mod, capsys, tmp_path, read_peer):
    _code, out, _ = run_diff(session_mod, capsys, tmp_path, IN_SYNC, read_peer=read_peer)
    assert "rewritten regardless" in out
    assert "ADR 0002" in out


# --------------------------------------------------------------------------
# Failure paths, which are the ones a user meets first.
# --------------------------------------------------------------------------

def test_a_lost_refresh_reply_cannot_render_a_partial_diff(session_mod,
                                                              capsys,
                                                              tmp_path, read_peer):
    code, out, _ = run_diff(session_mod, capsys, tmp_path, IN_SYNC,
                            disconnect=True, read_peer=read_peer)
    assert code == 1
    assert out == ""


def test_silence_is_an_error_not_an_empty_diff(session_mod, capsys, tmp_path,
                                               monkeypatch, read_peer):
    """"nobody answered" and "nothing differs" are opposite situations and
    must not print the same thing.

    The window is shortened because this test's whole point is that it
    expires -- paying the real 8 s to learn that is what pushed the CI
    mutation job past its limit once already.
    """
    monkeypatch.setattr(reads_mod, "DUMP_READ_SECONDS", 0.6)
    code, out, _ = run_diff(session_mod, capsys, tmp_path, [], read_peer=read_peer)
    assert code == 1
    assert "matches the config" not in out


# --------------------------------------------------------------------------
# `--snapshot`: what a dump cannot show.
# --------------------------------------------------------------------------

def run_snapshot(session_mod, capsys, tmp_path, registers, *, read_peer):
    path = tmp_path / "routing.conf"
    path.write_text(CONFIG)
    config = session_mod.load_config(path)
    with read_peer(registers):
        code = cli._snapshot(config, path)
    return code, capsys.readouterr().out


#: A device that reports a link flag, a level meter and a setting.
MIXED = [
    ("/output/9/stereo", "i", (1,)),          # no value domain: no dump line
    ("/output/5/volume", "f", (0.0,)),        # a setting: dumped
    ("/output/5/level", "f", (-42.0,)),       # streams: excluded
    ("/input/1/48v", "i", (0,)),              # withheld from configs
]


def test_it_carries_what_a_config_cannot_express(session_mod, capsys, tmp_path, read_peer):
    """The reason this exists, and it was found the hard way.

    A measurement left `/output/9/stereo` unlinked on a working desk and
    two `--dump-config` runs compared equal, because `stereo` has no
    value domain and no dump ever carried it. The link flags are the
    register class that produced every defect in 0.1.3, so a restoration
    proof blind to them is not one.
    """
    code, out = run_snapshot(session_mod, capsys, tmp_path, MIXED, read_peer=read_peer)
    assert code == 0
    assert "/output/9/stereo 1" in out
    assert "/input/1/48v 0" in out
    assert "/output/5/volume 0.0" in out


def test_streaming_registers_are_left_out(session_mod, capsys, tmp_path, read_peer):
    """A meter changes between any two reads. Including it would make
    every comparison differ and none of them mean anything."""
    _code, out = run_snapshot(session_mod, capsys, tmp_path, MIXED, read_peer=read_peer)
    assert "/output/5/level" not in out
    assert "streaming and left out" not in out      # that line goes to the log


def test_the_output_is_sorted_so_two_runs_can_be_diffed(session_mod, capsys,
                                                        tmp_path, read_peer):
    _code, out = run_snapshot(session_mod, capsys, tmp_path, MIXED, read_peer=read_peer)
    paths = [line.split(" ")[0] for line in out.splitlines()
             if not line.startswith("#")]
    assert paths == sorted(paths)


def test_silence_is_an_error_not_an_empty_snapshot(session_mod, capsys,
                                                   tmp_path, monkeypatch, read_peer):
    monkeypatch.setattr(reads_mod, "DUMP_READ_SECONDS", 0.6)
    code, out = run_snapshot(session_mod, capsys, tmp_path, [], read_peer=read_peer)
    assert code == 1
    assert out == ""


# --------------------------------------------------------------------------
# The exit-code contract, which is the reason --diff is scriptable at all.
# --------------------------------------------------------------------------

def test_the_three_outcomes_have_three_codes(session_mod, capsys, tmp_path,
                                             monkeypatch, read_peer):
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
    matched, _out, _ = run_diff(session_mod, capsys, tmp_path, IN_SYNC, read_peer=read_peer)
    differed, _out, _ = run_diff(session_mod, capsys, tmp_path, DRIFTED, read_peer=read_peer)
    monkeypatch.setattr(reads_mod, "DUMP_READ_SECONDS", 0.6)
    silent, _out, _ = run_diff(session_mod, capsys, tmp_path, [], read_peer=read_peer)

    assert (matched, differed, silent) == (EXIT_OK, EXIT_DIFFERS, EXIT_FAILURE)


def test_a_rewrite_alone_does_not_make_it_differ(session_mod, capsys,
                                                 tmp_path, read_peer):
    """`/mix/<out>/playback/<pb>` is never reported and is written on
    every apply. Counting it would pin the exit code at 3 for ever and
    make it worth nothing."""
    code, out, _ = run_diff(session_mod, capsys, tmp_path, IN_SYNC, read_peer=read_peer)
    assert "rewritten regardless" in out      # there are some
    assert code == 0                          # and they do not count


def test_only_diff_can_return_it(session_mod, capsys, tmp_path, read_peer):
    """The service runs `oscmix-session` with no flag, so systemd never
    sees a 3. If it ever did, `Restart=on-failure` would treat it as a
    failure, which is the safe direction for "the state is not what was
    asked for"."""
    code, _out = run_snapshot(session_mod, capsys, tmp_path, MIXED, read_peer=read_peer)
    assert code == 0


def test_two_port_draws_never_collide():
    """send == recv is a backend answering itself and a CLI reading
    silence; free_udp_port now remembers its recent draws, and this
    holds it to that."""

    for _ in range(500):
        assert free_udp_port() != free_udp_port()


def test_snapshot_names_the_connected_box_and_epoch(capsys, read_peer):
    from oscmix_desk import Config

    # This must come from the connection that produced these observations,
    # never from a second ALSA/proc lookup after the read has finished.
    with read_peer(MIXED):
        assert reads_mod._snapshot(Config()) == 0
    out = capsys.readouterr().out
    assert 'serial 00000000' in out
    assert '000102030405060708090a0b0c0d0e0f' in out
