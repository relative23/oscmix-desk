"""The systemd readiness notification.

``sd_notify`` is best-effort by design -- it must never be the reason a
session fails -- but "best effort" is exactly the kind of code that
quietly stops working. Under ``Type=notify`` a lost READY=1 puts the unit
into a restart loop, so the silence has to be deliberate, not accidental.
"""

import socket

import pytest

from oscmix_desk import notify


@pytest.fixture
def notify_mod():
    from oscmix_desk import notify

    return notify


def test_nothing_is_sent_without_a_notify_socket(notify_mod, monkeypatch):
    # Running from a shell rather than from systemd: not an error.
    monkeypatch.delenv("NOTIFY_SOCKET", raising=False)
    notify_mod.sd_notify("READY=1")


def test_the_state_reaches_the_socket(session_mod, tmp_path, monkeypatch):
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    path = tmp_path / "notify.sock"
    listener.bind(str(path))
    listener.settimeout(5)
    monkeypatch.setenv("NOTIFY_SOCKET", str(path))
    try:
        notify.sd_notify("READY=1")
        assert listener.recv(64) == b"READY=1"
    finally:
        listener.close()


def test_an_abstract_socket_address_is_translated(notify_mod, monkeypatch):
    # systemd hands out abstract namespace sockets as "@name"; Python
    # wants a leading NUL. Getting this wrong loses every notification on
    # the systems that use it.
    sent = {}

    class FakeSocket:
        def __init__(self, *args):
            pass

        def connect(self, address):
            sent["address"] = address

        def sendall(self, payload):
            sent["payload"] = payload

        def close(self):
            pass

    monkeypatch.setenv("NOTIFY_SOCKET", "@systemd/notify")
    monkeypatch.setattr(notify_mod.socket, "socket",
                        lambda *a, **k: FakeSocket())
    notify_mod.sd_notify("READY=1")
    assert sent["address"] == "\0systemd/notify"
    assert sent["payload"] == b"READY=1"


def test_a_broken_socket_never_propagates(notify_mod, monkeypatch):
    # The notification is an optimisation. A session that dies because
    # systemd's socket went away would be worse than one that stays quiet.
    monkeypatch.setenv("NOTIFY_SOCKET", "/nonexistent/notify.sock")
    notify_mod.sd_notify("READY=1")
