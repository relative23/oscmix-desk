"""The two start-up helpers that only subprocess tests reached.

Waiting for the backend to bind its port, and the handlers that turn a
signal into a stop: driven in process here, because a test that starts a
real session loads the checked-out source and never a mutant (ADR 0005).
"""

import time

import pytest
from session_doubles import RunningChild
from support import device_key


class PollingChild:
    """A backend whose exit can be scheduled by poll count."""

    def __init__(self, exits_after=None, returncode=None):
        self.pid = 202
        self.polls = 0
        self.exits_after = exits_after
        self.returncode = returncode
        self.terminated = False

    def poll(self):
        self.polls += 1
        if self.exits_after is not None and self.polls >= self.exits_after:
            self.returncode = 1
        return self.returncode

    def terminate(self):
        self.terminated = True

def test_a_strangers_port_is_not_backend_readiness(
        session_module, monkeypatch, tmp_path):
    from test_process import fake_proc

    proc = fake_proc(tmp_path, {"201": ("other", "other"),
                                "202": ("oscmix", "oscmix")},
                     listening_port=7301, owner="201")
    monkeypatch.setattr(session_module, "PORT_READY_TIMEOUT", 0.01)
    assert session_module._await_backend_port(
        PollingChild(), session_module.Config(osc_port=7301), proc) is False

def test_a_failed_apply_releases_the_device_lock(
        session_module, session_mod, monkeypatch, tmp_path):
    from support import write_config

    from oscmix_desk.locking import take_device_lock

    path = write_config(tmp_path / "routing.conf",
                        "[route:x]\nplayback = 1/2\noutput = 1/2\n")

    def fail(*args, **kwargs):
        raise OSError("send failed")

    monkeypatch.setattr(session_module, "apply_routing", fail)
    with pytest.raises(OSError, match="send failed"):
        session_module._apply_and_verify(
            RunningChild(), session_mod.load_config(path), {"stop": False}, path)
    lock = take_device_lock(path, device_key(path), wait=0)
    assert lock is not None, "a failed write leaked its lock descriptor"
    lock.release()

def test_the_port_wait_returns_as_soon_as_the_backend_listens(
        session_module, monkeypatch, caplog, tmp_path):
    from oscmix_desk import Config

    answers = iter([False, False, True])
    asked = []

    def listening(port, root):
        asked.append((port, root))
        return next(answers)

    from test_process import fake_proc

    proc = fake_proc(tmp_path, {"202": ("oscmix", "oscmix")},
                     listening_port=7301, owner="202")
    monkeypatch.setattr(session_module, "udp_port_listening", listening)
    monkeypatch.setattr(session_module, "PORT_READY_TIMEOUT", 5.0)
    child = PollingChild()
    started = time.monotonic()
    with caplog.at_level("INFO"):
        session_module._await_backend_port(child, Config(osc_port=7301),
                                           proc)
    assert "listening on UDP 7301" in caplog.text
    # The configured port and the given /proc root, every time -- not
    # whatever a stub that ignores its arguments would accept.
    assert asked == [(7301, proc)] * 3
    assert "not listening" not in caplog.text
    assert time.monotonic() - started < 2.0, "it waited out the timeout"

def test_the_port_wait_stops_when_the_backend_dies(session_module, monkeypatch,
                                                  caplog, tmp_path):
    # A child that exited will never bind the port; waiting on would burn
    # the whole timeout for nothing and hide the exit behind a misleading
    # "not listening" warning.
    from oscmix_desk import Config

    monkeypatch.setattr(session_module, "udp_port_listening",
                        lambda port, root: False)
    monkeypatch.setattr(session_module, "PORT_READY_TIMEOUT", 5.0)
    child = PollingChild(exits_after=2)
    started = time.monotonic()
    with caplog.at_level("INFO"):
        session_module._await_backend_port(child, Config(osc_port=7301),
                                           tmp_path)
    assert time.monotonic() - started < 2.0
    assert "not listening" not in caplog.text
    assert child.polls >= 2

def test_the_port_wait_times_out_with_a_warning(session_module, monkeypatch,
                                                caplog, tmp_path):
    from oscmix_desk import Config

    monkeypatch.setattr(session_module, "udp_port_listening",
                        lambda port, root: False)
    monkeypatch.setattr(session_module, "PORT_READY_TIMEOUT", 0.3)
    with caplog.at_level("WARNING"):
        session_module._await_backend_port(PollingChild(), Config(osc_port=7301),
                                           tmp_path)
    assert "not listening on UDP 7301" in caplog.text

def _capture_signal_handlers(session_module, monkeypatch):
    installed = {}
    monkeypatch.setattr(session_module.signal, "signal",
                        lambda signum, handler: installed.__setitem__(signum,
                                                                      handler))
    return installed

def test_stop_handlers_turn_a_signal_into_an_orderly_shutdown(session_module,
                                                              monkeypatch):
    import signal

    installed = _capture_signal_handlers(session_module, monkeypatch)
    notices = []
    monkeypatch.setattr(session_module, "sd_notify", notices.append)
    child = PollingChild()
    stop = {"stop": False}
    session_module._install_stop_handlers(child, stop)
    assert set(installed) == {signal.SIGTERM, signal.SIGINT}

    installed[signal.SIGTERM](signal.SIGTERM, None)
    assert stop["stop"] is True
    assert notices == ["STOPPING=1"], "systemd must hear about the stop first"
    assert child.terminated is True

def test_stop_handlers_do_not_terminate_a_backend_that_is_already_gone(
        session_module, monkeypatch):
    import signal

    installed = _capture_signal_handlers(session_module, monkeypatch)
    monkeypatch.setattr(session_module, "sd_notify", lambda *_a: None)
    child = PollingChild(returncode=0)
    stop = {"stop": False}
    session_module._install_stop_handlers(child, stop)
    installed[signal.SIGINT](signal.SIGINT, None)
    assert stop["stop"] is True
    assert child.terminated is False
