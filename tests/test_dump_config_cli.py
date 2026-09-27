"""`--dump-config` as the command, not just the inference.

The round trip is covered in tests/test_dump_config.py against pure
functions. This is the part that opens a socket: what it does when the
connection is lost, when nobody answers, and what it prints when the device
does.

It exists because the coverage ratchet caught the gap the moment the
feature landed -- 70% to 53% on cli.py -- which is the ratchet doing
exactly its job. The fix is covering the path, not lowering the gate.
"""

import time

import pytest
from control_peer import HELLO, REFRESH

from oscmix_desk import cli
from oscmix_desk import reads as reads_mod


def run_dump(session_mod, capsys, registers, *, read_peer, disconnect=False):
    config = session_mod.Config(device_name="Fireface UCX II")
    with read_peer(registers, lose_reply=REFRESH if disconnect else 0):
        code = cli._dump_config(config)
    return code, capsys.readouterr().out


MONITOR = [
    ("/input/1/stereo", "i", (1,)),
    ("/output/5/stereo", "i", (1,)),
    ("/mix/5/input/1", "fi", (-6.0, 0)),
]


def test_it_prints_a_config_the_parser_accepts(session_mod, capsys, tmp_path, read_peer):
    code, out = run_dump(session_mod, capsys, MONITOR, read_peer=read_peer)
    assert code == session_mod.EXIT_OK
    assert "[route:in1-2-out5-6]" in out
    assert "input = 1/2" in out
    assert "level = -6.0" in out

    # The output is a config, not a report about one.
    path = tmp_path / "routing.conf"
    path.write_text(out)
    route, = session_mod.load_config(path).routes
    assert route.source == ("input", (1, 2))
    assert route.output == (5, 6)


def test_it_says_what_it_could_not_read(session_mod, capsys, read_peer):
    _code, out = run_dump(session_mod, capsys, MONITOR, read_peer=read_peer)
    assert "does not report" in out
    assert "/mix/{out}/playback/{pb}" in out
    assert "Merge, do not replace" in out


def test_a_device_with_no_monitoring_is_not_an_error(session_mod, capsys, read_peer):
    code, out = run_dump(session_mod, capsys, [
        ("/output/5/stereo", "i", (1,)),
        ("/mix/5/input/1", "fi", (float("-inf"), 0)),
    ], read_peer=read_peer)
    assert code == session_mod.EXIT_OK
    assert "No representable input route was recovered" in out
    assert "[route:" not in out


def test_a_lost_refresh_reply_refuses_to_render_partial_state(session_mod, capsys,
                                                             caplog, read_peer):
    with caplog.at_level("ERROR"):
        code, out = run_dump(session_mod, capsys, MONITOR, disconnect=True, read_peer=read_peer)
    assert code == session_mod.EXIT_FAILURE
    assert out == ""
    assert "disconnected" in caplog.text


def test_silence_is_an_error_not_an_empty_config(session_mod, capsys, caplog,
                                                 monkeypatch, read_peer):
    monkeypatch.setattr(reads_mod, "DUMP_READ_SECONDS", 0.2)
    with caplog.at_level("ERROR"):
        code, out = run_dump(session_mod, capsys, [], read_peer=read_peer)
    assert code == session_mod.EXIT_FAILURE
    assert out == ""
    assert "no device-origin reports" in caplog.text


def test_it_stops_when_the_dump_goes_quiet(session_mod, capsys, read_peer):
    # Waiting out the whole window regardless would make the command
    # take DUMP_READ_SECONDS every time; the dump is over in ~2 s on a
    # UCX II. Measured here rather than assumed.
    started = time.monotonic()
    code, _out = run_dump(session_mod, capsys, MONITOR, read_peer=read_peer)
    elapsed = time.monotonic() - started
    assert code == session_mod.EXIT_OK
    assert elapsed < reads_mod.DUMP_READ_SECONDS, (
        "the read waited out the full window (%.1fs) instead of stopping "
        "when the dump went quiet" % elapsed)


def test_subscription_receives_the_first_dump_bundle(session_mod, read_peer):
    # ODK1 subscribes and acknowledges refresh before hardware can answer;
    # no UDP/ICMP settle wait or cached playback flag is needed for delivery.
    with read_peer(MONITOR) as peer:
        observed = reads_mod._read_device(session_mod.Config())
        assert observed.registers == {p: a for p, _t, a in MONITOR}
        assert peer.requests == [HELLO, REFRESH]
        assert peer.writes == []


def test_latest_value_in_the_read_window_replaces_earlier_report(session_mod, read_peer):
    with read_peer([('/output/5/volume', 'f', (-6.,)),
                    ('/output/5/volume', 'f', (-30.,))]):
        observed = reads_mod._read_device(session_mod.Config())
    assert observed.registers == {'/output/5/volume': (-30.,)}


@pytest.mark.parametrize('action', ['_snapshot', '_diff', '_dump_config'])
def test_read_commands_keep_the_selected_configuration_and_reader_role(
        tmp_path, read_peer, monkeypatch, action, capsys):
    from oscmix_desk.model import Config

    config = Config(osc_port=9345, osc_recv_port=9346)
    path = tmp_path / 'selected/routing.conf'
    calls = []
    with read_peer([('/input/3/gain', 'f', (12.0,))]) as peer, monkeypatch.context() as patch:
        connect = reads_mod.connect_backend

        def checked(selected, selected_path, *, reader):
            calls.append((selected, selected_path, reader))
            return connect(selected, selected_path, reader=reader)

        patch.setattr(reads_mod, 'connect_backend', checked)
        assert getattr(reads_mod, action)(config, path) == 0
    assert calls == [(config, path, True)]
    assert peer.requests == [HELLO, REFRESH]
    assert peer.writes == []
    assert capsys.readouterr().out


def test_dump_keeps_nondefault_ports_and_reconstructed_channel_and_global_values(
        session_mod, tmp_path, capsys, read_peer):
    from oscmix_desk.model import Config

    config = Config(osc_port=9345, osc_recv_port=9346)
    with read_peer([('/input/3/gain', 'f', (12.0,)),
                    ('/output/5/volume', 'f', (-9.0,)), ('/echo', 'i', (1,))]):
        assert reads_mod._dump_config(config) == 0
    text = capsys.readouterr().out
    path = tmp_path / 'export.conf'
    path.write_text(text)
    restored = session_mod.load_config(path)
    assert restored.device_name == config.device_name
    assert restored.usb_id == config.usb_id
    assert (restored.osc_port, restored.osc_recv_port) == (9345, 9346)
    assert {(s.family, s.channel, s.option): s.value for s in restored.channels} == {
        ('input', 3, 'gain'): 12.0}
    assert restored.globals == ()
    # The observed REMEMBER values remain available as comments; exporting
    # them must not silently turn them into declarations to apply.
    assert '# volume = -9.0' in text
    assert '# enabled = true' in text
    path.write_text(text.replace('# volume =', 'volume =').replace('# enabled =', 'enabled ='))
    restored = session_mod.load_config(path)
    assert {(s.family, s.channel, s.option): s.value for s in restored.channels} == {
        ('input', 3, 'gain'): 12.0, ('output', 5, 'volume'): -9.0}
    assert {setting.path: setting.value for setting in restored.globals} == {'/echo': 1}


def test_dump_warns_when_invalid_channel_or_global_reports_cannot_be_exported(
        session_mod, capsys, caplog, read_peer):
    with caplog.at_level('WARNING'):
        code, text = run_dump(session_mod, capsys, [
            ('/input/3/gain', 'f', (float('nan'),)), ('/echo', 'i', (-1,)),
        ], read_peer=read_peer)
    assert code == 0
    assert 'incomplete export' in caplog.text
    assert '/input/3/gain' in caplog.text
    assert '/echo' in caplog.text
    assert '[input:3]' not in text
    assert '[echo]' not in text


def test_a_changed_register_extends_the_quiet_window_to_receive_later_changes(monkeypatch):
    from types import SimpleNamespace

    from oscmix_desk.model import Config

    clock = [0.0]
    deliveries = iter([(0.1, [('/output/5/volume', 'f', (-10.0,))]),
                       (0.7, [('/output/5/volume', 'f', (-20.0,))]),
                       (1.2, []), (1.4, [('/output/5/volume', 'f', (-30.0,))]), (2.5, [])])
    closed = []

    def messages(_timeout):
        clock[0], reports = next(deliveries)
        return reports

    peer = SimpleNamespace(request_dump=lambda: None, messages=messages,
        device_name='Fireface UCX II (24216011)', epoch=b'\xab' * 16,
        close=lambda: closed.append(True))
    monkeypatch.setattr(reads_mod, 'connect_backend', lambda *_args, **_kwargs: peer)
    monkeypatch.setattr(reads_mod.time, 'monotonic', lambda: clock[0])
    monkeypatch.setattr(reads_mod, 'DUMP_QUIET_SECONDS', 1.0)
    reading = reads_mod._read_device(Config())
    assert reading.registers == {'/output/5/volume': (-30.0,)}
    assert reading.serial == '24216011'
    assert closed == [True]
