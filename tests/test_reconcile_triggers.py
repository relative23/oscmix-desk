"""When the routing is re-applied, and how it is asked for.

Three triggers were on the roadmap: SIGHUP, resume, hotplug. Only two of
them needed building.

**Hotplug was already covered**, and building a second path for it would
have been the mistake this release keeps finding. `udev/90-rme-fireface.rules`
pulls `oscmix.service` in on `add`, and on `remove` the backend exits
with its device, so a replug is a full process restart with a full apply --
recorded in `tests/data/cold-plug-timeline.json`, whose condition line
reads "cold USB replug -- device unplugged 14.4 s, udev restarted the
unit".

**SIGHUP** is the mechanism, and it reconciles rather than restarting:
pinned settings are re-applied, remembered ones are left where the user
put them.

**Resume** rides on SIGHUP through a system-sleep hook, because there is
no user-level sleep.target to hang a unit on.
"""

import re

import pytest
from conftest import repo_file


def _key(path):
    """The device key the code under test derives for this config.

    From the same resolution the code uses, against the /proc the suite
    points it at (ADR 0024) -- not against the machine's own card list.
    """
    import os
    from pathlib import Path

    from oscmix_desk.discovery import resolve_device
    from oscmix_desk.profiles import load_config

    config = load_config(path)
    return resolve_device(config.usb_id, config.device_name, config.serial,
                          Path(os.environ["OSCMIX_PROC_ROOT"])).key


def unit_text():
    return repo_file("systemd", "oscmix.service").read_text()


def hook_text():
    return repo_file("systemd", "system-sleep", "oscmix").read_text()


# --------------------------------------------------------------------------
# How a reconcile is asked for.
# --------------------------------------------------------------------------

def test_the_unit_reloads_by_signalling_only_the_main_process():
    """`systemctl kill` is a footgun here, and that was measured.

    `systemctl --user kill --signal=SIGHUP oscmix.service` signals *every*
    process in the unit. Neither oscmix nor alsaseqio installs a SIGHUP
    handler, so the default action applies and both die; the service went
    inactive on the machine this was tried on.

    ExecReload with $MAINPID reaches the session process alone, which is
    the only one that knows what a reload means.
    """
    text = unit_text()
    assert "ExecReload=" in text, "no way to reconcile without a restart"
    reload_line = next(line for line in text.splitlines()
                       if line.startswith("ExecReload="))
    assert "$MAINPID" in reload_line, (
        "a reload must not reach the backend processes: %s" % reload_line)
    assert "HUP" in reload_line


def test_the_unit_does_not_restart_to_reconcile():
    # A restart would tear the backend down and re-apply everything
    # indiscriminately, putting remembered faders back -- which is what
    # the pin/remember model exists to prevent.
    reload_line = next(line for line in unit_text().splitlines()
                       if line.startswith("ExecReload="))
    assert "restart" not in reload_line.lower()


# --------------------------------------------------------------------------
# Resume.
# --------------------------------------------------------------------------

def test_the_resume_hook_is_a_system_sleep_script_not_a_user_unit():
    """Checked against systemd rather than assumed.

    A user unit with `WantedBy=sleep.target` installs cleanly, enables
    cleanly and never runs: on systemd 259 `systemctl --user cat
    sleep.target` reports "No files found for sleep.target". The user
    manager has no such target. A system-sleep hook does run, and can
    reach the user manager via `--machine=<user>@.host`.
    """
    path = repo_file("systemd", "system-sleep", "oscmix")
    assert path.exists()
    assert path.stat().st_mode & 0o111, "a sleep hook has to be executable"
    assert not list(repo_file("systemd").glob("*resume*.service")), (
        "a user unit cannot hook sleep.target; that route was measured "
        "and does not exist")


def test_the_resume_hook_only_acts_after_waking():
    # `pre` runs on the way down, when reconciling is pointless and the
    # device is about to go away.
    assert re.search(r'\[\s*"\$1"\s*=\s*"post"\s*\]', hook_text()), (
        "the hook must exit unless invoked with 'post'")


def test_the_resume_hook_reloads_rather_than_killing():
    """Checked against the commands, not the whole file.

    The comment above them explains why `systemctl kill --signal=SIGHUP`
    is wrong, and a test that searched the text would have banned the
    explanation along with the mistake.
    """
    commands = [line for line in hook_text().splitlines()
                if line.strip() and not line.lstrip().startswith("#")]
    body = "\n".join(commands)
    assert "reload oscmix.service" in body
    assert "--signal" not in body, (
        "signalling the unit kills the backend -- measured; use reload")


def test_the_resume_hook_survives_a_missing_service():
    """Waking up with no Fireface attached is the common case.

    A hook that reported failure there would put a line in every wake-up
    log, and people learn to ignore logs that cry wolf.
    """
    assert "|| :" in hook_text() or "|| true" in hook_text()


# --------------------------------------------------------------------------
# Hotplug: covered already, and this says where.
# --------------------------------------------------------------------------

def test_hotplug_is_handled_by_udev_and_not_by_a_second_mechanism():
    """The trigger that needed no code.

    If this ever stops being true -- the rule loses its `add` pull-in --
    then hotplug silently stops re-applying anything, and the session has
    no path of its own to fall back on. The `remove` half is asserted too,
    though what ends the service on unplug is the backend exiting with
    its device, not `StopWhenUnneeded` (an enabled unit is never unneeded;
    ADR 0013, amended). So all of it is asserted here rather than assumed
    from a comment.
    """
    rules = repo_file("udev", "90-rme-fireface.rules").read_text()
    assert 'ACTION=="add"' in rules
    assert "SYSTEMD_USER_WANTS" in rules
    assert "oscmix.service" in rules
    assert 'ACTION=="remove"' in rules
    assert "StopWhenUnneeded=yes" in unit_text()


def test_no_timer_anywhere_triggers_a_reconcile():
    """Triggers are events, never a clock.

    A timer would make this a background process that fights the user on
    a schedule -- and, given that the device does not report a change,
    each tick would cost a full 2002-register dump. The roadmap ruled it
    out and this keeps it ruled out.
    """
    for path in repo_file("systemd").rglob("*"):
        if path.is_file():
            assert path.suffix != ".timer", path
    assert "OnUnitActiveSec" not in unit_text()
    assert "OnCalendar" not in unit_text()


# --------------------------------------------------------------------------
# What SIGHUP does inside the process.
# --------------------------------------------------------------------------

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

    from oscmix_desk import session as session_module

    path = tmp_path / "routing.conf"
    path.write_text("[route:x]\noutput = 99\nplayback = 1\n")
    applied = []
    monkeypatch.setattr(session_module, "reconcile_now",
                        lambda *a, **k: applied.append(a))

    session_module._reconcile(argparse.Namespace(config=path),
                              session_mod.Config(), {"stop": False})
    assert applied == [], "a config that does not parse must not be applied"


def _reload_in_background(session_mod, session_module, path, stop, verifier):
    import argparse
    import threading

    thread = threading.Thread(
        target=lambda: session_module._reconcile(
            argparse.Namespace(config=path), session_mod.Config(), stop,
            verifier),
        daemon=True)
    thread.start()
    return thread


def _routes_file(tmp_path):
    path = tmp_path / "routing.conf"
    path.write_text("[route:x]\nplayback = 1/2\noutput = 1/2\n")
    return path


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
    monkeypatch.setattr(session_module, "reconcile_now",
                        lambda *a, **k: applied.append(time.monotonic()))
    release = threading.Event()
    verifier = threading.Thread(target=release.wait, daemon=True)
    verifier.start()

    reload = _reload_in_background(session_mod, session_module,
                                   _routes_file(tmp_path), {"stop": False},
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
    monkeypatch.setattr(session_module, "reconcile_now",
                        lambda *a, **k: applied.append(1))
    release = threading.Event()
    verifier = threading.Thread(target=release.wait, daemon=True)
    verifier.start()
    stop = {"stop": False}
    reload = _reload_in_background(session_mod, session_module,
                                   _routes_file(tmp_path), stop, verifier)
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
    monkeypatch.setattr(session_module, "reconcile_now",
                        lambda *a, **k: applied.append(1))
    monkeypatch.setattr(session_module, "RECONCILE_WAIT_FOR_VERIFIER", 0.3)
    release = threading.Event()
    verifier = threading.Thread(target=release.wait, daemon=True)
    verifier.start()
    with caplog.at_level("WARNING"):
        reload = _reload_in_background(session_mod, session_module,
                                       _routes_file(tmp_path),
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
    monkeypatch.setattr(session_module, "reconcile_now",
                        lambda *a, **k: applied.append(1))
    verifier = threading.Thread(target=lambda: None)
    verifier.start()
    verifier.join()
    reload = _reload_in_background(session_mod, session_module,
                                   _routes_file(tmp_path), {"stop": False},
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

    from oscmix_desk import session as session_module

    path = write_config(tmp_path / "routing.conf",
                        "[route:main]\noutput = 1/2\nplayback = 1/2\n")
    write_config(tmp_path / "profiles" / "tracking.conf",
                 "[route:direct]\noutput = 5/6\nplayback = 5/6\n")
    (tmp_path / "active-profile").write_text("tracking\n")
    applied = []
    monkeypatch.setattr(session_module, "reconcile_now",
                        lambda config, *a, **k: applied.append(config))
    with caplog.at_level("INFO"):
        session_module._reconcile(argparse.Namespace(config=path),
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


# --------------------------------------------------------------------------
# What a reconcile writes, against a backend that can be inspected.
# --------------------------------------------------------------------------

CONF = """
[route:main]
playback = 1/2
output = 1/2
level = 0.0

[output:1]
volume = -6.0
"""


def _config(tmp_path, extra=""):
    from oscmix_desk.config import load_config

    path = tmp_path / "routing.conf"
    path.write_text(CONF + extra)
    return load_config(path)


class _Device:
    """A backend that reports whatever it is told to, and records writes."""

    def __init__(self, reports):
        self.sent = []
        self._reports = reports
        from oscmix_desk import backend as backend_mod
        self.traits = backend_mod.OSCMIX

    def send(self, messages):
        self.sent.extend((p, t, tuple(a)) for p, t, a in messages)

    def request_dump(self):
        pass

    def listen(self):
        return _Replay(self._reports)


class _Replay:
    def __init__(self, reports):
        self._reports = list(reports)

    def messages(self, _timeout):
        while self._reports:
            yield self._reports.pop(0)

    def close(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        pass


def test_a_reconcile_leaves_a_remembered_value_alone(tmp_path, monkeypatch):
    """The behaviour the whole trigger exists for.

    Measured at the device first: SIGHUP with the fader moved to -20.0 dB
    logged "1 left to the device (/output/1/volume)" and the device still
    read -20.0 afterwards. This is that, in a form CI can run.
    """
    from oscmix_desk import routing, verify

    monkeypatch.setattr(routing, "LINK_ECHO_TIMEOUT", 0.01)
    monkeypatch.setattr(routing, "LINK_SETTLE", 0.01)
    # A mismatch deliberately holds the observation window open for a
    # correcting report, so at the shipped 10 s this test would pay all
    # of it -- per mutant, in the mutation run. The outcome is what is
    # under test; tests/test_pin_remember.py owns the durations.
    monkeypatch.setattr(verify, "VERIFY_TIMEOUT", 0.3)

    device = _Device([("/output/1/stereo", "i", (1,)),
                      ("/playback/1/stereo", "i", (1,)),
                      ("/output/1/volume", "f", (-20.0,))])
    assert verify.reconcile_now(_config(tmp_path), "test", backend=device)

    written = {path for path, _t, _a in device.sent}
    assert "/output/1/volume" not in written, (
        "the fader the user moved was written back")
    assert "/mix/1/playback/1" in written, (
        "the routing itself still has to be re-established")


def test_a_reconcile_corrects_a_pinned_value(tmp_path, monkeypatch):
    from oscmix_desk import routing, verify

    monkeypatch.setattr(routing, "LINK_ECHO_TIMEOUT", 0.01)
    monkeypatch.setattr(routing, "LINK_SETTLE", 0.01)
    monkeypatch.setattr(verify, "VERIFY_TIMEOUT", 0.3)

    device = _Device([("/output/1/stereo", "i", (1,)),
                      ("/playback/1/stereo", "i", (1,)),
                      ("/output/1/volume", "f", (-20.0,))])
    config = _config(tmp_path, "\n[pin]\noutput.volume = pin\n")
    assert verify.reconcile_now(config, "test", backend=device)

    sent = {path: args for path, _t, args in device.sent}
    assert sent.get("/output/1/volume") == (-6.0,), (
        "a pinned register that drifted has to be written back")


def test_a_reconcile_refuses_rather_than_writing_blind(tmp_path):
    """No dump means no way to tell pinned from remembered *at the device*.

    Writing anyway would be the indiscriminate re-apply this model
    exists to end, so a held receive port is a refusal -- and it says so
    rather than reporting success.
    """
    from oscmix_desk import verify

    class Deaf(_Device):
        def listen(self):
            return None

    device = Deaf([])
    assert verify.reconcile_now(_config(tmp_path), "test",
                                backend=device) is False
    assert device.sent == []


def test_a_stop_during_a_reconcile_writes_nothing(tmp_path):
    from oscmix_desk import verify

    device = _Device([("/output/1/stereo", "i", (1,))])
    assert verify.reconcile_now(_config(tmp_path), "test",
                                should_stop=lambda: True,
                                backend=device) is False
    assert device.sent == []


def test_a_reconcile_corrects_what_the_register_table_pins(tmp_path,
                                                           monkeypatch):
    """The table's own policy, with no `[pin]` section anywhere.

    Found by mutation testing: replacing `device_for_name(...)` with
    `None` inside reconcile_now survived every test. With no device
    model the policy lookup falls through to REMEMBER for everything, so
    pinning stops working entirely -- and the only tests that covered
    the pinned branch used a `[pin]` override, which is consulted
    *before* the model and therefore kept working.

    `reflevel` is pinned by the table because it has to match the cable.
    Nothing overrides it here, so this fails if the model is not
    consulted.
    """
    from oscmix_desk import routing, verify

    monkeypatch.setattr(routing, "LINK_ECHO_TIMEOUT", 0.01)
    monkeypatch.setattr(routing, "LINK_SETTLE", 0.01)
    monkeypatch.setattr(verify, "VERIFY_TIMEOUT", 0.3)

    config = _config(tmp_path, "\n[output:5]\nreflevel = +4dBu\n")
    device = _Device([("/output/1/stereo", "i", (1,)),
                      ("/playback/1/stereo", "i", (1,)),
                      ("/output/5/reflevel", "is", (2, "+19dBu")),
                      ("/output/1/volume", "f", (-6.0,))])
    assert verify.reconcile_now(config, "test", backend=device)

    sent = {path: args for path, _t, args in device.sent}
    # An enum is *written* as its index and *reported* as (index, name).
    # "+4dBu" is index 0 of ("+4dBu", "+13dBu", "+19dBu"); the device
    # above reports index 2, so this drifted.
    assert sent.get("/output/5/reflevel") == (0,), (
        "a register the table pins drifted and was not written back")


def test_a_reconcile_leaves_what_the_register_table_remembers(tmp_path,
                                                              monkeypatch):
    """The mirror, and the half that the same mutant also hid.

    With no device model everything reads as remembered, so a test that
    only checked the remembered direction would pass on a broken lookup.
    This pairs with the one above: same run, same backend, one register
    corrected and one left alone, decided only by the table.
    """
    from oscmix_desk import routing, verify

    monkeypatch.setattr(routing, "LINK_ECHO_TIMEOUT", 0.01)
    monkeypatch.setattr(routing, "LINK_SETTLE", 0.01)
    monkeypatch.setattr(verify, "VERIFY_TIMEOUT", 0.3)

    config = _config(tmp_path, "\n[output:5]\nreflevel = +4dBu\n")
    device = _Device([("/output/1/stereo", "i", (1,)),
                      ("/playback/1/stereo", "i", (1,)),
                      ("/output/5/reflevel", "is", (2, "+19dBu")),
                      ("/output/1/volume", "f", (-20.0,))])
    assert verify.reconcile_now(config, "test", backend=device)

    written = {path for path, _t, _a in device.sent}
    assert "/output/5/reflevel" in written, "pinned by the table"
    assert "/output/1/volume" not in written, "remembered by the table"


def test_the_reconcile_log_does_not_claim_to_be_selective(tmp_path,
                                                          monkeypatch, caplog):
    """The write is not selective, so the line must not say it is.

    `reconcile_now` re-applies everything except what is kept -- it
    cannot do less, because the playback mix matrix is never reported
    and so can never be shown to be intact. An earlier wording said
    "N to correct", which reads as though only those N were written.
    """
    import logging

    from oscmix_desk import routing, verify

    monkeypatch.setattr(routing, "LINK_ECHO_TIMEOUT", 0.01)
    monkeypatch.setattr(routing, "LINK_SETTLE", 0.01)
    monkeypatch.setattr(verify, "VERIFY_TIMEOUT", 0.3)

    device = _Device([("/output/1/stereo", "i", (1,)),
                      ("/playback/1/stereo", "i", (1,)),
                      ("/output/1/volume", "f", (-6.0,))])
    with caplog.at_level(logging.INFO):
        verify.reconcile_now(_config(tmp_path), "test", backend=device)
    line = next(r.getMessage() for r in caplog.records
                if "reconcile (test)" in r.getMessage())

    assert "re-applying" in line
    assert "to correct" not in line
    # Nothing drifted, and the routing was still written.
    assert "0 drifted" in line
    assert "/mix/1/playback/1" in {p for p, _t, _a in device.sent}


# --------------------------------------------------------------------------
# What a reload carries over, and what it re-reads.
# --------------------------------------------------------------------------

def _reconciled(monkeypatch, args, running):
    """Run the SIGHUP path and return the Config it reconciled with."""
    from oscmix_desk import session as session_module

    seen = []
    monkeypatch.setattr(session_module, "reconcile_now",
                        lambda config, *a, **k: seen.append(config))
    session_module._reconcile(args, running, {"stop": False})
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

    from oscmix_desk import session as session_module

    monkeypatch.setattr(session_module, "discover_config_path", lambda: None)
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
    path.write_text("[osc]\nport = 9100\nrecv-port = 9101\n"
                    "[device]\nname = Fireface 802\nusb-id = 1234:5678\n"
                    "serial = 99887766\n" + CONF)
    running = session_mod.Config(device_name="Fireface UCX II",
                                 osc_port=7222, osc_recv_port=8222,
                                 serial="24216011")

    fresh = _reconciled(monkeypatch, argparse.Namespace(config=path), running)
    assert fresh is not running, "the file was not re-read at all"
    assert [r.name for r in fresh.routes] == ["main"]
    assert (fresh.osc_port, fresh.osc_recv_port) == (7222, 8222)
    assert fresh.device_name == "Fireface UCX II", (
        "a reload cannot move to another device; that needs a restart")
    # Nor to another box of the same model: the lock and the backend were
    # taken for the interface this process pinned (ADR 0024).
    assert (fresh.usb_id, fresh.serial) == ("2a39:3fd9", "24216011")


# --------------------------------------------------------------------------
# The trigger that was measured and then not built.
# --------------------------------------------------------------------------

def test_no_sample_rate_trigger_exists_and_that_is_deliberate():
    """A rate change destroys nothing on this device, so nothing reacts.

    Measured on a UCX II across 48 kHz -> 44.1 kHz: 1931 of 1932 reported
    registers were identical, the one that differed was
    `/clock/samplerate` itself, and the playback mix matrix survived too
    -- shown by signal, since it is never reported. A 1 kHz tone at
    -40 dBFS into playback 1/2 still came out at outputs 1, 5 and 7.

    The trigger would have been the cheapest of the three: unlike the
    registers a config sets, `/clock/samplerate` *is* pushed when it
    changes, so no poll is needed. It is not built because there is
    nothing measured for it to repair, and this test exists so that
    stays a decision rather than becoming an oversight -- if somebody
    adds the handler, they have to come here and say what loss it fixes.

    docs/decisions/0013-reconcile-triggers.md, "Alternatives considered".
    """
    import ast

    # Parsed, not grepped: the comment in registers.py that records the
    # measurement mentions the register by name, and a text search would
    # have banned the explanation along with the feature. An AST sees
    # string literals only.
    #
    # registers.py is excluded, and the distinction is the point: that
    # file *declares* the register -- with no domain, because upstream's
    # node has no setter -- so the read-back and --dump-config know it
    # exists. Declaring is not reacting. Anywhere else, naming this path
    # in code means something is watching it.
    handlers = []
    for path in sorted(repo_file("src", "oscmix_desk").rglob("*.py")):
        if path.name == "registers.py":
            continue
        for node in ast.walk(ast.parse(path.read_text())):
            if (isinstance(node, ast.Constant) and isinstance(node.value, str)
                    and "clock/samplerate" in node.value):
                handlers.append("%s:%d" % (path.name, node.lineno))
    assert handlers == [], (
        "something now acts on the sample rate: %s -- ADR 0013 says the "
        "measured loss is zero, so say what changed" % handlers)


# --------------------------------------------------------------------------
# The unit takes the lock every other writer takes (ADR 0019).
# --------------------------------------------------------------------------

def test_a_reconcile_stands_down_while_another_writer_holds_the_lock(
        tmp_path, monkeypatch, session_mod, caplog):
    """A switch is writing the device, so this reconcile is not.

    Before 0.6.5 the reconcile only waited for the start-up verifier,
    which is one of the three writers. The other two are a `--profile`
    switch and `--no-profile`, in a second process, and a reconcile that
    started between their phases interleaved with them.
    """
    import argparse

    from oscmix_desk import profiles as profiles_mod
    from oscmix_desk import session as session_module

    path = _routes_file(tmp_path)
    applied = []
    monkeypatch.setattr(session_module, "reconcile_now",
                        lambda *a, **k: applied.append(a))
    monkeypatch.setattr(profiles_mod, "SWITCH_LOCK_WAIT", 0.3)
    held = profiles_mod.take_device_lock(path, _key(path))
    assert held is not None
    try:
        with caplog.at_level("WARNING"):
            session_module._reconcile(argparse.Namespace(config=path),
                                      session_mod.Config(), {"stop": False})
    finally:
        held.release()
    assert applied == [], "nothing may be written while the other writer is"
    assert "send the reload again" in caplog.text


def test_a_reconcile_holds_the_lock_while_it_writes_and_frees_it_after(
        tmp_path, monkeypatch, session_mod):
    import argparse

    from oscmix_desk import profiles as profiles_mod
    from oscmix_desk import session as session_module

    path = _routes_file(tmp_path)
    during = []
    monkeypatch.setattr(
        session_module, "reconcile_now",
        lambda *a, **k: during.append(
            profiles_mod.take_device_lock(path, _key(path), wait=0.1)))
    session_module._reconcile(argparse.Namespace(config=path),
                              session_mod.Config(), {"stop": False})
    assert during == [None], "a switch must not get in while this writes"
    after = profiles_mod.take_device_lock(path, _key(path), wait=0.2)
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
    import re

    from oscmix_desk import session as session_module

    path = _routes_file(tmp_path)
    seen = []
    notices = []
    monkeypatch.setattr(session_module, "reconcile_now",
                        lambda *a: seen.append(a) or True)
    monkeypatch.setattr(session_module, "sd_notify", notices.append)
    stop = {"stop": False}
    session_module._reconcile(argparse.Namespace(config=path),
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

    from oscmix_desk import session as session_module

    monkeypatch.setattr(session_module, "discover_config_path",
                        lambda: "discovered")
    assert session_module._config_path(argparse.Namespace()) == "discovered"
    assert session_module._config_path(
        argparse.Namespace(config="given")) == "given"


def test_a_reconcile_that_wrote_nothing_does_not_report_success(
        tmp_path, monkeypatch, session_mod):
    """The mixer GUI holds the receive port, so nothing is written.

    `reconcile_now` refuses rather than writing blind (ADR 0013) and
    says so by returning False. Until 0.6.6 the status line said
    "reconciled" either way, which is what an operator reads.
    """
    import argparse
    import re

    from oscmix_desk import session as session_module

    path = _routes_file(tmp_path)
    notices = []
    monkeypatch.setattr(session_module, "reconcile_now", lambda *a: False)
    monkeypatch.setattr(session_module, "sd_notify", notices.append)
    session_module._reconcile(argparse.Namespace(config=path),
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
    import re
    import threading

    from oscmix_desk import session as session_module

    path = _routes_file(tmp_path)
    notices = []
    written = []
    monkeypatch.setattr(session_module, "sd_notify", notices.append)
    monkeypatch.setattr(session_module, "reconcile_now",
                        lambda *a: written.append(1) or True)
    verifier = None
    release = threading.Event()
    if cause == "lock held":
        monkeypatch.setattr(session_module, "take_device_lock",
                            lambda *a: None)
    elif cause == "config broken":
        path.write_text("[route:x]\noutput = 99\nplayback = 1\n")
    else:
        monkeypatch.setattr(session_module, "RECONCILE_WAIT_FOR_VERIFIER", 0.2)
        verifier = threading.Thread(target=release.wait, daemon=True)
        verifier.start()
    try:
        session_module._reconcile(argparse.Namespace(config=path),
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

    from oscmix_desk import session as session_module

    notices = []
    monkeypatch.setattr(session_module, "sd_notify", notices.append)
    monkeypatch.setattr(session_module, "reconcile_now", lambda *a: True)
    session_module._reconcile(argparse.Namespace(config=_routes_file(tmp_path)),
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

    from oscmix_desk import profiles as profiles_mod
    from oscmix_desk import session as session_module

    path = _routes_file(tmp_path)
    write_config(tmp_path / "profiles" / "later.conf",
                 "[route:y]\nplayback = 5/6\noutput = 5/6\n")
    applied = []
    monkeypatch.setattr(session_module, "reconcile_now",
                        lambda config, *a: applied.append(config) or True)
    monkeypatch.setattr(profiles_mod, "SWITCH_LOCK_WAIT", 5.0)

    held = profiles_mod.take_device_lock(path, _key(path))
    assert held is not None

    def commit_then_release():
        (tmp_path / "active-profile").write_text("later\n")
        held.release()

    threading.Timer(0.3, commit_then_release).start()
    session_module._reconcile(argparse.Namespace(config=path),
                              session_mod.Config(), {"stop": False})
    assert [r.output for r in applied[0].routes] == [(5, 6)], \
        "the reconcile applied the desk it read before waiting"
