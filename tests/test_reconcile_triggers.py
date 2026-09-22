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

from support import repo_file


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
    each tick would cost a full 2252-register dump. The roadmap ruled it
    out and this keeps it ruled out.
    """
    for path in repo_file("systemd").rglob("*"):
        if path.is_file():
            assert path.suffix != ".timer", path
    assert "OnUnitActiveSec" not in unit_text()
    assert "OnCalendar" not in unit_text()


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

    # Parsed, not grepped: the comment in devices.py that records the
    # measurement mentions the register by name, and a text search would
    # have banned the explanation along with the feature. An AST sees
    # string literals only.
    #
    # devices.py is excluded, and the distinction is the point: that
    # file *declares* the register -- with no domain, because upstream's
    # node has no setter -- so the read-back and --dump-config know it
    # exists. Declaring is not reacting. Anywhere else, naming this path
    # in code means something is watching it.
    handlers = []
    for path in sorted(repo_file("src", "oscmix_desk").rglob("*.py")):
        if path.name == "devices.py":
            continue
        for node in ast.walk(ast.parse(path.read_text())):
            if (isinstance(node, ast.Constant) and isinstance(node.value, str)
                    and "clock/samplerate" in node.value):
                handlers.append("%s:%d" % (path.name, node.lineno))
    assert handlers == [], (
        "something now acts on the sample rate: %s -- ADR 0013 says the "
        "measured loss is zero, so say what changed" % handlers)
