"""The unit takes the lock every other writer takes (ADR 0019).

Around its start-up apply and verifier and around every reconcile: one
writer at a time, and a reconcile that cannot have the lock stands down
and says so.
"""

import re

import pytest
from conftest import device_key
from reconcile_desk import routes_file

from oscmix_desk import locking
from oscmix_desk import reload as reload_mod


def test_a_reconcile_stands_down_while_another_writer_holds_the_lock(
        tmp_path, monkeypatch, session_mod, caplog):
    """A switch is writing the device, so this reconcile is not.

    Before 0.6.5 the reconcile only waited for the start-up verifier,
    which is one of the three writers. The other two are a `--profile`
    switch and `--no-profile`, in a second process, and a reconcile that
    started between their phases interleaved with them.
    """
    import argparse


    path = routes_file(tmp_path)
    applied = []
    monkeypatch.setattr(reload_mod, "reconcile_now",
                        lambda *a, **k: applied.append(a))
    monkeypatch.setattr(locking, "SWITCH_LOCK_WAIT", 0.3)
    held = locking.take_device_lock(path, device_key(path))
    assert held is not None
    try:
        with caplog.at_level("WARNING"):
            reload_mod._reconcile(argparse.Namespace(config=path),
                                      session_mod.Config(), {"stop": False})
    finally:
        held.release()
    assert applied == [], "nothing may be written while the other writer is"
    assert "send the reload again" in caplog.text

def test_a_reconcile_holds_the_lock_while_it_writes_and_frees_it_after(
        tmp_path, monkeypatch, session_mod):
    import argparse


    path = routes_file(tmp_path)
    during = []
    monkeypatch.setattr(reload_mod, "reconcile_now",
        lambda *a, **k: during.append(
            locking.take_device_lock(path, device_key(path), wait=0.1)))
    reload_mod._reconcile(argparse.Namespace(config=path),
                              session_mod.Config(), {"stop": False})
    assert during == [None], "a switch must not get in while this writes"
    after = locking.take_device_lock(path, device_key(path), wait=0.2)
    assert after is not None, "and must get in once it is done"
    after.release()

def test_a_reconcile_hands_on_the_trigger_the_stop_check_and_its_phases(
        tmp_path, monkeypatch, session_mod):
    """What the reconcile passes down is what the journal and the
    shutdown path depend on: the trigger names the reload in the log
    line, the stop check is what stops a write mid-shutdown, and the
    two `STATUS=` notices are what `systemctl --user status` shows.
    """
    import argparse


    path = routes_file(tmp_path)
    seen = []
    notices = []
    monkeypatch.setattr(reload_mod, "reconcile_now",
                        lambda *a: seen.append(a) or True)
    monkeypatch.setattr(reload_mod, "sd_notify", notices.append)
    stop = {"stop": False}
    reload_mod._reconcile(argparse.Namespace(config=path),
                              session_mod.Config(), stop)

    assert len(seen) == 1
    _config, trigger, should_stop = seen[0]
    assert trigger == "SIGHUP"
    assert should_stop() is False
    stop["stop"] = True
    assert should_stop() is True, "the live flag, not a copy of it"
    assert notices[0] == "STATUS=reconciling (SIGHUP)"
    assert re.fullmatch(r"STATUS=running; reconciled at \d\d:\d\d:\d\d",
                        notices[-1]), notices[-1]

def test_the_config_path_falls_back_to_discovery(monkeypatch, session_mod):
    # The lock and the reload both need it, and a session started
    # without --config has only the discovery to go on.
    import argparse


    monkeypatch.setattr(reload_mod, "discover_config_path",
                        lambda: "discovered")
    assert reload_mod._config_path(argparse.Namespace()) == "discovered"
    assert reload_mod._config_path(
        argparse.Namespace(config="given")) == "given"

def test_a_reconcile_that_wrote_nothing_does_not_report_success(
        tmp_path, monkeypatch, session_mod):
    """The mixer GUI holds the receive port, so nothing is written.

    `reconcile_now` refuses rather than writing blind (ADR 0013) and
    says so by returning False. Until 0.6.6 the status line said
    "reconciled" either way, which is what an operator reads.
    """
    import argparse


    path = routes_file(tmp_path)
    notices = []
    monkeypatch.setattr(reload_mod, "reconcile_now", lambda *a: False)
    monkeypatch.setattr(reload_mod, "sd_notify", notices.append)
    reload_mod._reconcile(argparse.Namespace(config=path),
                              session_mod.Config(), {"stop": False})
    assert re.fullmatch(r"STATUS=running; reconcile skipped at "
                        r"\d\d:\d\d:\d\d", notices[-1]), notices[-1]

@pytest.mark.parametrize("cause", ["lock held", "config broken",
                                   "verifier still running"])
def test_every_reconcile_that_stands_down_says_so(cause, tmp_path, monkeypatch,
                                                 session_mod):
    """Not only a held receive port. The lock held elsewhere, a verifier
    that outlived the wait and a config that no longer parses returned
    before the status line and left the previous one standing -- often
    "verifier finished", which reads as all well (0.6.10)."""
    import argparse
    import threading


    path = routes_file(tmp_path)
    notices = []
    written = []
    monkeypatch.setattr(reload_mod, "sd_notify", notices.append)
    monkeypatch.setattr(reload_mod, "reconcile_now",
                        lambda *a: written.append(1) or True)
    verifier = None
    release = threading.Event()
    if cause == "lock held":
        monkeypatch.setattr(reload_mod, "take_device_lock",
                            lambda *a: None)
    elif cause == "config broken":
        path.write_text("[route:x]\noutput = 99\nplayback = 1\n")
    else:
        monkeypatch.setattr(reload_mod, "RECONCILE_WAIT_FOR_VERIFIER", 0.2)
        verifier = threading.Thread(target=release.wait, daemon=True)
        verifier.start()
    try:
        reload_mod._reconcile(argparse.Namespace(config=path),
                                  session_mod.Config(), {"stop": False},
                                  verifier)
    finally:
        release.set()
    assert written == []
    assert re.fullmatch(r"STATUS=running; reconcile skipped at "
                        r"\d\d:\d\d:\d\d", notices[-1]), notices

def test_a_reconcile_cut_short_by_a_stop_reports_nothing(tmp_path, monkeypatch,
                                                         session_mod):
    """The stop handler has sent STOPPING=1 already; a "running; ..." line
    after it would describe a unit that is going down."""
    import argparse


    notices = []
    monkeypatch.setattr(reload_mod, "sd_notify", notices.append)
    monkeypatch.setattr(reload_mod, "reconcile_now", lambda *a: True)
    reload_mod._reconcile(argparse.Namespace(config=routes_file(tmp_path)),
                              session_mod.Config(), {"stop": True})
    assert not any(notice.startswith("STATUS=running") for notice in notices), \
        notices

def test_a_reconcile_reads_the_desk_under_the_lock(tmp_path, monkeypatch,
                                                   session_mod):
    """A switch that commits while the reconcile waits must win.

    Until 0.6.6 the desk was read before the lock: two writers were
    serialised, and the second wrote a snapshot older than the first.
    """
    import argparse
    import threading

    from conftest import write_config


    path = routes_file(tmp_path)
    write_config(tmp_path / "profiles" / "later.conf",
                 "[route:y]\nplayback = 5/6\noutput = 5/6\n")
    applied = []
    monkeypatch.setattr(reload_mod, "reconcile_now",
                        lambda config, *a: applied.append(config) or True)
    monkeypatch.setattr(locking, "SWITCH_LOCK_WAIT", 5.0)

    held = locking.take_device_lock(path, device_key(path))
    assert held is not None

    def commit_then_release():
        (tmp_path / "active-profile").write_text("later\n")
        held.release()

    threading.Timer(0.3, commit_then_release).start()
    reload_mod._reconcile(argparse.Namespace(config=path),
                              session_mod.Config(), {"stop": False})
    assert [r.output for r in applied[0].routes] == [(5, 6)], \
        "the reconcile applied the desk it read before waiting"

def test_the_reconcile_locks_the_desk_it_reloads(tmp_path, monkeypatch,
                                                 session_mod):
    """The lock is taken for the session's config path and the interface
    it pinned; a None path would key the fallback lock beside nothing."""
    import argparse


    path = routes_file(tmp_path)
    taken = []
    monkeypatch.setattr(reload_mod, "take_device_lock",
                        lambda where, key: taken.append((where, key)))
    monkeypatch.setattr(reload_mod, "sd_notify", lambda *_a: None)
    config = session_mod.Config()
    config.serial = "24216011"
    reload_mod._reconcile(argparse.Namespace(config=path), config,
                              {"stop": False})
    assert taken == [(path, "2a39-3fd9-24216011")]
