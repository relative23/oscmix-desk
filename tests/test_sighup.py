"""What SIGHUP does inside the process (ADR 0013).

It reconciles rather than restarting: the handler only sets a flag, the
reload waits for the start-up verifier, a broken file keeps the running
desk, and what is re-read keeps the machine the session runs on.
"""


from conftest import repo_file
from reconcile_desk import CONF, routes_file

from oscmix_desk import reload as reload_mod


def test_the_signal_handler_only_sets_a_flag():
    """Everything a reconcile does is forbidden in a signal handler.

    A handler runs between two bytecodes of whatever was executing, so
    opening a socket, reading a file or waiting out the link barrier
    there means doing it *inside* the apply it was meant to follow. The
    handler sets a flag; the supervise loop is the one place in this
    process where nothing else is half-done.
    """
    import ast

    source = repo_file("src", "oscmix_desk", "session.py").read_text()
    tree = ast.parse(source)
    handler = next(
        node for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == "handle_reload")
    statements = [n for n in handler.body
                  if not isinstance(n, (ast.Expr, ast.Pass))
                  or not isinstance(getattr(n, "value", None), ast.Constant)]
    assert len(statements) == 1, (
        "the SIGHUP handler does more than raise a flag: %s"
        % ast.dump(handler))
    assert isinstance(statements[0], ast.Assign)
    assert not [n for n in ast.walk(handler) if isinstance(n, ast.Call)], (
        "a signal handler must not call anything")

def test_a_reload_request_is_cleared_before_it_is_served(session_mod):
    """A SIGHUP during a reconcile queues another rather than vanishing.

    Clearing the flag after the callback would swallow every request that
    arrived while one was running -- so holding the key down, or two
    wake-ups close together, would lose the last one.
    """
    from oscmix_desk import process

    seen = []

    class Child:
        def __init__(self):
            self.calls = 0

        def wait(self, timeout=None):
            self.calls += 1
            if self.calls > 3:
                return 0
            raise process.subprocess.TimeoutExpired("x", timeout)

    reload_requested = {"reload": True}

    def on_reload():
        # The flag must already be down when the work starts.
        seen.append(reload_requested["reload"])

    process.supervise(Child(), {"stop": False}, on_reload=on_reload,
                      reload_requested=reload_requested)
    assert seen == [False], (
        "the flag was still set while the reconcile ran, so a SIGHUP "
        "arriving now would be lost")

def test_a_stop_wins_over_a_pending_reload(session_mod, monkeypatch):
    """Shutdown must not be delayed by a reconcile nobody will see.

    The reconcile writes routing and waits out the barrier; doing that
    to a backend that is being torn down is both pointless and the exact
    "half-applied mix" the two-phase design exists to prevent.
    """
    from oscmix_desk import process

    # The escalation grace, not the property under test.
    monkeypatch.setattr(process, "CHILD_STOP_GRACE", 0.05)
    called = []

    class Child:
        def wait(self, timeout=None):
            # A real Popen.wait() without a timeout blocks until the
            # child is gone and then returns; only the timed form can
            # raise. A double that raised either way made the reap after
            # SIGKILL look like a hang.
            if timeout is None:
                return 0
            raise process.subprocess.TimeoutExpired("x", timeout)

        def poll(self):
            return None

        def terminate(self):
            pass

        def kill(self):
            return 0

    process.supervise(Child(), {"stop": True},
                      on_reload=lambda: called.append(1),
                      reload_requested={"reload": True})
    assert called == []

def test_a_broken_config_on_reload_keeps_the_running_one(tmp_path, session_mod,
                                                        monkeypatch):
    """SIGHUP with a typo must not take the routing down.

    The session is holding state somebody is listening to. Exiting over
    an unparseable file -- one nobody was forced to edit, and which the
    running configuration does not depend on -- would turn a typo into
    silence.
    """
    import argparse


    path = tmp_path / "routing.conf"
    path.write_text("[route:x]\noutput = 99\nplayback = 1\n")
    applied = []
    monkeypatch.setattr(reload_mod, "reconcile_now",
                        lambda *a, **k: applied.append(a))

    reload_mod._reconcile(argparse.Namespace(config=path),
                              session_mod.Config(), {"stop": False})
    assert applied == [], "a config that does not parse must not be applied"

def _reload_in_background(session_mod, session_module, path, stop, verifier):
    import argparse
    import threading

    thread = threading.Thread(
        target=lambda: reload_mod._reconcile(
            argparse.Namespace(config=path), session_mod.Config(), stop,
            verifier),
        daemon=True)
    thread.start()
    return thread

def test_a_reload_waits_for_the_startup_verifier(tmp_path, session_mod,
                                                monkeypatch):
    """Two writers, one device: the reconcile queues behind the verifier.

    The verifier releases the receive port between its phases, so a
    SIGHUP arriving in one of those gaps used to run a second
    apply_routing on the main thread while the verifier's retry was
    starting its own -- two link phases and two mix writes interleaved,
    which is the ordering the two-phase apply exists to guarantee. The
    resume hook makes the window real: after a suspend the device
    re-enumerates, udev restarts the unit, and the hook's reload lands
    in the verifier's window.
    """
    import threading
    import time

    from oscmix_desk import session as session_module

    applied = []
    monkeypatch.setattr(reload_mod, "reconcile_now",
                        lambda *a, **k: applied.append(time.monotonic()))
    release = threading.Event()
    verifier = threading.Thread(target=release.wait, daemon=True)
    verifier.start()

    reload = _reload_in_background(session_mod, session_module,
                                   routes_file(tmp_path), {"stop": False},
                                   verifier)
    time.sleep(0.4)
    assert applied == [], "the reconcile wrote while the verifier ran"
    released_at = time.monotonic()
    release.set()
    reload.join(timeout=5)
    assert not reload.is_alive()
    assert len(applied) == 1
    assert applied[0] >= released_at

def test_a_stop_during_the_wait_abandons_the_reload(tmp_path, session_mod,
                                                   monkeypatch):
    # A reload queued behind the verifier must not outlive a shutdown
    # request: the supervise loop is the main thread, and a write after
    # SIGTERM would land on a backend that is being taken down.
    import threading
    import time

    from oscmix_desk import session as session_module

    applied = []
    monkeypatch.setattr(reload_mod, "reconcile_now",
                        lambda *a, **k: applied.append(1))
    release = threading.Event()
    verifier = threading.Thread(target=release.wait, daemon=True)
    verifier.start()
    stop = {"stop": False}
    reload = _reload_in_background(session_mod, session_module,
                                   routes_file(tmp_path), stop, verifier)
    time.sleep(0.2)
    stop["stop"] = True
    stopped_at = time.monotonic()
    reload.join(timeout=5)
    release.set()
    assert not reload.is_alive()
    assert applied == []
    # Noticed within the join granularity, not a second later: the
    # supervise loop is the main thread, and a shutdown waits on it.
    assert time.monotonic() - stopped_at < 0.6

def test_a_verifier_that_outlives_the_bound_is_not_waited_for_forever(
        tmp_path, session_mod, monkeypatch, caplog):
    # The bound exists so a wedged verifier cannot hold the supervise
    # loop; the skipped reload is logged, never silent.
    import threading

    from oscmix_desk import session as session_module

    applied = []
    monkeypatch.setattr(reload_mod, "reconcile_now",
                        lambda *a, **k: applied.append(1))
    monkeypatch.setattr(reload_mod, "RECONCILE_WAIT_FOR_VERIFIER", 0.3)
    release = threading.Event()
    verifier = threading.Thread(target=release.wait, daemon=True)
    verifier.start()
    with caplog.at_level("WARNING"):
        reload = _reload_in_background(session_mod, session_module,
                                       routes_file(tmp_path),
                                       {"stop": False}, verifier)
        reload.join(timeout=5)
    release.set()
    assert not reload.is_alive()
    assert applied == []
    assert "reconcile skipped" in caplog.text

def test_a_finished_verifier_does_not_delay_the_reload(tmp_path, session_mod,
                                                      monkeypatch):
    import threading

    from oscmix_desk import session as session_module

    applied = []
    monkeypatch.setattr(reload_mod, "reconcile_now",
                        lambda *a, **k: applied.append(1))
    verifier = threading.Thread(target=lambda: None)
    verifier.start()
    verifier.join()
    reload = _reload_in_background(session_mod, session_module,
                                   routes_file(tmp_path), {"stop": False},
                                   verifier)
    reload.join(timeout=5)
    assert applied == [1]

def test_a_reload_applies_the_remembered_profile(tmp_path, session_mod,
                                                 monkeypatch, caplog):
    """The resume hook's reload must re-apply the desk that was chosen.

    A reload used to re-read routing.conf and apply that, which is how a
    profile vanished after every wake (ADR 0018). It asks the same
    question the start asks now: the active profile, else routing.conf.
    """
    import argparse

    from conftest import write_config


    path = write_config(tmp_path / "routing.conf",
                        "[route:main]\noutput = 1/2\nplayback = 1/2\n")
    write_config(tmp_path / "profiles" / "tracking.conf",
                 "[route:direct]\noutput = 5/6\nplayback = 5/6\n")
    (tmp_path / "active-profile").write_text("tracking\n")
    applied = []
    monkeypatch.setattr(reload_mod, "reconcile_now",
                        lambda config, *a, **k: applied.append(config))
    with caplog.at_level("INFO"):
        reload_mod._reconcile(argparse.Namespace(config=path),
                                  session_mod.Config(), {"stop": False})
    assert [r.output for r in applied[0].routes] == [(5, 6)]
    # The reload line names the profile that was reloaded, not the file
    # the marker sits beside: on the first live run it said routing.conf
    # while the profile was in effect.
    assert "SIGHUP: reloaded %s" % (tmp_path / "profiles" / "tracking.conf") \
        in caplog.text

def test_the_installer_and_uninstaller_agree_about_the_sleep_hook():
    """uninstall.sh says it removes everything install.sh created.

    A hook left behind runs on every wake for a service that is no longer
    there. Harmless, and exactly the kind of leftover that makes people
    distrust an uninstaller.
    """
    install = repo_file("install.sh").read_text()
    uninstall = repo_file("uninstall.sh").read_text()
    assert "system-sleep/oscmix" in install
    assert "system-sleep/oscmix" in uninstall

def test_skipping_the_root_step_skips_both_of_its_parts():
    """--no-udev is documented as "no root needed", so it has to mean it.

    The flag predates the resume hook. If the hook had been added outside
    its guard, `--no-udev` would have kept promising a root-free install
    while asking for a password.
    """
    install = repo_file("install.sh").read_text()
    guarded = install.split('if [ "$DO_UDEV" = 1 ]; then', 1)[1]
    guarded = guarded.split("\nelse\n", 1)[0]
    assert "system-sleep/oscmix" in guarded, (
        "the resume hook is installed outside the --no-udev guard")

def _reconciled(monkeypatch, args, running):
    """Run the SIGHUP path and return the Config it reconciled with."""

    seen = []
    monkeypatch.setattr(reload_mod, "reconcile_now",
                        lambda config, *a, **k: seen.append(config))
    reload_mod._reconcile(args, running, {"stop": False})
    return seen[0] if seen else None

def test_a_reload_with_no_config_file_reconciles_what_is_running(
        tmp_path, session_mod, monkeypatch):
    """The defaults case, and a mutation-testing find.

    Replacing `fresh = config` with `fresh = None` survived every test:
    nothing exercised a SIGHUP on a machine with no routing.conf, where
    the running configuration is all there is. That path ends in
    `reconcile_now(None, ...)`.
    """
    import argparse


    monkeypatch.setattr(reload_mod, "discover_config_path", lambda: None)
    running = session_mod.Config(device_name="Fireface UCX II")
    assert _reconciled(monkeypatch, argparse.Namespace(config=None),
                       running) is running

def test_a_reload_keeps_the_ports_the_backend_is_bound_to(
        tmp_path, session_mod, monkeypatch):
    """Re-reading the desk must not move the process's own sockets.

    The backend is already listening. Taking new ports from the file
    would mean writing to a port nobody is on -- and OSC over UDP has no
    delivery guarantee, so it would fail in complete silence.
    """
    import argparse

    path = tmp_path / "routing.conf"
    path.write_text(CONF)
    # What the start replaced: --osc-port, --device, and the serial it
    # pinned. The file says none of it and is this session's all the same.
    running = session_mod.load_config(path)
    running.osc_port, running.device_name = 9000, "Fireface UCX II (24216011)"
    running.overrides = session_mod.CommandLine(
        "Fireface UCX II (24216011)", 9000)
    running.serial = "24216011"

    fresh = _reconciled(monkeypatch, argparse.Namespace(config=path), running)
    assert fresh is not running, "the file was not re-read at all"
    assert [r.name for r in fresh.routes] == ["main"]
    assert (fresh.osc_port, fresh.osc_recv_port) == (9000, 8222)
    assert fresh.device_name == "Fireface UCX II (24216011)"
    assert (fresh.usb_id, fresh.serial) == ("2a39:3fd9", "24216011")

    # A file that names another backend and another box is not moved to,
    # and since 0.6.11 it is not applied *here* either: it was pinned to
    # this session's interface and written, so a desk for one box reached
    # another (ADR 0024 kept the settings, ADR 0026 is about the desk).
    path.write_text("[osc]\nport = 9100\nrecv-port = 9101\n"
                    "[device]\nname = Fireface 802\nusb-id = 1234:5678\n"
                    "serial = 99887766\n" + CONF)
    assert _reconciled(monkeypatch, argparse.Namespace(config=path),
                       running) is None
