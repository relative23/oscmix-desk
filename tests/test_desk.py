"""One operation owns resources through success, refusal and partial failure."""

import errno
from dataclasses import replace

import pytest
from control_peer import BEGIN, END, HELLO, REFRESH
from support import write_config
from two_boxes import B

from oscmix_desk import (
    ApplyIntent,
    ChannelSetting,
    Config,
    Desk,
    OutcomeState,
    VerifyResult,
    locking,
    open_desk,
    profiles,
)
from oscmix_desk import desk as desk_mod
from oscmix_desk.discovery import Device, lock_key
from oscmix_desk.errors import ConfigError, DeviceLockUnavailable, ReceivePortError, WriteFailed
from oscmix_desk.osc import encode_osc

KEY = lock_key("2a39:3fd9", B[1])
VOLUME = "/output/1/volume"


def declared():
    return Config(serial=B[1], channels=(ChannelSetting("output", 1, "volume", -12.0),))


def unlocked(path=None):
    lock = locking.take_device_lock(path, KEY, wait=0)
    if lock is None:
        return False
    lock.release()
    return True


@pytest.fixture
def connected(monkeypatch, recording_backend):
    """Substitute transport for fault injection; keep real file ownership."""
    monkeypatch.setattr(desk_mod, "_target",
                        lambda config, reach: Device(config.usb_id, B[1], B[0]))
    monkeypatch.setattr(desk_mod, "connect_backend", lambda *_a, **_k: recording_backend)
    return recording_backend


def test_public_operation_shares_one_checked_connection_and_lease(coordinated, tmp_path):
    with coordinated() as peer:
        peer.reports = [encode_osc(VOLUME, "f", -12.0)]
        config = declared()
        with open_desk(config, tmp_path / "routing.conf") as operation:
            assert isinstance(operation, Desk)
            assert peer.requests == [HELLO], "entering takes no write lease"
            assert not unlocked()
            assert operation.apply(ApplyIntent.EXPLICIT) is True
            result = operation.verify()
            assert isinstance(result, VerifyResult)
            assert result.confirmed == [VOLUME]
            assert operation.repair() is True
            assert peer.requests.count(BEGIN) == 1
            assert END not in peer.requests
        assert peer.requests.count(END) == 1
        assert peer.requests.count(REFRESH) == 2
        assert peer.order.count(VOLUME) == 1, "repair preserves REMEMBER"
        assert unlocked()
        assert config == declared(), "pinning never mutates the caller's config"


def test_factory_pins_discovered_path_once(tmp_path, monkeypatch, connected):
    first = write_config(tmp_path / "first.conf", "")
    second = write_config(tmp_path / "second.conf", "")
    monkeypatch.setenv("OSCMIX_CONFIG", str(first))
    operation = open_desk(Config())
    monkeypatch.setenv("OSCMIX_CONFIG", str(second))
    with operation:
        assert operation.config_path == first
        assert operation.config.serial == B[1]
        assert connected.operations == []
        with pytest.raises(AttributeError):
            operation.config = Config()
        with pytest.raises(AttributeError):
            operation.config_path = second
    assert connected.operations == ["close"]


def test_reader_can_connect_while_the_writer_lock_is_held(coordinated):
    with coordinated() as peer:
        held = locking.take_device_lock(None, KEY)
        try:
            with desk_mod._operation(declared(), reader=True) as reader:
                for action in (reader.apply, reader.verify, reader.repair):
                    with pytest.raises(OSError, match="reader Desk") as refused:
                        action()
                    assert refused.value.errno == errno.EPERM
                reader._control.request_dump()
            assert peer.requests == [HELLO, REFRESH]
            assert peer.writes == []
            assert not unlocked(), "closing a reader must not release the writer's lock"
        finally:
            held.release()
        assert unlocked()


def test_operation_is_single_use_and_acquires_nothing_before_entering(connected):
    operation = open_desk(declared())
    with pytest.raises(ReceivePortError):
        operation.apply()
    assert connected.operations == []
    with operation:
        with pytest.raises(RuntimeError, match="cannot be entered again"):
            operation.__enter__()
        with pytest.raises(TypeError, match="ApplyIntent"):
            operation.apply("explicit-desk")
        assert connected.operations == []
        operation.apply()
    with pytest.raises(RuntimeError):
        operation.__enter__()
    with pytest.raises(RuntimeError):
        operation._open()
    for action in (operation.apply, operation.verify, operation.repair):
        with pytest.raises(ReceivePortError):
            action()
    operation._close()
    assert connected.operations == ["begin", "finish", "close"]


@pytest.mark.parametrize("stage", ["begin", "send", "finish", "close"])
def test_failure_releases_the_file_lock_and_never_replays(
        monkeypatch, connected, stage):
    failure = OSError(errno.EIO, "injected %s failure" % stage)

    def fail(*_args):
        connected.operations.append(stage)
        raise failure

    monkeypatch.setattr(connected, stage, fail)
    operation = open_desk(declared())
    with pytest.raises(OSError, match="injected"), operation:
        operation.apply()
    assert unlocked()
    assert connected.operations.count(stage) == 1
    assert connected.operations.count("close") == 1
    for action in (operation.apply, operation.verify, operation.repair):
        with pytest.raises(ReceivePortError):
            action()
    operation._close()
    assert connected.operations.count(stage) == 1


def test_caught_partial_write_cannot_be_replayed(monkeypatch, connected):
    failure = WriteFailed(OSError(errno.EIO, "write failed"), [VOLUME], [])

    def fail(messages):
        if messages:
            raise failure

    monkeypatch.setattr(connected, "send", fail)
    with open_desk(declared()) as operation:
        with pytest.raises(WriteFailed) as raised:
            operation.apply()
        assert raised.value.written == (VOLUME,)
        assert raised.value.unwritten == ()
        for action in (operation.apply, operation.verify, operation.repair):
            with pytest.raises(ReceivePortError):
                action()
    assert connected.operations == ["begin", "close"]
    assert unlocked()


@pytest.mark.parametrize("action", ["verify", "repair"])
def test_caught_read_failure_blocks_further_methods_but_allows_end(
        monkeypatch, connected, action):
    def fail():
        raise ReceivePortError(errno.EIO, "refresh failed")

    monkeypatch.setattr(connected, "request_dump", fail)
    with open_desk(declared()) as operation:
        with pytest.raises(ReceivePortError, match="refresh failed"):
            getattr(operation, action)()
        with pytest.raises(ReceivePortError, match="no longer usable"):
            operation.apply()
    assert connected.operations == ["begin", "finish", "close"]
    assert unlocked()


def test_a_body_error_survives_cleanup_failure(monkeypatch, connected, caplog):
    original = ValueError("caller failed")

    def close():
        connected.operations.append("close")
        raise OSError("close failed")

    monkeypatch.setattr(connected, "close", close)
    def caller():
        with open_desk(declared()) as operation:
            operation.apply()
            raise original

    with pytest.raises(ValueError, match="caller failed") as raised:
        caller()
    assert raised.value is original
    assert connected.operations == ["begin", "close"]
    assert "cleanup failed" in caplog.text
    assert unlocked()


def test_failed_open_releases_ownership_and_preserves_its_reason(monkeypatch, connected, caplog):
    original = OSError(errno.ECONNREFUSED, "connection refused")
    supplied = connected

    def connect(*_args, **_options):
        assert not unlocked(), "identity is checked while owning the lock"
        raise original

    monkeypatch.setattr(desk_mod, "connect_backend", connect)
    operation = open_desk(declared())
    with pytest.raises(OSError, match="connection refused") as raised:
        operation.__enter__()
    assert raised.value is original
    assert supplied.operations == []
    assert unlocked()
    with pytest.raises(RuntimeError):
        operation.__enter__()


def test_opening_error_survives_cleanup_of_a_supplied_connection(
        monkeypatch, connected, caplog):
    original = ConfigError("desk is not usable")

    def reread(_running):
        raise original

    def close():
        raise OSError("close failed")

    monkeypatch.setattr(connected, "close", close)
    operation = desk_mod._operation(declared(), backend=connected, resolved=True, reread=reread)
    with pytest.raises(ConfigError, match="desk is not usable") as raised:
        operation.__enter__()
    assert raised.value is original
    assert "cleanup failed" in caplog.text
    assert unlocked()


def test_lock_refusal_never_connects(monkeypatch, connected):
    monkeypatch.setattr(desk_mod, "device_lock", lambda *_a, **_k: "lock is unavailable")
    with pytest.raises(DeviceLockUnavailable, match="lock is unavailable"), open_desk(declared()):
        pytest.fail("no operation without ownership")
    assert connected.operations == []


@pytest.mark.parametrize("retarget", [False, True])
def test_reread_is_inside_the_lock_and_cannot_change_its_device(connected, retarget):
    config = declared()

    def reread(running):
        assert not unlocked()
        return replace(running, serial="other") if retarget else replace(running, channels=())

    operation = desk_mod._operation(config, backend=connected, resolved=True, reread=reread)
    if retarget:
        with pytest.raises(ConfigError, match="locked device"):
            operation.__enter__()
    else:
        with operation:
            assert operation.config.channels == ()
            assert operation.config.serial == B[1]
    assert unlocked()
    assert connected.operations == ["close"]


def test_cancellation_invalidates_the_operation_even_if_the_flag_is_cleared(connected):
    stop = {"stop": False}
    operation = desk_mod._operation(declared(), backend=connected, resolved=True,
                                    should_stop=lambda: stop["stop"])
    with operation:
        stop["stop"] = True
        with pytest.raises(ReceivePortError) as refused:
            operation.apply()
        assert refused.value.errno == errno.ECANCELED
        stop["stop"] = False
        with pytest.raises(ReceivePortError, match="no longer usable"):
            operation.apply()
    assert connected.operations == ["close"]
    assert unlocked()


def test_cancelled_apply_returns_incomplete_and_disconnects(monkeypatch, connected):
    stop = {"stop": False}

    def apply(*_args, **_options):
        stop["stop"] = True

    monkeypatch.setattr(desk_mod, "apply_routing", apply)
    with desk_mod._operation(declared(), backend=connected, resolved=True,
                             should_stop=lambda: stop["stop"]) as operation:
        assert operation.apply() is False
        stop["stop"] = False
        with pytest.raises(ReceivePortError):
            operation.verify()
    assert connected.operations == ["begin", "close"]
    assert unlocked()


def test_stop_during_the_settle_wait_disconnects_without_end(monkeypatch, connected):
    monkeypatch.setattr(desk_mod, "wait_unless_stopped", lambda *_args: True)
    with open_desk(declared()) as operation:
        operation._begin()
        assert operation._wait(0) is True
        with pytest.raises(ReceivePortError, match="no longer usable"):
            operation.repair()
    assert connected.operations == ["begin", "close"]
    assert unlocked()


def test_profile_marker_commits_after_end_and_close_while_still_locked(
        tmp_path, monkeypatch, connected):
    path = write_config(tmp_path / "routing.conf", "[device]\nserial=%s\n" % B[1])
    write_config(tmp_path / "profiles" / "quiet.conf", "[output:1]\nvolume=-12\n")
    remember = profiles.remember_active_profile
    commits = []

    def commit(name, config_path):
        assert connected.operations == ["begin", "finish", "close"]
        assert not unlocked(path)
        commits.append(name)
        return remember(name, config_path)

    monkeypatch.setattr(profiles, "remember_active_profile", commit)
    result = profiles.switch_profile("quiet", path, verify=False)
    assert result.applied
    assert result.persisted
    assert commits == ["quiet"]
    assert unlocked(path)


def test_cleanup_failure_cannot_turn_a_partial_profile_into_nothing_written(
        tmp_path, monkeypatch, connected):
    path = write_config(tmp_path / "routing.conf", "[device]\nserial=%s\n" % B[1])
    write_config(tmp_path / "profiles" / "quiet.conf", "[output:1]\nvolume=-12\n")
    marker = tmp_path / "active-profile"
    marker.write_text("previous\n")

    def send(messages):
        if messages:
            raise WriteFailed(OSError("write failed"), [VOLUME], [])

    def close():
        raise OSError("close failed")

    monkeypatch.setattr(connected, "send", send)
    monkeypatch.setattr(connected, "close", close)
    result = profiles.switch_profile("quiet", path, verify=False)
    assert result.state == OutcomeState.WRITTEN_IN_PART
    assert result.written == (VOLUME,)
    assert not result.persisted
    assert marker.read_text() == "previous\n"
    assert unlocked(path)
